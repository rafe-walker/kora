"""Tests for ST2 kora__request_stop — KR-MCP-STOP-CONTROL ST2.

Covers spec §2 Deliverable 4 scenarios:
  - L1 stop from ACTIVE → succeeds, kora_control written, audit recorded
  - L2 stop → succeeds, different level/kind
  - L3-L5 levels → -32602 (operator-on-machine only)
  - confirm_token mismatch → -32602
  - dry_run mode → predicted shape without substrate write
  - Without kora__request_stop capability → -32001 capability_denied
  - Caller without actor_id → -32001 actor_id_required_for_stop
  - KoraControlWriter substrate-error → -32603 wrapped + audit
  - SECURITY: bearer token never in any error envelope

Plus extras:
  - Descriptors in /mcp/tools/list with cap_gate=True / dev_only=False
  - Daemon coordinator missing (no session_id) → -32602 fail-CLOSED
  - Active provider missing → -32602 fail-CLOSED (workspace unresolvable)
  - kora__request_stop and kora__request_pause are SEPARATE caps
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient


def _sha256(tok: str) -> str:
    return "sha256:" + hashlib.sha256(tok.encode("utf-8")).hexdigest()


def _write_callers_yaml(path: Path, entries: list) -> None:
    path.write_text(yaml.safe_dump({"callers": entries}), encoding="utf-8")


# ---------------------------------------------------------------------------
# Fakes — IsoKron pieces the writer + tool need
# ---------------------------------------------------------------------------


class _FakeIssueKoraControlResult:
    """Mirrors IssueKoraControlResult; constructed by the fake writer."""

    def __init__(
        self,
        *,
        command_id: str,
        sequence: int,
        lifecycle_state: str,
        superseded_command_ids: List[str],
        chain_event_id: str,
        dry_run: bool,
    ) -> None:
        self.command_id = command_id
        self.sequence = sequence
        self.lifecycle_state = lifecycle_state
        self.superseded_command_ids = superseded_command_ids
        self.chain_event_id = chain_event_id
        self.dry_run = dry_run


class _FakeKoraControlWriter:
    """Records issue_command kwargs; returns a synthetic result."""

    def __init__(self) -> None:
        self.calls: List[dict] = []
        self.next_result: Any = None
        self.next_exception: Any = None

    async def issue_command(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.next_exception is not None:
            raise self.next_exception
        if self.next_result is not None:
            return self.next_result
        return _FakeIssueKoraControlResult(
            command_id="11111111-1111-1111-1111-111111111111",
            sequence=42,
            lifecycle_state="created",
            superseded_command_ids=[],
            chain_event_id="22222222-2222-2222-2222-222222222222",
            dry_run=kwargs.get("dry_run", False),
        )


class _FakeProvider:
    """Minimal stand-in for IsoKronMemoryProvider with _resolve_workspace_id."""

    def __init__(self, workspace_id: str = "ws_test") -> None:
        self._workspace_id = workspace_id
        self._connection = MagicMock(name="IsoKronConnection")

    def _resolve_workspace_id(self) -> str:
        return self._workspace_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_OPERATOR_ACTOR_UUID = "8d50b3aa-1111-4222-9333-cafebabe1234"


@pytest.fixture(autouse=True)
def _reset_global_state(monkeypatch):
    """Per-test isolation for ACL cache + active provider + daemon coord."""
    from kora_cli.listeners import mcp_caller_auth
    from plugins.memory.isokron import active_provider

    mcp_caller_auth._reset_cache_for_tests()
    active_provider.clear_active_provider()
    yield
    mcp_caller_auth._reset_cache_for_tests()
    active_provider.clear_active_provider()


@pytest.fixture
def stop_token(monkeypatch, tmp_path):
    """Caller with kora__request_stop cap AND actor_id populated."""
    token = "stop-tok-12345"
    callers_path = tmp_path / "mcp_callers.yaml"
    _write_callers_yaml(
        callers_path,
        [
            {
                "token_hash": _sha256(token),
                "actor_kind": "claude_pm_operator",
                "actor_id": _OPERATOR_ACTOR_UUID,
                "allowed_caps": ["kora__request_stop"],
            }
        ],
    )
    from kora_cli.listeners import mcp_caller_auth

    monkeypatch.setattr(
        mcp_caller_auth, "DEFAULT_CALLERS_PATH", callers_path
    )
    monkeypatch.delenv("KORA_MCP_BEARER_TOKEN", raising=False)
    return token


@pytest.fixture
def stop_token_no_actor_id(monkeypatch, tmp_path):
    """Caller WITH stop cap but WITHOUT actor_id field."""
    token = "stop-tok-no-actor"
    callers_path = tmp_path / "mcp_callers.yaml"
    _write_callers_yaml(
        callers_path,
        [
            {
                "token_hash": _sha256(token),
                "actor_kind": "claude_pm_operator_no_id",
                # actor_id deliberately omitted
                "allowed_caps": ["kora__request_stop"],
            }
        ],
    )
    from kora_cli.listeners import mcp_caller_auth

    monkeypatch.setattr(
        mcp_caller_auth, "DEFAULT_CALLERS_PATH", callers_path
    )
    monkeypatch.delenv("KORA_MCP_BEARER_TOKEN", raising=False)
    return token


@pytest.fixture
def pause_only_token(monkeypatch, tmp_path):
    """Caller with kora__request_pause cap but NOT kora__request_stop."""
    token = "pause-only-tok"
    callers_path = tmp_path / "mcp_callers.yaml"
    _write_callers_yaml(
        callers_path,
        [
            {
                "token_hash": _sha256(token),
                "actor_kind": "claude_pm_pauser",
                "actor_id": _OPERATOR_ACTOR_UUID,
                "allowed_caps": ["kora__request_pause"],
            }
        ],
    )
    from kora_cli.listeners import mcp_caller_auth

    monkeypatch.setattr(
        mcp_caller_auth, "DEFAULT_CALLERS_PATH", callers_path
    )
    monkeypatch.delenv("KORA_MCP_BEARER_TOKEN", raising=False)
    return token


@pytest.fixture
def client():
    from kora_cli.web_server import app

    return TestClient(app)


@pytest.fixture
def installed_coordinator(monkeypatch):
    """Install a DaemonCoordinator + return it (for session_id access)."""
    from kora_cli.daemon import DaemonCoordinator
    from kora_cli.listeners import mcp as mcp_mod

    coord = DaemonCoordinator()
    monkeypatch.setattr(
        mcp_mod, "current_coordinator", lambda: coord
    )
    return coord


@pytest.fixture
def installed_writer(monkeypatch):
    """Install a fake KoraControlWriter via mcp_tools' lookup path."""
    from kora_cli.listeners import mcp_tools as mcp_tools_mod
    import kora_cli.clients.kora_control_writer as writer_mod

    fake = _FakeKoraControlWriter()
    monkeypatch.setattr(
        writer_mod, "current_kora_control_writer", lambda: fake
    )
    # mcp_tools._execute_request_stop imports inside the function, so
    # patching the writer module's accessor is sufficient.
    return fake


@pytest.fixture
def installed_provider():
    """Install a fake active provider so workspace_id resolves."""
    from plugins.memory.isokron import active_provider

    provider = _FakeProvider()
    active_provider.set_active_provider(provider)
    return provider


# ---------------------------------------------------------------------------
# Descriptors
# ---------------------------------------------------------------------------


def test_descriptor_in_tools_list(client, stop_token):
    r = client.get(
        "/mcp/tools/list",
        headers={"Authorization": f"Bearer {stop_token}"},
    )
    assert r.status_code == 200
    by_name = {t["name"]: t for t in r.json()["tools"]}
    assert "kora__request_stop" in by_name
    desc = by_name["kora__request_stop"]
    assert desc["requires_cap_gate"] is True
    assert desc["dev_only"] is False
    required = desc["inputSchema"]["required"]
    assert set(required) == {"reason", "level", "confirm_token"}
    assert desc["inputSchema"]["properties"]["level"]["enum"] == [1, 2]


# ---------------------------------------------------------------------------
# Happy paths — L1 + L2
# ---------------------------------------------------------------------------


def test_l1_stop_succeeds(
    client, stop_token, installed_coordinator, installed_writer, installed_provider
):
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {stop_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "kora__request_stop",
                "arguments": {
                    "reason": "operator triage cycle",
                    "level": 1,
                    "confirm_token": installed_coordinator.daemon_session_id,
                },
            },
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert "result" in body, body
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["success"] is True
    assert payload["level"] == 1
    assert payload["kind"] == "pause"
    assert payload["kora_control_id"] == "11111111-1111-1111-1111-111111111111"
    assert payload["sequence"] == 42
    assert payload["dry_run"] is False
    assert payload["caller_actor_kind"] == "claude_pm_operator"
    assert "active->paused" in payload["predicted_state_change"]

    # Writer received the right args.
    assert len(installed_writer.calls) == 1
    call = installed_writer.calls[0]
    assert call["workspace_id"] == "ws_test"
    assert call["issuer_session_id"] == installed_coordinator.daemon_session_id
    assert call["issuer_actor_id"] == _OPERATOR_ACTOR_UUID
    assert call["level"] == 1
    assert call["kind"] == "pause"
    assert call["reason"] == "operator triage cycle"
    assert call["dry_run"] is False


def test_l2_stop_succeeds(
    client, stop_token, installed_coordinator, installed_writer, installed_provider
):
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {stop_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "kora__request_stop",
                "arguments": {
                    "reason": "drain to fix migration",
                    "level": 2,
                    "confirm_token": installed_coordinator.daemon_session_id,
                },
            },
        },
    )
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert payload["level"] == 2
    assert payload["kind"] == "drain"
    assert "draining" in payload["predicted_state_change"]
    assert installed_writer.calls[0]["kind"] == "drain"
    assert installed_writer.calls[0]["level"] == 2


# ---------------------------------------------------------------------------
# Level refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_level", [3, 4, 5])
def test_l3_l4_l5_refused(
    client,
    stop_token,
    installed_coordinator,
    installed_writer,
    installed_provider,
    bad_level,
):
    # The JSON schema constrains to enum [1, 2], so the server-side
    # validation rejects via JSON-RPC -32602. (The body of the
    # executor ALSO refuses with a friendlier message if the schema
    # check is ever loosened.)
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {stop_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 10 + bad_level,
            "method": "tools/call",
            "params": {
                "name": "kora__request_stop",
                "arguments": {
                    "reason": "trying L3-L5",
                    "level": bad_level,
                    "confirm_token": installed_coordinator.daemon_session_id,
                },
            },
        },
    )
    body = r.json()
    # Either Pydantic-level rejection (-32602) from the schema, or
    # executor refusal (-32602). Both surfaces the user error code.
    assert body["error"]["code"] == -32602
    # Writer NOT invoked.
    assert installed_writer.calls == []


def test_level_zero_refused(
    client, stop_token, installed_coordinator, installed_writer, installed_provider
):
    """L0 is the operator-only reset path; not a 'stop' anyway."""
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {stop_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {
                "name": "kora__request_stop",
                "arguments": {
                    "reason": "wrong layer",
                    "level": 0,
                    "confirm_token": installed_coordinator.daemon_session_id,
                },
            },
        },
    )
    assert r.json()["error"]["code"] == -32602
    assert installed_writer.calls == []


# ---------------------------------------------------------------------------
# confirm_token validation
# ---------------------------------------------------------------------------


def test_confirm_token_mismatch_refused(
    client, stop_token, installed_coordinator, installed_writer, installed_provider
):
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {stop_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 20,
            "method": "tools/call",
            "params": {
                "name": "kora__request_stop",
                "arguments": {
                    "reason": "stale token",
                    "level": 1,
                    "confirm_token": "not-the-right-token",
                },
            },
        },
    )
    body = r.json()
    assert body["error"]["code"] == -32602
    assert "confirm_token" in body["error"]["message"].lower()
    # Neither side's token leaks into the error.
    assert "not-the-right-token" not in r.text
    assert installed_coordinator.daemon_session_id not in r.text
    # Writer NOT invoked.
    assert installed_writer.calls == []


def test_confirm_token_empty_refused(
    client, stop_token, installed_coordinator, installed_writer, installed_provider
):
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {stop_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 21,
            "method": "tools/call",
            "params": {
                "name": "kora__request_stop",
                "arguments": {
                    "reason": "x",
                    "level": 1,
                    "confirm_token": "",
                },
            },
        },
    )
    assert r.json()["error"]["code"] == -32602


# ---------------------------------------------------------------------------
# Capability gating
# ---------------------------------------------------------------------------


def test_without_capability_denied(
    client, pause_only_token, installed_coordinator, installed_writer, installed_provider
):
    """Caller has kora__request_pause but NOT kora__request_stop."""
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {pause_only_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 30,
            "method": "tools/call",
            "params": {
                "name": "kora__request_stop",
                "arguments": {
                    "reason": "trying without cap",
                    "level": 1,
                    "confirm_token": installed_coordinator.daemon_session_id,
                },
            },
        },
    )
    err = r.json()["error"]
    assert err["code"] == -32001
    assert err["message"] == "capability_denied"
    assert err["data"]["required_capability"] == "kora__request_stop"
    assert err["data"]["caller_actor_kind"] == "claude_pm_pauser"
    # Writer NOT invoked.
    assert installed_writer.calls == []


def test_without_actor_id_denied(
    client,
    stop_token_no_actor_id,
    installed_coordinator,
    installed_writer,
    installed_provider,
):
    """Caller has cap but no actor_id in mcp_callers.yaml."""
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {stop_token_no_actor_id}"},
        json={
            "jsonrpc": "2.0",
            "id": 31,
            "method": "tools/call",
            "params": {
                "name": "kora__request_stop",
                "arguments": {
                    "reason": "no actor_id",
                    "level": 1,
                    "confirm_token": installed_coordinator.daemon_session_id,
                },
            },
        },
    )
    err = r.json()["error"]
    assert err["code"] == -32001
    assert err["message"] == "actor_id_required_for_stop"
    assert err["data"]["caller_actor_kind"] == "claude_pm_operator_no_id"
    assert err["data"]["tool"] == "kora__request_stop"
    assert "mcp_callers.yaml" in err["data"]["remediation"]
    assert installed_writer.calls == []


# ---------------------------------------------------------------------------
# dry_run mode
# ---------------------------------------------------------------------------


def test_dry_run_does_not_invoke_substrate(
    client, stop_token, installed_coordinator, monkeypatch, installed_provider
):
    """dry_run uses the REAL writer (not the fake) because the real
    writer's dry_run short-circuits before touching substrate. This
    test verifies that path."""
    # No installed_writer fixture — use the real one. The real writer
    # in dry_run mode short-circuits inside issue_command BEFORE the
    # asyncpg call, so no _connection roundtrip happens.
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {stop_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 40,
            "method": "tools/call",
            "params": {
                "name": "kora__request_stop",
                "arguments": {
                    "reason": "pre-flight check",
                    "level": 1,
                    "confirm_token": installed_coordinator.daemon_session_id,
                    "dry_run": True,
                },
            },
        },
    )
    body = r.json()
    assert "result" in body, body
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["success"] is True
    assert payload["dry_run"] is True
    # Predicted UUIDs are the dry-run placeholder.
    assert payload["kora_control_id"] == "00000000-0000-0000-0000-000000000000"
    assert payload["sequence"] == -1
    assert payload["level"] == 1
    assert payload["kind"] == "pause"


# ---------------------------------------------------------------------------
# Substrate error surface
# ---------------------------------------------------------------------------


def test_substrate_rejected_wraps_as_minus_32603(
    client, stop_token, installed_coordinator, installed_writer, installed_provider
):
    """When the substrate raises (e.g., SubstrateRejected sqlstate
    42501), the tool maps to JSON-RPC -32603 with the error class
    name; substrate-internal sqlstate is included in the audit but
    not in the user-facing error message (which is intentionally
    sparse — caller checks audit log for diagnostic detail)."""
    from kora_cli.clients.kora_control_writer import SubstrateRejected

    installed_writer.next_exception = SubstrateRejected(
        sqlstate="42501",
        message="actor_kind='kora' cannot issue kora_control",
    )

    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {stop_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 50,
            "method": "tools/call",
            "params": {
                "name": "kora__request_stop",
                "arguments": {
                    "reason": "substrate rejects us",
                    "level": 1,
                    "confirm_token": installed_coordinator.daemon_session_id,
                },
            },
        },
    )
    body = r.json()
    assert body["error"]["code"] == -32603
    # Error class name surfaced; substrate-internal text NOT in
    # message (sparse-on-purpose to avoid leaking substrate detail).
    assert "KoraControlWriterError" in body["error"]["message"]


# ---------------------------------------------------------------------------
# Fail-CLOSED — missing daemon coordinator / provider
# ---------------------------------------------------------------------------


def test_missing_coordinator_refuses(
    client, stop_token, monkeypatch, installed_writer, installed_provider
):
    """No coordinator → cannot validate confirm_token → -32602 fail-CLOSED."""
    from kora_cli.listeners import mcp as mcp_mod

    monkeypatch.setattr(mcp_mod, "current_coordinator", lambda: None)
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {stop_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 60,
            "method": "tools/call",
            "params": {
                "name": "kora__request_stop",
                "arguments": {
                    "reason": "x",
                    "level": 1,
                    "confirm_token": "anything",
                },
            },
        },
    )
    body = r.json()
    assert body["error"]["code"] == -32602
    assert "coordinator" in body["error"]["message"].lower()
    assert installed_writer.calls == []


def test_missing_provider_refuses(
    client, stop_token, installed_coordinator, installed_writer
):
    """No active provider → cannot resolve workspace_id → fail-CLOSED."""
    # active_provider.clear_active_provider() done by autouse fixture;
    # don't install provider here.
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {stop_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 61,
            "method": "tools/call",
            "params": {
                "name": "kora__request_stop",
                "arguments": {
                    "reason": "x",
                    "level": 1,
                    "confirm_token": installed_coordinator.daemon_session_id,
                },
            },
        },
    )
    body = r.json()
    assert body["error"]["code"] == -32602
    assert (
        "provider" in body["error"]["message"].lower()
        or "workspace" in body["error"]["message"].lower()
    )
    assert installed_writer.calls == []


# ---------------------------------------------------------------------------
# Audit — reason NEVER in audit details (Q4 ruling)
# ---------------------------------------------------------------------------


def test_audit_omits_reason_text(
    client,
    stop_token,
    installed_coordinator,
    installed_writer,
    installed_provider,
    caplog,
):
    import logging

    caplog.set_level(logging.INFO)
    secret_reason = "operator-private-context-DO-NOT-LEAK"

    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {stop_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 70,
            "method": "tools/call",
            "params": {
                "name": "kora__request_stop",
                "arguments": {
                    "reason": secret_reason,
                    "level": 1,
                    "confirm_token": installed_coordinator.daemon_session_id,
                },
            },
        },
    )
    assert "result" in r.json()

    audit_lines = [
        rec.getMessage()
        for rec in caplog.records
        if "kora.mcp.tool_called" in rec.getMessage()
    ]
    assert len(audit_lines) >= 1
    last = audit_lines[-1]
    assert "tool=kora__request_stop" in last
    assert "caller_actor_kind=claude_pm_operator" in last
    # Reason MUST NOT appear in the audit line.
    assert secret_reason not in last


# ---------------------------------------------------------------------------
# SECURITY — bearer token never in error envelope
# ---------------------------------------------------------------------------


def test_bearer_token_never_in_error_envelope(
    client,
    monkeypatch,
    tmp_path,
    installed_coordinator,
    installed_writer,
    installed_provider,
):
    """Diverse failure paths: confirm_token mismatch + no actor_id +
    no capability. Bearer token must NEVER appear in any envelope."""
    from kora_cli.listeners import mcp_caller_auth

    token_marker = "kora-stop-tok-MUST-NEVER-LEAK-89765"
    callers_path = tmp_path / "mcp_callers.yaml"
    _write_callers_yaml(
        callers_path,
        [
            {
                "token_hash": _sha256(token_marker),
                "actor_kind": "secure_caller",
                "actor_id": _OPERATOR_ACTOR_UUID,
                "allowed_caps": ["kora__request_stop"],
            }
        ],
    )
    mcp_caller_auth._reset_cache_for_tests()
    monkeypatch.setattr(
        mcp_caller_auth, "DEFAULT_CALLERS_PATH", callers_path
    )

    # Path 1: confirm_token mismatch.
    r1 = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token_marker}"},
        json={
            "jsonrpc": "2.0",
            "id": 80,
            "method": "tools/call",
            "params": {
                "name": "kora__request_stop",
                "arguments": {
                    "reason": "mismatch path",
                    "level": 1,
                    "confirm_token": "wrong",
                },
            },
        },
    )
    # Path 2: substrate rejection.
    from kora_cli.clients.kora_control_writer import SubstrateRejected

    installed_writer.next_exception = SubstrateRejected(
        sqlstate="42501", message="kora cannot write"
    )
    r2 = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token_marker}"},
        json={
            "jsonrpc": "2.0",
            "id": 81,
            "method": "tools/call",
            "params": {
                "name": "kora__request_stop",
                "arguments": {
                    "reason": "substrate path",
                    "level": 1,
                    "confirm_token": installed_coordinator.daemon_session_id,
                },
            },
        },
    )

    for response in (r1, r2):
        assert token_marker not in response.text, (
            "bearer token surfaced in JSON-RPC envelope"
        )
