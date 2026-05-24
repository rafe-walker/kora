"""Tool-trim proposal generator — KR-PROMOTE-TOOL-TRIMMING.

For each route with sufficient call volume, identifies tools that
were never called in the window and proposes adding them to the
route's drop-list.

# "Tools registered for a route" sourcing

v1 uses the **union of all tools observed across routes** as the
"available tools" set. Reason: extracting the per-route registered
tool list from the live engine requires importing a heavy chain
(``listeners.mcp_tools`` → MCP transport) that's neither cheap
nor available outside the daemon. The union-across-observation is
a conservative proxy — it gives operator the set of tools that
SOMEONE called, but THIS route didn't, which is exactly the
operator-attention signal.

A future bucket can replace ``_available_tool_names`` with a real
per-route registered-tool projection once
``pre_tool_list_finalized`` ships a side-channel that records the
registered list per route. The proposer's interface (the
``available_tool_names`` set) is the seam.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Literal, Set

from .observer import RouteToolUsage

logger = logging.getLogger(__name__)


MIN_CALLS_ENV = "KORA_PROMOTE_TOOL_TRIMMING_MIN_CALLS"
OBSERVATION_WINDOW_DAYS_ENV = "KORA_PROMOTE_TOOL_TRIMMING_WINDOW_DAYS"

DEFAULT_MIN_CALLS = 20
DEFAULT_OBSERVATION_WINDOW_DAYS = 30


ProposalStatus = Literal["pending", "approved", "rejected", "expired"]


@dataclass(frozen=True, slots=True)
class ToolTrimProposal:
    """Wire-stable proposal shape."""

    proposal_id: str
    route: str
    unused_tools: List[str]  # alphabetical
    total_calls_for_route: int
    observation_window_days: int
    confidence: float  # derived from sample size
    created_at: datetime
    status: ProposalStatus = "pending"
    review_notes: str = ""


def _format_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def proposal_to_dict(p: ToolTrimProposal) -> Dict[str, Any]:
    out = asdict(p)
    out["created_at"] = _format_iso(p.created_at)
    out["unused_tools"] = list(p.unused_tools)
    return out


def _int_env(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value < minimum:
        return default
    return value


def _available_tool_names(rollups: List[RouteToolUsage]) -> Set[str]:
    """Union-across-routes of tools observed in the window. v1 proxy
    for "tools registered for any route" — see module docstring for
    the rationale + future swap-in seam."""
    out: Set[str] = set()
    for r in rollups:
        out.update(r.tools_called)
    return out


def generate_proposals(
    rollups: List[RouteToolUsage],
    *,
    now: datetime,
    available_tool_names: Iterable[str] | None = None,
) -> List[ToolTrimProposal]:
    """For each eligible route, propose dropping tools the route
    never called. Returns proposals sorted by route (stable
    ordering for tests + operator triage)."""
    min_calls = _int_env(MIN_CALLS_ENV, DEFAULT_MIN_CALLS, minimum=2)
    window_days = _int_env(
        OBSERVATION_WINDOW_DAYS_ENV,
        DEFAULT_OBSERVATION_WINDOW_DAYS,
        minimum=1,
    )

    if available_tool_names is None:
        available = _available_tool_names(rollups)
    else:
        available = set(available_tool_names)

    out: List[ToolTrimProposal] = []
    for r in rollups:
        if r.total_calls < min_calls:
            continue
        unused = sorted(available - r.tools_called)
        if not unused:
            continue
        confidence = min(1.0, r.total_calls / (2 * min_calls))
        out.append(
            ToolTrimProposal(
                proposal_id=str(uuid.uuid4()),
                route=r.route,
                unused_tools=unused,
                total_calls_for_route=r.total_calls,
                observation_window_days=window_days,
                confidence=round(confidence, 4),
                created_at=now,
                status="pending",
            )
        )
    out.sort(key=lambda p: p.route)
    return out
