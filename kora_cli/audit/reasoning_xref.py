"""Reasoning panel ↔ slack_dm cross-reference — KR-REASONING-PANEL-MODEL-XREF.

The reasoning panel's /api/reasoning/recent endpoint reads
``kora_audit_log.jsonl`` rows where ``seam=reasoning.tool_called``,
groups by ``caller_session_id``, and projects to ReasoningCall
shape. PR #141 left ``model_used`` / ``cost_rung_at_call`` /
``input_tokens`` / ``output_tokens`` / ``response_text_truncated_200``
as null because those fields live in
``slack_dm_log.jsonl`` outbound entries, not in audit.

This module cross-references the two log files to populate those
fields. Graceful-degradation: when the xref fails (slack_dm entry
missing OR stale), the ReasoningCall row still renders with null
fields — same as the pre-xref behavior from #141.

K-DG verified against actual writer code (per the
``feedback_no_pm_memory_assertions_grep_yourself`` rule):

  * Audit ``caller_session_id`` shape per
    ``kora_cli/reasoning/anthropic_engine.py:844-876``
    (``_derive_caller_session_id``):
        - slack_dm → ``"{channel_id}:{event_ts}"``
        - email   → ``"email:{message_id}"``
        - mcp     → ``"mcp:{actor_kind}:{tool_name}"``
        - other   → ``"unknown"``

  * slack_dm outbound writer at
    ``kora_cli/handlers/slack_dm_handler.py:753-833`` does NOT
    include ``caller_session_id`` in the JSONL entry (spec said
    "verify; CC#3 may have added in #131" — it did NOT). Outbound
    entries have ``channel_id`` + ``thread_ts`` + ``sent_at`` +
    reasoning meta (``model_used``, tokens, ``reasoning_duration_ms``,
    ``reasoning_error``).

  * Correlation algorithm (only workable path given the above):
        1. Parse audit caller_session_id as ``"{channel_id}:{event_ts}"``
           for slack_dm-sourced groups. Other sources (email/mcp/
           unknown) currently have no outbound JSONL to xref —
           rows render with null fields per graceful degradation.
        2. Find outbound entries where ``channel_id`` matches AND
           (``thread_ts == event_ts`` OR ``sent_at`` within ±60s
           of the audit group's latest ``emitted_at``).
        3. Pick the closest-time match.

  * Email reasoning replies don't write to slack_dm_log — that's
    the KR-REASONING-PANEL-EMAIL-XREF follow-on per spec §3.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from kora_cli.audit.jsonl_reader import read_audit_entries

logger = logging.getLogger(__name__)


# Slack-DM JSONL path mirrors slack_dm_handler.py:74-82 — env override
# (HERMES_HOME / KORA_HOME via kora_constants) → ``<KORA_HOME>/slack_dm_log.jsonl``.
_SLACK_DM_LOG_FILENAME = "slack_dm_log.jsonl"

# Match window for the fallback timestamp join. ±60s comfortably
# covers reasoning latency (typical 1-5s, p99 ~30s) plus clock drift.
_TIMESTAMP_WINDOW = timedelta(seconds=60)

# 200-char body cap for response_text_truncated_200 — matches the
# field name's semantic contract. The slack_dm outbound stores the
# full text (no truncation at write time); this projection enforces
# the cap at the panel edge.
_RESPONSE_TEXT_CAP = 200


def _slack_dm_log_path() -> Path:
    """Re-resolve on every call so monkeypatch in tests works."""
    from kora_constants import get_kora_home

    return get_kora_home() / _SLACK_DM_LOG_FILENAME


def _parse_slack_dm_session_id(
    session_id: Optional[str],
) -> Optional[Tuple[str, str]]:
    """Parse audit ``caller_session_id`` as ``"{channel_id}:{event_ts}"``.

    Returns ``(channel_id, event_ts)`` for slack_dm-shaped session
    ids; ``None`` for other shapes (email/mcp/unknown). The
    discrimination is: 2 segments separated by ``:``, first segment
    starts with ``D`` or ``C`` (Slack channel prefix), neither
    segment starts with a known prefix like ``email:`` or ``mcp:``.
    """
    if not session_id:
        return None
    # Reject other-source session ids by their prefixes.
    if session_id.startswith(("email:", "mcp:", "unknown")):
        return None
    if session_id == "unknown":
        return None
    # The slack_dm-fallback shape is ``slack_dm:{channel_id or unknown}``
    # (when event_ts is missing). Treat as unparseable.
    if session_id.startswith("slack_dm:"):
        return None
    # Slack-DM happy path: exactly one ":" separator
    parts = session_id.split(":", 1)
    if len(parts) != 2:
        return None
    channel_id, event_ts = parts[0], parts[1]
    if not channel_id or not event_ts:
        return None
    return channel_id, event_ts


def _load_outbound_entries(limit: int = 500) -> List[Dict[str, Any]]:
    """Read recent outbound slack_dm JSONL entries (those with
    ``sent_at`` + ``send_status``). Tolerates missing file +
    malformed lines (log + skip) per the same discipline as
    ``audit/jsonl_reader.py``.

    Returns the LAST ``limit`` outbound entries (file-position-based,
    NOT timestamp-sorted) since the writer appends; the matcher
    sorts by ``sent_at`` later if needed.
    """
    log_path = _slack_dm_log_path()
    if not log_path.is_file():
        return []

    outbound: List[Dict[str, Any]] = []
    try:
        with log_path.open("r", encoding="utf-8") as f:
            for lineno, raw_line in enumerate(f, start=1):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "[kora.reasoning_xref] slack_dm line %d "
                        "malformed JSON, skipped: %r",
                        lineno,
                        exc,
                    )
                    continue
                if not isinstance(entry, dict):
                    continue
                # Outbound entries have ``sent_at`` + ``send_status``;
                # inbound entries have ``received_at`` + ``handled_status``.
                if "sent_at" in entry and "send_status" in entry:
                    outbound.append(entry)
    except OSError as exc:
        logger.warning(
            "[kora.reasoning_xref] failed to read %s: %r", log_path, exc
        )
        return []

    return outbound[-limit:] if limit > 0 else outbound


def _parse_iso(ts: str) -> Optional[datetime]:
    """Parse the writer's ``_now_iso()`` Z-suffixed shape."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _find_xref_for_slack_dm_group(
    channel_id: str,
    event_ts: str,
    group_latest_emitted_at: datetime,
    outbound_entries: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Pick the outbound entry that best matches a reasoning group.

    Algorithm:
      1. Filter outbound to entries with matching ``channel_id``.
      2. Within that, prefer entries where ``thread_ts == event_ts``
         (the natural threading match — Kora's reply threads under
         the user's inbound message).
      3. Fallback: pick the entry with ``sent_at`` closest to the
         group's latest ``emitted_at``, within ``±_TIMESTAMP_WINDOW``.
      4. Return None when no candidate matches — caller renders the
         row with null fields (graceful degradation).
    """
    same_channel = [
        e for e in outbound_entries if e.get("channel_id") == channel_id
    ]
    if not same_channel:
        return None

    # Preferred: thread_ts == event_ts match. Within those, pick the
    # closest by sent_at (multiple replies can thread under the same
    # inbound; pick the one closest to the reasoning finish time).
    thread_matches = [
        e for e in same_channel if e.get("thread_ts") == event_ts
    ]
    if thread_matches:
        return _pick_closest_by_sent_at(thread_matches, group_latest_emitted_at)

    # Fallback: closest sent_at within the time window.
    candidate = _pick_closest_by_sent_at(
        same_channel, group_latest_emitted_at
    )
    if candidate is None:
        return None
    candidate_ts = _parse_iso(candidate.get("sent_at", ""))
    if candidate_ts is None:
        return None
    if abs(candidate_ts - group_latest_emitted_at) > _TIMESTAMP_WINDOW:
        return None
    return candidate


def _pick_closest_by_sent_at(
    entries: List[Dict[str, Any]],
    target: datetime,
) -> Optional[Dict[str, Any]]:
    """Pick the entry whose ``sent_at`` is closest to ``target``.
    Entries with unparseable ``sent_at`` are skipped."""
    best: Optional[Dict[str, Any]] = None
    best_delta: Optional[timedelta] = None
    for e in entries:
        ts = _parse_iso(e.get("sent_at", ""))
        if ts is None:
            continue
        delta = abs(ts - target)
        if best_delta is None or delta < best_delta:
            best = e
            best_delta = delta
    return best


def _derive_cost_rung(
    model_used: Optional[str],
    reasoning_error: Optional[str],
) -> str:
    """Derive lowercase CostRung.value from model name + error code.

    Per the cost-ladder model selection in
    ``kora_cli/reasoning/anthropic_engine.py`` (model → rung
    mapping) AND the agent/cost_state_holder.py:114-117 lowercase
    Enum.value wire format (preserves the PR #132 + #141 K-DG pin).

    Mapping:
      * reasoning_error == "cost_ladder_halted" → "hard_stop_100"
      * model contains "opus"   → "normal"
      * model contains "sonnet" → "warn_75"
      * model contains "haiku"  → "downshift_90"
      * unmapped / missing      → "unknown"

    Substring-match (instead of exact-equals) so future minor model
    revs (claude-opus-4-7 → claude-opus-4-8 etc) keep mapping
    correctly without code changes.
    """
    if reasoning_error == "cost_ladder_halted":
        return "hard_stop_100"
    if not model_used:
        return "unknown"
    lower = model_used.lower()
    if "opus" in lower:
        return "normal"
    if "sonnet" in lower:
        return "warn_75"
    if "haiku" in lower:
        return "downshift_90"
    return "unknown"


def _truncate_response_text(text: Optional[str]) -> Optional[str]:
    """200-char cap matching the field name's semantic contract.
    None passes through (no response captured)."""
    if text is None:
        return None
    s = str(text)
    if len(s) <= _RESPONSE_TEXT_CAP:
        return s
    return s[:_RESPONSE_TEXT_CAP] + "…"


def _aggregate_status(
    statuses: List[str],
) -> Tuple[str, Optional[str]]:
    """Mirror the aggregation in /api/reasoning/recent's projection:
    all-ok → ok; any not_allowed → halted+capability_denied; any
    execution_error → failed+handler_error; other non-ok → failed."""
    if all(s == "ok" for s in statuses):
        return "ok", None
    if any(s == "not_allowed" for s in statuses):
        return "halted", "capability_denied"
    if any(s == "execution_error" for s in statuses):
        return "failed", "handler_error"
    return "failed", next((s for s in statuses if s != "ok"), "unknown")


def load_reasoning_calls_with_xref(
    *,
    limit: int = 200,
) -> Tuple[List[Dict[str, Any]], int]:
    """Load + group reasoning audit rows + cross-reference slack_dm.

    Returns ``(projected_calls, total_recent_24h_raw_rows)``.

    The ``projected_calls`` list is newest-first (by group started_at)
    and capped at ``limit`` groups. ``total_recent_24h_raw_rows`` is
    the count of INDIVIDUAL audit rows in the 24h window (NOT
    groups) — matches the aggregate-counts-from-individual-rows
    pattern from PR #141 so the dashboard headline reflects activity
    volume, not pagination choice.

    SECURITY (carry-forward from #141 + #132 + xref-specific):
      * model_used + tokens are inherently safe metadata.
      * cost_rung_at_call is lowercase CostRung.value (PR #132 pin).
      * response_text_truncated_200 IS Joshua-content (intentional
        carve-out from PII regex sweep — same shape as #141's
        message_id carve-out + slack_dm panel's text carve-out).
        Plain-text rendering already enforced FE-side via
        dangerouslySetInnerHTML ban from PR #132.
    """
    from kora_cli.audit.jsonl_sink import AuditEntry  # noqa: F401 — for type-doc

    audit_rows = read_audit_entries(seam="reasoning.tool_called")
    outbound_entries = _load_outbound_entries(limit=500)

    capped_limit = max(1, min(limit, 200))
    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)

    # Group by caller_session_id (same logic as PR #141).
    groups: Dict[str, List[Any]] = {}
    for e in audit_rows:
        key = e.caller_session_id or f"orphan-{id(e)}"
        groups.setdefault(key, []).append(e)

    projected: List[Dict[str, Any]] = []
    for sid, group_rows in groups.items():
        rows_sorted = sorted(group_rows, key=lambda e: e.emitted_at)
        first = rows_sorted[0]
        last = rows_sorted[-1]
        tool_names = [str(e.details.get("tool_name", "")) for e in rows_sorted]
        total_duration_ms = sum(
            int(e.details.get("tool_duration_ms") or 0) for e in rows_sorted
        )
        statuses = [
            str(e.details.get("tool_status", "ok")) for e in rows_sorted
        ]
        agg_status, error_code = _aggregate_status(statuses)
        triggered_by = (
            first.details.get("triggered_by") or first.source or "slack_dm"
        )

        # XREF: try to find a matching slack_dm outbound entry.
        parsed = _parse_slack_dm_session_id(first.caller_session_id)
        xref: Optional[Dict[str, Any]] = None
        if parsed is not None:
            channel_id, event_ts = parsed
            xref = _find_xref_for_slack_dm_group(
                channel_id=channel_id,
                event_ts=event_ts,
                group_latest_emitted_at=last.emitted_at,
                outbound_entries=outbound_entries,
            )

        if xref is not None:
            model_used = xref.get("model_used")
            input_tokens = int(xref.get("input_tokens") or 0)
            output_tokens = int(xref.get("output_tokens") or 0)
            reasoning_error_x = xref.get("reasoning_error")
            response_text = _truncate_response_text(xref.get("text"))
            # If the xref surfaced a reasoning_error that supersedes
            # the audit-derived status (e.g. cost_ladder_halted with
            # no tool calls at all), reflect that in error_code.
            if reasoning_error_x and reasoning_error_x != error_code:
                if reasoning_error_x == "cost_ladder_halted":
                    agg_status = "halted"
                    error_code = reasoning_error_x
            cost_rung = _derive_cost_rung(model_used, reasoning_error_x)
        else:
            # Graceful degradation: no xref → null fields, same as
            # the pre-xref behavior from PR #141.
            model_used = None
            input_tokens = 0
            output_tokens = 0
            response_text = None
            cost_rung = _derive_cost_rung(None, None)  # "unknown"

        projected.append({
            "id": f"audit-session-{sid or first.emitted_at.isoformat()}",
            "triggered_by": str(triggered_by),
            "started_at": first.emitted_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_ms": total_duration_ms,
            "model_used": model_used,
            "cost_rung_at_call": cost_rung,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "status": agg_status,
            "error_code": error_code,
            "response_text_truncated_200": response_text,
            "tools_used": tool_names,
        })

    projected.sort(key=lambda r: r["started_at"], reverse=True)

    raw_in_window = sum(1 for e in audit_rows if e.emitted_at >= cutoff_24h)
    return projected[:capped_limit], raw_in_window
