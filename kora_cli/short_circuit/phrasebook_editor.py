"""KR-FE-PHRASEBOOK-EDITOR-AND-CRUD — write-path support.

Hosts everything that the read-only viewer (PR #167) didn't need:

  * Per-entry validation (regex compiles, snapshot paths resolve to
    real fields, length caps, catastrophic-backtracking guard,
    duplicate-key dedup)
  * Atomic write to the operator override at
    ``${KORA_HOME}/phrasebook/slack_dm.yml`` via
    :func:`utils.atomic_replace` (same pattern as snapshot writer)
  * Backup-on-write with ISO-Z-timestamped filenames + env-tunable
    rotation (``KORA_PHRASEBOOK_BACKUP_COUNT``)
  * Revert to a specific backup OR the most-recent OR full removal
    (falls back to bundled default when no override present)
  * Backup listing for the cockpit dropdown

The read path stays in :mod:`kora_cli.short_circuit.dm_phrasebook` —
this module is import-once-on-write and never invoked by the live
DM handler. Keeps the hot path clean of YAML-serialization +
filesystem code.

Snapshot field-path validation
==============================

Validation rejects ``{snapshot.X.Y}`` placeholders that don't
resolve to a known scalar in the snapshot v4 schema. This is a
STATIC allow-list, pinned against ``kora_cli/snapshot/state_snapshot.py``
by ``test_static_schema_matches_snapshot_collectors``.

Why static vs dynamic walk of a live snapshot:

  * Validation must be deterministic regardless of holder warm-up
    state (a freshly-booted daemon's snapshot may have everything
    degraded to ``"unknown"``; validation must still accept the
    canonical paths)
  * A static set documents the operator-facing API surface — what
    paths CAN be referenced is decoupled from what's currently
    populated
  * If we walked live snapshot keys, ``daemon_health.listeners.X``
    dynamic listener names would appear as valid paths during
    normal operation, then fail validation when the listener
    isn't running. False inconsistency.

The allow-list includes only SCALAR paths (paths that render
sensibly via ``str(value)`` substitution). Nested dicts like
``alerts`` aren't valid templates (str(dict) leaks Python's repr).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


REQUIRED_FIELDS = ("pattern", "category", "description", "reply_template")

# Length caps — defensive against runaway templates / regex
# bombs. Operator-facing values; not security boundaries (regex
# compile + backtracking guard below are the real safety net).
MAX_PATTERN_LENGTH = 512
MAX_REPLY_TEMPLATE_LENGTH = 4096
MAX_DESCRIPTION_LENGTH = 256
MAX_CATEGORY_LENGTH = 64
MAX_ENTRIES_PER_PHRASEBOOK = 200

# Catastrophic-backtracking safety guard. Catches the most common
# pathological shapes: nested unbounded quantifiers like ``(a+)+``,
# ``(.*)*``, ``(x*)?`` etc. Doesn't catch all ReDoS but covers the
# operator-typo cases. A real ReDoS analyzer is overkill for the
# v1 operator surface.
_NESTED_UNBOUNDED_QUANTIFIER_RE = re.compile(r"\([^()]*[+*][^()]*\)[+*?]")

# Snapshot placeholder regex — mirrors dm_phrasebook._PLACEHOLDER_RE
# verbatim. Pinned by the existing
# test_placeholder_regex_matches_dm_phrasebook_source in
# tests/kora_cli/test_phrasebook_endpoints.py.
_PLACEHOLDER_RE = re.compile(r"\{snapshot\.([a-zA-Z0-9_.]+)\}")

# Backup rotation default + env override. Default is 10 backups
# kept; operator can set KORA_PHRASEBOOK_BACKUP_COUNT to a smaller
# number for low-disk environments or a larger number to keep a
# longer revert history.
DEFAULT_BACKUP_KEEP = 10
BACKUP_KEEP_ENV = "KORA_PHRASEBOOK_BACKUP_COUNT"

# Static snapshot v4 scalar-path allow-list for placeholder
# validation. KEEP THIS IN SYNC with kora_cli/snapshot/state_snapshot.py
# — pinned by test_static_schema_matches_snapshot_collectors which
# greps the snapshot collector functions for each path.
#
# Only SCALAR paths are listed. Nested-dict paths like
# "alerts.by_severity" aren't included because they str() to
# Python repr and would render garbage in operator-facing DMs.
SNAPSHOT_SCALAR_PATHS: frozenset[str] = frozenset(
    [
        # operational_state — _collect_operational_state
        "operational_state.primary",
        "operational_state.paused",
        "operational_state.pause_reason",
        # alerts — _collect_alerts (scalars only; by_category is
        # a dict of dynamic keys → invalid template path)
        "alerts.active_count",
        "alerts.by_severity.critical",
        "alerts.by_severity.warning",
        "alerts.by_severity.info",
        # cost_ladder — _collect_cost_ladder (schema v3)
        "cost_ladder.current_tier",
        "cost_ladder.monthly_budget_pct_used",
        "cost_ladder.model_default",
        "cost_ladder.spent_to_date_usd",
        "cost_ladder.credit_pool_usd",
        # service_health — _collect_service_health (5 known probes)
        "service_health.supabase",
        "service_health.fly",
        "service_health.vercel",
        "service_health.sentry",
        "service_health.doppler",
        # daemon_health — _collect_daemon_health (schema v4)
        # listeners.* + cost_telemetry.* are dynamic-key dicts;
        # not included.
        "daemon_health.overall_status",
        "daemon_health.boot_at",
        "daemon_health.uptime_seconds",
        "daemon_health.recent_error_count_5min",
        # tasks — _collect_tasks (v1 deferred to "unknown" but
        # canonical paths are still operator-referenceable)
        "tasks.open_count",
        "tasks.in_progress_count",
        # Snapshot metadata — operator may want to embed the
        # computed_at in a reply ("snapshot was N min old when I
        # answered").
        "computed_at",
        "schema_version",
    ]
)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryValidationError:
    """One validation failure. ``entry_index = -1`` for root-level
    errors (e.g. payload isn't a list). ``field = '_root'`` for
    errors that don't tie to a specific field (duplicates, count)."""

    entry_index: int
    field: str
    error: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "entry_index": self.entry_index,
            "field": self.field,
            "error": self.error,
        }


def validate_entries(entries: Any) -> List[EntryValidationError]:
    """Return a list of validation errors. Empty list ≡ valid;
    callers must check len(errors) == 0 before writing.

    Checks (in order; later checks skipped for an entry that
    already failed an earlier check, to avoid noise):
      1. Payload is a list of dicts
      2. List length <= MAX_ENTRIES_PER_PHRASEBOOK
      3. Per entry: required fields present + non-empty strings
      4. Per entry: length caps on pattern / reply / desc / cat
      5. Per entry: pattern compiles as Python regex (re.IGNORECASE)
      6. Per entry: pattern doesn't have nested unbounded quantifier
      7. Per entry: every {snapshot.X.Y} placeholder in
         reply_template resolves to SNAPSHOT_SCALAR_PATHS
      8. Cross-entry: no duplicate (pattern, category) tuples
    """
    errors: List[EntryValidationError] = []

    # 1. payload is a list
    if not isinstance(entries, list):
        errors.append(EntryValidationError(-1, "_root", "entries must be a list"))
        return errors

    # 2. length cap
    if len(entries) > MAX_ENTRIES_PER_PHRASEBOOK:
        errors.append(
            EntryValidationError(
                -1,
                "_root",
                f"too many entries ({len(entries)} > {MAX_ENTRIES_PER_PHRASEBOOK})",
            )
        )
        # don't short-circuit — operator may still want per-entry
        # feedback on the offenders below

    seen_pairs: set = set()

    for i, entry in enumerate(entries):
        # 3a. entry shape
        if not isinstance(entry, dict):
            errors.append(EntryValidationError(i, "_root", "entry must be an object"))
            continue

        # 3b. required fields present + non-empty
        missing_field = False
        for f in REQUIRED_FIELDS:
            v = entry.get(f)
            if not isinstance(v, str) or not v.strip():
                errors.append(
                    EntryValidationError(i, f, f"required field '{f}' missing or empty")
                )
                missing_field = True
        if missing_field:
            continue  # downstream checks need the strings

        pattern_str: str = entry["pattern"]
        reply_str: str = entry["reply_template"]
        cat_str: str = entry["category"]
        desc_str: str = entry["description"]

        # 4. length caps
        if len(pattern_str) > MAX_PATTERN_LENGTH:
            errors.append(
                EntryValidationError(
                    i, "pattern", f"pattern exceeds {MAX_PATTERN_LENGTH} characters"
                )
            )
        if len(reply_str) > MAX_REPLY_TEMPLATE_LENGTH:
            errors.append(
                EntryValidationError(
                    i,
                    "reply_template",
                    f"reply_template exceeds {MAX_REPLY_TEMPLATE_LENGTH} characters",
                )
            )
        if len(desc_str) > MAX_DESCRIPTION_LENGTH:
            errors.append(
                EntryValidationError(
                    i,
                    "description",
                    f"description exceeds {MAX_DESCRIPTION_LENGTH} characters",
                )
            )
        if len(cat_str) > MAX_CATEGORY_LENGTH:
            errors.append(
                EntryValidationError(
                    i, "category", f"category exceeds {MAX_CATEGORY_LENGTH} characters"
                )
            )

        # 5. pattern compiles
        compile_ok = True
        try:
            re.compile(pattern_str, re.IGNORECASE)
        except re.error as exc:
            errors.append(
                EntryValidationError(i, "pattern", f"invalid regex: {exc}")
            )
            compile_ok = False

        # 6. catastrophic-backtracking guard (only if compile-ok;
        # otherwise the regex is invalid for a different reason)
        if compile_ok and _NESTED_UNBOUNDED_QUANTIFIER_RE.search(pattern_str):
            errors.append(
                EntryValidationError(
                    i,
                    "pattern",
                    "nested unbounded quantifier (possible catastrophic "
                    "backtracking) — rewrite to use bounded counts or "
                    "anchored alternations",
                )
            )

        # 7. snapshot placeholder paths
        for path in _PLACEHOLDER_RE.findall(reply_str):
            if path not in SNAPSHOT_SCALAR_PATHS:
                errors.append(
                    EntryValidationError(
                        i,
                        "reply_template",
                        f"snapshot path '{path}' not in known scalar "
                        f"schema (see SNAPSHOT_SCALAR_PATHS in "
                        f"kora_cli/short_circuit/phrasebook_editor.py)",
                    )
                )

        # 8. cross-entry dedup
        key = (pattern_str, cat_str)
        if key in seen_pairs:
            errors.append(
                EntryValidationError(
                    i,
                    "_root",
                    f"duplicate (pattern, category) — already declared at "
                    f"index {_find_first_index(entries, pattern_str, cat_str, i)}",
                )
            )
        seen_pairs.add(key)

    return errors


def _find_first_index(
    entries: List[Any], pattern_str: str, cat_str: str, before: int
) -> int:
    for j in range(before):
        e = entries[j]
        if not isinstance(e, dict):
            continue
        if e.get("pattern") == pattern_str and e.get("category") == cat_str:
            return j
    return -1


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _override_path() -> Path:
    """``${KORA_HOME}/phrasebook/slack_dm.yml`` — the operator
    override that takes precedence over the bundled default
    in :func:`dm_phrasebook.load_phrasebook`."""
    from kora_constants import get_kora_home

    return get_kora_home() / "phrasebook" / "slack_dm.yml"


def _backup_dir() -> Path:
    """``${KORA_HOME}/phrasebook/backups/`` — sibling of the
    override. Created lazily by write_backup_for + write_phrasebook."""
    from kora_constants import get_kora_home

    return get_kora_home() / "phrasebook" / "backups"


def _backup_keep_count() -> int:
    """Read the rotation count from env. Defaults + clamps to
    sensible bounds (1-1000) so a typo doesn't blow up disk."""
    raw = os.environ.get(BACKUP_KEEP_ENV, "").strip()
    if not raw:
        return DEFAULT_BACKUP_KEEP
    try:
        n = int(raw)
    except ValueError:
        logger.warning(
            "[kora.phrasebook] %s=%r is not an int — using default %d",
            BACKUP_KEEP_ENV,
            raw,
            DEFAULT_BACKUP_KEEP,
        )
        return DEFAULT_BACKUP_KEEP
    if n < 1:
        return 1
    if n > 1000:
        return 1000
    return n


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------


def write_backup_for(override_path: Path) -> Optional[Path]:
    """Copy the current override into the backups dir. Returns the
    backup path on success, None when the override doesn't exist
    (first-edit case — nothing to back up)."""
    if not override_path.is_file():
        return None
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    bkp_dir = _backup_dir()
    bkp_dir.mkdir(parents=True, exist_ok=True)
    bkp_path = bkp_dir / f"slack_dm.{ts}.yml"
    # If the same-second timestamp collides (rapid sequential
    # writes), append a counter so we never silently clobber.
    counter = 1
    while bkp_path.exists():
        bkp_path = bkp_dir / f"slack_dm.{ts}-{counter}.yml"
        counter += 1
    shutil.copy2(override_path, bkp_path)
    return bkp_path


def rotate_backups(keep: int) -> List[Path]:
    """Delete backups beyond the ``keep`` most-recent. Returns the
    paths that were removed."""
    bkp_dir = _backup_dir()
    if not bkp_dir.is_dir():
        return []
    # ISO-Z timestamps in filenames sort chronologically as plain
    # strings (oldest first). sorted() + slice-off-the-old.
    files = sorted(bkp_dir.glob("slack_dm.*.yml"))
    if len(files) <= keep:
        return []
    to_remove = files[: len(files) - keep]
    removed: List[Path] = []
    for p in to_remove:
        try:
            p.unlink()
            removed.append(p)
        except OSError as exc:
            logger.warning(
                "[kora.phrasebook] backup rotation failed for %s: %r",
                p,
                exc,
            )
    return removed


def list_backups() -> List[Dict[str, Any]]:
    """Return newest-first list of available backups for the cockpit
    dropdown. Each entry: filename / timestamp / size_bytes /
    entry_count (None when the backup file can't be parsed —
    indicates a corrupt backup the operator might want to skip)."""
    bkp_dir = _backup_dir()
    if not bkp_dir.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for p in sorted(bkp_dir.glob("slack_dm.*.yml"), reverse=True):
        # Filename: slack_dm.{TIMESTAMP}.yml — strip prefix +
        # suffix to recover the timestamp.
        ts = p.name[len("slack_dm.") : -len(".yml")]
        try:
            size = p.stat().st_size
        except OSError:
            continue
        entry_count: Optional[int]
        try:
            doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            if isinstance(doc, dict) and isinstance(doc.get("entries"), list):
                entry_count = len(doc["entries"])
            else:
                entry_count = None
        except Exception:
            entry_count = None
        out.append(
            {
                "filename": p.name,
                "timestamp": ts,
                "size_bytes": size,
                "entry_count": entry_count,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Write + revert
# ---------------------------------------------------------------------------


def write_phrasebook(entries: List[Dict[str, Any]]) -> Path:
    """Atomically write entries to the override path. Caller MUST
    have called :func:`validate_entries` first + checked it
    returned empty. This function does NOT re-validate (separation
    keeps endpoint code readable + makes the error path testable
    in isolation)."""
    from utils import atomic_replace

    override = _override_path()
    override.parent.mkdir(parents=True, exist_ok=True)

    # Serialize in a deterministic field order so YAML diffs stay
    # readable across edits. sort_keys=False preserves the per-
    # entry field order we set; the entry list itself is operator-
    # ordered (first-match-wins is the runtime semantic).
    payload = {
        "entries": [
            {
                "pattern": e["pattern"],
                "category": e["category"],
                "description": e["description"],
                "reply_template": e["reply_template"],
            }
            for e in entries
        ],
    }
    yaml_text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)

    # Atomic write — write to tmp, then atomic_replace into place.
    # Same pattern as kora_cli/snapshot/state_snapshot.py.
    tmp = override.with_suffix(".yml.tmp")
    tmp.write_text(yaml_text, encoding="utf-8")
    atomic_replace(tmp, override)
    return override


def revert_phrasebook(filename: Optional[str] = None) -> Dict[str, Any]:
    """Revert the override to a specific backup OR the most-recent
    backup OR (no backup available) remove the override entirely.

    Args:
      filename: When given, revert to this specific backup
        (filename only, not path — looked up under _backup_dir()).
        When None, revert to the most-recent backup.

    Returns: ``{"reverted_to": "<source>", "source_path": "<path>" | None}``
    where source is one of:
      * "<backup filename>" — restored a specific backup
      * "bundled_default" — no backup available + override removed
        (live handler now falls back to the bundled default)

    Raises FileNotFoundError when the requested filename doesn't
    exist; the endpoint surfaces this as a 404."""
    override = _override_path()
    bkp_dir = _backup_dir()

    target_backup: Optional[Path] = None
    if filename is not None:
        # Specific backup requested — defense against path
        # traversal: filename must be in our directory and match
        # the slack_dm.*.yml shape.
        if (
            "/" in filename
            or "\\" in filename
            or ".." in filename
            or not filename.startswith("slack_dm.")
            or not filename.endswith(".yml")
        ):
            raise ValueError(f"invalid backup filename: {filename!r}")
        candidate = bkp_dir / filename
        if not candidate.is_file():
            raise FileNotFoundError(
                f"backup not found: {filename}"
            )
        target_backup = candidate
    else:
        if bkp_dir.is_dir():
            files = sorted(bkp_dir.glob("slack_dm.*.yml"), reverse=True)
            if files:
                target_backup = files[0]

    if target_backup is None:
        # No backup → remove override; live handler will fall
        # back to the bundled default automatically.
        if override.is_file():
            try:
                override.unlink()
            except OSError as exc:
                logger.warning(
                    "[kora.phrasebook] override unlink failed: %r", exc
                )
                raise
        return {"reverted_to": "bundled_default", "source_path": None}

    # Copy backup → override atomically (same pattern as
    # write_phrasebook). Don't move/delete the backup itself;
    # operator may want to revert again to the same point.
    from utils import atomic_replace

    override.parent.mkdir(parents=True, exist_ok=True)
    tmp = override.with_suffix(".yml.tmp")
    shutil.copy2(target_backup, tmp)
    atomic_replace(tmp, override)
    return {
        "reverted_to": target_backup.name,
        "source_path": str(override),
    }
