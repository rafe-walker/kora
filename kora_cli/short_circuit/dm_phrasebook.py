"""KR-CHEAP-TRIVIAL-DM-SHORTCIRCUIT — regex + snapshot interpolation phrasebook.

Pre-filter on the Slack DM handler that catches routine status
queries ("what's my burn?", "any alerts?") and answers from the
pre-warmed snapshot at **zero LLM cost**. Estimated coverage:
30-40% of operator traffic. Pairs with prompt caching (#158) as
the other R3-4 cost-reduction item — caching halves the price
of LLM-bound work; short-circuit eliminates the LLM call entirely
for matched patterns.

# Why no LLM

For a tight set of routine queries the answer is a deterministic
projection of the daemon snapshot. The reasoning engine would
just read that same snapshot via tools then narrate it — wasted
roundtrip. Skipping the engine costs nothing and responds in
single-digit milliseconds.

# Phrasebook resolution order

  1. ``${KORA_HOME}/phrasebook/slack_dm.yml`` (operator override)
  2. Bundled ``default_slack_dm_phrasebook.yml`` (package_data)

A malformed override file falls back to the bundled default
with a WARN log — phrasebook errors never break DM handling.

# Fall-through semantics

A match WITHOUT a renderable reply is NOT an error — it's a
deliberate fall-through to the reasoning engine. Triggers:

  - ``snapshot`` is ``None`` (stale / missing / corrupted)
  - A template field path resolves to a missing key
  - A template field value is the literal string ``"unknown"``
    (PR #157's degraded-field sentinel)
  - A template field value is ``None``

The first three are the same operator signal: "this short-cut
isn't safe right now; let Kora reason about it instead." The
caller (handler) sees ``None`` from ``try_short_circuit`` and
proceeds with normal engine resolution.

# Observability hook

The handler tags successful short-circuit replies with
``reasoning_meta["model_used"] == "short_circuit"`` so CC#1's
KR-CHEAP-COST-TELEMETRY can classify them as zero-cost route
hits without inspecting reply text.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Resolution sentinel used by render_reply to signal a renderable
# field is missing or degraded. Distinguishes "missing path" from
# "field is literally None" at the walk layer; render_reply maps
# both to fall-through.
_MISSING = object()


@dataclass(frozen=True)
class PhrasebookEntry:
    pattern: re.Pattern[str]
    reply_template: str
    description: str
    category: str


@dataclass(frozen=True)
class ShortCircuitMatch:
    """Successful regex + template render. Handler uses
    ``reply_text`` verbatim and ``entry.category`` for telemetry."""

    entry: PhrasebookEntry
    reply_text: str


# ---------------------------------------------------------------------------
# Phrasebook loading
# ---------------------------------------------------------------------------


_DEFAULT_PHRASEBOOK_FILENAME = "default_slack_dm_phrasebook.yml"
_OPERATOR_OVERRIDE_RELATIVE = Path("phrasebook") / "slack_dm.yml"


def _operator_override_path() -> Optional[Path]:
    """Return ``${KORA_HOME}/phrasebook/slack_dm.yml`` if the
    KORA_HOME accessor is available + the file exists. None
    otherwise — caller falls back to the bundled default.

    Import is lazy so the phrasebook module loads in test contexts
    where ``kora_constants`` isn't importable.
    """
    try:
        from kora_constants import get_kora_home
    except Exception:
        return None
    try:
        candidate = get_kora_home() / _OPERATOR_OVERRIDE_RELATIVE
    except Exception:
        return None
    return candidate if candidate.is_file() else None


def _read_bundled_default() -> str:
    """Read the bundled phrasebook YAML from package data."""
    # importlib.resources works from both source tree + installed
    # wheel as long as pyproject's package-data registers
    # ``short_circuit/*.yml`` under the ``kora_cli`` package.
    try:
        from importlib.resources import files

        return (
            files("kora_cli.short_circuit")
            .joinpath(_DEFAULT_PHRASEBOOK_FILENAME)
            .read_text(encoding="utf-8")
        )
    except Exception:
        # Source-tree fallback for very-early-boot edge cases.
        fallback = Path(__file__).with_name(_DEFAULT_PHRASEBOOK_FILENAME)
        return fallback.read_text(encoding="utf-8")


def _parse_entries(yaml_text: str, source_label: str) -> List[PhrasebookEntry]:
    """Parse YAML text into PhrasebookEntry list. Malformed →
    raises ValueError. Caller catches + falls back to default."""
    import yaml as _yaml

    doc = _yaml.safe_load(yaml_text) or {}
    if not isinstance(doc, dict):
        raise ValueError(
            f"phrasebook {source_label} top-level must be a mapping"
        )
    raw_entries = doc.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError(
            f"phrasebook {source_label} missing top-level 'entries' list"
        )

    entries: List[PhrasebookEntry] = []
    for idx, item in enumerate(raw_entries):
        if not isinstance(item, dict):
            raise ValueError(
                f"phrasebook {source_label} entry[{idx}] is not a mapping"
            )
        pattern_str = item.get("pattern")
        reply_template = item.get("reply_template")
        category = item.get("category", "uncategorized")
        description = item.get("description", "")
        if not isinstance(pattern_str, str) or not pattern_str:
            raise ValueError(
                f"phrasebook {source_label} entry[{idx}] missing "
                f"'pattern' string"
            )
        if not isinstance(reply_template, str) or not reply_template:
            raise ValueError(
                f"phrasebook {source_label} entry[{idx}] missing "
                f"'reply_template' string"
            )
        try:
            pattern = re.compile(pattern_str, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(
                f"phrasebook {source_label} entry[{idx}] pattern "
                f"is not valid regex: {exc}"
            ) from exc
        entries.append(
            PhrasebookEntry(
                pattern=pattern,
                reply_template=reply_template,
                category=str(category),
                description=str(description),
            )
        )
    return entries


def load_phrasebook(path: Optional[Path] = None) -> List[PhrasebookEntry]:
    """Load the phrasebook from disk.

    Resolution order:
      1. ``path`` if explicitly passed (test injection)
      2. ``${KORA_HOME}/phrasebook/slack_dm.yml`` if it exists
      3. Bundled default

    A malformed override file logs WARNING + falls back to the
    bundled default. A malformed bundled default is fatal (this
    is a packaging bug; surface loudly).
    """
    explicit_or_override = path or _operator_override_path()
    if explicit_or_override is not None:
        try:
            text = explicit_or_override.read_text(encoding="utf-8")
            return _parse_entries(text, str(explicit_or_override))
        except Exception as exc:
            logger.warning(
                "[kora.short_circuit] phrasebook at %s unloadable: %r — "
                "falling back to bundled default",
                explicit_or_override,
                exc,
            )

    default_text = _read_bundled_default()
    return _parse_entries(default_text, "<bundled default>")


# ---------------------------------------------------------------------------
# Match
# ---------------------------------------------------------------------------


def match_message(
    text: str, phrasebook: List[PhrasebookEntry]
) -> Optional[PhrasebookEntry]:
    """First-match-wins regex test against the phrasebook.

    The input is stripped of leading/trailing whitespace before
    matching — operator typing "  hello  " should still match
    ``^hello$``-style patterns. Patterns themselves can include
    flexible whitespace handling via ``\\s*`` if needed.

    Returns the first matching ``PhrasebookEntry`` or ``None``.
    """
    if not isinstance(text, str):
        return None
    normalized = text.strip()
    if not normalized:
        return None
    for entry in phrasebook:
        if entry.pattern.match(normalized):
            return entry
    return None


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


# Match {snapshot.path.to.field} placeholders. Dots in the path,
# alphanumeric segments. Excludes braces so {{escape}} can be
# layered in later if needed.
_PLACEHOLDER_RE = re.compile(r"\{snapshot\.([a-zA-Z0-9_.]+)\}")


def _walk_snapshot(snapshot: Dict[str, Any], dotted: str) -> Any:
    """Walk a dotted path through nested dicts. Returns
    :data:`_MISSING` when any segment isn't present (or the
    intermediate isn't a dict)."""
    current: Any = snapshot
    for segment in dotted.split("."):
        if not isinstance(current, dict):
            return _MISSING
        if segment not in current:
            return _MISSING
        current = current[segment]
    return current


def render_reply(
    entry: PhrasebookEntry, snapshot: Optional[Dict[str, Any]]
) -> Optional[str]:
    """Render ``entry.reply_template`` against the snapshot.

    Returns ``None`` (handler falls through to reasoning engine)
    when:
      * ``snapshot`` is ``None`` (no fresh snapshot exists)
      * Any placeholder path is missing from the snapshot
      * Any placeholder value is the literal string ``"unknown"``
        (PR #157's degraded sentinel)
      * Any placeholder value is ``None``

    Otherwise returns the rendered string with placeholders
    substituted by str(value).
    """
    if snapshot is None:
        return None

    missing_or_degraded: List[str] = []

    def _sub(match: re.Match[str]) -> str:
        dotted = match.group(1)
        value = _walk_snapshot(snapshot, dotted)
        if value is _MISSING or value is None or value == "unknown":
            missing_or_degraded.append(dotted)
            return ""
        return str(value)

    rendered = _PLACEHOLDER_RE.sub(_sub, entry.reply_template)
    if missing_or_degraded:
        logger.debug(
            "[kora.short_circuit] entry %s falling through — "
            "missing/degraded fields: %s",
            entry.category,
            missing_or_degraded,
        )
        return None
    return rendered


# ---------------------------------------------------------------------------
# One-shot entry point
# ---------------------------------------------------------------------------


def try_short_circuit(
    text: str,
    phrasebook: List[PhrasebookEntry],
    snapshot: Optional[Dict[str, Any]],
) -> Optional[ShortCircuitMatch]:
    """Convenience: match + render in one call. Returns a
    :class:`ShortCircuitMatch` on success, ``None`` on no-match
    OR render fall-through.

    Handler calls this on every inbound DM before resolving the
    reasoning engine.
    """
    entry = match_message(text, phrasebook)
    if entry is None:
        return None
    reply_text = render_reply(entry, snapshot)
    if reply_text is None:
        return None
    return ShortCircuitMatch(entry=entry, reply_text=reply_text)
