"""Unit tests for ``agent/constitution_audit.py`` (KR-P2-A ST3).

Covers:
- Disagreement / escalation event-type selection by verdict outcome
- Payload shape (disagreement carries policy snapshot; escalation
  carries operator-actionable context)
- Fail-LOUD policy: missing provider / connection / workspace_id /
  substrate emit failure → raises ``ConstitutionAuditEmitError``
- PASS verdict is a no-op
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.constitution_audit import (
    CONSTITUTION_DISAGREEMENT_EVENT,
    ConstitutionAuditEmitError,
    ESCALATION_KIND_CONSTITUTION_INCONCLUSIVE,
    ESCALATION_REQUESTED_EVENT,
    _build_payload,
    emit_constitution_audit_event,
)
from agent.constitution_pre_screen import (
    PreScreenEnvelope,
    PreScreenVerdict,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_envelope(
    *,
    required_capability: str = "cap_local_file_io",
    actor_id: str = "kora",
    revision_id: str | None = "rev-uuid-abc",
    rules_hash: str | None = "deadbeef1234",
) -> PreScreenEnvelope:
    return PreScreenEnvelope(
        tool_name="read_file",
        required_capability=required_capability,
        actor_id=actor_id,
        constitution_revision_id=revision_id,
        rules_hash=rules_hash,
    )


def _make_provider(
    *,
    workspace_id_returned: str | None = "ws-1",
    connection=None,
) -> SimpleNamespace:
    if connection is None:
        connection = SimpleNamespace(
            get_mcp_client=MagicMock(return_value="fake-mcp-client"),
            submit_and_wait=MagicMock(return_value="evt-001"),
        )
    return SimpleNamespace(
        _resolve_workspace_id=lambda: workspace_id_returned,
        _connection=connection,
    )


def _make_agent(*, provider) -> SimpleNamespace:
    if provider is None:
        memory_manager = SimpleNamespace(get_provider=lambda name: None)
    else:
        memory_manager = SimpleNamespace(
            get_provider=lambda name: provider if name == "isokron" else None,
        )
    return SimpleNamespace(_memory_manager=memory_manager)


# ---------------------------------------------------------------------------
# Happy paths — event_type + payload shape
# ---------------------------------------------------------------------------


def test_fail_emits_disagreement_event_with_policy_payload():
    envelope = _make_envelope()
    verdict = PreScreenVerdict.fail("denied", envelope=envelope)
    provider = _make_provider()
    agent = _make_agent(provider=provider)
    emit_constitution_audit_event(
        agent, "read_file", {"path": "/etc/hosts"}, verdict
    )
    provider._connection.submit_and_wait.assert_called_once()
    coro_arg = provider._connection.submit_and_wait.call_args[0][0]
    # The submitted coroutine is emit_kora_event(...) — close it so we
    # don't leak "never awaited" warnings (the real function is patched
    # in other tests; here we passed the real one but never awaited the
    # coroutine because submit_and_wait was mocked).
    coro_arg.close()


def test_inconclusive_emits_escalation_event():
    verdict = PreScreenVerdict.inconclusive("operator must adjudicate")
    provider = _make_provider()
    agent = _make_agent(provider=provider)
    emit_constitution_audit_event(agent, "read_file", {}, verdict)
    provider._connection.submit_and_wait.assert_called_once()
    coro_arg = provider._connection.submit_and_wait.call_args[0][0]
    coro_arg.close()


def test_pass_verdict_is_no_op():
    verdict = PreScreenVerdict.pass_(_make_envelope())
    provider = _make_provider()
    agent = _make_agent(provider=provider)
    emit_constitution_audit_event(agent, "read_file", {}, verdict)
    provider._connection.submit_and_wait.assert_not_called()


# ---------------------------------------------------------------------------
# Payload-shape unit tests (synchronous _build_payload)
# ---------------------------------------------------------------------------


def test_build_payload_disagreement_carries_policy_snapshot():
    envelope = _make_envelope(
        required_capability="cap_local_shell_exec",
        revision_id="rev-xyz",
        rules_hash="abc123",
    )
    verdict = PreScreenVerdict.fail("kora lacks cap", envelope=envelope)
    payload = _build_payload(
        CONSTITUTION_DISAGREEMENT_EVENT,
        "terminal",
        {"command": "rm -rf /"},
        verdict,
    )
    assert payload == {
        "tool_name": "terminal",
        "denied_capability": "cap_local_shell_exec",
        "active_constitution_revision_id": "rev-xyz",
        "rules_hash": "abc123",
        "actor_id": "kora",
        "snapshot_args": {"command": "rm -rf /"},
        "reason": "kora lacks cap",
    }


def test_build_payload_escalation_carries_operator_context():
    verdict = PreScreenVerdict.inconclusive(
        "tool 'mystery_tool' has no entry in TOOL_CAPABILITY_MAP"
    )
    payload = _build_payload(
        ESCALATION_REQUESTED_EVENT, "mystery_tool", {"x": 1}, verdict
    )
    assert payload == {
        "tool_name": "mystery_tool",
        "escalation_kind": ESCALATION_KIND_CONSTITUTION_INCONCLUSIVE,
        "escalation_reason": (
            "tool 'mystery_tool' has no entry in TOOL_CAPABILITY_MAP"
        ),
        "actor_id": "kora",
        "snapshot_args": {"x": 1},
    }


def test_build_payload_handles_envelope_none_for_inconclusive():
    """INCONCLUSIVE verdicts (e.g. unmapped tool) carry no envelope."""
    verdict = PreScreenVerdict.inconclusive("no provider loaded")
    payload = _build_payload(
        ESCALATION_REQUESTED_EVENT, "read_file", None, verdict
    )
    # actor_id falls back to the implicit Kora actor.
    assert payload["actor_id"] == "kora"
    assert payload["snapshot_args"] == {}


def test_build_payload_disagreement_with_partial_envelope_keeps_nulls():
    envelope = PreScreenEnvelope(
        tool_name="x",
        required_capability="cap_y",
        actor_id="kora",
        constitution_revision_id=None,  # fresh workspace
        rules_hash=None,
    )
    verdict = PreScreenVerdict.fail("denied", envelope=envelope)
    payload = _build_payload(
        CONSTITUTION_DISAGREEMENT_EVENT, "x", {}, verdict
    )
    assert payload["active_constitution_revision_id"] is None
    assert payload["rules_hash"] is None
    assert payload["denied_capability"] == "cap_y"


# ---------------------------------------------------------------------------
# Fail-LOUD — every infra failure raises ConstitutionAuditEmitError
# ---------------------------------------------------------------------------


def test_raises_when_provider_missing():
    verdict = PreScreenVerdict.fail("denied", envelope=_make_envelope())
    agent = _make_agent(provider=None)
    with pytest.raises(ConstitutionAuditEmitError) as exc_info:
        emit_constitution_audit_event(agent, "read_file", {}, verdict)
    err = exc_info.value
    assert err.event_type == CONSTITUTION_DISAGREEMENT_EVENT
    assert err.tool_name == "read_file"
    assert "IsoKron provider not loaded" in str(err)


def test_raises_when_memory_manager_is_none():
    verdict = PreScreenVerdict.fail("denied", envelope=_make_envelope())
    agent = SimpleNamespace(_memory_manager=None)
    with pytest.raises(ConstitutionAuditEmitError) as exc_info:
        emit_constitution_audit_event(agent, "read_file", {}, verdict)
    assert "IsoKron provider not loaded" in str(exc_info.value)


def test_raises_when_workspace_id_resolution_raises():
    def _boom():
        raise RuntimeError("provider not initialized")

    provider = SimpleNamespace(
        _resolve_workspace_id=_boom,
        _connection=SimpleNamespace(),
    )
    agent = _make_agent(provider=provider)
    verdict = PreScreenVerdict.fail("denied", envelope=_make_envelope())
    with pytest.raises(ConstitutionAuditEmitError) as exc_info:
        emit_constitution_audit_event(agent, "read_file", {}, verdict)
    assert "workspace_id resolution raised" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, RuntimeError)


def test_raises_when_workspace_id_is_empty():
    provider = _make_provider(workspace_id_returned="")
    agent = _make_agent(provider=provider)
    verdict = PreScreenVerdict.fail("denied", envelope=_make_envelope())
    with pytest.raises(ConstitutionAuditEmitError) as exc_info:
        emit_constitution_audit_event(agent, "read_file", {}, verdict)
    assert "workspace_id" in str(exc_info.value)


def test_raises_when_workspace_id_is_none():
    provider = _make_provider(workspace_id_returned=None)
    agent = _make_agent(provider=provider)
    verdict = PreScreenVerdict.fail("denied", envelope=_make_envelope())
    with pytest.raises(ConstitutionAuditEmitError):
        emit_constitution_audit_event(agent, "read_file", {}, verdict)


def test_raises_when_connection_is_none():
    provider = SimpleNamespace(
        _resolve_workspace_id=lambda: "ws-1",
        _connection=None,
    )
    agent = _make_agent(provider=provider)
    verdict = PreScreenVerdict.fail("denied", envelope=_make_envelope())
    with pytest.raises(ConstitutionAuditEmitError) as exc_info:
        emit_constitution_audit_event(agent, "read_file", {}, verdict)
    assert "connection not initialized" in str(exc_info.value)


def test_raises_when_submit_and_wait_raises():
    def _boom_submit(coro, *, timeout):
        coro.close()  # don't leak the un-awaited coroutine
        raise RuntimeError("MCP dispatch tier down")

    connection = SimpleNamespace(
        get_mcp_client=MagicMock(return_value="fake-mcp-client"),
        submit_and_wait=MagicMock(side_effect=_boom_submit),
    )
    provider = _make_provider(connection=connection)
    agent = _make_agent(provider=provider)
    verdict = PreScreenVerdict.fail("denied", envelope=_make_envelope())
    with pytest.raises(ConstitutionAuditEmitError) as exc_info:
        emit_constitution_audit_event(agent, "read_file", {}, verdict)
    assert "substrate emit raised" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Logging on success
# ---------------------------------------------------------------------------


def test_logs_info_on_successful_emit(caplog):
    import logging as _logging

    verdict = PreScreenVerdict.inconclusive("unknown tool")
    provider = _make_provider()
    agent = _make_agent(provider=provider)
    with caplog.at_level(_logging.INFO, logger="agent.constitution_audit"):
        emit_constitution_audit_event(agent, "read_file", {}, verdict)
    coro = provider._connection.submit_and_wait.call_args[0][0]
    coro.close()
    assert any(
        "[kora.constitution.audit]" in rec.getMessage()
        and "read_file" in rec.getMessage()
        and "evt-001" in rec.getMessage()
        for rec in caplog.records
    )
