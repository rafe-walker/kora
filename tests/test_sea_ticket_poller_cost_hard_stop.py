"""KR-P2-K ST4 — cost-ladder HARD_STOP_100 safe-release + PAUSED{COST}
transition tests for :class:`SeaTicketPoller`.

Covers:

  - Pre-claim hard-stop: when the cost holder reports HARD_STOP_100
    BEFORE the claim attempt, the poller transitions operational-state
    to PAUSED with reason=COST and does NOT call
    ``kora__claim_sea_ticket`` (no claim ever held).
  - Post-loop hard-stop: when the rung crosses to HARD_STOP_100
    DURING agent-loop execution, the poller safe-releases the held
    claim with resolution=``deferred_cost_limit``, emits the
    ``kora.sea_ticket.resolved`` chain event with that resolution,
    AND transitions to PAUSED{COST} (not READY).
  - Post-loop hard-stop + lease-lost: when the lease is lost AND
    the rung is hard-stopped, we transition to PAUSED{COST}
    (no claim to release; substrate lease already gone).
  - Normal-rung path is unchanged (regression guard for KR-P2-CLEANUP
    ST1's READY ↔ ACTIVE transitions).
  - Cost-holder uninitialized: fail-soft — no hard-stop check fires,
    happy path proceeds.
  - Cost-holder ``active_rung`` raising: fail-soft — treated as
    not-hard-stop, happy path proceeds.

These tests call ``_claim_and_work`` directly with a pre-built
``SeaTicket`` to bypass the broken ``_submit_async`` event-loop
pattern in the existing fixtures (the ``_fetch_next_ticket`` async
pool path is flaky under xdist). The hard-stop logic under test
runs entirely inside ``_claim_and_work``, so this is sufficient.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.cost_state_holder import (
    CostStateHolder,
    _reset_cost_holder_for_tests,
    init_cost_holder,
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
    get_holder,
    init_holder,
)
from plugins.memory.isokron.kora_operation_ledger import KoraOperationRow
from plugins.memory.isokron.sea_ticket_poller import (
    KORA_CLAIM_RESULT_CLAIMED,
    SeaTicket,
    SeaTicketPoller,
    SeaTicketResolution,
)


# ---------------------------------------------------------------------------
# Fixtures — reset both singletons between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singletons():
    _reset_holder_for_tests()
    _reset_cost_holder_for_tests()
    yield
    _reset_holder_for_tests()
    _reset_cost_holder_for_tests()


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


_ACTOR_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_TICKET_ID = "11111111-1111-1111-1111-111111111111"
_FENCE_TOKEN = "ffffffff-ffff-ffff-ffff-ffffffffffff"
_WORK_ATTEMPT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _build_ticket() -> SeaTicket:
    return SeaTicket(
        ticket_id=_TICKET_ID,
        workspace_id="org_test",
        ticket_title="T",
        ticket_objective="O",
        sea_status="assigned",
        sea_priority="medium",
        sea_idea_kind="task",
        sea_captured_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
        created_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
    )


def _claim_response_payload() -> dict[str, Any]:
    return {
        "result": KORA_CLAIM_RESULT_CLAIMED,
        "claim_fence_token": _FENCE_TOKEN,
        "lease_expires_at": "2026-05-21T18:00:00Z",
        "claim_count": 1,
        "chain_event_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
        "work_attempt_id": _WORK_ATTEMPT_ID,
    }


class _FakeLedger:
    async def allocate_operation(self, **kwargs: Any) -> KoraOperationRow:
        return KoraOperationRow(
            work_attempt_id=kwargs["work_attempt_id"],
            sequence_within_attempt=0,
            kora_operation_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
            workspace_id=kwargs["workspace_id"],
            ticket_id=kwargs["ticket_id"],
            status="allocated",
            tool_name=kwargs["tool_name"],
            dispatch_result=None,
            dispatch_error=None,
            created_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
            updated_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
        )


def _make_poller(
    mcp: Any,
    *,
    invoker=None,
    actor_id: str = _ACTOR_ID,
    control_command=None,
) -> SeaTicketPoller:
    """Build a poller with the broken async-pool paths stubbed out.

    Replaces ``_resolve_kora_actor_id`` so we don't hit the flaky
    ``_submit_async``-based fixture; replaces the kora_control_reader
    with a mock that returns no STOP-KORA by default.
    """
    kwargs: dict[str, Any] = {
        "mcp_client": mcp,
        "memory_provider": MagicMock(),
        "ledger": _FakeLedger(),
        "heartbeat_interval_seconds": 60,
    }
    if invoker is not None:
        kwargs["agent_loop_invoker"] = invoker
    poller = SeaTicketPoller(**kwargs)

    async def _fake_resolve(_workspace_id: str) -> Optional[str]:
        return actor_id

    poller._resolve_kora_actor_id = _fake_resolve  # type: ignore[assignment]

    reader = MagicMock()
    reader.get_active_command = AsyncMock(return_value=control_command)
    poller._kora_control_reader = reader  # type: ignore[assignment]

    return poller


def _init_ready_holder() -> OperationalStateHolder:
    return init_holder(
        OperationalState(
            primary_state=PrimaryState.READY,
            claim_permission=ClaimPermission.NORMAL,
        )
    )


def _capture_transitions(
    holder: OperationalStateHolder,
) -> list[tuple[str, str, str, frozenset]]:
    """Listener-record of (from, to, trigger, new_reasons)."""
    captured: list[tuple[str, str, str, frozenset]] = []

    async def listener(old, new, trigger):
        captured.append(
            (
                old.primary_state.value,
                new.primary_state.value,
                trigger,
                frozenset(r.value for r in new.degradation_reasons),
            )
        )

    holder.add_listener(listener)
    return captured


def _init_hard_stop_cost_holder() -> CostStateHolder:
    """Create a cost holder whose ``active_rung`` is HARD_STOP_100."""
    cost_holder = init_cost_holder(
        billing_period_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        credit_pool_usd=200.00,
    )
    # Bump spent past the pool so active_rung == HARD_STOP_100. We
    # set this via the holder's internal state (writing directly via
    # dataclasses.replace) — both the public surface
    # (record_inference / reconcile) require a normalized usage that
    # would resolve to a real $ amount via usage_pricing. Bypassing
    # that here keeps the test focused on the rung-driven branch.
    import dataclasses

    cost_holder._state = dataclasses.replace(  # type: ignore[attr-defined]
        cost_holder.current,
        spent_to_date_usd=210.00,
    )
    return cost_holder


# ---------------------------------------------------------------------------
# Pre-claim hard-stop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_claim_hard_stop_skips_claim_and_pauses_with_cost_reason():
    """When HARD_STOP_100 is active BEFORE the claim, the poller
    transitions PAUSED{COST} and never calls kora__claim_sea_ticket."""
    holder = _init_ready_holder()
    _init_hard_stop_cost_holder()
    captured = _capture_transitions(holder)

    mcp = MagicMock()
    mcp.invoke = AsyncMock()  # should never be called
    poller = _make_poller(mcp)

    await poller._claim_and_work(_build_ticket())

    # No substrate-side claim attempted.
    assert mcp.invoke.await_count == 0

    # Single transition: READY → PAUSED with {COST}.
    assert len(captured) == 1
    from_, to_, trigger, reasons = captured[0]
    assert from_ == "ready"
    assert to_ == "paused"
    assert "HARD_STOP_100" in trigger
    assert "pre-claim" in trigger
    assert reasons == frozenset({"cost"})

    assert get_holder().current.primary_state is PrimaryState.PAUSED
    assert DegradationReason.COST in get_holder().current.degradation_reasons


# ---------------------------------------------------------------------------
# Post-loop hard-stop — claim held, safe-release with deferred_cost_limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_loop_hard_stop_safe_releases_with_deferred_cost_limit():
    """When the rung crosses to HARD_STOP_100 DURING agent-loop work,
    the poller safe-releases the claim with the deferred_cost_limit
    resolution AND transitions to PAUSED{COST} (not READY)."""
    holder = _init_ready_holder()
    captured = _capture_transitions(holder)

    # Cost holder starts at NORMAL; rung flips to HARD_STOP after the
    # claim is acquired but before the post-loop check.
    cost_holder = init_cost_holder(
        billing_period_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        credit_pool_usd=200.00,
    )

    async def invoker(_t, _c, _hb) -> SeaTicketResolution:
        # Mid-flight: rung crosses to hard-stop.
        import dataclasses

        cost_holder._state = dataclasses.replace(  # type: ignore[attr-defined]
            cost_holder.current, spent_to_date_usd=210.00
        )
        return SeaTicketResolution.COMPLETED

    mcp = MagicMock()
    mcp.invoke = AsyncMock(
        side_effect=[
            _claim_response_payload(),  # kora__claim_sea_ticket
            {"result": "released"},  # kora__release_claim
        ]
    )
    poller = _make_poller(mcp, invoker=invoker)

    emit_mock = AsyncMock(return_value="evt-1")
    with patch(
        "plugins.memory.isokron.sea_ticket_resolution.emit_sea_ticket_resolved",
        emit_mock,
    ):
        await poller._claim_and_work(_build_ticket())

    # Claim + release both fired.
    assert [c.args[0] for c in mcp.invoke.call_args_list] == [
        "kora__claim_sea_ticket",
        "kora__release_claim",
    ]

    # emit_sea_ticket_resolved called with resolution=DEFERRED_COST_LIMIT
    assert emit_mock.await_count == 1
    emit_kwargs = emit_mock.await_args.kwargs
    assert emit_kwargs["resolution"] is SeaTicketResolution.DEFERRED_COST_LIMIT

    # Two transitions: ACTIVE on claim, then PAUSED{COST} after release.
    assert len(captured) == 2
    assert captured[0][:3] == ("ready", "active", "claim acquired")
    from_, to_, trigger, reasons = captured[1]
    assert from_ == "active"
    assert to_ == "paused"
    assert "HARD_STOP_100" in trigger
    assert "post-loop" in trigger
    assert reasons == frozenset({"cost"})


# ---------------------------------------------------------------------------
# Post-loop hard-stop + lease lost — pause without release
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lease_lost_with_hard_stop_transitions_to_paused_cost_no_release():
    """If the lease is lost mid-work AND the rung is hard-stopped,
    the poller skips the release (lease already substrate-side
    expired) but still transitions to PAUSED{COST}."""
    holder = _init_ready_holder()
    captured = _capture_transitions(holder)

    cost_holder = init_cost_holder(
        billing_period_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        credit_pool_usd=200.00,
    )

    async def invoker(_t, _c, hb) -> SeaTicketResolution:
        # Lease lost mid-work — heartbeat signals it.
        hb.lease_lost = True
        # Rung also hard-stopped.
        import dataclasses

        cost_holder._state = dataclasses.replace(  # type: ignore[attr-defined]
            cost_holder.current, spent_to_date_usd=210.00
        )
        return SeaTicketResolution.RELEASED

    mcp = MagicMock()
    # Only the claim hits the substrate; no release in the lease-lost
    # path even when hard-stopped (lease is gone).
    mcp.invoke = AsyncMock(return_value=_claim_response_payload())
    poller = _make_poller(mcp, invoker=invoker)

    await poller._claim_and_work(_build_ticket())

    assert mcp.invoke.await_count == 1
    assert mcp.invoke.await_args.args[0] == "kora__claim_sea_ticket"

    # Two transitions: ACTIVE then PAUSED{COST}.
    assert len(captured) == 2
    assert captured[0][:3] == ("ready", "active", "claim acquired")
    from_, to_, trigger, reasons = captured[1]
    assert from_ == "active"
    assert to_ == "paused"
    assert "HARD_STOP_100" in trigger
    assert "lease lost" in trigger
    assert reasons == frozenset({"cost"})


# ---------------------------------------------------------------------------
# Normal rung — existing flow unchanged (regression guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_rung_does_not_pause_or_override_resolution():
    """Regression guard: at NORMAL rung the existing KR-P2-CLEANUP ST1
    flow (ACTIVE → READY, no resolution override) is unchanged."""
    holder = _init_ready_holder()
    captured = _capture_transitions(holder)

    # Cost holder initialized but well below 100% — rung = NORMAL.
    init_cost_holder(
        billing_period_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        credit_pool_usd=200.00,
    )

    async def invoker(_t, _c, _hb) -> SeaTicketResolution:
        return SeaTicketResolution.COMPLETED

    mcp = MagicMock()
    mcp.invoke = AsyncMock(
        side_effect=[_claim_response_payload(), {"result": "released"}]
    )
    poller = _make_poller(mcp, invoker=invoker)

    emit_mock = AsyncMock(return_value="evt-1")
    with patch(
        "plugins.memory.isokron.sea_ticket_resolution.emit_sea_ticket_resolved",
        emit_mock,
    ):
        await poller._claim_and_work(_build_ticket())

    # Original resolution preserved (no DEFERRED_COST_LIMIT override).
    assert emit_mock.await_args.kwargs["resolution"] is (
        SeaTicketResolution.COMPLETED
    )

    # Existing READY → ACTIVE → READY transitions.
    assert [c[:3] for c in captured] == [
        ("ready", "active", "claim acquired"),
        ("active", "ready", "claim released"),
    ]
    # No degradation reasons set anywhere.
    assert all(c[3] == frozenset() for c in captured)


# ---------------------------------------------------------------------------
# Fail-soft — cost holder uninitialized
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_holder_uninitialized_does_not_block_normal_flow():
    """The gateway may start the poller before the cost holder is
    initialized (e.g. agent session hasn't booted yet). The hard-stop
    check must fail-soft (treated as not-hard-stop), so the cycle
    completes normally."""
    holder = _init_ready_holder()
    captured = _capture_transitions(holder)

    # Cost holder NOT initialized.

    async def invoker(_t, _c, _hb) -> SeaTicketResolution:
        return SeaTicketResolution.COMPLETED

    mcp = MagicMock()
    mcp.invoke = AsyncMock(
        side_effect=[_claim_response_payload(), {"result": "released"}]
    )
    poller = _make_poller(mcp, invoker=invoker)

    emit_mock = AsyncMock(return_value="evt-1")
    with patch(
        "plugins.memory.isokron.sea_ticket_resolution.emit_sea_ticket_resolved",
        emit_mock,
    ):
        await poller._claim_and_work(_build_ticket())

    # Normal release path; no PAUSE.
    assert [c[:3] for c in captured] == [
        ("ready", "active", "claim acquired"),
        ("active", "ready", "claim released"),
    ]
    assert emit_mock.await_args.kwargs["resolution"] is (
        SeaTicketResolution.COMPLETED
    )


# ---------------------------------------------------------------------------
# Fail-soft — active_rung raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_rung_exception_treated_as_not_hard_stop(caplog):
    """If ``cost_holder.active_rung`` raises (e.g. holder state
    corruption), the consumer treats it as not-hard-stop and logs
    DEBUG. The cycle proceeds normally."""
    holder = _init_ready_holder()
    captured = _capture_transitions(holder)

    cost_holder = init_cost_holder(
        billing_period_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        credit_pool_usd=200.00,
    )

    def _boom() -> Any:
        raise RuntimeError("rung lookup boom")

    cost_holder.active_rung = _boom  # type: ignore[assignment]

    async def invoker(_t, _c, _hb) -> SeaTicketResolution:
        return SeaTicketResolution.COMPLETED

    mcp = MagicMock()
    mcp.invoke = AsyncMock(
        side_effect=[_claim_response_payload(), {"result": "released"}]
    )
    poller = _make_poller(mcp, invoker=invoker)

    emit_mock = AsyncMock(return_value="evt-1")
    with caplog.at_level(
        logging.DEBUG, logger="plugins.memory.isokron.sea_ticket_poller"
    ):
        with patch(
            "plugins.memory.isokron.sea_ticket_resolution"
            ".emit_sea_ticket_resolved",
            emit_mock,
        ):
            await poller._claim_and_work(_build_ticket())

    # Cycle ran normally — no PAUSE.
    assert [c[:3] for c in captured] == [
        ("ready", "active", "claim acquired"),
        ("active", "ready", "claim released"),
    ]
    # DEBUG line acknowledging the swallowed exception.
    debug_messages = [
        r.message for r in caplog.records if r.levelno == logging.DEBUG
    ]
    assert any("cost-rung check raised" in m for m in debug_messages)


# ---------------------------------------------------------------------------
# DEFERRED_COST_LIMIT enum value is wire-format-stable
# ---------------------------------------------------------------------------


def test_deferred_cost_limit_resolution_value():
    """Cockpit consumers index on the string value; pin it."""
    assert SeaTicketResolution.DEFERRED_COST_LIMIT.value == "deferred_cost_limit"
