"""KR-P2-K ST5 — tests for ``CostStateHolder.refresh_billing_period``.

Covers:
  - Happy path: spent_to_date resets to 0; period bumps; reconciliation
    state clears.
  - Naive datetime rejected.
  - Same-or-earlier period start rejected.
  - latest_rate_limit_pulse preserved across refresh.
  - credit_pool_usd + extra_usage_off preserved across refresh.
  - Post-refresh active_rung returns NORMAL even if pre-refresh was
    HARD_STOP_100.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.cost_state_holder import (
    CostRung,
    CostStateHolder,
    RateLimitAxis,
    RateLimitPulse,
    _reset_cost_holder_for_tests,
)


@pytest.fixture(autouse=True)
def _reset():
    _reset_cost_holder_for_tests()
    yield
    _reset_cost_holder_for_tests()


def _holder(*, pool: float = 200.00) -> CostStateHolder:
    return CostStateHolder(
        billing_period_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        credit_pool_usd=pool,
    )


def test_refresh_resets_spent_to_zero():
    holder = _holder()
    import dataclasses

    holder._state = dataclasses.replace(  # type: ignore[attr-defined]
        holder.current, spent_to_date_usd=150.00
    )
    holder.refresh_billing_period(datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert holder.current.spent_to_date_usd == 0.0


def test_refresh_bumps_billing_period_start():
    holder = _holder()
    new = datetime(2026, 6, 1, tzinfo=timezone.utc)
    holder.refresh_billing_period(new)
    assert holder.current.billing_period_start == new


def test_refresh_clears_reconciliation_state():
    holder = _holder()
    import dataclasses

    holder._state = dataclasses.replace(  # type: ignore[attr-defined]
        holder.current,
        last_reconciled_at=datetime(2026, 5, 15, tzinfo=timezone.utc),
        last_reconciled_anthropic_usd=50.00,
    )
    holder.refresh_billing_period(datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert holder.current.last_reconciled_at is None
    assert holder.current.last_reconciled_anthropic_usd is None


def test_refresh_preserves_latest_rate_limit_pulse():
    """The rate-limit window doesn't align with the billing period;
    a recent pulse remains operator-informative across refresh."""
    holder = _holder()
    pulse = RateLimitPulse(
        requests=RateLimitAxis(
            limit=1000,
            remaining=850,
            reset_at=datetime(2026, 5, 21, 12, tzinfo=timezone.utc),
        ),
        tokens=RateLimitAxis(
            limit=10_000_000,
            remaining=7_500_000,
            reset_at=datetime(2026, 5, 21, 12, 5, tzinfo=timezone.utc),
        ),
        captured_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
    )
    holder.record_rate_limit_pulse(pulse)
    holder.refresh_billing_period(datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert holder.current.latest_rate_limit_pulse is pulse


def test_refresh_preserves_credit_pool_and_extra_usage_off():
    holder = CostStateHolder(
        billing_period_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        credit_pool_usd=500.00,
        extra_usage_off=False,
    )
    holder.refresh_billing_period(datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert holder.current.credit_pool_usd == 500.00
    assert holder.current.extra_usage_off is False


def test_refresh_rejects_naive_datetime():
    holder = _holder()
    with pytest.raises(ValueError, match="timezone-aware"):
        holder.refresh_billing_period(datetime(2026, 6, 1))  # naive


def test_refresh_rejects_same_period_start():
    holder = _holder()
    same = datetime(2026, 5, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="strictly forward"):
        holder.refresh_billing_period(same)


def test_refresh_rejects_earlier_period_start():
    holder = _holder()
    earlier = datetime(2026, 4, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="strictly forward"):
        holder.refresh_billing_period(earlier)


def test_post_refresh_active_rung_drops_to_normal_from_hard_stop():
    """After refresh, even a previously-hard-stopped holder reports
    NORMAL rung — the new period starts with 0 spend."""
    holder = _holder()
    import dataclasses

    # Force pre-refresh to HARD_STOP.
    holder._state = dataclasses.replace(  # type: ignore[attr-defined]
        holder.current, spent_to_date_usd=210.00
    )
    assert holder.active_rung() is CostRung.HARD_STOP_100

    holder.refresh_billing_period(datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert holder.active_rung() is CostRung.NORMAL
