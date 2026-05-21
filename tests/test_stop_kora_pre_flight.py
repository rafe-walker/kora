"""Unit tests for ``agent/stop_kora_pre_flight.py`` (KR-P2-J ST3 helpers).

Covers:
  - ``run_stop_kora_pre_flight`` short-circuits (no memory_manager,
    no isokron provider, no _connection)
  - Happy path: no active command → no-action verdict
  - Active command (L1) → non-blocking verdict at pre_tool_call
  - Active command (L2/L3) → blocking verdict + lifecycle advance
  - Lifecycle advance failure → ``mark_failed`` called
  - Actor UUID resolution: cached, lookup, failure modes
  - ``build_stop_kora_block_result`` JSON shape (block_kind="stop_kora",
    stop_kora_action discriminator, command_id traceability)
  - Reader ``get_active_command`` raises → degraded no-action verdict
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from agent.stop_kora_handler import STOPKoraAction, STOPKoraVerdict
from agent.stop_kora_pre_flight import (
    STOP_KORA_BLOCK_KIND,
    _resolve_kora_actor_uuid,
    build_stop_kora_block_result,
    run_stop_kora_pre_flight,
)
from plugins.memory.isokron.kora_control_reader import KoraControlCommand


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _cmd(level: int, *, kind: str = "stop") -> KoraControlCommand:
    now = datetime(2026, 5, 21, 12, 0, 0, tzinfo=timezone.utc)
    return KoraControlCommand(
        command_id="11111111-1111-1111-1111-111111111111",
        workspace_id="ws-1",
        issuer_session_id="cockpit-session-1",
        issuer_actor_id="22222222-2222-2222-2222-222222222222",
        issuer_actor_kind="operator",
        level=level,
        kind=kind,
        reason="test command",
        target_session=None,
        sequence=1,
        lifecycle_state="visible_to_runtime",
        created_at=now,
        visible_to_runtime_at=now,
        expires_at=None,
        observed_at=None,
        acknowledged_at=None,
        enforced_at=None,
    )


def _make_connection(
    *,
    submit_returns: Any = None,
    submit_side_effect: Any = None,
) -> SimpleNamespace:
    if submit_side_effect is not None:
        submit = MagicMock(side_effect=submit_side_effect)
    else:
        # Wrap the constant-return path so unawaited coroutines get
        # closed (silences pytest RuntimeWarning).
        def _default(coro, *, timeout):
            coro.close()
            return submit_returns
        submit = MagicMock(side_effect=_default)
    return SimpleNamespace(
        get_pg_pool=MagicMock(return_value="fake-pool"),
        submit_and_wait=submit,
    )


def _make_provider(
    *,
    workspace_id: Optional[str] = "ws-1",
    workspace_id_raises: bool = False,
    connection: Optional[SimpleNamespace] = None,
) -> SimpleNamespace:
    def _resolve():
        if workspace_id_raises:
            raise RuntimeError("provider not initialized")
        return workspace_id

    return SimpleNamespace(
        _resolve_workspace_id=_resolve,
        _connection=connection,
    )


def _make_agent(provider: Optional[SimpleNamespace]) -> SimpleNamespace:
    if provider is None:
        return SimpleNamespace(_memory_manager=None)
    memory_manager = SimpleNamespace(
        get_provider=lambda name: provider if name == "isokron" else None,
    )
    return SimpleNamespace(_memory_manager=memory_manager)


KORA_ACTOR_UUID = "33333333-3333-3333-3333-333333333333"


# ---------------------------------------------------------------------------
# Short-circuit paths (no memory_manager / no provider / no _connection)
# ---------------------------------------------------------------------------


def test_returns_none_when_no_memory_manager():
    agent = _make_agent(None)
    assert run_stop_kora_pre_flight(agent) is None


def test_returns_none_when_no_isokron_provider():
    agent = SimpleNamespace(
        _memory_manager=SimpleNamespace(get_provider=lambda name: None)
    )
    assert run_stop_kora_pre_flight(agent) is None


def test_returns_none_when_no_connection():
    provider = _make_provider(connection=None)
    agent = _make_agent(provider)
    assert run_stop_kora_pre_flight(agent) is None


# ---------------------------------------------------------------------------
# Happy path: no active command → no-action verdict; reader raises → degraded
# ---------------------------------------------------------------------------


def test_no_active_command_returns_no_action_verdict():
    # submit_and_wait returns None for both _query_kora_actor_uuid AND
    # reader.get_active_command (the agent fixture doesn't differentiate).
    conn = _make_connection(submit_returns=None)
    provider = _make_provider(connection=conn)
    agent = _make_agent(provider)

    verdict = run_stop_kora_pre_flight(agent)
    assert verdict is not None
    assert verdict.action is None
    assert verdict.is_blocking() is False
    assert verdict.context == "pre_tool_call"


def test_reader_get_active_command_raising_returns_degraded_no_action():
    """Defensive: reader exceptions become WARN-logged no-action verdicts;
    tool call proceeds (substrate hiccup shouldn't block all tools)."""
    submit_calls: list = []

    def _submit(coro, *, timeout):
        coro.close()
        submit_calls.append(coro)
        # First call is _query_kora_actor_uuid → None
        # Second call is reader.get_active_command → raise
        if len(submit_calls) == 1:
            return None
        raise RuntimeError("substrate dispatch tier down")

    conn = SimpleNamespace(
        get_pg_pool=MagicMock(return_value="fake-pool"),
        submit_and_wait=MagicMock(side_effect=_submit),
    )
    provider = _make_provider(connection=conn)
    agent = _make_agent(provider)

    verdict = run_stop_kora_pre_flight(agent)
    assert verdict is not None
    assert verdict.action is None
    assert verdict.is_blocking() is False


# ---------------------------------------------------------------------------
# Active commands — verdict + lifecycle advance
# ---------------------------------------------------------------------------


def test_active_l1_command_returns_non_blocking_verdict():
    """L1 (BLOCK_NEW_INTAKE) at pre_tool_call is informational —
    intake already happened, so the tool call proceeds."""
    submit_calls: list[Any] = []

    def _submit(coro, *, timeout):
        coro.close()
        submit_calls.append("call")
        # First: actor UUID lookup → "uuid"
        # Second: get_active_command → L1 cmd
        if len(submit_calls) == 1:
            return KORA_ACTOR_UUID
        return _cmd(1)

    conn = SimpleNamespace(
        get_pg_pool=MagicMock(return_value="fake-pool"),
        submit_and_wait=MagicMock(side_effect=_submit),
    )
    provider = _make_provider(connection=conn)
    agent = _make_agent(provider)

    verdict = run_stop_kora_pre_flight(agent)
    assert verdict is not None
    assert verdict.action is STOPKoraAction.BLOCK_NEW_INTAKE
    # At pre_tool_call context, L1 is NOT blocking
    assert verdict.is_blocking() is False


def test_active_l2_command_returns_blocking_verdict_and_advances_lifecycle():
    """L2 (DRAIN_CURRENT_FINISH) at pre_tool_call blocks AND advances
    lifecycle acknowledged → enforcing → enforced."""
    submit_calls: list[str] = []

    def _submit(coro, *, timeout):
        coro.close()
        # Track which call this is
        call_idx = len(submit_calls)
        submit_calls.append(str(call_idx))
        if call_idx == 0:
            return KORA_ACTOR_UUID  # actor UUID lookup
        if call_idx == 1:
            return _cmd(2)  # get_active_command → L2
        # Calls 2, 3, 4 are mark_acknowledged, mark_enforcing, mark_enforced
        return None

    conn = SimpleNamespace(
        get_pg_pool=MagicMock(return_value="fake-pool"),
        submit_and_wait=MagicMock(side_effect=_submit),
    )
    provider = _make_provider(connection=conn)
    agent = _make_agent(provider)

    verdict = run_stop_kora_pre_flight(agent)
    assert verdict is not None
    assert verdict.action is STOPKoraAction.DRAIN_CURRENT_FINISH
    assert verdict.is_blocking() is True
    # 5 calls: actor-uuid, get_active_command, mark_ack, mark_enforcing, mark_enforced
    assert conn.submit_and_wait.call_count == 5


def test_active_l3_command_returns_abort_verdict_and_advances():
    """L3 (ABORT_RELEASE_CLAIM) at pre_tool_call blocks + advances."""
    submit_calls: list[int] = []

    def _submit(coro, *, timeout):
        coro.close()
        idx = len(submit_calls)
        submit_calls.append(idx)
        if idx == 0:
            return KORA_ACTOR_UUID
        if idx == 1:
            return _cmd(3)
        return None

    conn = SimpleNamespace(
        get_pg_pool=MagicMock(return_value="fake-pool"),
        submit_and_wait=MagicMock(side_effect=_submit),
    )
    provider = _make_provider(connection=conn)
    agent = _make_agent(provider)

    verdict = run_stop_kora_pre_flight(agent)
    assert verdict.action is STOPKoraAction.ABORT_RELEASE_CLAIM
    assert verdict.is_blocking() is True


def test_active_l4_l5_command_returns_external_kill_non_blocking():
    """L4/L5 (EXTERNAL_KILL) at pre_tool_call is informational —
    operator-side kill, blocking this tool call doesn't help."""
    submit_calls: list[Any] = []

    def _submit(coro, *, timeout):
        coro.close()
        idx = len(submit_calls)
        submit_calls.append(idx)
        if idx == 0:
            return KORA_ACTOR_UUID
        return _cmd(4)

    conn = SimpleNamespace(
        get_pg_pool=MagicMock(return_value="fake-pool"),
        submit_and_wait=MagicMock(side_effect=_submit),
    )
    provider = _make_provider(connection=conn)
    agent = _make_agent(provider)

    verdict = run_stop_kora_pre_flight(agent)
    assert verdict.action is STOPKoraAction.EXTERNAL_KILL
    assert verdict.is_blocking() is False
    # Only 2 calls: actor UUID + get_active_command. No lifecycle advance
    # because the verdict is not blocking.
    assert conn.submit_and_wait.call_count == 2


def test_blocking_verdict_without_actor_uuid_skips_lifecycle_advance(caplog):
    """When kora actor UUID is unresolvable (lookup returns None),
    blocking verdict is still returned but lifecycle advance is
    skipped. WARN log captures the missed advance."""
    submit_calls: list[Any] = []

    def _submit(coro, *, timeout):
        coro.close()
        idx = len(submit_calls)
        submit_calls.append(idx)
        if idx == 0:
            return None  # actor UUID lookup fails
        return _cmd(2)  # get_active_command → L2

    conn = SimpleNamespace(
        get_pg_pool=MagicMock(return_value="fake-pool"),
        submit_and_wait=MagicMock(side_effect=_submit),
    )
    provider = _make_provider(connection=conn)
    agent = _make_agent(provider)

    import logging
    with caplog.at_level(
        logging.WARNING, logger="agent.stop_kora_pre_flight"
    ):
        verdict = run_stop_kora_pre_flight(agent)

    assert verdict.action is STOPKoraAction.DRAIN_CURRENT_FINISH
    assert verdict.is_blocking() is True
    # Only 2 calls: actor UUID (None) + get_active_command. No mark_*.
    assert conn.submit_and_wait.call_count == 2
    assert any(
        "STOP-KORA active" in rec.getMessage()
        and "unresolvable" in rec.getMessage()
        for rec in caplog.records
    )


def test_lifecycle_advance_failure_triggers_mark_failed(caplog):
    """If mark_acknowledged → mark_enforcing → mark_enforced sequence
    fails partway, mark_failed is called as a best-effort fallback."""
    submit_calls: list[int] = []
    advance_failure_idx = 3  # fail on mark_enforcing (3rd in sequence)

    def _submit(coro, *, timeout):
        coro.close()
        idx = len(submit_calls)
        submit_calls.append(idx)
        if idx == 0:
            return KORA_ACTOR_UUID
        if idx == 1:
            return _cmd(2)
        if idx == advance_failure_idx:
            raise RuntimeError("substrate dispatch returned 500")
        return None

    conn = SimpleNamespace(
        get_pg_pool=MagicMock(return_value="fake-pool"),
        submit_and_wait=MagicMock(side_effect=_submit),
    )
    provider = _make_provider(connection=conn)
    agent = _make_agent(provider)

    import logging
    with caplog.at_level(
        logging.WARNING, logger="agent.stop_kora_pre_flight"
    ):
        verdict = run_stop_kora_pre_flight(agent)

    # Verdict still returned (block_result is independent of advance)
    assert verdict.is_blocking() is True
    # Advance sequence + mark_failed = at least 4 calls after the
    # actor-UUID + get_active_command (so ≥ 6 total). mark_failed runs.
    assert conn.submit_and_wait.call_count >= 5
    assert any(
        "lifecycle advance failed" in rec.getMessage()
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Actor UUID resolution
# ---------------------------------------------------------------------------


def test_actor_uuid_cached_on_agent_after_first_lookup():
    """The actor UUID is cached on agent._cached_kora_actor_uuid so
    subsequent pre-flights skip the query."""
    submit_calls: list[Any] = []

    def _submit(coro, *, timeout):
        coro.close()
        submit_calls.append("c")
        if len(submit_calls) == 1:
            return KORA_ACTOR_UUID  # actor UUID lookup (first time)
        return None  # all subsequent calls (get_active_command)

    conn = SimpleNamespace(
        get_pg_pool=MagicMock(return_value="fake-pool"),
        submit_and_wait=MagicMock(side_effect=_submit),
    )
    provider = _make_provider(connection=conn)
    agent = _make_agent(provider)

    # First call: actor UUID query runs
    run_stop_kora_pre_flight(agent)
    assert agent._cached_kora_actor_uuid == KORA_ACTOR_UUID

    # Reset call tracking; second call should use cached UUID (skip query)
    first_call_count = conn.submit_and_wait.call_count
    run_stop_kora_pre_flight(agent)
    # Second call had: get_active_command (1) only — no actor UUID query
    assert conn.submit_and_wait.call_count == first_call_count + 1


def test_resolve_actor_uuid_returns_none_when_workspace_id_unresolved():
    conn = _make_connection(submit_returns=None)
    provider = _make_provider(workspace_id=None, connection=conn)
    agent = _make_agent(provider)
    assert _resolve_kora_actor_uuid(agent) is None
    # Query was NOT called (workspace_id check short-circuits)
    conn.submit_and_wait.assert_not_called()


def test_resolve_actor_uuid_returns_none_when_workspace_resolve_raises():
    conn = _make_connection(submit_returns=None)
    provider = _make_provider(workspace_id_raises=True, connection=conn)
    agent = _make_agent(provider)
    assert _resolve_kora_actor_uuid(agent) is None


def test_resolve_actor_uuid_returns_none_when_query_raises():
    conn = SimpleNamespace(
        get_pg_pool=MagicMock(return_value="fake-pool"),
        submit_and_wait=MagicMock(
            side_effect=lambda coro, *, timeout: (coro.close(), _raise(RuntimeError("boom")))[1]
        ),
    )
    provider = _make_provider(connection=conn)
    agent = _make_agent(provider)
    assert _resolve_kora_actor_uuid(agent) is None


def _raise(exc):
    raise exc


def test_resolve_actor_uuid_returns_uuid_when_query_succeeds():
    def _submit(coro, *, timeout):
        coro.close()
        return KORA_ACTOR_UUID

    conn = SimpleNamespace(
        get_pg_pool=MagicMock(return_value="fake-pool"),
        submit_and_wait=MagicMock(side_effect=_submit),
    )
    provider = _make_provider(connection=conn)
    agent = _make_agent(provider)
    assert _resolve_kora_actor_uuid(agent) == KORA_ACTOR_UUID


# ---------------------------------------------------------------------------
# build_stop_kora_block_result — JSON shape
# ---------------------------------------------------------------------------


def test_build_block_result_drain_shape():
    verdict = STOPKoraVerdict(
        action=STOPKoraAction.DRAIN_CURRENT_FINISH,
        command_id="cmd-xyz",
        reason="operator paused for drift review",
        context="pre_tool_call",
    )
    raw = build_stop_kora_block_result(verdict)
    parsed = json.loads(raw)
    assert parsed["block_kind"] == STOP_KORA_BLOCK_KIND == "stop_kora"
    assert parsed["stop_kora_action"] == "drain_current_finish"
    assert parsed["command_id"] == "cmd-xyz"
    assert "drain_current_finish" in parsed["error"]
    assert "cmd-xyz" in parsed["error"]
    assert "operator paused for drift review" in parsed["error"]


def test_build_block_result_abort_shape():
    verdict = STOPKoraVerdict(
        action=STOPKoraAction.ABORT_RELEASE_CLAIM,
        command_id="cmd-abort",
        reason="immediate abort",
        context="pre_tool_call",
    )
    raw = build_stop_kora_block_result(verdict)
    parsed = json.loads(raw)
    assert parsed["block_kind"] == "stop_kora"
    assert parsed["stop_kora_action"] == "abort_release_claim"
    assert parsed["command_id"] == "cmd-abort"


def test_build_block_result_handles_none_action():
    """Defensive — should never be called with None action, but make
    sure the JSON shape doesn't crash."""
    verdict = STOPKoraVerdict(
        action=None,
        command_id="cmd-none",
        reason="defensive",
        context="pre_tool_call",
    )
    raw = build_stop_kora_block_result(verdict)
    parsed = json.loads(raw)
    assert parsed["block_kind"] == "stop_kora"
    assert parsed["stop_kora_action"] is None


def test_build_block_result_preserves_unicode_reason():
    verdict = STOPKoraVerdict(
        action=STOPKoraAction.DRAIN_CURRENT_FINISH,
        command_id="cmd-i18n",
        reason="操作员暂停 — drift detected",
        context="pre_tool_call",
    )
    raw = build_stop_kora_block_result(verdict)
    assert "操作员暂停" in raw
    parsed = json.loads(raw)
    assert "操作员暂停" in parsed["error"]
