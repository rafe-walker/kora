"""Aggregator for the COST-PANEL /api/cost-state endpoint (KR-P2-COST-FLIP).

Projects the :class:`agent.cost_state_holder.CostStateHolder` snapshot
+ a substrate read for deferred-cost-limit tickets into the API shape
COST-PANEL renders. Mirrors the DR-FLIP aggregator pattern: one helper
function + one frozen-dataclass result type so the endpoint stays
thin.

The summary intentionally does NOT persist anything — every read is
fresh against the holder + substrate. Manual-reload UX is consistent
with all 11 admin panels.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, List, Optional

from agent.cost_downshift import (
    CRITICALITY_DOWNSHIFT_ELIGIBLE,
    ModelTier,
    select_effective_model_tier,
)
from agent.cost_state_holder import (
    CostRung,
    CostState,
    CostStateHolder,
    DEFAULT_RECONCILE_TOLERANCE_PCT,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CostStateSummary:
    """Aggregated cost-ladder state for the operator-facing /api/cost-state.

    All fields are projection-ready for the FE shape. ``rate_limit_pulse``
    is optional and present-only when a direct-Anthropic call recently
    populated it (the OpenAI-compat path doesn't surface the headers).

    ``reconciliation_history`` carries a single synthesized entry from
    the holder's ``last_reconciled_*`` snapshot when reconciliation has
    happened at least once, else empty. Tagged with ``synthesized:
    True`` so the FE can render a "synth" badge — same pattern as
    DR-FLIP's epoch_history synthesis. When a future bucket adds a
    real reconciliation-history accessor, the helper switches and the
    synthesized branch becomes unreachable.

    ``deferred_tickets`` carries up to N rows from the substrate
    ``tickets`` table filtered to ``sea_status = 'deferred_cost_limit'``.
    """

    current: dict
    rate_limit_pulse: Optional[dict]
    deferred_tickets: List[dict] = field(default_factory=list)
    reconciliation_history: List[dict] = field(default_factory=list)


_RUNG_WIRE: dict[CostRung, str] = {
    CostRung.NORMAL: "normal",
    CostRung.WARN_75: "warn_75",
    CostRung.DOWNSHIFT_90: "downshift_90",
    CostRung.HARD_STOP_100: "hard_stop_100",
}

# Threshold pct mirrors what the COST-PANEL spec carries today (the
# header that the FE shows under "rung threshold"). When the rung is
# NORMAL the "next" threshold is 75 — surface that so the FE can
# render "rung WARN at 75%" hints consistently.
_RUNG_THRESHOLD_PCT: dict[CostRung, int] = {
    CostRung.NORMAL: 75,
    CostRung.WARN_75: 75,
    CostRung.DOWNSHIFT_90: 90,
    CostRung.HARD_STOP_100: 100,
}


def _project_current(holder: CostStateHolder, state: CostState) -> dict:
    """Project the holder snapshot into the ``current`` API shape.

    Effective tier + downshift fields are derived via
    :func:`select_effective_model_tier` for a representative
    ``downshift_eligible`` ticket against the configured frontier tier
    (Opus). This gives the operator the "what would a typical ticket
    run at right now" answer — frontier-only tickets would still run
    at Opus per the downshift rules, but those are the minority.
    """
    rung = holder.active_rung()
    decision = select_effective_model_tier(
        ticket_criticality=CRITICALITY_DOWNSHIFT_ELIGIBLE,
        active_rung=rung,
        configured_tier=ModelTier.OPUS,
    )
    effective_tier = decision.effective_tier
    effective_tier_wire = (
        effective_tier.value if effective_tier is not None else "opus"
    )

    pct_used = holder.current_pct_used() * 100.0
    burn = holder.burn_rate_usd_per_day()
    projected = holder.projected_end_of_period_usd()

    now = datetime.now(timezone.utc)
    days_remaining = max(
        0,
        (
            _billing_period_end(state.billing_period_start) - now
        ).days,
    )

    return {
        "billing_period_start": _iso(state.billing_period_start),
        "billing_period_end": _iso(_billing_period_end(state.billing_period_start)),
        "days_remaining": days_remaining,
        "credit_pool_usd": round(state.credit_pool_usd, 2),
        "spent_to_date_usd": round(state.spent_to_date_usd, 2),
        "burn_rate_usd_per_day": round(burn, 2),
        "projected_end_of_period_usd": round(projected, 2),
        "active_rung": _RUNG_WIRE[rung],
        "active_rung_threshold_pct": _RUNG_THRESHOLD_PCT[rung],
        "current_pct_used": round(pct_used, 2),
        "effective_model_tier": effective_tier_wire,
        # ``downshift_active`` is true whenever the rung implies any
        # behavioural change (warn or worse). Even at WARN_75 we're
        # actively downshifting downshift-eligible tickets.
        "downshift_active": rung is not CostRung.NORMAL,
        "downshift_reason": decision.reason,
        "extra_usage_off": state.extra_usage_off,
    }


def _project_rate_limit_pulse(state: CostState) -> Optional[dict]:
    """Project the cached Anthropic rate-limit headers into the API shape,
    or ``None`` when no direct-Anthropic call has populated them yet."""
    pulse = state.latest_rate_limit_pulse
    if pulse is None:
        return None
    return {
        "captured_at": _iso(pulse.captured_at),
        "requests": {
            "limit": int(pulse.requests.limit),
            "remaining": int(pulse.requests.remaining),
            "reset_at": _iso(pulse.requests.reset_at),
        },
        "tokens": {
            "limit": int(pulse.tokens.limit),
            "remaining": int(pulse.tokens.remaining),
            "reset_at": _iso(pulse.tokens.reset_at),
        },
    }


def _synthesize_reconciliation_history(state: CostState) -> List[dict]:
    """Build a single-entry reconciliation history from the holder snapshot.

    Stop-gap until a real history accessor lands — same shape pattern
    as DR-FLIP's epoch_history synthesis. Returns ``[]`` when no
    reconciliation has happened yet (operator sees an empty section
    rather than a row with null fields).
    """
    if state.last_reconciled_at is None:
        return []
    if state.last_reconciled_anthropic_usd is None:
        return []
    anthropic = float(state.last_reconciled_anthropic_usd)
    local = float(state.spent_to_date_usd)
    delta = anthropic - local
    delta_pct = (abs(delta) / local * 100.0) if local > 0 else 0.0
    within_tolerance = delta_pct <= (DEFAULT_RECONCILE_TOLERANCE_PCT * 100.0)
    return [
        {
            "reconciled_at": _iso(state.last_reconciled_at),
            "local_estimator_usd": round(local, 2),
            "anthropic_reported_usd": round(anthropic, 2),
            "delta_usd": round(delta, 2),
            "delta_pct": round(delta_pct, 2),
            "within_tolerance": within_tolerance,
            "synthesized": True,
        }
    ]


async def _read_deferred_tickets(provider: Any) -> List[dict]:
    """Substrate query for ``deferred_cost_limit`` tickets via the
    existing connection on ``provider``.

    Returns ``[]`` (rather than raising) on any of:
      * provider missing or has no ``_connection``
      * kora actor_id unresolvable for the workspace
      * read raises

    The defer list is best-effort surfacing — a substrate hiccup
    shouldn't take the whole COST panel down. Errors logged loudly.
    """
    if provider is None:
        return []
    connection = getattr(provider, "_connection", None)
    if connection is None:
        logger.warning(
            "[cost_state_summary] provider has no _connection; "
            "deferred tickets empty"
        )
        return []
    try:
        from plugins.memory.isokron.assigned_sea_tickets import (
            _resolve_kora_actor_id,
        )
        from plugins.memory.isokron.cost_deferred_tickets import (
            read_deferred_cost_limit_tickets,
        )

        actor_id = await _resolve_kora_actor_id(provider)
        if actor_id is None:
            logger.warning(
                "[cost_state_summary] no kora actor_id resolvable; "
                "deferred tickets empty"
            )
            return []
        import asyncio

        pool = connection.get_pg_pool()
        future = connection._submit_async(
            read_deferred_cost_limit_tickets(actor_id=actor_id, pool=pool)
        )
        return await asyncio.wrap_future(future)
    except Exception:
        logger.exception(
            "[cost_state_summary] deferred-ticket read raised; returning []"
        )
        return []


async def get_cost_state_summary(
    cost_holder: CostStateHolder,
    provider: Any,
) -> CostStateSummary:
    """Aggregate cost-ladder state for the operator-facing /api/cost-state.

    The holder reads are synchronous (immutable snapshot via
    ``cost_holder.current``); deferred-ticket read goes via the
    provider's pool. Failures in the substrate read degrade to an
    empty deferred list rather than failing the whole summary — the
    panel is a diagnostic surface and the rung / burn rate are the
    load-bearing fields.

    Args:
        cost_holder: The process-wide cost-ladder holder. Caller
            obtains via :func:`agent.cost_state_holder.get_cost_holder`.
        provider: The active memory provider (for the substrate pool
            access). When None, deferred_tickets stays [].
    """
    state = cost_holder.current
    current = _project_current(cost_holder, state)
    rate_limit_pulse = _project_rate_limit_pulse(state)
    reconciliation_history = _synthesize_reconciliation_history(state)
    deferred_tickets = await _read_deferred_tickets(provider)

    return CostStateSummary(
        current=current,
        rate_limit_pulse=rate_limit_pulse,
        deferred_tickets=deferred_tickets,
        reconciliation_history=reconciliation_history,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _billing_period_end(period_start: datetime) -> datetime:
    """End-of-month boundary in UTC: first day of the next month - 1 day,
    end of day. Mirrors :func:`agent.cost_state_holder._days_in_billing_period`'s
    month-boundary semantics."""
    if period_start.month == 12:
        next_month = period_start.replace(
            year=period_start.year + 1, month=1, day=1
        )
    else:
        next_month = period_start.replace(month=period_start.month + 1, day=1)
    # Last second of the period (panel uses the calendar end-of-month).
    from datetime import timedelta

    return next_month - timedelta(seconds=1)
