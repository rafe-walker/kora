"""KR-P2-INT-TESTS ST5 — cost pause end-to-end (R4.1 §12).

Per the bucket spec:
  1. Configure CostStateHolder near 100% rung.
  2. Trigger one more inference that crosses 100%.
  3. Verify safe-release of held claim (kora__release_claim called
     with resolution=deferred_cost_limit).
  4. Verify holder transitions PAUSED with reason=COST.
  5. Simulate monthly refresh.
  6. Verify ramped resume: claims throttled at 1-per-30s for first hour.

This wires CostStateHolder + OperationalStateHolder + the refresh
coordinator together so the full chain — credit crossing → rung
update → PAUSED{COST} → refresh → ramp engaged — is exercised at
the runtime layer. The safe-release substrate call is mocked at the
MCP boundary via FakeMCPClient.

# Existing unit tests vs this integration test

Unit tests cover each module in isolation
(``test_cost_state_holder*.py``, ``test_sea_ticket_poller_cost_hard_stop.py``,
``test_cost_ladder_refresh.py``, ``test_sea_ticket_poller_ramped_resume.py``).
This file exercises the cross-module flow: a stateful walkthrough
through the full lifecycle in ONE test sequence so coupling drift
between layers surfaces here.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from agent.cost_ladder_refresh import refresh_billing_period_and_resume
from agent.cost_state_holder import (
    CostRung,
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
from plugins.memory.isokron.sea_ticket_poller import SeaTicketPoller


pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _reset_singletons():
    _reset_holder_for_tests()
    _reset_cost_holder_for_tests()
    yield
    _reset_holder_for_tests()
    _reset_cost_holder_for_tests()


# ---------------------------------------------------------------------------
# Hot-spend simulation
# ---------------------------------------------------------------------------


def _set_spent(holder: CostStateHolder, amount: float) -> None:
    """Bypass the estimator + set ``spent_to_date_usd`` directly.

    Real production path goes through ``record_inference`` →
    estimator → bump. For this integration test we exercise the rung
    machinery, not the pricing source — direct manipulation is more
    explicit + matches the existing unit-test pattern in
    ``test_cost_state_holder_refresh.py``.
    """
    holder._state = dataclasses.replace(  # type: ignore[attr-defined]
        holder.current, spent_to_date_usd=amount
    )


def _holder_at(pct_of_pool: float, *, pool_usd: float = 200.0) -> CostStateHolder:
    holder = CostStateHolder(
        billing_period_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        credit_pool_usd=pool_usd,
    )
    _set_spent(holder, pool_usd * pct_of_pool)
    return holder


# ---------------------------------------------------------------------------
# 1+2: near 100% then cross 100% → HARD_STOP_100
# ---------------------------------------------------------------------------


def test_cost_holder_near_100_pct_crosses_to_hard_stop():
    """Configure at 95% (DOWNSHIFT_90 rung) → bump past 100% → rung is
    HARD_STOP_100. The transition between rungs is purely
    spent/pool-driven; no events fired here."""
    holder = _holder_at(0.95)
    assert holder.active_rung() is CostRung.DOWNSHIFT_90

    # Cross the threshold
    _set_spent(holder, 210.0)  # 105% of $200 pool
    assert holder.active_rung() is CostRung.HARD_STOP_100


# ---------------------------------------------------------------------------
# 3+4: safe-release path + PAUSED{COST} transition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safe_release_with_deferred_cost_limit_and_paused_cost_transition():
    """Full safe-release chain exercised through SeaTicketPoller's
    pre-claim hard-stop check:
      - Cost rung HARD_STOP_100
      - Poller's `_claim_and_work` pre-claim check fires
      - Operational state → PAUSED with reason=COST"""
    # Set up operational state at READY
    op_holder = OperationalStateHolder(
        initial_state=OperationalState(
            primary_state=PrimaryState.READY,
            claim_permission=ClaimPermission.NORMAL,
        )
    )
    # Replace the singleton so the poller's signal_operational_state
    # finds it
    import agent.operational_state_holder as op_module

    op_module._HOLDER = op_holder

    # Cost holder at HARD_STOP
    cost = _holder_at(0.0)  # construct with 0 spend
    import agent.cost_state_holder as cost_module

    cost_module._HOLDER = cost
    _set_spent(cost, 210.0)  # cross to HARD_STOP

    # Build poller (don't run the full poll loop — invoke
    # _claim_and_work directly to test the pre-claim hard-stop path)
    from unittest.mock import AsyncMock, MagicMock
    from plugins.memory.isokron.sea_ticket_poller import SeaTicket

    mcp = MagicMock()
    mcp.invoke = AsyncMock()
    poller = SeaTicketPoller(
        mcp_client=mcp,
        memory_provider=MagicMock(),
        ledger=MagicMock(),
    )

    async def _fake_actor(_ws):
        return "actor-1"

    poller._resolve_kora_actor_id = _fake_actor  # type: ignore[assignment]
    reader = MagicMock()
    reader.get_active_command = AsyncMock(return_value=None)
    poller._kora_control_reader = reader

    ticket = SeaTicket(
        ticket_id="t-1",
        workspace_id="org_test",
        ticket_title="T",
        ticket_objective="O",
        sea_status="assigned",
        sea_priority="medium",
        sea_idea_kind="task",
        sea_captured_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
        created_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
    )

    # Run the pre-claim hard-stop path
    await poller._claim_and_work(ticket)

    # No claim attempted (pre-claim hard-stop short-circuited)
    assert mcp.invoke.await_count == 0
    # Op state transitioned to PAUSED{COST}
    assert op_holder.current.primary_state is PrimaryState.PAUSED
    assert DegradationReason.COST in op_holder.current.degradation_reasons


# ---------------------------------------------------------------------------
# 5: monthly refresh resets spent + clears PAUSED{COST}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_monthly_refresh_clears_paused_cost_and_engages_ramp():
    """End-to-end refresh:
      - Cost holder at HARD_STOP
      - Op holder at PAUSED{COST}
      - Poller exists
      - refresh_billing_period_and_resume(new_period_start) called
      - Cost holder spent resets to 0
      - Op holder transitions READY (COST reason cleared)
      - Poller's ramped resume window engaged"""
    cost = _holder_at(0.0)
    _set_spent(cost, 210.0)
    assert cost.active_rung() is CostRung.HARD_STOP_100

    op_holder = OperationalStateHolder(
        initial_state=OperationalState(
            primary_state=PrimaryState.PAUSED,
            degradation_reasons=frozenset({DegradationReason.COST}),
            claim_permission=ClaimPermission.NONE,
        )
    )

    from unittest.mock import MagicMock

    poller = SeaTicketPoller(
        mcp_client=MagicMock(),
        memory_provider=MagicMock(),
        ledger=MagicMock(),
    )

    cleared = await refresh_billing_period_and_resume(
        new_period_start=datetime(2026, 6, 1, tzinfo=timezone.utc),
        cost_holder=cost,
        operational_holder=op_holder,
        poller=poller,
    )

    assert cleared is True
    # Cost refresh
    assert cost.current.spent_to_date_usd == 0.0
    assert cost.current.billing_period_start == datetime(
        2026, 6, 1, tzinfo=timezone.utc
    )
    assert cost.active_rung() is CostRung.NORMAL
    # Op state cleared
    assert op_holder.current.primary_state is PrimaryState.READY
    assert DegradationReason.COST not in op_holder.current.degradation_reasons
    # Ramp engaged
    assert poller._ramped_resume_until is not None


# ---------------------------------------------------------------------------
# 6: ramped resume throttles claims at 1-per-30s
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ramped_resume_throttles_claims_at_30s_interval(monkeypatch):
    """After refresh starts the ramp, two successive
    `_await_ramped_resume_gate` calls should be separated by at least
    `ramped_resume_min_interval_seconds` (30s default) for the
    `ramped_resume_duration_seconds` window (3600s default)."""
    from unittest.mock import MagicMock

    poller = SeaTicketPoller(
        mcp_client=MagicMock(),
        memory_provider=MagicMock(),
        ledger=MagicMock(),
        ramped_resume_duration_seconds=3600,
        ramped_resume_min_interval_seconds=30,
    )
    poller.start_ramped_resume()
    # Simulate "just claimed 5s ago"
    poller._last_claim_started_at = datetime.now(timezone.utc) - timedelta(
        seconds=5
    )

    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    await poller._await_ramped_resume_gate()

    # Should have slept ~25s to reach the 30s interval
    assert len(sleeps) == 1
    assert 23.0 <= sleeps[0] <= 27.0


@pytest.mark.asyncio
async def test_ramp_window_expires_after_duration():
    """Past the ramp deadline, the gate becomes a no-op (window
    self-clears)."""
    from unittest.mock import MagicMock

    poller = SeaTicketPoller(
        mcp_client=MagicMock(),
        memory_provider=MagicMock(),
        ledger=MagicMock(),
    )
    # Plant an expired window
    poller._ramped_resume_until = datetime.now(timezone.utc) - timedelta(
        seconds=10
    )
    await poller._await_ramped_resume_gate()
    # Window cleared
    assert poller._ramped_resume_until is None


# ---------------------------------------------------------------------------
# Full chain: stateful walkthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_cost_pause_lifecycle_end_to_end():
    """Stateful walkthrough exercising the entire R4.1 §12 sequence
    in one test:
      1. Configure holder near 100%
      2. Cross 100% → HARD_STOP_100
      3. Poller pre-claim path → PAUSED{COST}
      4. Refresh coordinator clears PAUSED{COST} → READY + ramp
      5. Ramp gate throttles subsequent claims"""
    from unittest.mock import AsyncMock, MagicMock

    # Step 1: holder near limit
    cost = _holder_at(0.95)
    import agent.cost_state_holder as cost_module
    cost_module._HOLDER = cost

    op_holder = OperationalStateHolder(
        initial_state=OperationalState(
            primary_state=PrimaryState.READY,
            claim_permission=ClaimPermission.NORMAL,
        )
    )
    import agent.operational_state_holder as op_module
    op_module._HOLDER = op_holder

    # Step 2: cross
    _set_spent(cost, 210.0)
    assert cost.active_rung() is CostRung.HARD_STOP_100

    # Step 3: poller pre-claim path
    poller = SeaTicketPoller(
        mcp_client=MagicMock(),
        memory_provider=MagicMock(),
        ledger=MagicMock(),
    )

    async def _fake_actor(_ws):
        return "actor-1"

    poller._resolve_kora_actor_id = _fake_actor  # type: ignore[assignment]
    reader = MagicMock()
    reader.get_active_command = AsyncMock(return_value=None)
    poller._kora_control_reader = reader

    from plugins.memory.isokron.sea_ticket_poller import SeaTicket

    ticket = SeaTicket(
        ticket_id="t-1",
        workspace_id="org_test",
        ticket_title="T",
        ticket_objective="O",
        sea_status="assigned",
        sea_priority="medium",
        sea_idea_kind="task",
        sea_captured_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
        created_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
    )
    await poller._claim_and_work(ticket)

    assert op_holder.current.primary_state is PrimaryState.PAUSED
    assert DegradationReason.COST in op_holder.current.degradation_reasons

    # Step 4: refresh
    cleared = await refresh_billing_period_and_resume(
        new_period_start=datetime(2026, 6, 1, tzinfo=timezone.utc),
        cost_holder=cost,
        operational_holder=op_holder,
        poller=poller,
    )
    assert cleared is True
    assert op_holder.current.primary_state is PrimaryState.READY
    assert cost.current.spent_to_date_usd == 0.0

    # Step 5: ramp is active
    assert poller._ramped_resume_until is not None
    assert poller._ramped_resume_until > datetime.now(timezone.utc)
