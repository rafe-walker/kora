"""Integration test for KR-P2-A end-to-end Constitution pre-screen
denial-+-audit flow through ``execute_tool_calls_sequential``.

Builds a real ``AIAgent`` (the test-fixture form) with a stubbed
``_memory_manager`` that exposes a fake IsoKron provider. Patches
``_run_constitution_pre_screen`` to return a deterministic verdict
and ``emit_constitution_audit_event`` to capture the audit-emit call.
Asserts:

- Tool body did NOT execute (handle_function_call was not called)
- Audit emit was invoked with the right verdict + args
- The model-facing tool result encodes the ``block_kind``
  discriminator (``constitution_reject`` or ``constitution_escalate``)

This closes the integration-test slot called out in bucket spec § ST1 § 4.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.constitution_pre_screen import (
    PreScreenEnvelope,
    PreScreenOutcome,
    PreScreenVerdict,
)
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
            get_mcp_client=MagicMock(return_value="fake-mcp-client"),
            submit_and_wait=MagicMock(return_value="evt-001"),
        ),
        _constitution_cache=SimpleNamespace(
            get=lambda key: ("rev-uuid-abc", "deadbeef1234")
            if key == "ws-1"
            else None,
        ),
    )


# ---------------------------------------------------------------------------
# Fixture: AIAgent with stubbed memory_manager / isokron provider
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_with_isokron_stub():
    """Real AIAgent with ``_memory_manager`` patched to expose a fake provider."""

    with (
        patch(
            "run_agent.get_tool_definitions",
            return_value=_make_tool_defs("read_file", "mystery_tool"),
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
        # Memory-tool dispatch surface (the executor consults these in
        # the non-blocked path; safe defaults that say "no memory tools").
        has_tool=lambda fn: False,
        on_session_end=lambda *args, **kw: None,
        shutdown_all=lambda: None,
    )
    return a


# ---------------------------------------------------------------------------
# End-to-end — FAIL pre-screen → disagreement event + tool blocked
# ---------------------------------------------------------------------------


def test_pre_screen_fail_emits_disagreement_and_blocks_tool(
    agent_with_isokron_stub,
):
    fail_verdict = PreScreenVerdict.fail(
        "actor_id='kora' lacks capability 'cap_local_file_io' "
        "required by tool 'read_file'.",
        envelope=PreScreenEnvelope(
            tool_name="read_file",
            required_capability="cap_local_file_io",
            actor_id="kora",
            constitution_revision_id="rev-uuid-abc",
            rules_hash="deadbeef1234",
        ),
    )

    tc = _mock_tool_call(
        name="read_file",
        arguments=json.dumps({"path": "/etc/hosts"}),
        call_id="c-fail-1",
    )
    msg = _mock_assistant_msg(tool_calls=[tc])
    messages = []

    with (
        patch(
            "agent.tool_executor._run_constitution_pre_screen",
            return_value=fail_verdict,
        ),
        patch(
            "agent.tool_executor.emit_constitution_audit_event"
        ) as mock_emit,
        patch("run_agent.handle_function_call") as mock_hfc,
    ):
        agent_with_isokron_stub._execute_tool_calls_sequential(
            msg, messages, "task-1"
        )

    # Tool body did NOT execute.
    mock_hfc.assert_not_called()

    # Audit emit invoked exactly once with the FAIL verdict + tool args.
    mock_emit.assert_called_once()
    emit_args, emit_kwargs = mock_emit.call_args
    # Positional call: (agent, tool_name, tool_args, verdict)
    _emit_agent, emit_tool_name, emit_tool_args, emit_verdict = emit_args
    assert emit_tool_name == "read_file"
    assert emit_tool_args == {"path": "/etc/hosts"}
    assert emit_verdict.outcome is PreScreenOutcome.FAIL

    # Model-facing tool message encodes the block_kind.
    assert len(messages) == 1
    payload = json.loads(messages[0]["content"])
    assert payload["block_kind"] == "constitution_reject"
    assert "lacks capability 'cap_local_file_io'" in payload["error"]


# ---------------------------------------------------------------------------
# End-to-end — INCONCLUSIVE pre-screen → escalation event + tool blocked
# ---------------------------------------------------------------------------


def test_pre_screen_inconclusive_emits_escalation_and_blocks_tool(
    agent_with_isokron_stub,
):
    inconclusive_verdict = PreScreenVerdict.inconclusive(
        "tool 'mystery_tool' has no entry in TOOL_CAPABILITY_MAP"
    )

    tc = _mock_tool_call(
        name="mystery_tool", arguments="{}", call_id="c-incl-1"
    )
    msg = _mock_assistant_msg(tool_calls=[tc])
    messages = []

    with (
        patch(
            "agent.tool_executor._run_constitution_pre_screen",
            return_value=inconclusive_verdict,
        ),
        patch(
            "agent.tool_executor.emit_constitution_audit_event"
        ) as mock_emit,
        patch("run_agent.handle_function_call") as mock_hfc,
    ):
        agent_with_isokron_stub._execute_tool_calls_sequential(
            msg, messages, "task-1"
        )

    mock_hfc.assert_not_called()
    mock_emit.assert_called_once()
    _, _, _, emit_verdict = mock_emit.call_args[0]
    assert emit_verdict.outcome is PreScreenOutcome.INCONCLUSIVE

    payload = json.loads(messages[0]["content"])
    assert payload["block_kind"] == "constitution_escalate"
    assert "no entry in TOOL_CAPABILITY_MAP" in payload["error"]


# ---------------------------------------------------------------------------
# Pre-screen PASS → no audit emit, tool body executes
# ---------------------------------------------------------------------------


def test_pre_screen_pass_does_not_emit_audit_and_lets_tool_run(
    agent_with_isokron_stub,
):
    pass_verdict = PreScreenVerdict.pass_(
        PreScreenEnvelope(
            tool_name="read_file",
            required_capability="cap_local_file_io",
            actor_id="kora",
            constitution_revision_id="rev-uuid-abc",
            rules_hash="deadbeef1234",
        )
    )

    tc = _mock_tool_call(
        name="read_file", arguments=json.dumps({"path": "/x"}), call_id="c-pass-1"
    )
    msg = _mock_assistant_msg(tool_calls=[tc])
    messages = []

    with (
        patch(
            "agent.tool_executor._run_constitution_pre_screen",
            return_value=pass_verdict,
        ),
        patch(
            "agent.tool_executor.emit_constitution_audit_event"
        ) as mock_emit,
        # KR-P2-J ST3 added a STOP-KORA pre-flight downstream of the
        # Constitution pre-screen. The agent_with_isokron_stub fixture
        # has a blanket ``submit_and_wait → "evt-001"`` mock that
        # confuses the STOP-KORA reader (which would interpret the
        # string as a command). Patch the helper to a no-op so this
        # test stays focused on the Constitution PASS → tool-runs
        # contract.
        patch(
            "agent.tool_executor.run_stop_kora_pre_flight",
            return_value=None,
        ),
        patch("run_agent.handle_function_call", return_value="file contents"),
    ):
        agent_with_isokron_stub._execute_tool_calls_sequential(
            msg, messages, "task-1"
        )

    # PASS verdict → no audit emit.
    mock_emit.assert_not_called()


# ---------------------------------------------------------------------------
# Fail-LOUD — audit emit failure propagates
# ---------------------------------------------------------------------------


def test_audit_emit_failure_propagates_through_executor(
    agent_with_isokron_stub,
):
    """When ``emit_constitution_audit_event`` raises, the executor must
    NOT swallow it — the denied call cannot be audited, so the agent
    refuses to proceed silently.
    """
    from agent.constitution_audit import ConstitutionAuditEmitError

    fail_verdict = PreScreenVerdict.fail(
        "denied",
        envelope=PreScreenEnvelope(
            tool_name="read_file",
            required_capability="cap_local_file_io",
            actor_id="kora",
            constitution_revision_id=None,
            rules_hash=None,
        ),
    )

    tc = _mock_tool_call(
        name="read_file", arguments="{}", call_id="c-emit-fail-1"
    )
    msg = _mock_assistant_msg(tool_calls=[tc])
    messages = []

    with (
        patch(
            "agent.tool_executor._run_constitution_pre_screen",
            return_value=fail_verdict,
        ),
        patch(
            "agent.tool_executor.emit_constitution_audit_event",
            side_effect=ConstitutionAuditEmitError(
                "kora.constitution.disagreement_raised",
                "read_file",
                "MCP dispatch tier down",
            ),
        ),
        patch("run_agent.handle_function_call") as mock_hfc,
        pytest.raises(ConstitutionAuditEmitError),
    ):
        agent_with_isokron_stub._execute_tool_calls_sequential(
            msg, messages, "task-1"
        )

    # Tool body never ran (audit failed before block_result was assembled).
    mock_hfc.assert_not_called()
