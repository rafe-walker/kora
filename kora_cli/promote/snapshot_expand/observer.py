"""Tool-call observation collector — KR-PROMOTE-SNAPSHOT-EXPAND.

Reads ``reasoning.tool_called`` audit rows (emitted by the engine's
:func:`_emit_tool_called_audit` writer; see the audit/ sub-plugin)
and projects each into a :class:`ToolCallObservation` shape the
proposer consumes.

# Why ``reasoning.tool_called``

That seam captures every tool invocation the reasoning loop
dispatched. Status-shaped queries that hit live tool-calling (e.g.
"how many open tickets?" → a Sea_Tickets tool call) leave a row
here. Clustering by ``tool_name`` surfaces recurring tool-call
shapes whose answers a snapshot field could pre-compute.

# Filtering

  * ``since`` — lower-bound timestamp; defaults to 7 days back
    when not passed.
  * Tool names already known to back snapshot fields (e.g. the
    cost-ladder rung is in the snapshot, so a tool call against
    ``kora__cost_ladder_status`` shouldn't propose another field
    for the same data) are filtered out via :data:`KNOWN_BACKED_TOOLS`.
  * Empty / malformed entries (no ``tool_name`` in details) are
    skipped — the writer doesn't omit the field but defensive
    drops cover post-extension drift.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

logger = logging.getLogger(__name__)


# Tools whose answers are ALREADY covered by snapshot fields. A
# cluster of these would propose a redundant field — exclude up-
# front so the proposer doesn't waste a slot. Extend when new
# snapshot fields are added (the snapshot-expand loop's own
# audit trail makes it discoverable when an operator approves a
# field that adds to this list).
KNOWN_BACKED_TOOLS = frozenset(
    {
        "kora__cost_ladder_status",
        "kora__operational_state",
        "kora__alerts_active",
        "kora__service_health",
        "kora__daemon_health",
        # Sea_Tickets read is now snapshot-backed (v5) — exclude.
        "kora__assigned_sea_tickets",
    }
)


@dataclass(frozen=True, slots=True)
class ToolCallObservation:
    """One reasoning tool invocation, projected for clustering."""

    tool_name: str
    caller_session_id: str
    timestamp: datetime
    # ``arguments_summary`` is a short string projection of the
    # tool input — used by the proposer to detect when a tool was
    # consistently called with the same args (strong signal a
    # snapshot field can pre-compute the answer). May be empty
    # when the audit row's details don't carry arguments.
    arguments_summary: str


async def collect_recent_tool_calls(
    *,
    since: Optional[datetime] = None,
) -> List[ToolCallObservation]:
    """Read the audit JSONL filtered to ``reasoning.tool_called`` and
    return a list of :class:`ToolCallObservation`.

    Args:
      since: Lower bound (aware datetime; UTC assumed if naive).
        Defaults to 7 days before now if not provided.

    Returns observations sorted by ``timestamp`` ascending. Failures
    in the underlying reader return ``[]`` (the reader itself is
    fail-soft).
    """
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(days=7)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    try:
        from kora_cli.audit.jsonl_reader import read_audit_entries
    except Exception as exc:
        logger.warning(
            "[kora.promote.snapshot_expand.observer] audit reader "
            "import failed: %r — no observations",
            exc,
        )
        return []

    try:
        entries = read_audit_entries(seam="reasoning.tool_called", since=since)
    except Exception as exc:
        logger.warning(
            "[kora.promote.snapshot_expand.observer] read_audit_entries "
            "raised %r — no observations",
            exc,
        )
        return []

    out: List[ToolCallObservation] = []
    for entry in entries:
        details = entry.details or {}
        tool_name = details.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            continue
        if tool_name in KNOWN_BACKED_TOOLS:
            continue
        csid = entry.caller_session_id or ""
        # The arguments_summary projection is intentionally a short
        # repr — the proposer just needs a "did this same shape
        # repeat" signal, not full arg replay. Long arg values
        # truncate at 80 chars to keep clustering cheap.
        args_raw = details.get("arguments")
        if isinstance(args_raw, dict) and args_raw:
            args_repr = ",".join(
                f"{k}={str(v)[:40]}" for k, v in sorted(args_raw.items())
            )
        else:
            args_repr = ""
        out.append(
            ToolCallObservation(
                tool_name=tool_name,
                caller_session_id=str(csid),
                timestamp=entry.emitted_at,
                arguments_summary=args_repr[:80],
            )
        )

    out.sort(key=lambda o: o.timestamp)
    return out
