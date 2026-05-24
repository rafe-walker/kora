"""DM observation collector — KR-PROMOTE-PHRASEBOOK-FOUNDATION (Deliverable B).

Reads :file:`slack_dm_log.jsonl` (post-#184: handler-driven replies +
probe-wake DMs both write here with ``caller_session_id``) and
projects entries into the :class:`ReasoningObservation` shape the
proposer consumes.

# Filtering

  * ``route_filter`` — only emit observations whose route matches.
    Default ``["slack_dm"]``. Future loops may add
    ``"probe_investigation"`` / ``"alert_investigation"``.
  * Short-circuit hits (``model_used == "short_circuit"``) are
    ALWAYS dropped — they're the things we're trying to grow,
    not the things we're trying to learn from.
  * Entries missing the engine path (``model_used`` absent)
    are dropped — they're canned-fallback / non-reasoning paths
    and don't carry the Q+A signal we need to cluster.
  * Time window enforced via ``since`` (cheap timestamp parse).

# Question text

The slack_dm_log captures Kora's REPLY text, not Joshua's question.
The question is inferable from the ``caller_session_id`` (slack_dm
shape: ``"{channel_id}:{event_ts}"``) by reading the inbound
slack-DM JSONL. v1 ships **reply-only clustering** — the proposer
clusters by Kora's response shape (which is itself a strong
fingerprint of the question shape, since reasoning composes
similar answers for similar questions). The
``inbound_lookup`` parameter is a hook for a future bucket to
plumb the inbound side in; defaults to a no-op resolver returning
``None`` (operator_question stays empty).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)


_DEFAULT_ROUTE_FILTER = ("slack_dm",)


@dataclass(frozen=True, slots=True)
class ReasoningObservation:
    """One reasoning-driven DM reply, projected for clustering."""

    operator_question: str  # may be "" in v1 (see module docstring)
    kora_response: str
    timestamp: datetime
    caller_session_id: str
    cost_usd: Optional[float]
    model_used: str
    route: str  # mirrors slack_dm_log's source/route attribution


def _resolve_log_path() -> Path:
    """Same env-override pattern as the handler so tests can
    redirect via ``KORA_SLACK_DM_LOG_PATH``."""
    override = os.environ.get("KORA_SLACK_DM_LOG_PATH", "").strip()
    if override:
        return Path(override)
    from kora_constants import get_kora_home

    return get_kora_home() / "slack_dm_log.jsonl"


def _parse_ts(raw: object) -> Optional[datetime]:
    """ISO 8601 (possibly Z-suffixed) → aware datetime. None on
    malformed input."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _entry_route(entry: dict) -> str:
    """Project the entry's route hint. The handler's
    ``caller_session_id`` shape encodes the source:

      * ``"<channel_id>:<event_ts>"`` (slack_dm) — has a colon
        but doesn't start with one of the structured prefixes.
      * ``"probe:<probe>:<category>"`` (probe_investigation).
      * ``"email:<message_id>"`` (email).
      * ``"mcp:<actor>:<tool>"`` (mcp).

    Defaults to ``"slack_dm"`` when shape doesn't match a known
    structured prefix (operator-DM is the v1 happy path).
    """
    csid = entry.get("caller_session_id") or ""
    if isinstance(csid, str):
        if csid.startswith("probe:"):
            return "probe_investigation"
        if csid.startswith("email:"):
            return "email"
        if csid.startswith("mcp:"):
            return "mcp"
    return "slack_dm"


def _estimate_cost_usd(entry: dict) -> Optional[float]:
    """Compute per-call cost from tokens + model via the canonical
    pricing helper. Returns ``None`` when model is unknown / fields
    are missing. Mirrors the wake_consumer's ``_compute_total_cost_usd``
    helper (same canonical-pricing path)."""
    model = entry.get("model_used")
    if not isinstance(model, str) or not model:
        return None
    try:
        from agent.usage_pricing import CanonicalUsage, estimate_usage_cost
    except Exception:
        return None

    def _int_or_zero(key: str) -> int:
        value = entry.get(key)
        return value if isinstance(value, int) else 0

    usage = CanonicalUsage(
        input_tokens=_int_or_zero("input_tokens"),
        output_tokens=_int_or_zero("output_tokens"),
        cache_read_tokens=_int_or_zero("cache_read_input_tokens"),
        cache_write_tokens=_int_or_zero("cache_creation_input_tokens"),
    )
    try:
        result = estimate_usage_cost(model, usage)
    except Exception:
        return None
    if result.status == "unknown" or result.amount_usd is None:
        return None
    return float(result.amount_usd)


InboundLookup = Callable[[str], Optional[str]]


async def collect_recent_observations(
    *,
    since: datetime,
    route_filter: Optional[List[str]] = None,
    inbound_lookup: Optional[InboundLookup] = None,
    log_path: Optional[Path] = None,
) -> List[ReasoningObservation]:
    """Read the slack_dm outbound log and project entries into
    :class:`ReasoningObservation`s.

    Args:
      since: Lower bound (aware datetime; UTC assumed if naive).
        Entries with ``sent_at < since`` are skipped.
      route_filter: Allow-list of routes to retain. Defaults to
        ``["slack_dm"]``.
      inbound_lookup: Optional resolver
        ``caller_session_id → operator_question``. v1 callers
        leave ``None``; future buckets may plumb the inbound side.
      log_path: Override for tests; production resolves via
        :func:`_resolve_log_path`.

    Returns observations sorted by ``timestamp`` ascending.
    """
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    routes = tuple(route_filter) if route_filter is not None else _DEFAULT_ROUTE_FILTER
    target = log_path or _resolve_log_path()
    if not target.is_file():
        return []

    out: List[ReasoningObservation] = []
    try:
        raw_text = target.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "[kora.promote.phrasebook.observer] read failed for %s: %r",
            target,
            exc,
        )
        return []

    for lineno, line in enumerate(raw_text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.debug(
                "[kora.promote.phrasebook.observer] line %d malformed "
                "JSON, skipped: %r",
                lineno,
                exc,
            )
            continue
        if not isinstance(entry, dict):
            continue

        # Filter: must be an engine-driven reply (model_used
        # present + non-short_circuit) sent successfully.
        model = entry.get("model_used")
        if not isinstance(model, str) or not model:
            continue
        if model == "short_circuit":
            continue
        if entry.get("send_status") != "ok":
            continue
        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            continue

        ts = _parse_ts(entry.get("sent_at"))
        if ts is None or ts < since:
            continue

        route = _entry_route(entry)
        if route not in routes:
            continue

        csid = entry.get("caller_session_id") or ""
        operator_question = ""
        if inbound_lookup is not None and isinstance(csid, str) and csid:
            try:
                resolved = inbound_lookup(csid)
                if isinstance(resolved, str):
                    operator_question = resolved
            except Exception as exc:
                logger.debug(
                    "[kora.promote.phrasebook.observer] inbound_lookup "
                    "raised %r for csid=%r — operator_question stays empty",
                    exc,
                    csid,
                )

        out.append(
            ReasoningObservation(
                operator_question=operator_question,
                kora_response=text,
                timestamp=ts,
                caller_session_id=str(csid) if csid else "",
                cost_usd=_estimate_cost_usd(entry),
                model_used=model,
                route=route,
            )
        )

    out.sort(key=lambda o: o.timestamp)
    return out
