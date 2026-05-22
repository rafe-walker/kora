"""Unit tests for ``plugins/memory/isokron/kora_operation_ledger.py`` (KR-P2-E ST2).

Covers:
  - allocate_operation issues the CTE INSERT with the right params and
    projects the returning row
  - mark_dispatched / mark_committed / mark_abandoned issue the right
    UPDATE with the right status string + return the projected row
  - reread_for_retry composite-PK lookup; missing key returns None
  - asyncpg failures bubble as KoraOperationLedgerError with the
    failure_context prefix
  - UPDATE returning 0 rows (operation_id not found) raises explicitly
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from plugins.memory.isokron.kora_operation_ledger import (
    KoraOperationLedger,
    KoraOperationLedgerError,
    KoraOperationRow,
    _row_to_kora_operation,
)


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


class _FakePool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.rows: list[Any] = []
        self.raise_next: Optional[Exception] = None  # type: ignore[name-defined]

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


# Avoid forward-ref Optional import noise.
from typing import Optional  # noqa: E402


class _FakeConn:
    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self._pool.calls.append((sql, args))
        if self._pool.raise_next is not None:
            exc = self._pool.raise_next
            self._pool.raise_next = None
            raise exc
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


def _seeded_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "work_attempt_id": "11111111-1111-1111-1111-111111111111",
        "sequence_within_attempt": 0,
        "kora_operation_id": "22222222-2222-2222-2222-222222222222",
        "workspace_id": "org_test",
        "ticket_id": "33333333-3333-3333-3333-333333333333",
        "status": "allocated",
        "tool_name": "kora__some_tool",
        "dispatch_result": None,
        "dispatch_error": None,
        "created_at": datetime(2026, 5, 21, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 5, 21, tzinfo=timezone.utc),
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# _row_to_kora_operation projection
# ---------------------------------------------------------------------------


def test_row_to_kora_operation_projects_all_fields():
    raw = _seeded_row()
    proj = _row_to_kora_operation(raw)
    assert isinstance(proj, KoraOperationRow)
    assert proj.work_attempt_id == raw["work_attempt_id"]
    assert proj.sequence_within_attempt == 0
    assert proj.kora_operation_id == raw["kora_operation_id"]
    assert proj.workspace_id == "org_test"
    assert proj.ticket_id == raw["ticket_id"]
    assert proj.status == "allocated"
    assert proj.tool_name == "kora__some_tool"
    assert proj.dispatch_result is None
    assert proj.dispatch_error is None


# ---------------------------------------------------------------------------
# allocate_operation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allocate_operation_issues_cte_insert_with_right_params():
    pool = _FakePool()
    pool.queue(_seeded_row(sequence_within_attempt=0))
    ledger = KoraOperationLedger(_FakeConnection(pool))

    op = await ledger.allocate_operation(
        work_attempt_id="11111111-1111-1111-1111-111111111111",
        workspace_id="org_test",
        ticket_id="33333333-3333-3333-3333-333333333333",
        tool_name="kora__some_tool",
    )

    assert op.sequence_within_attempt == 0
    assert op.status == "allocated"

    assert len(pool.calls) == 1
    sql, args = pool.calls[0]
    # The CTE allocates sequence_within_attempt; runtime doesn't pass
    # it.
    assert "WITH next_seq AS" in sql
    assert "MAX(sequence_within_attempt)" in sql
    assert "status = 'allocated'" not in sql  # status set via INSERT VALUES, not WHERE
    assert args == (
        "11111111-1111-1111-1111-111111111111",
        "org_test",
        "33333333-3333-3333-3333-333333333333",
        "kora__some_tool",
    )


@pytest.mark.asyncio
async def test_allocate_operation_raises_on_substrate_error():
    pool = _FakePool()
    pool.raise_next = RuntimeError(
        "trigger _kora_operation_ledger_check_work_attempt rejected"
    )
    ledger = KoraOperationLedger(_FakeConnection(pool))
    with pytest.raises(KoraOperationLedgerError) as exc_info:
        await ledger.allocate_operation(
            work_attempt_id="x",
            workspace_id="org_test",
            ticket_id="t",
            tool_name="kora__some_tool",
        )
    assert "allocate_operation" in str(exc_info.value)
    assert "trigger" in str(exc_info.value)


# ---------------------------------------------------------------------------
# mark_* state transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_dispatched_updates_status_and_dispatch_result():
    pool = _FakePool()
    pool.queue(
        _seeded_row(status="dispatched", dispatch_result={"event_id": "evt"})
    )
    ledger = KoraOperationLedger(_FakeConnection(pool))

    op = await ledger.mark_dispatched(
        "22222222-2222-2222-2222-222222222222",
        dispatch_result={"event_id": "evt"},
    )
    assert op.status == "dispatched"
    assert op.dispatch_result == {"event_id": "evt"}

    sql, args = pool.calls[0]
    assert "SET status = 'dispatched'" in sql
    assert args == ("22222222-2222-2222-2222-222222222222", {"event_id": "evt"})


@pytest.mark.asyncio
async def test_mark_committed_sets_committed_status():
    pool = _FakePool()
    pool.queue(_seeded_row(status="committed"))
    ledger = KoraOperationLedger(_FakeConnection(pool))

    op = await ledger.mark_committed("22222222-2222-2222-2222-222222222222")
    assert op.status == "committed"

    sql, args = pool.calls[0]
    assert "SET status = 'committed'" in sql
    assert args == ("22222222-2222-2222-2222-222222222222",)


@pytest.mark.asyncio
async def test_mark_abandoned_records_reason_in_dispatch_error():
    pool = _FakePool()
    pool.queue(
        _seeded_row(status="abandoned", dispatch_error="failed_terminal")
    )
    ledger = KoraOperationLedger(_FakeConnection(pool))

    op = await ledger.mark_abandoned(
        "22222222-2222-2222-2222-222222222222", reason="failed_terminal"
    )
    assert op.status == "abandoned"
    assert op.dispatch_error == "failed_terminal"

    sql, args = pool.calls[0]
    assert "SET status = 'abandoned'" in sql
    assert args == (
        "22222222-2222-2222-2222-222222222222",
        "failed_terminal",
    )


@pytest.mark.asyncio
async def test_mark_dispatched_raises_when_operation_id_not_found():
    pool = _FakePool()  # no row queued — UPDATE returns no row
    ledger = KoraOperationLedger(_FakeConnection(pool))
    with pytest.raises(KoraOperationLedgerError) as exc_info:
        await ledger.mark_dispatched("ffffffff-ffff-ffff-ffff-ffffffffffff")
    assert "operation_id not found" in str(exc_info.value)


# ---------------------------------------------------------------------------
# reread_for_retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reread_for_retry_returns_row_when_found():
    pool = _FakePool()
    pool.queue(_seeded_row(sequence_within_attempt=2, status="dispatched"))
    ledger = KoraOperationLedger(_FakeConnection(pool))

    op = await ledger.reread_for_retry(
        work_attempt_id="11111111-1111-1111-1111-111111111111",
        sequence_within_attempt=2,
    )
    assert op is not None
    assert op.sequence_within_attempt == 2
    assert op.status == "dispatched"


@pytest.mark.asyncio
async def test_reread_for_retry_returns_none_when_missing():
    pool = _FakePool()  # no row queued
    ledger = KoraOperationLedger(_FakeConnection(pool))

    op = await ledger.reread_for_retry(
        work_attempt_id="x",
        sequence_within_attempt=9999,
    )
    assert op is None


@pytest.mark.asyncio
async def test_reread_raises_on_substrate_error():
    pool = _FakePool()
    pool.raise_next = RuntimeError("network blip")
    ledger = KoraOperationLedger(_FakeConnection(pool))
    with pytest.raises(KoraOperationLedgerError) as exc_info:
        await ledger.reread_for_retry(
            work_attempt_id="x", sequence_within_attempt=0
        )
    assert "reread_for_retry" in str(exc_info.value)
    assert "network blip" in str(exc_info.value)


# ---------------------------------------------------------------------------
# No mint_work_attempt_id — explicit contract check
# ---------------------------------------------------------------------------


def test_no_mint_work_attempt_id_method():
    """Top-level/0102 amendment: substrate mints work_attempt_id
    atomically inside kora_claim_sea_ticket. The runtime ledger must
    NOT mint — this test fails fast if a future refactor re-adds it.
    """
    ledger = KoraOperationLedger(MagicMock())
    assert not hasattr(ledger, "mint_work_attempt_id"), (
        "KoraOperationLedger must NOT expose mint_work_attempt_id — "
        "the substrate's kora_claim_sea_ticket SECDEF mints atomically. "
        "See top-level/0102 + KR-P2-E ST2 spec."
    )
