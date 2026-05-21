"""Integration test for KR-P2-J ST3 STOP-KORA pre-flight wire-in
through ``execute_tool_calls_sequential``.

Mirrors the shape of ``test_constitution_audit_integration.py``: builds
a real ``AIAgent`` (the test-fixture form), patches the STOP-KORA
pre-flight helper to return a deterministic verdict, and exercises
``_execute_tool_calls_sequential`` to verify the wire-in's
block-or-run decision lands as the model-facing tool message + the
``handle_function_call`` path.

Coverage:
  - STOP-KORA inactive → tool runs
  - L1 active → tool runs (informational at pre_tool_call)
  - L2/L3 active → tool blocked + ``block_kind="stop_kora"``
  - L4/L5 active → tool runs (informational, OS-side kill)
  - Constitution FAIL + STOP-KORA active → Constitution blocks first
    (precedence: Constitution is the policy layer; STOP-KORA is the
    coarser-grained drain/abort gate)
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.constitution_pre_screen import (
    PreScreenEnvelope,
    PreScreenVerdict,
)
from agent.stop_kora_handler import STOPKoraAction, STOPKoraVerdict
from run_agent import AIAgent


# ---------------------------------------------------------------------------
# Local helpers (mirror tests/run_agent/test_run_agent.py shape)
# ---------------------------------------------------------------------------


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


def _mock_assistant_msg(content="", tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _mock_tool_call(name, arguments="{}", call_id=None):
    return SimpleNamespace(
        id=call_id or f"call_{uuid.uuid4().hex[:8]}",
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _make_fake_isokron_provider():
    return SimpleNamespace(
        _resolve_workspace_id=lambda: "ws-1",
        _connection=SimpleNamespace(
            get_pg_pool=MagicMock(return_value="fake-pool"),
            get_mcp_client=MagicMock(return_value="fake-mcp-client"),
            submit_and_wait=MagicMock(side_effect=_close_and_return_none),
        ),
        _constitution_cache=SimpleNamespace(
            get=lambda key: ("rev-uuid-abc", "deadbeef1234")
            if key == "ws-1"
            else None,
        ),
    )


def _close_and_return_none(coro, *, timeout=None):
    """Close unawaited coroutines from helper mocks (silences pytest
    'coroutine was never awaited' warnings) and return None — fixture
    default for "no active STOP-KORA, no actor UUID resolved" path."""
    if hasattr(coro, "close"):
        coro.close()
    return None


# ---------------------------------------------------------------------------
# Fixture: AIAgent with stubbed _memory_manager
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_with_isokron_stub():
    with (
        patch(
            "run_agent.get_tool_definitions",
            return_value=_make_tool_defs("read_file"),
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()

    provider = _make_fake_isokron_provider()
    a._memory_manager = SimpleNamespace(
        get_provider=lambda name: provider if name == "isokron" else None,
        has_tool=lambda fn: False,
        on_session_end=lambda *args, **kw: None,
        shutdown_all=lambda: None,
    )
    return a


def _passing_constitution_verdict() -> PreScreenVerdict:
    return PreScreenVerdict.pass_(
        PreScreenEnvelope(
            tool_name="read_file",
            required_capability="cap_local_file_io",
            actor_id="kora",
            constitution_revision_id="rev-uuid-abc",
            rules_hash="deadbeef1234",
        )
    )


# ---------------------------------------------------------------------------
# Tool runs when STOP-KORA is inactive / non-blocking
# ---------------------------------------------------------------------------


def test_inactive_stop_kora_lets_tool_run(agent_with_isokron_stub):
    """No active STOP-KORA command → tool runs normally."""
    no_action_verdict = STOPKoraVerdict(
        action=None,
        command_id=None,
        reason=None,
        context="pre_tool_call",
    )
    tc = _mock_tool_call(name="read_file", arguments=json.dumps({"path": "/x"}))
    msg = _mock_assistant_msg(tool_calls=[tc])
    messages = []

    with (
        patch(
            "agent.tool_executor._run_constitution_pre_screen",
            return_value=_passing_constitution_verdict(),
        ),
        patch("agent.tool_executor.emit_constitution_audit_event"),
        patch(
            "agent.tool_executor.run_stop_kora_pre_flight",
            return_value=no_action_verdict,
        ),
        patch(
            "run_agent.handle_function_call", return_value="file contents"
        ) as mock_hfc,
    ):
        agent_with_isokron_stub._execute_tool_calls_sequential(
            msg, messages, "task-1"
        )

    mock_hfc.assert_called_once()


@pytest.mark.parametrize(
    "non_blocking_action",
    [
        STOPKoraAction.BLOCK_NEW_INTAKE,  # L1 — informational
        STOPKoraAction.EXTERNAL_KILL,     # L4/L5 — informational
    ],
)
def test_non_blocking_stop_kora_lets_tool_run(
    agent_with_isokron_stub, non_blocking_action
):
    """L1 (BLOCK_NEW_INTAKE) and L4/L5 (EXTERNAL_KILL) are informational
    at pre_tool_call — tool runs."""
    verdict = STOPKoraVerdict(
        action=non_blocking_action,
        command_id="cmd-info",
        reason="informational",
        context="pre_tool_call",
    )
    tc = _mock_tool_call(name="read_file", arguments=json.dumps({"path": "/x"}))
    msg = _mock_assistant_msg(tool_calls=[tc])
    messages = []

    with (
        patch(
            "agent.tool_executor._run_constitution_pre_screen",
            return_value=_passing_constitution_verdict(),
        ),
        patch("agent.tool_executor.emit_constitution_audit_event"),
        patch(
            "agent.tool_executor.run_stop_kora_pre_flight",
            return_value=verdict,
        ),
        patch("run_agent.handle_function_call", return_value="file contents") as mock_hfc,
    ):
        agent_with_isokron_stub._execute_tool_calls_sequential(
            msg, messages, "task-1"
        )

    # verdict.is_blocking() returns False at pre_tool_call for these
    # actions → tool runs.
    mock_hfc.assert_called_once()


# ---------------------------------------------------------------------------
# Tool blocked when STOP-KORA is L2/L3 blocking
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "blocking_action,expected_stop_kora_action",
    [
        (STOPKoraAction.DRAIN_CURRENT_FINISH, "drain_current_finish"),
        (STOPKoraAction.ABORT_RELEASE_CLAIM, "abort_release_claim"),
    ],
)
def test_blocking_stop_kora_blocks_tool_with_stop_kora_block_kind(
    agent_with_isokron_stub, blocking_action, expected_stop_kora_action
):
    """L2 (DRAIN) and L3 (ABORT) block at pre_tool_call. Tool body does
    NOT execute. The model-facing message carries block_kind="stop_kora"
    + the action discriminator + command_id for traceability."""
    verdict = STOPKoraVerdict(
        action=blocking_action,
        command_id="cmd-blocking-1",
        reason="operator drain due to drift review",
        context="pre_tool_call",
    )
    tc = _mock_tool_call(name="read_file", arguments=json.dumps({"path": "/x"}))
    msg = _mock_assistant_msg(tool_calls=[tc])
    messages = []

    with (
        patch(
            "agent.tool_executor._run_constitution_pre_screen",
            return_value=_passing_constitution_verdict(),
        ),
        patch("agent.tool_executor.emit_constitution_audit_event"),
        patch(
            "agent.tool_executor.run_stop_kora_pre_flight",
            return_value=verdict,
        ),
        patch("run_agent.handle_function_call") as mock_hfc,
    ):
        agent_with_isokron_stub._execute_tool_calls_sequential(
            msg, messages, "task-1"
        )

    mock_hfc.assert_not_called()
    assert len(messages) == 1
    payload = json.loads(messages[0]["content"])
    assert payload["block_kind"] == "stop_kora"
    assert payload["stop_kora_action"] == expected_stop_kora_action
    assert payload["command_id"] == "cmd-blocking-1"
    assert "operator drain" in payload["error"]


# ---------------------------------------------------------------------------
# Precedence — Constitution FAIL blocks first, STOP-KORA never runs
# ---------------------------------------------------------------------------


def test_constitution_fail_takes_precedence_over_stop_kora(
    agent_with_isokron_stub,
):
    """Wire-in order: Constitution pre-screen → STOP-KORA → guardrails.
    A Constitution FAIL short-circuits before STOP-KORA's helper runs."""
    fail_verdict = PreScreenVerdict.fail(
        "policy denies tool 'read_file' for capability 'cap_local_file_io'",
        envelope=PreScreenEnvelope(
            tool_name="read_file",
            required_capability="cap_local_file_io",
            actor_id="kora",
            constitution_revision_id="rev-uuid-abc",
            rules_hash="deadbeef1234",
        ),
    )

    tc = _mock_tool_call(name="read_file", arguments=json.dumps({"path": "/x"}))
    msg = _mock_assistant_msg(tool_calls=[tc])
    messages = []

    with (
        patch(
            "agent.tool_executor._run_constitution_pre_screen",
            return_value=fail_verdict,
        ),
        patch("agent.tool_executor.emit_constitution_audit_event"),
        patch(
            "agent.tool_executor.run_stop_kora_pre_flight"
        ) as mock_stop_kora,
        patch("run_agent.handle_function_call") as mock_hfc,
    ):
        agent_with_isokron_stub._execute_tool_calls_sequential(
            msg, messages, "task-1"
        )

    # Constitution FAIL → block_result set → STOP-KORA helper NOT called
    mock_stop_kora.assert_not_called()
    # Tool body NOT called
    mock_hfc.assert_not_called()
    # Block_kind is constitution_reject (not stop_kora)
    payload = json.loads(messages[0]["content"])
    assert payload["block_kind"] == "constitution_reject"
