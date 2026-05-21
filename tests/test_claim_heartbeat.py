"""Unit tests for ``plugins/memory/isokron/claim_heartbeat.py`` (KR-P2-E ST3).

Covers:
  - ProgressSignal counter mechanics + snapshot_and_reset atomicity
  - Heartbeat fires kora__refresh_claim only when progress has been
    signaled (P2 compliance)
  - Heartbeat skips refresh + logs when no progress has been signaled
  - Refresh exception sets lease_lost + exits the task
  - Substrate refresh result != 'claimed' sets lease_lost + exits
  - cancel() awaits clean shutdown + is idempotent
  - Refresh args carry the right shape (no progress_signal field —
    the actual SECDEF schema)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.memory.isokron.claim_heartbeat import (
    HeartbeatHandle,
    ProgressSignal,
    start_heartbeat,
)


# ---------------------------------------------------------------------------
# ProgressSignal
# ---------------------------------------------------------------------------


def test_progress_signal_starts_with_no_progress():
    sig = ProgressSignal()
    had, tokens, tools = sig.snapshot_and_reset()
    assert had is False
    assert tokens == 0
    assert tools == 0


def test_progress_signal_counts_token_advancement():
    sig = ProgressSignal()
    sig.signal_token_progress()
    sig.signal_token_progress()
    sig.signal_token_progress()
    had, tokens, tools = sig.snapshot_and_reset()
    assert had is True
    assert tokens == 3
    assert tools == 0


def test_progress_signal_counts_tool_boundaries():
    sig = ProgressSignal()
    sig.signal_tool_boundary()
    had, tokens, tools = sig.snapshot_and_reset()
    assert had is True
    assert tokens == 0
    assert tools == 1


def test_progress_signal_snapshot_resets_counters():
    sig = ProgressSignal()
    sig.signal_token_progress()
    sig.signal_tool_boundary()
    sig.snapshot_and_reset()
    # Second snapshot — should be clean.
    had, tokens, tools = sig.snapshot_and_reset()
    assert had is False
    assert tokens == 0
    assert tools == 0


# ---------------------------------------------------------------------------
# Heartbeat scaffolding
# ---------------------------------------------------------------------------


def _refresh_response(result: str = "claimed") -> dict[str, Any]:
    return {
        "result": result,
        "new_lease_expires_at": "2026-05-21T18:00:00Z"
        if result == "claimed"
        else None,
        "chain_event_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
    }


async def _spawn(
    *,
    mcp_invoke,
    interval: float,
    signal_progress_before_first_tick: bool = True,
) -> HeartbeatHandle:
    mcp = MagicMock()
    mcp.invoke = mcp_invoke
    handle = start_heartbeat(
        mcp_client=mcp,
        workspace_id="org_test",
        kora_operation_id="22222222-2222-2222-2222-222222222222",
        sea_ticket_id="11111111-1111-1111-1111-111111111111",
        claim_fence_token="ffffffff-ffff-ffff-ffff-ffffffffffff",
        heartbeat_interval_seconds=interval,
        extend_by_seconds=600,
    )
    if signal_progress_before_first_tick:
        handle.signal_token_progress()
    return handle


# ---------------------------------------------------------------------------
# Happy path — progress → refresh fires
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_fires_when_progress_signaled():
    invoke = AsyncMock(return_value=_refresh_response("claimed"))
    handle = await _spawn(mcp_invoke=invoke, interval=0.05)

    # Let the loop wake once.
    await asyncio.sleep(0.08)
    await handle.cancel()

    assert invoke.await_count >= 1
    # Check the call shape — no progress_signal field per the actual
    # MCP schema.
    args = invoke.await_args.args
    assert args[0] == "kora__refresh_claim"
    payload = args[1]
    assert payload["workspace_id"] == "org_test"
    assert payload["sea_ticket_id"] == "11111111-1111-1111-1111-111111111111"
    assert payload["claim_fence_token"] == "ffffffff-ffff-ffff-ffff-ffffffffffff"
    assert payload["extend_by_seconds"] == 600
    assert "progress_signal" not in payload
    # Heartbeat should still consider the lease alive.
    assert handle.lease_lost is False


# ---------------------------------------------------------------------------
# P2 compliance — no progress → no refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_skipped_when_no_progress_signaled(caplog):
    invoke = AsyncMock(return_value=_refresh_response("claimed"))
    handle = await _spawn(
        mcp_invoke=invoke, interval=0.05, signal_progress_before_first_tick=False
    )

    with caplog.at_level(logging.INFO, logger="plugins.memory.isokron.claim_heartbeat"):
        await asyncio.sleep(0.08)
        await handle.cancel()

    # MCP refresh was NOT called — P2 contract.
    assert invoke.await_count == 0
    # Log line landed.
    assert any(
        "no progress signaled" in record.message
        for record in caplog.records
    )
    assert handle.lease_lost is False


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_exception_sets_lease_lost():
    invoke = AsyncMock(side_effect=RuntimeError("network blip"))
    handle = await _spawn(mcp_invoke=invoke, interval=0.05)

    await asyncio.sleep(0.08)
    # Task should have exited on its own — cancel is a no-op.
    await handle.cancel()

    assert handle.lease_lost is True
    assert handle.task.done()


@pytest.mark.asyncio
async def test_substrate_non_claimed_result_sets_lease_lost():
    invoke = AsyncMock(
        return_value=_refresh_response("contract_version_mismatch")
    )
    handle = await _spawn(mcp_invoke=invoke, interval=0.05)

    await asyncio.sleep(0.08)
    await handle.cancel()

    assert handle.lease_lost is True


# ---------------------------------------------------------------------------
# Cancel semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_is_idempotent():
    invoke = AsyncMock(return_value=_refresh_response("claimed"))
    handle = await _spawn(mcp_invoke=invoke, interval=10)  # never wakes

    await handle.cancel()
    await handle.cancel()  # second call must not raise


@pytest.mark.asyncio
async def test_cancel_during_sleep_exits_cleanly():
    invoke = AsyncMock(return_value=_refresh_response("claimed"))
    handle = await _spawn(mcp_invoke=invoke, interval=10)

    # Immediately cancel — task is sleeping in the interval wait.
    await handle.cancel()
    # No refresh fired; lease not lost.
    assert invoke.await_count == 0
    assert handle.lease_lost is False
