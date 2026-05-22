"""Cost-ladder monthly refresh coordinator (KR-P2-K ST5, R4.1 §9.6).

Bridges the three holders + objects involved in a billing-period
boundary:

  - :class:`agent.cost_state_holder.CostStateHolder` — resets
    ``spent_to_date_usd`` to 0 and rolls ``billing_period_start``
    to the new period.
  - :class:`agent.operational_state_holder.OperationalStateHolder`
    — if currently PAUSED with reason=COST, transitions back to
    READY and removes the COST reason.
  - :class:`plugins.memory.isokron.sea_ticket_poller.SeaTicketPoller`
    — starts the ramped-resume drain window so the freshly-refreshed
    budget isn't immediately re-burned by a flood of backlogged
    tickets.

The coordinator is fail-soft on the holder side-effects: if the
operational holder isn't initialized (gateway-only deployments
where no agent session has booted), the cost refresh still
happens and the ramped resume still starts — only the
operational-state transition is skipped (no PAUSE to clear).

# Where this gets called from

The scheduler that fires at month boundaries is out of scope for
KR-P2-K ST5 — operators can wire it via cron, the agent's own
boot-time check, or a substrate-driven event. This module ships
the coordination function so any scheduler can call it uniformly.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from agent.cost_state_holder import CostStateHolder
from agent.operational_state import DegradationReason, PrimaryState
from agent.operational_state_holder import OperationalStateHolder

logger = logging.getLogger(__name__)


async def refresh_billing_period_and_resume(
    *,
    new_period_start: datetime,
    cost_holder: CostStateHolder,
    operational_holder: Optional[OperationalStateHolder] = None,
    poller: Optional[object] = None,
) -> bool:
    """Run the full month-boundary sequence.

    Args:
        new_period_start: Tz-aware start of the new billing period;
            forwarded to :meth:`CostStateHolder.refresh_billing_period`.
        cost_holder: Required — the cost holder to refresh.
        operational_holder: Optional — if provided AND currently in
            PAUSED with reason=COST, transitioned to READY with
            ``remove_reasons={COST}``.
        poller: Optional — if provided AND has a
            ``start_ramped_resume`` method (duck-typed to avoid a
            hard import on the poller module here), invoked to begin
            the throttled drain window.

    Returns:
        ``True`` if the operational holder was paused-on-cost and
        was successfully cleared by this refresh; ``False`` otherwise
        (no operational holder, not paused, or not paused for COST).

    Refresh step ordering matters:
      1. Cost holder reset first — so any racing inference response
         that lands during the refresh sees a fresh 0-spent gauge.
      2. Operational transition next — so the PAUSED{COST} state
         clears only after the cost gauge is fresh; if the
         operational transition fails, the cost state is still
         refreshed (caller can retry the transition).
      3. Ramped resume started last — so the poller's gate engages
         AFTER the operational state has cleared (PAUSED still
         blocks claims, so an early ramp start would be wasted).
    """
    cost_holder.refresh_billing_period(new_period_start)

    cleared_cost_pause = False
    if operational_holder is not None:
        current = operational_holder.current
        if (
            current.primary_state is PrimaryState.PAUSED
            and DegradationReason.COST in current.degradation_reasons
        ):
            try:
                await operational_holder.transition_to(
                    PrimaryState.READY,
                    trigger="cost ladder monthly refresh",
                    remove_reasons={DegradationReason.COST},
                )
                cleared_cost_pause = True
            except Exception:
                logger.warning(
                    "[kora.cost_ladder] PAUSED{COST} -> READY "
                    "transition raised during monthly refresh; cost "
                    "state IS refreshed but operational state still "
                    "PAUSED. Caller may retry the transition.",
                    exc_info=True,
                )

    if poller is not None:
        start_ramp = getattr(poller, "start_ramped_resume", None)
        if callable(start_ramp):
            try:
                start_ramp()
            except Exception:
                logger.warning(
                    "[kora.cost_ladder] start_ramped_resume raised "
                    "during monthly refresh; cost + operational state "
                    "ARE refreshed but ramp not engaged. Poller will "
                    "drain at normal rate.",
                    exc_info=True,
                )

    return cleared_cost_pause
