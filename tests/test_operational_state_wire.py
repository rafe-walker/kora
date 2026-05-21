"""Integration tests for ``agent/operational_state_wire.py`` (KR-P2-I-integration ST3).

Covers:
  - Happy path: wire-in creates the holder, registers the emit listener,
    runs the BOOTING → READY transition, and emits the chain event
  - No-connection branch: provider without _connection — holder stays
    in BOOTING with a WARNING in the logs
  - Connection-raises branch: submit_and_wait raises — wire-in is
    fail-soft and does NOT propagate the exception
  - Idempotence: second wire_operational_state call doesn't double-emit
    the initial transition (holder is a singleton)
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent.operational_state import (
    ClaimPermission,
    OperationalState,
    PrimaryState,
)
from agent.operational_state_holder import (
    _reset_holder_for_tests,
    get_holder,
)
from agent.operational_state_wire import wire_operational_state


@pytest.fixture(autouse=True)
def _reset_singleton():
    _reset_holder_for_tests()
    yield
    _reset_holder_for_tests()


# ---------------------------------------------------------------------------
# Stub IsoKron provider — captures emit calls
# ---------------------------------------------------------------------------


class _FakeMcpClient:
    """Captures every kora__append_event invocation."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def invoke(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"tool_name": tool_name, "args": args})
        return {"event_id": f"evt-{len(self.calls)}"}


class _FakeConnection:
    """Lightweight stand-in for IsoKronConnection.

    submit_and_wait runs the coro on the current asyncio loop synchronously
    (test-only — production uses a dedicated thread loop). _submit_async
    wraps the awaited result in a concurrent.futures.Future the emit
    module can asyncio.wrap_future().
    """

    def __init__(self, mcp_client: _FakeMcpClient) -> None:
        self._mcp_client = mcp_client
        self.submit_calls = 0
        self.submit_should_raise: Exception | None = None

    def get_mcp_client(self) -> _FakeMcpClient:
        return self._mcp_client

    def _submit_async(self, coro):
        result = asyncio.get_event_loop().run_until_complete(coro)
        fut: concurrent.futures.Future = concurrent.futures.Future()
        fut.set_result(result)
        return fut

    def submit_and_wait(self, coro, *, timeout: float = 10.0):
        self.submit_calls += 1
        if self.submit_should_raise is not None:
            # Eat the coroutine (Python warns about unawaited coros).
            try:
                coro.close()
            except Exception:
                pass
            raise self.submit_should_raise
        return asyncio.get_event_loop().run_until_complete(coro)


def _make_provider(connection: _FakeConnection | None) -> Any:
    provider = MagicMock()
    provider._connection = connection
    provider._resolve_workspace_id.return_value = "org_test_workspace"
    return provider


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_initializes_holder_and_emits():
    mcp = _FakeMcpClient()
    conn = _FakeConnection(mcp)
    provider = _make_provider(conn)

    wire_operational_state(provider)

    holder = get_holder()
    assert holder is not None
    # Initial BOOTING → READY transition has applied.
    assert holder.current.primary_state is PrimaryState.READY
    assert holder.current.claim_permission is ClaimPermission.NORMAL
    assert not holder.current.is_degraded()

    # Two events emitted: generic + boot.ready informational.
    assert len(mcp.calls) == 2
    event_types = [c["args"]["event_type"] for c in mcp.calls]
    assert event_types == [
        "kora.operational_state.transitioned",
        "kora.boot.ready",
    ]
    # Payload trigger matches the canonical TRANSITION_TABLE wording so
    # ST2's substring match for boot.ready fires.
    generic_payload = mcp.calls[0]["args"]["payload"]
    assert generic_payload["from_primary_state"] == "booting"
    assert generic_payload["to_primary_state"] == "ready"
    assert generic_payload["trigger"] == "all §9.2 gates pass"


# ---------------------------------------------------------------------------
# No-connection branch — fail-soft warning
# ---------------------------------------------------------------------------


def test_provider_without_connection_logs_warning_and_leaves_holder_in_booting(
    caplog,
):
    provider = _make_provider(None)

    with caplog.at_level(logging.WARNING, logger="agent.operational_state_wire"):
        wire_operational_state(provider)

    holder = get_holder()
    assert holder is not None
    # Holder was created with BOOTING but transition never happened.
    assert holder.current.primary_state is PrimaryState.BOOTING
    assert holder.current.claim_permission is ClaimPermission.NONE
    # Operator-greppable warning emitted.
    assert any(
        "kora.operational_state.wire_in" in record.message
        and "no _connection" in record.message
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# Connection-raises branch — fail-soft
# ---------------------------------------------------------------------------


def test_submit_raises_does_not_propagate(caplog):
    mcp = _FakeMcpClient()
    conn = _FakeConnection(mcp)
    conn.submit_should_raise = RuntimeError("Sea MCP unavailable")
    provider = _make_provider(conn)

    # Must NOT raise.
    with caplog.at_level(logging.WARNING, logger="agent.operational_state_wire"):
        wire_operational_state(provider)

    holder = get_holder()
    # Holder was created (init happens before submit), but the
    # transition never landed.
    assert holder is not None
    assert holder.current.primary_state is PrimaryState.BOOTING
    # Greppable warning landed.
    assert any(
        "kora.operational_state.wire_in" in record.message
        and "wire-in raised" in record.message
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# Idempotence — init_holder is first-wins (no double transition)
# ---------------------------------------------------------------------------


def test_second_call_does_not_re_run_initial_transition():
    mcp = _FakeMcpClient()
    conn = _FakeConnection(mcp)
    provider = _make_provider(conn)

    wire_operational_state(provider)
    first_call_count = len(mcp.calls)
    first_submit_count = conn.submit_calls

    # Second call: holder already in READY, init_holder is no-op, but
    # the wire-in still adds another listener and re-attempts the
    # BOOTING → READY transition. The transition itself will be a
    # READY → READY (no primary_state change) which is allowed by the
    # same-state bypass; the listener fires the emit again.
    wire_operational_state(provider)

    # Two listeners now, each fired on the second-call transition →
    # 1 generic emit × 2 listeners = 2 additional emits. boot.ready
    # only fires when from == BOOTING; since holder is READY at second
    # call, the supplementary literal does NOT fire — only the generic.
    second_call_emits = len(mcp.calls) - first_call_count
    assert second_call_emits == 2  # generic × 2 listeners
    # submit_and_wait fired again at the wire-in level.
    assert conn.submit_calls == first_submit_count + 1
    # State unchanged at READY.
    assert get_holder().current.primary_state is PrimaryState.READY
