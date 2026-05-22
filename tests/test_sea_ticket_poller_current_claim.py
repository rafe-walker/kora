"""KR-P2-L ST1 — SeaTicketPoller ``current_claim`` lifecycle tests.

Pins that the poller's ``current_claim`` attribute is set on
successful claim allocation and cleared on every exit path
(release, lease-lost, hard-stop pre-claim, hard-stop post-loop).
This is the read source for the ``claim_state`` health subsignal.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.cost_state_holder import (
    _reset_cost_holder_for_tests,
    init_cost_holder,
)
from agent.operational_state import (
    ClaimPermission,
    OperationalState,
    PrimaryState,
)
from agent.operational_state_holder import (
    _reset_holder_for_tests,
    init_holder,
)
from plugins.memory.isokron.kora_operation_ledger import KoraOperationRow
from plugins.memory.isokron.sea_ticket_poller import (
    KORA_CLAIM_RESULT_CLAIMED,
    ActiveClaim,
    SeaTicket,
    SeaTicketPoller,
    SeaTicketResolution,
)


@pytest.fixture(autouse=True)
def _reset():
    _reset_holder_for_tests()
    _reset_cost_holder_for_tests()
    yield
    _reset_holder_for_tests()
    _reset_cost_holder_for_tests()


_TICKET_ID = "11111111-1111-1111-1111-111111111111"
_WORK_ATTEMPT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_FENCE = "ffffffff-ffff-ffff-ffff-ffffffffffff"
_ACTOR_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


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


def _claim_response() -> dict[str, Any]:
    return {
        "result": KORA_CLAIM_RESULT_CLAIMED,
        "claim_fence_token": _FENCE,
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


def _make_poller(mcp: Any, *, invoker=None) -> SeaTicketPoller:
    kwargs: dict[str, Any] = {
        "mcp_client": mcp,
        "memory_provider": MagicMock(),
        "ledger": _FakeLedger(),
    }
    if invoker is not None:
        kwargs["agent_loop_invoker"] = invoker
    poller = SeaTicketPoller(**kwargs)

    async def _fake_resolve(_workspace_id: str) -> Optional[str]:
        return _ACTOR_ID

    poller._resolve_kora_actor_id = _fake_resolve  # type: ignore[assignment]

    reader = MagicMock()
    reader.get_active_command = AsyncMock(return_value=None)
    poller._kora_control_reader = reader  # type: ignore[assignment]

    return poller


def _init_ready_holder() -> None:
    init_holder(
        OperationalState(
            primary_state=PrimaryState.READY,
            claim_permission=ClaimPermission.NORMAL,
        )
    )


# ---------------------------------------------------------------------------
# current_claim default + set/clear lifecycle
# ---------------------------------------------------------------------------


def test_current_claim_is_none_at_construction():
    mcp = MagicMock()
    poller = _make_poller(mcp)
    assert poller.current_claim is None


@pytest.mark.asyncio
async def test_current_claim_set_during_work_and_cleared_after_release():
    _init_ready_holder()

    captured_during: list[Optional[ActiveClaim]] = []

    async def invoker(_t, _c, _hb) -> SeaTicketResolution:
        # Snapshot the current claim while the agent loop is running.
        captured_during.append(poller.current_claim)
        return SeaTicketResolution.COMPLETED

    mcp = MagicMock()
    mcp.invoke = AsyncMock(
        side_effect=[_claim_response(), {"result": "released"}]
    )
    poller = _make_poller(mcp, invoker=invoker)

    emit_mock = AsyncMock(return_value="evt-1")
    with patch(
        "plugins.memory.isokron.sea_ticket_resolution.emit_sea_ticket_resolved",
        emit_mock,
    ):
        await poller._claim_and_work(_build_ticket())

    # During the agent loop, current_claim was populated with the
    # work_attempt_id from the substrate response.
    assert len(captured_during) == 1
    claim_during = captured_during[0]
    assert claim_during is not None
    assert claim_during.ticket_id == _TICKET_ID
    assert claim_during.work_attempt_id == _WORK_ATTEMPT_ID
    assert claim_during.claim_fence_token == _FENCE

    # After release, current_claim cleared back to None.
    assert poller.current_claim is None


@pytest.mark.asyncio
async def test_current_claim_cleared_after_lease_lost():
    _init_ready_holder()

    async def invoker(_t, _c, hb) -> SeaTicketResolution:
        hb.lease_lost = True
        return SeaTicketResolution.RELEASED

    mcp = MagicMock()
    mcp.invoke = AsyncMock(return_value=_claim_response())
    poller = _make_poller(mcp, invoker=invoker)

    await poller._claim_and_work(_build_ticket())
    assert poller.current_claim is None


@pytest.mark.asyncio
async def test_current_claim_cleared_after_agent_loop_exception():
    _init_ready_holder()

    async def invoker(_t, _c, _hb) -> SeaTicketResolution:
        raise RuntimeError("agent boom")

    mcp = MagicMock()
    mcp.invoke = AsyncMock(
        side_effect=[_claim_response(), {"result": "released"}]
    )
    poller = _make_poller(mcp, invoker=invoker)

    emit_mock = AsyncMock(return_value="evt-1")
    with patch(
        "plugins.memory.isokron.sea_ticket_resolution.emit_sea_ticket_resolved",
        emit_mock,
    ):
        await poller._claim_and_work(_build_ticket())

    # Even when the agent loop raises, the try/finally clears the claim.
    assert poller.current_claim is None


@pytest.mark.asyncio
async def test_current_claim_remains_none_when_pre_claim_hard_stop_skips():
    """Pre-claim hard-stop path returns before any claim is set;
    current_claim stays None throughout."""
    _init_ready_holder()

    # Force HARD_STOP_100 rung
    import dataclasses

    cost_holder = init_cost_holder(
        billing_period_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        credit_pool_usd=200.00,
    )
    cost_holder._state = dataclasses.replace(  # type: ignore[attr-defined]
        cost_holder.current, spent_to_date_usd=210.00
    )

    mcp = MagicMock()
    mcp.invoke = AsyncMock()
    poller = _make_poller(mcp)

    await poller._claim_and_work(_build_ticket())

    assert poller.current_claim is None
    # mcp.invoke was never called (no claim attempted)
    assert mcp.invoke.await_count == 0


@pytest.mark.asyncio
async def test_current_claim_cleared_after_post_loop_hard_stop_safe_release():
    """If the rung crosses to HARD_STOP_100 mid-work, the claim is
    safe-released and current_claim is cleared."""
    _init_ready_holder()

    cost_holder = init_cost_holder(
        billing_period_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        credit_pool_usd=200.00,
    )

    async def invoker(_t, _c, _hb) -> SeaTicketResolution:
        # Mid-flight: rung flips
        import dataclasses

        cost_holder._state = dataclasses.replace(  # type: ignore[attr-defined]
            cost_holder.current, spent_to_date_usd=210.00
        )
        return SeaTicketResolution.COMPLETED

    mcp = MagicMock()
    mcp.invoke = AsyncMock(
        side_effect=[_claim_response(), {"result": "released"}]
    )
    poller = _make_poller(mcp, invoker=invoker)

    emit_mock = AsyncMock(return_value="evt-1")
    with patch(
        "plugins.memory.isokron.sea_ticket_resolution.emit_sea_ticket_resolved",
        emit_mock,
    ):
        await poller._claim_and_work(_build_ticket())

    assert poller.current_claim is None
