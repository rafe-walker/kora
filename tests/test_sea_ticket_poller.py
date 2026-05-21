"""Unit tests for ``plugins/memory/isokron/sea_ticket_poller.py`` (KR-P2-E ST1).

Covers:
  - Construction defaults + injected KoraControlReader override
  - run_forever respects stop() and sleeps when no work is available
  - _poll_once returns 0 when get_next_available_sea_ticket has no row
  - _poll_once claims + invokes the agent loop + releases on the happy path
  - Claim non-CLAIMED result (e.g. already_claimed) → no agent loop, no release
  - Claim returns NULL fence_token / work_attempt_id → refuse to proceed
  - Agent-loop invoker raises → still releases with claim_fence_token
  - Pre-claim STOP-KORA L1+ blocks the claim entirely
  - actor_id resolution is cached after the first hit
  - Release error is swallowed (doesn't kill the poll loop)
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from types import SimpleNamespace

from plugins.memory.isokron.kora_control_reader import KoraControlReader
from plugins.memory.isokron.sea_ticket_poller import (
    KORA_CLAIM_RESULT_ALREADY_CLAIMED,
    KORA_CLAIM_RESULT_CLAIMED,
    SeaTicket,
    SeaTicketPoller,
    SeaTicketResolution,
)


# ---------------------------------------------------------------------------
# Test scaffolding — fake mcp_client, fake memory_provider, fake pool
# ---------------------------------------------------------------------------


class _FakePool:
    """Minimal asyncpg.Pool stand-in. Returns pre-seeded rows from fetchrow."""

    def __init__(self) -> None:
        self.rows: list[Any] = []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

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

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self._pool.calls.append((sql, args))
        return self._pool.rows.pop(0) if self._pool.rows else None


class _FakeConnection:
    """IsoKronConnection stub. _submit_async runs the coro on the
    current loop and wraps the result in a concurrent.futures.Future."""

    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool

    def get_pg_pool(self) -> _FakePool:
        return self._pool

    def _submit_async(self, coro):
        result = asyncio.get_event_loop().run_until_complete(coro)
        fut: concurrent.futures.Future = concurrent.futures.Future()
        fut.set_result(result)
        return fut


def _make_memory_provider(
    pool: _FakePool, workspace_id: str = "org_test"
) -> Any:
    provider = MagicMock()
    provider._connection = _FakeConnection(pool)
    provider._resolve_workspace_id.return_value = workspace_id
    return provider


def _seeded_ticket_row(**overrides: Any) -> dict[str, Any]:
    row = {
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
    row.update(overrides)
    return row


def _seeded_actor_row(
    actor_id: str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
) -> dict[str, Any]:
    return {"actor_id": actor_id}


def _claim_response(
    *,
    result: str = KORA_CLAIM_RESULT_CLAIMED,
    fence: str = "fffffffff-ffff-ffff-ffff-fffffffffffff",
    work_attempt: str = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
) -> dict[str, Any]:
    return {
        "result": result,
        "claim_fence_token": fence if result == KORA_CLAIM_RESULT_CLAIMED else None,
        "lease_expires_at": "2026-05-21T18:00:00Z"
        if result == KORA_CLAIM_RESULT_CLAIMED
        else None,
        "claim_count": 1 if result == KORA_CLAIM_RESULT_CLAIMED else None,
        "chain_event_id": "cccccccc-cccc-cccc-cccc-cccccccccccc"
        if result == KORA_CLAIM_RESULT_CLAIMED
        else None,
        "work_attempt_id": work_attempt
        if result == KORA_CLAIM_RESULT_CLAIMED
        else None,
    }


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_constructor_defaults_kora_control_reader_to_none_for_lazy_build():
    """KR-P2-J ST1's KoraControlReader requires kora_actor_id at
    construction. The poller resolves actor_id lazily per workspace,
    so the reader is lazy-built on first ``_claim_and_work`` rather
    than eagerly in ``__init__``."""
    poller = SeaTicketPoller(
        mcp_client=MagicMock(),
        memory_provider=MagicMock(),
    )
    assert poller._kora_control_reader is None


def test_constructor_accepts_custom_kora_control_reader():
    """Tests + multi-actor wiring pass a pre-built reader; the poller
    uses it as-is (no lazy build)."""
    custom = KoraControlReader(
        memory_provider=MagicMock(),
        kora_actor_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )
    poller = SeaTicketPoller(
        mcp_client=MagicMock(),
        memory_provider=MagicMock(),
        kora_control_reader=custom,
    )
    assert poller._kora_control_reader is custom


# ---------------------------------------------------------------------------
# _poll_once — empty queue + happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_once_returns_zero_when_no_ticket_available():
    pool = _FakePool()
    pool.queue(_seeded_actor_row())  # actor_id resolution
    # No ticket row queued — get_next_available_sea_ticket returns None.
    poller = SeaTicketPoller(
        mcp_client=MagicMock(),
        memory_provider=_make_memory_provider(pool),
    )
    assert await poller._poll_once() == 0


@pytest.mark.asyncio
async def test_poll_once_full_happy_path_claims_works_releases():
    pool = _FakePool()
    pool.queue(_seeded_actor_row())  # actor resolution
    pool.queue(_seeded_ticket_row())  # ticket fetch

    mcp = MagicMock()
    mcp.invoke = AsyncMock()
    mcp.invoke.side_effect = [
        _claim_response(),  # kora__claim_sea_ticket
        {"result": "released", "chain_event_id": None},  # kora__release_claim
    ]

    invoked: list[tuple[SeaTicket, Any]] = []

    async def invoker(
        t: SeaTicket, claim: Any, _hb: Any
    ) -> SeaTicketResolution:
        invoked.append((t, claim))
        return SeaTicketResolution.COMPLETED

    poller = SeaTicketPoller(
        mcp_client=mcp,
        memory_provider=_make_memory_provider(pool),
        agent_loop_invoker=invoker,
    )

    assert await poller._poll_once() == 1

    # Two MCP calls fired in order.
    assert [c.args[0] for c in mcp.invoke.call_args_list] == [
        "kora__claim_sea_ticket",
        "kora__release_claim",
    ]
    # Claim arg sanity.
    claim_args = mcp.invoke.call_args_list[0].args[1]
    assert claim_args["workspace_id"] == "org_test"
    assert claim_args["sea_ticket_id"] == "11111111-1111-1111-1111-111111111111"
    assert claim_args["lease_duration_seconds"] == 600
    assert "kora_operation_id" in claim_args
    # Release arg shape — no `resolution` field per the actual SECDEF
    # schema. Resolution rides a separate chain event (ST4).
    release_args = mcp.invoke.call_args_list[1].args[1]
    assert "resolution" not in release_args
    assert release_args["claim_fence_token"] == _claim_response()["claim_fence_token"]
    # Agent loop was invoked exactly once with the ticket + claim state.
    assert len(invoked) == 1
    invoked_ticket, invoked_claim = invoked[0]
    assert invoked_ticket.ticket_id == "11111111-1111-1111-1111-111111111111"
    assert invoked_claim.work_attempt_id == _claim_response()["work_attempt_id"]


# ---------------------------------------------------------------------------
# Claim rejections + degenerate substrate responses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_already_claimed_response_skips_agent_loop_and_release():
    pool = _FakePool()
    pool.queue(_seeded_actor_row())
    pool.queue(_seeded_ticket_row())

    mcp = MagicMock()
    mcp.invoke = AsyncMock(
        return_value=_claim_response(result=KORA_CLAIM_RESULT_ALREADY_CLAIMED)
    )

    invoked: list = []

    async def invoker(
        _t: SeaTicket, _c: Any, _hb: Any
    ) -> SeaTicketResolution:
        invoked.append(1)
        return SeaTicketResolution.COMPLETED

    poller = SeaTicketPoller(
        mcp_client=mcp,
        memory_provider=_make_memory_provider(pool),
        agent_loop_invoker=invoker,
    )
    assert await poller._poll_once() == 1

    # Only claim fired; no release because we never held the lease.
    assert mcp.invoke.await_count == 1
    assert invoked == []


@pytest.mark.asyncio
async def test_claim_returns_null_fence_token_refuses_to_proceed():
    pool = _FakePool()
    pool.queue(_seeded_actor_row())
    pool.queue(_seeded_ticket_row())

    mcp = MagicMock()
    bad_payload = _claim_response()
    bad_payload["claim_fence_token"] = None
    mcp.invoke = AsyncMock(return_value=bad_payload)

    invoked: list = []

    async def invoker(_t, _c, _hb):
        invoked.append(1)
        return SeaTicketResolution.COMPLETED

    poller = SeaTicketPoller(
        mcp_client=mcp,
        memory_provider=_make_memory_provider(pool),
        agent_loop_invoker=invoker,
    )
    await poller._poll_once()

    assert mcp.invoke.await_count == 1  # claim only
    assert invoked == []


@pytest.mark.asyncio
async def test_agent_loop_exception_still_releases_claim():
    pool = _FakePool()
    pool.queue(_seeded_actor_row())
    pool.queue(_seeded_ticket_row())

    mcp = MagicMock()
    mcp.invoke = AsyncMock()
    mcp.invoke.side_effect = [
        _claim_response(),
        {"result": "released", "chain_event_id": None},
    ]

    async def invoker(_t, _c, _hb):
        raise RuntimeError("agent loop boom")

    poller = SeaTicketPoller(
        mcp_client=mcp,
        memory_provider=_make_memory_provider(pool),
        agent_loop_invoker=invoker,
    )
    await poller._poll_once()

    # claim + release both fired despite the invoker raising.
    assert [c.args[0] for c in mcp.invoke.call_args_list] == [
        "kora__claim_sea_ticket",
        "kora__release_claim",
    ]


@pytest.mark.asyncio
async def test_heartbeat_lease_lost_skips_release():
    """ST3: if the heartbeat detected substrate-side lease loss while
    the agent loop was running, the poller must NOT call release —
    the fence_token is already invalid and substrate's claim_expired
    is the durable record."""
    pool = _FakePool()
    pool.queue(_seeded_actor_row())
    pool.queue(_seeded_ticket_row())

    mcp = MagicMock()
    mcp.invoke = AsyncMock(return_value=_claim_response())  # only claim should fire

    # Invoker that signals lease loss via the handle. The agent loop
    # would have detected loss via `handle.lease_lost` and aborted; we
    # simulate that by setting it directly on the handle the invoker
    # receives.
    async def invoker(_t, _c, hb):
        hb.lease_lost = True
        return SeaTicketResolution.RELEASED

    poller = SeaTicketPoller(
        mcp_client=mcp,
        memory_provider=_make_memory_provider(pool),
        agent_loop_invoker=invoker,
        heartbeat_interval_seconds=60,  # never fires in this test window
    )
    await poller._poll_once()

    # Only the claim was invoked — no release.
    assert mcp.invoke.await_count == 1
    assert mcp.invoke.await_args.args[0] == "kora__claim_sea_ticket"


@pytest.mark.asyncio
async def test_release_error_is_swallowed():
    pool = _FakePool()
    pool.queue(_seeded_actor_row())
    pool.queue(_seeded_ticket_row())

    mcp = MagicMock()
    mcp.invoke = AsyncMock()
    mcp.invoke.side_effect = [
        _claim_response(),
        RuntimeError("network blip"),
    ]

    poller = SeaTicketPoller(
        mcp_client=mcp,
        memory_provider=_make_memory_provider(pool),
    )
    # Must not raise — release errors live the lease expire path.
    await poller._poll_once()


# ---------------------------------------------------------------------------
# STOP-KORA pre-claim gate
# ---------------------------------------------------------------------------


class _StopKoraReader:
    """Duck-typed test double — surfaces an active L1 command.

    Deliberately does NOT subclass the real
    :class:`KoraControlReader` from KR-P2-J ST1: the real reader's
    constructor requires (memory_provider, kora_actor_id) + actor_registry
    plumbing we don't have here; the real
    :class:`KoraControlCommand` is a frozen dataclass with 17 fields,
    way more than the poller reads (only ``.level`` and ``.reason``).
    A :class:`types.SimpleNamespace` covers the poller's read surface
    fine.
    """

    async def get_active_command(self, _actor_id=None):
        return SimpleNamespace(level=1, reason="operator stop intake")


@pytest.mark.asyncio
async def test_stop_kora_level_1_blocks_claim():
    pool = _FakePool()
    pool.queue(_seeded_actor_row())
    pool.queue(_seeded_ticket_row())

    mcp = MagicMock()
    mcp.invoke = AsyncMock()

    poller = SeaTicketPoller(
        mcp_client=mcp,
        memory_provider=_make_memory_provider(pool),
        kora_control_reader=_StopKoraReader(),
    )
    assert await poller._poll_once() == 1  # ticket was "processed" (skipped)

    # No MCP calls — we never reached the claim.
    assert mcp.invoke.await_count == 0


@pytest.mark.asyncio
async def test_kora_control_reader_lazy_built_on_first_claim():
    """When no reader is injected, the poller lazy-constructs the real
    KoraControlReader with the resolved Kora actor_id on the first
    ``_claim_and_work``. Subsequent polls reuse the cached instance."""
    pool = _FakePool()
    pool.queue(_seeded_actor_row(actor_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"))
    pool.queue(_seeded_ticket_row())

    mcp = MagicMock()
    mcp.invoke = AsyncMock(
        return_value=_claim_response(result=KORA_CLAIM_RESULT_ALREADY_CLAIMED)
    )

    poller = SeaTicketPoller(
        mcp_client=mcp,
        memory_provider=_make_memory_provider(pool),
    )
    assert poller._kora_control_reader is None  # not yet built

    # First poll lazy-builds the reader. The real reader queries the
    # substrate for an active command; without a real pool the reader's
    # internal SELECT returns None (FakePool's row queue is empty
    # after the ticket fetch + actor resolve), so the pre-claim branch
    # treats it as "no STOP-KORA active" and proceeds to claim.
    await poller._poll_once()

    assert poller._kora_control_reader is not None
    assert isinstance(poller._kora_control_reader, KoraControlReader)
    # Real reader stores ``_kora_actor_id`` privately — verify it was
    # constructed with the resolved actor_id, not a stub value.
    assert (
        poller._kora_control_reader._kora_actor_id
        == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"
    )


# ---------------------------------------------------------------------------
# actor_id caching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_actor_id_resolved_once_then_cached():
    pool = _FakePool()
    # actor row + first ticket
    pool.queue(_seeded_actor_row(actor_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"))
    pool.queue(_seeded_ticket_row())
    # Second poll: only a ticket row — actor_id should come from cache,
    # no second actor resolution query.
    pool.queue(_seeded_ticket_row(ticket_id="22222222-2222-2222-2222-222222222222"))

    mcp = MagicMock()
    mcp.invoke = AsyncMock()
    mcp.invoke.side_effect = [
        _claim_response(),
        {"result": "released", "chain_event_id": None},
        _claim_response(fence="22222222-2222-2222-2222-222222222222"),
        {"result": "released", "chain_event_id": None},
    ]

    poller = SeaTicketPoller(
        mcp_client=mcp,
        memory_provider=_make_memory_provider(pool),
    )
    await poller._poll_once()
    await poller._poll_once()

    # 3 SQL queries fired across the two polls:
    #   poll 1: actor resolve + ticket fetch + ticket fetch from _claim_and_work's
    #           own actor-resolve call (cached after the first)
    # Actually the actor cache means: 1 actor resolve (poll 1), 1 ticket fetch
    # (poll 1), 1 ticket fetch (poll 2) — total 3.
    assert len(pool.calls) == 3
    # First call was actor resolution.
    assert "FROM public.actor_registry" in pool.calls[0][0]
    # Remaining 2 are ticket fetches.
    assert "FROM public.get_next_available_sea_ticket" in pool.calls[1][0]
    assert "FROM public.get_next_available_sea_ticket" in pool.calls[2][0]


# ---------------------------------------------------------------------------
# run_forever — stop signal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_forever_exits_on_stop_signal():
    pool = _FakePool()  # no rows — every poll returns 0 → sleep
    poller = SeaTicketPoller(
        mcp_client=MagicMock(),
        memory_provider=_make_memory_provider(pool),
        poll_interval_seconds=0,  # tight loop for the test
    )
    # Stop after a short delay.
    async def stopper():
        await asyncio.sleep(0.02)
        poller.stop()

    await asyncio.gather(poller.run_forever(), stopper())
