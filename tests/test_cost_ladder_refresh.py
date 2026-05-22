"""KR-P2-K ST5 — tests for ``agent.cost_ladder_refresh``.

Covers the cross-holder coordinator
:func:`refresh_billing_period_and_resume`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent.cost_ladder_refresh import refresh_billing_period_and_resume
from agent.cost_state_holder import (
    CostStateHolder,
    _reset_cost_holder_for_tests,
)
from agent.operational_state import (
    ClaimPermission,
    DegradationReason,
    OperationalState,
    PrimaryState,
)
from agent.operational_state_holder import (
    OperationalStateHolder,
    _reset_holder_for_tests,
)


@pytest.fixture(autouse=True)
def _reset():
    _reset_holder_for_tests()
    _reset_cost_holder_for_tests()
    yield
    _reset_holder_for_tests()
    _reset_cost_holder_for_tests()


def _cost_holder() -> CostStateHolder:
    return CostStateHolder(
        billing_period_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        credit_pool_usd=200.00,
    )


def _op_holder_paused_cost() -> OperationalStateHolder:
    initial = OperationalState(
        primary_state=PrimaryState.PAUSED,
        degradation_reasons=frozenset({DegradationReason.COST}),
        claim_permission=ClaimPermission.NONE,
    )
    return OperationalStateHolder(initial_state=initial)


def _op_holder_ready() -> OperationalStateHolder:
    return OperationalStateHolder(
        initial_state=OperationalState(
            primary_state=PrimaryState.READY,
            claim_permission=ClaimPermission.NORMAL,
        )
    )


class _FakePoller:
    def __init__(self) -> None:
        self.ramp_started_count = 0

    def start_ramped_resume(self) -> None:
        self.ramp_started_count += 1


# ---------------------------------------------------------------------------
# Happy path — paused on cost, fully cleared
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paused_cost_cleared_to_ready_and_ramp_started():
    cost = _cost_holder()
    op = _op_holder_paused_cost()
    poller = _FakePoller()

    # Bump pre-refresh spend so the reset is observable.
    import dataclasses

    cost._state = dataclasses.replace(  # type: ignore[attr-defined]
        cost.current, spent_to_date_usd=210.00
    )

    cleared = await refresh_billing_period_and_resume(
        new_period_start=datetime(2026, 6, 1, tzinfo=timezone.utc),
        cost_holder=cost,
        operational_holder=op,
        poller=poller,
    )

    assert cleared is True
    # Cost refreshed
    assert cost.current.spent_to_date_usd == 0.0
    assert cost.current.billing_period_start == datetime(
        2026, 6, 1, tzinfo=timezone.utc
    )
    # Op state cleared
    assert op.current.primary_state is PrimaryState.READY
    assert DegradationReason.COST not in op.current.degradation_reasons
    # Ramp engaged
    assert poller.ramp_started_count == 1


# ---------------------------------------------------------------------------
# Not paused on cost — refresh + ramp, no transition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ready_state_refresh_still_starts_ramp_no_transition():
    cost = _cost_holder()
    op = _op_holder_ready()
    poller = _FakePoller()

    cleared = await refresh_billing_period_and_resume(
        new_period_start=datetime(2026, 6, 1, tzinfo=timezone.utc),
        cost_holder=cost,
        operational_holder=op,
        poller=poller,
    )

    assert cleared is False
    # Cost still refreshed
    assert cost.current.billing_period_start == datetime(
        2026, 6, 1, tzinfo=timezone.utc
    )
    # Op state unchanged
    assert op.current.primary_state is PrimaryState.READY
    # Ramp still engaged (the throttle is a useful default after any
    # period boundary, not just after COST clears)
    assert poller.ramp_started_count == 1


# ---------------------------------------------------------------------------
# Paused for a different reason — refresh, no transition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paused_for_non_cost_reason_no_clear():
    cost = _cost_holder()
    op = OperationalStateHolder(
        initial_state=OperationalState(
            primary_state=PrimaryState.PAUSED,
            degradation_reasons=frozenset({DegradationReason.OPERATOR}),
            claim_permission=ClaimPermission.NONE,
        )
    )
    poller = _FakePoller()

    cleared = await refresh_billing_period_and_resume(
        new_period_start=datetime(2026, 6, 1, tzinfo=timezone.utc),
        cost_holder=cost,
        operational_holder=op,
        poller=poller,
    )

    assert cleared is False
    # Op state UNCHANGED — operator pause is unrelated to the cost
    # ladder; the coordinator must not interfere.
    assert op.current.primary_state is PrimaryState.PAUSED
    assert op.current.degradation_reasons == frozenset(
        {DegradationReason.OPERATOR}
    )


# ---------------------------------------------------------------------------
# Paused on multiple reasons including COST — only COST clears
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_reasons_only_cost_cleared():
    cost = _cost_holder()
    op = OperationalStateHolder(
        initial_state=OperationalState(
            primary_state=PrimaryState.PAUSED,
            degradation_reasons=frozenset(
                {DegradationReason.COST, DegradationReason.OPERATOR}
            ),
            claim_permission=ClaimPermission.NONE,
        )
    )

    cleared = await refresh_billing_period_and_resume(
        new_period_start=datetime(2026, 6, 1, tzinfo=timezone.utc),
        cost_holder=cost,
        operational_holder=op,
    )

    assert cleared is True
    # COST removed; OPERATOR still present.
    assert DegradationReason.COST not in op.current.degradation_reasons
    assert DegradationReason.OPERATOR in op.current.degradation_reasons
    # Op state moves to READY per the coordinator's transition; the
    # OPERATOR reason rides forward as a degraded-READY posture.
    assert op.current.primary_state is PrimaryState.READY


# ---------------------------------------------------------------------------
# Optional holder / poller — cost refresh still proceeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_operational_holder_cost_only_refresh():
    cost = _cost_holder()
    poller = _FakePoller()

    cleared = await refresh_billing_period_and_resume(
        new_period_start=datetime(2026, 6, 1, tzinfo=timezone.utc),
        cost_holder=cost,
        operational_holder=None,
        poller=poller,
    )
    assert cleared is False
    assert cost.current.billing_period_start == datetime(
        2026, 6, 1, tzinfo=timezone.utc
    )
    assert poller.ramp_started_count == 1


@pytest.mark.asyncio
async def test_no_poller_cost_and_op_refresh_only():
    cost = _cost_holder()
    op = _op_holder_paused_cost()

    cleared = await refresh_billing_period_and_resume(
        new_period_start=datetime(2026, 6, 1, tzinfo=timezone.utc),
        cost_holder=cost,
        operational_holder=op,
        poller=None,
    )
    assert cleared is True
    assert op.current.primary_state is PrimaryState.READY


# ---------------------------------------------------------------------------
# Fail-soft on op transition raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_op_transition_raises_cost_still_refreshed(caplog):
    cost = _cost_holder()
    op = _op_holder_paused_cost()
    poller = _FakePoller()

    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("op boom")

    op.transition_to = boom  # type: ignore[assignment]

    import logging as _logging

    with caplog.at_level(_logging.WARNING, logger="agent.cost_ladder_refresh"):
        cleared = await refresh_billing_period_and_resume(
            new_period_start=datetime(2026, 6, 1, tzinfo=timezone.utc),
            cost_holder=cost,
            operational_holder=op,
            poller=poller,
        )

    assert cleared is False
    # Cost still refreshed even though op transition raised
    assert cost.current.billing_period_start == datetime(
        2026, 6, 1, tzinfo=timezone.utc
    )
    # Ramp still engaged
    assert poller.ramp_started_count == 1
    # WARNING surfaced
    assert any("PAUSED{COST} -> READY" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Fail-soft on poller start_ramped_resume raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poller_ramp_raise_does_not_block_refresh(caplog):
    cost = _cost_holder()
    op = _op_holder_paused_cost()
    poller = MagicMock()
    poller.start_ramped_resume.side_effect = RuntimeError("ramp boom")

    import logging as _logging

    with caplog.at_level(_logging.WARNING, logger="agent.cost_ladder_refresh"):
        cleared = await refresh_billing_period_and_resume(
            new_period_start=datetime(2026, 6, 1, tzinfo=timezone.utc),
            cost_holder=cost,
            operational_holder=op,
            poller=poller,
        )

    # Cost + op both refreshed
    assert cost.current.billing_period_start == datetime(
        2026, 6, 1, tzinfo=timezone.utc
    )
    assert cleared is True
    assert op.current.primary_state is PrimaryState.READY
    # WARN line emitted
    assert any("start_ramped_resume raised" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_poller_without_start_ramped_resume_attribute_skipped():
    """Duck-typed call site — a mock without ``start_ramped_resume`` (or
    a poller from an older minor version) is silently skipped."""
    cost = _cost_holder()
    op = _op_holder_paused_cost()
    poller = object()  # bare object — no start_ramped_resume

    cleared = await refresh_billing_period_and_resume(
        new_period_start=datetime(2026, 6, 1, tzinfo=timezone.utc),
        cost_holder=cost,
        operational_holder=op,
        poller=poller,
    )
    assert cleared is True
    assert op.current.primary_state is PrimaryState.READY
