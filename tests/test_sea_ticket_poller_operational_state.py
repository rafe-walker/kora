"""KR-P2-CLEANUP ST1 — operational-state wire-in tests for
``SeaTicketPoller`` (KR-P2-I-integration ST4, deferred from #34).

Covers:
  - Happy path: claim → READY → ACTIVE; release → ACTIVE → READY.
    Two transitions fire in order; trigger strings match spec.
  - Lease-lost branch: claim → READY → ACTIVE; heartbeat lost →
    ACTIVE → READY (no release).
  - Fail-soft when ``get_holder()`` returns None: no transitions
    attempted, no exception, full claim/release cycle still runs.
  - Fail-soft when ``transition_to`` raises: WARN logged, cycle
    continues, both halves of the cycle complete.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.operational_state import (
    ClaimPermission,
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
# Test scaffolding — minimal repros of the patterns from
# test_sea_ticket_poller.py. Kept here so ST1's wire-in tests can stand
# alone and the existing ST1 (KR-P2-E) tests don't have to change.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singleton():
    _reset_holder_for_tests()
    yield
    _reset_holder_for_tests()


class _FakePool:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    def queue(self, row: Any) -> None:
        self.rows.append(row)

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self_inner) -> Any:
                return _FakeConn(pool)

            async def __aexit__(self_inner, *exc: Any) -> bool:
                return False

        return _Ctx()


class _FakeConn:
    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool

    async def fetchrow(self, _sql: str, *_args: Any) -> Any:
        return self._pool.rows.pop(0) if self._pool.rows else None


class _FakeConnection:
    """See ``tests/test_sea_ticket_poller.py:_FakeConnection`` for the
    full root-cause + fix-shape note on ``_submit_async``. Same bug,
    same fix: run the coro on a fresh worker thread + loop so the
    test's caller-loop and the coro's runner-loop are independent."""

    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool

    def get_pg_pool(self) -> _FakePool:
        return self._pool

    def _submit_async(self, coro):
        fut: concurrent.futures.Future = concurrent.futures.Future()

        def _runner():
            new_loop = asyncio.new_event_loop()
            try:
                fut.set_result(new_loop.run_until_complete(coro))
            except BaseException as exc:
                fut.set_exception(exc)
            finally:
                new_loop.close()

        threading.Thread(target=_runner, daemon=True).start()
        return fut


def _make_provider(pool: _FakePool) -> Any:
    provider = MagicMock()
    provider._connection = _FakeConnection(pool)
    provider._resolve_workspace_id.return_value = "org_test"
    return provider


def _seeded_actor_row(actor_id: str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"):
    return {"actor_id": actor_id}


def _seeded_ticket_row():
    return {
        "ticket_id": "11111111-1111-1111-1111-111111111111",
        "workspace_id": "org_test",
        "ticket_title": "T",
        "ticket_objective": "O",
        "sea_status": "assigned",
        "sea_priority": "medium",
        "sea_idea_kind": "task",
        "sea_captured_at": datetime(2026, 5, 21, tzinfo=timezone.utc),
        "created_at": datetime(2026, 5, 21, tzinfo=timezone.utc),
    }


def _claim_response(
    *, fence: str = "ffffffff-ffff-ffff-ffff-ffffffffffff"
) -> dict[str, Any]:
    return {
        "result": KORA_CLAIM_RESULT_CLAIMED,
        "claim_fence_token": fence,
        "lease_expires_at": "2026-05-21T18:00:00Z",
        "claim_count": 1,
        "chain_event_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
        "work_attempt_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
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


def _build_poller(
    mcp: Any,
    *,
    invoker=None,
    pool: _FakePool | None = None,
) -> SeaTicketPoller:
    pool = pool if pool is not None else _FakePool()
    pool.queue(_seeded_actor_row())
    pool.queue(_seeded_ticket_row())
    kwargs: dict[str, Any] = {
        "mcp_client": mcp,
        "memory_provider": _make_provider(pool),
        "ledger": _FakeLedger(),
        "heartbeat_interval_seconds": 60,
    }
    if invoker is not None:
        kwargs["agent_loop_invoker"] = invoker
    return SeaTicketPoller(**kwargs)


def _capture_transitions(
    holder: OperationalStateHolder,
) -> list[tuple[str, str, str]]:
    """Register a listener that records every transition."""
    captured: list[tuple[str, str, str]] = []

    async def listener(old, new, trigger):
        captured.append((old.primary_state.value, new.primary_state.value, trigger))

    holder.add_listener(listener)
    return captured


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_cycle_fires_active_then_ready_transitions():
    holder = init_holder(
        OperationalState(
            primary_state=PrimaryState.READY,
            claim_permission=ClaimPermission.NORMAL,
        )
    )
    captured = _capture_transitions(holder)

    mcp = MagicMock()
    mcp.invoke = AsyncMock()
    mcp.invoke.side_effect = [
        _claim_response(),  # claim
        {"result": "released", "chain_event_id": None},  # release
    ]
    poller = _build_poller(mcp)

    await poller._poll_once()

    # Two transitions fired in order: ACTIVE then READY.
    assert captured == [
        ("ready", "active", "claim acquired"),
        ("active", "ready", "claim released"),
    ]
    # Holder's current state is back to READY after the cycle.
    assert get_holder().current.primary_state is PrimaryState.READY


# ---------------------------------------------------------------------------
# Lease-lost branch — ACTIVE → READY still fires (no release)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lease_lost_branch_still_transitions_back_to_ready():
    holder = init_holder(
        OperationalState(
            primary_state=PrimaryState.READY,
            claim_permission=ClaimPermission.NORMAL,
        )
    )
    captured = _capture_transitions(holder)

    mcp = MagicMock()
    # Only claim fires substrate-side; no release in the lease-lost path.
    mcp.invoke = AsyncMock(return_value=_claim_response())

    async def invoker(_t, _c, hb):
        # Simulate the heartbeat detecting lease loss mid-work.
        hb.lease_lost = True
        return SeaTicketResolution.RELEASED

    poller = _build_poller(mcp, invoker=invoker)
    await poller._poll_once()

    # Both transitions fired despite the lease-lost early exit.
    assert captured == [
        ("ready", "active", "claim acquired"),
        ("active", "ready", "claim released"),
    ]
    # Only the claim hit the substrate; no release call.
    assert mcp.invoke.await_count == 1
    assert mcp.invoke.await_args.args[0] == "kora__claim_sea_ticket"
    assert get_holder().current.primary_state is PrimaryState.READY


# ---------------------------------------------------------------------------
# Fail-soft — holder not initialized
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_holder_not_initialized_no_exception_cycle_still_runs(caplog):
    """The gateway poller may start before any agent session has run
    ``wire_operational_state``. In that window, get_holder() returns
    None and the consumer loop must still claim+release cleanly."""
    # No init_holder call — singleton stays None.
    assert get_holder() is None

    mcp = MagicMock()
    mcp.invoke = AsyncMock()
    mcp.invoke.side_effect = [
        _claim_response(),
        {"result": "released", "chain_event_id": None},
    ]
    poller = _build_poller(mcp)

    with caplog.at_level(logging.DEBUG, logger="plugins.memory.isokron.sea_ticket_poller"):
        await poller._poll_once()

    # Full claim + release cycle still ran.
    assert [c.args[0] for c in mcp.invoke.call_args_list] == [
        "kora__claim_sea_ticket",
        "kora__release_claim",
    ]
    # DEBUG log lines acknowledge the skip.
    debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("holder not initialized" in m for m in debug_messages)


# ---------------------------------------------------------------------------
# Fail-soft — holder.transition_to raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transition_to_raise_does_not_break_cycle(caplog):
    """If the holder is initialized but transition_to raises (e.g. a
    listener exception slips past the holder's own catch), the poller
    must keep going."""
    holder = init_holder(
        OperationalState(
            primary_state=PrimaryState.READY,
            claim_permission=ClaimPermission.NORMAL,
        )
    )

    # Replace transition_to with one that raises.
    async def boom(*_a, **_kw):
        raise RuntimeError("holder boom")

    holder.transition_to = boom  # type: ignore[assignment]

    mcp = MagicMock()
    mcp.invoke = AsyncMock()
    mcp.invoke.side_effect = [
        _claim_response(),
        {"result": "released", "chain_event_id": None},
    ]
    poller = _build_poller(mcp)

    with caplog.at_level(logging.WARNING, logger="plugins.memory.isokron.sea_ticket_poller"):
        await poller._poll_once()

    # Both substrate calls still fired.
    assert mcp.invoke.await_count == 2
    # WARNING line per failed transition (two attempts: ACTIVE + READY).
    warns = [
        r.message
        for r in caplog.records
        if r.levelno == logging.WARNING
        and "operational-state transition" in r.message
    ]
    assert len(warns) == 2
