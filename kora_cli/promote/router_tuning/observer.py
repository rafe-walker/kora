"""Per-route escalation observer — KR-PROMOTE-ROUTER-TUNING.

Reads the live :func:`kora_cli.telemetry.get_telemetry` singleton's
:meth:`snapshot` and projects per-route rollups for the proposer.

# v1 scope (STOP-ASK §4)

The bucket STOP-ASK §4 anticipated: the observer wants per-call
"was the Opus reply materially better than Haiku's would be?" data.
That data isn't exposed today — collecting it would need either:

  1. A second Haiku call per Opus call to do post-hoc quality
     scoring (doubles spend on every escalation), OR
  2. A new audit row whenever the operator manually issues
     ``/opus`` to fix a Haiku miss (not yet emitted).

v1 ships with the data we DO have: ``calls_count`` +
``escalation_count`` per route. That's enough to surface "this
route escalates 60% of the time — please review the trigger" for
operator-attention; the actual tuning decision stays operator-
gated regardless. A future bucket can wire option 2 (cheap; one
audit row per operator override) to refine the rationale.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RouteEscalationRollup:
    """One route's projection from cost_telemetry's rolling_24h window."""

    route: str
    calls_count: int
    escalation_count: int
    escalation_rate: float  # 0.0..1.0; 0.0 when calls_count == 0
    cost_estimate_usd_total: float


def _safe_rate(escalations: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return min(1.0, escalations / total)


def collect_route_rollups() -> List[RouteEscalationRollup]:
    """Return per-route rollups from the live cost_telemetry singleton.

    Fail-soft: telemetry singleton unavailable / snapshot raises →
    empty list. Proposer treats that as "no data this cycle, no
    proposals" — same fail-soft contract as the other promotion
    loops.

    Routes are sorted alphabetically for stable test assertions;
    the proposer reorders by score before emitting.
    """
    try:
        from kora_cli.telemetry import (
            WINDOW_ROLLING_24H,
            get_telemetry,
        )
    except Exception as exc:
        logger.debug(
            "[kora.promote.router_tuning.observer] telemetry import "
            "failed: %r — no rollups",
            exc,
        )
        return []

    try:
        all_windows = get_telemetry().snapshot()
    except Exception as exc:
        logger.warning(
            "[kora.promote.router_tuning.observer] telemetry.snapshot() "
            "raised %r — no rollups",
            exc,
        )
        return []

    window_data = all_windows.get(WINDOW_ROLLING_24H, {})
    if not isinstance(window_data, dict):
        return []

    out: List[RouteEscalationRollup] = []
    for route, counters in window_data.items():
        if not isinstance(counters, dict):
            continue
        calls = int(counters.get("calls_count") or 0)
        escs = int(counters.get("escalation_count") or 0)
        cost = float(counters.get("cost_estimate_usd_total") or 0.0)
        out.append(
            RouteEscalationRollup(
                route=str(route),
                calls_count=calls,
                escalation_count=escs,
                escalation_rate=_safe_rate(escs, calls),
                cost_estimate_usd_total=round(cost, 6),
            )
        )
    out.sort(key=lambda r: r.route)
    return out
