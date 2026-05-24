"""Tool-call usage observer — KR-PROMOTE-TOOL-TRIMMING.

Reads ``reasoning.tool_called`` audit rows and tallies them by
(route, tool_name) over the observation window. Returns a nested
dict the proposer consumes.

# Route attribution

The ``reasoning.tool_called`` writer (see
``kora_cli/reasoning/anthropic_engine.py::_emit_tool_called_audit``)
stamps ``route`` inside the audit ``details`` dict from the
engine's per-call source mapping (post KR-PROMOTE-EXPAND-AND-
TELEMETRY-WIRES). Rows without a ``route`` field bucket into
``"unknown"`` per the cost-telemetry taxonomy.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RouteToolUsage:
    """Per-route tool-usage rollup over the observation window."""

    route: str
    total_calls: int
    tools_called: Set[str]  # tool names with ≥1 call
    per_tool_calls: Dict[str, int]  # tool_name → call count


async def collect_route_tool_usage(
    *, since: Optional[datetime] = None
) -> List[RouteToolUsage]:
    """Tally ``reasoning.tool_called`` entries by (route, tool_name).

    Args:
      since: Lower bound (aware datetime). Defaults to 30 days
        before now — long enough to surface durably-unused tools
        without being polluted by a single bursty week.
    """
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(days=30)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    try:
        from kora_cli.audit.jsonl_reader import read_audit_entries
    except Exception as exc:
        logger.warning(
            "[kora.promote.tool_trimming.observer] audit reader import "
            "failed: %r — no rollups",
            exc,
        )
        return []

    try:
        entries = read_audit_entries(seam="reasoning.tool_called", since=since)
    except Exception as exc:
        logger.warning(
            "[kora.promote.tool_trimming.observer] read_audit_entries "
            "raised %r — no rollups",
            exc,
        )
        return []

    by_route_tool: Dict[str, Dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    for entry in entries:
        details = entry.details or {}
        tool_name = details.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            continue
        # Route can live either in ``details["route"]`` (KR-PROMOTE-
        # EXPAND-AND-TELEMETRY-WIRES future write site) or fall back
        # to a derived caller_session_id prefix; v1 reads both.
        route = details.get("route")
        if not isinstance(route, str) or not route:
            csid = entry.caller_session_id or ""
            if csid.startswith("probe:"):
                route = "probe_investigation"
            elif csid.startswith("email:"):
                route = "email_inbound"
            elif csid.startswith("mcp:"):
                route = "mcp_tool"
            elif csid:
                route = "slack_dm"
            else:
                route = "unknown"
        by_route_tool[route][tool_name] += 1

    out: List[RouteToolUsage] = []
    for route, tool_map in sorted(by_route_tool.items()):
        total = sum(tool_map.values())
        out.append(
            RouteToolUsage(
                route=route,
                total_calls=total,
                tools_called=set(tool_map.keys()),
                per_tool_calls=dict(tool_map),
            )
        )
    return out
