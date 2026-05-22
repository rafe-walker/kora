"""Tests for ST2 mutating tools + capability gating —
KR-MCP-RUNTIME-SURFACE ST2.

Covers:
  - All 3 new tools surface in tools/list with requires_cap_gate=True
  - kora__send_webhook_test_event surfaces dev_only=true
  - Capability denial returns JSON-RPC -32001 with required_capability
    + caller_actor_kind in error data
  - Anonymous Mode-1 callers CANNOT invoke mutating tools (no caps)
  - ST1 read-only tools remain ungated for both anonymous + identified
    callers
  - kora__request_state_transition: valid transition + invalid target +
    invalid TRANSITION_TABLE edge
  - kora__create_sea_ticket: forwards to IsoKronMCPClient.invoke +
    surfaces returned ticket_id + audit-logs the call
  - kora__send_webhook_test_event: refuses on KORA_DEPLOY_ENV=prd with
    -32001 dev_only; works in dev/stg
  - Audit log line emitted with caller actor_kind + tool name
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from kora_cli.listeners import mcp_caller_auth


def _sha256(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def _write_callers_yaml(path: Path, entries: list) -> None:
    path.write_text(yaml.safe_dump({"callers": entries}), encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_caller_cache():
    mcp_caller_auth._reset_cache_for_tests()
    yield
    mcp_caller_auth._reset_cache_for_tests()


@pytest.fixture
def fully_authorized_caller_token(monkeypatch, tmp_path):
    """Set up a yaml caller with ALL ST2 mutating-tool caps allowed.

    Returns the bearer token string the test client should present.
    """
    token = "fully-auth-tok"
    callers_path = tmp_path / "mcp_callers.yaml"
    _write_callers_yaml(
        callers_path,
        [
            {
                "token_hash": _sha256(token),
                "actor_kind": "claude_pm_isokron",
                "allowed_caps": [
                    "kora__request_state_transition",
                    "kora__create_sea_ticket",
                    "kora__send_webhook_test_event",
                ],
            }
        ],
    )
    monkeypatch.setattr(
        mcp_caller_auth, "DEFAULT_CALLERS_PATH", callers_path
    )
    monkeypatch.delenv("KORA_MCP_BEARER_TOKEN", raising=False)
    return token


@pytest.fixture
def no_caps_caller_token(monkeypatch, tmp_path):
    """A yaml caller with an empty allowed_caps list."""
    token = "no-caps-tok"
    callers_path = tmp_path / "mcp_callers.yaml"
    _write_callers_yaml(
        callers_path,
        [
            {
                "token_hash": _sha256(token),
                "actor_kind": "read_only_caller",
                "allowed_caps": [],
            }
        ],
    )
    monkeypatch.setattr(
        mcp_caller_auth, "DEFAULT_CALLERS_PATH", callers_path
    )
    monkeypatch.delenv("KORA_MCP_BEARER_TOKEN", raising=False)
    return token


@pytest.fixture
def anonymous_env_token(monkeypatch, tmp_path):
    """Mode-1 env-only token → anonymous caller (no caps)."""
    monkeypatch.setattr(
        mcp_caller_auth,
        "DEFAULT_CALLERS_PATH",
        tmp_path / "nonexistent.yaml",
    )
    monkeypatch.setenv("KORA_MCP_BEARER_TOKEN", "anon-tok")
    return "anon-tok"


@pytest.fixture
def client():
    from kora_cli.web_server import app

    return TestClient(app)


# ---------------------------------------------------------------------------
# tools/list — descriptor flags
# ---------------------------------------------------------------------------


def test_tools_list_includes_st2_tools(client, fully_authorized_caller_token):
    r = client.get(
        "/mcp/tools/list",
        headers={"Authorization": f"Bearer {fully_authorized_caller_token}"},
    )
    assert r.status_code == 200
    names = [t["name"] for t in r.json()["tools"]]
    for new_tool in (
        "kora__request_state_transition",
        "kora__create_sea_ticket",
        "kora__send_webhook_test_event",
    ):
        assert new_tool in names


def test_st2_tools_descriptors_carry_cap_gate_flag(
    client, fully_authorized_caller_token
):
    r = client.get(
        "/mcp/tools/list",
        headers={"Authorization": f"Bearer {fully_authorized_caller_token}"},
    )
    by_name = {t["name"]: t for t in r.json()["tools"]}
    for tname in (
        "kora__request_state_transition",
        "kora__create_sea_ticket",
        "kora__send_webhook_test_event",
    ):
        assert by_name[tname]["requires_cap_gate"] is True


def test_send_webhook_test_event_descriptor_dev_only(
    client, fully_authorized_caller_token
):
    r = client.get(
        "/mcp/tools/list",
        headers={"Authorization": f"Bearer {fully_authorized_caller_token}"},
    )
    by_name = {t["name"]: t for t in r.json()["tools"]}
    assert by_name["kora__send_webhook_test_event"]["dev_only"] is True
    # The other 2 mutating tools are NOT dev-only.
    assert by_name["kora__create_sea_ticket"]["dev_only"] is False
    assert by_name["kora__request_state_transition"]["dev_only"] is False


def test_st1_read_tools_descriptors_not_cap_gated(
    client, fully_authorized_caller_token
):
    r = client.get(
        "/mcp/tools/list",
        headers={"Authorization": f"Bearer {fully_authorized_caller_token}"},
    )
    by_name = {t["name"]: t for t in r.json()["tools"]}
    for tname in (
        "kora__daemon_status",
        "kora__get_operational_state",
        "kora__get_health_rollup",
        "kora__get_recent_ledger_entries",
        "kora__get_recent_chain_events",
        "kora__list_active_sea_tickets",
    ):
        assert by_name[tname].get("requires_cap_gate", False) is False


# ---------------------------------------------------------------------------
# Capability denial
# ---------------------------------------------------------------------------


def test_anonymous_caller_denied_on_mutating_tool(
    client, anonymous_env_token
):
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {anonymous_env_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "kora__create_sea_ticket",
                "arguments": {"title": "hello"},
            },
        },
    )
    assert r.status_code == 200
    err = r.json()["error"]
    assert err["code"] == -32001
    assert err["message"] == "capability_denied"
    assert err["data"]["required_capability"] == "kora__create_sea_ticket"
    assert err["data"]["caller_actor_kind"] == "anonymous"


def test_no_caps_caller_denied_on_mutating_tool(
    client, no_caps_caller_token
):
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {no_caps_caller_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "kora__request_state_transition",
                "arguments": {"target_state": "paused", "reason": "test"},
            },
        },
    )
    err = r.json()["error"]
    assert err["code"] == -32001
    assert err["data"]["caller_actor_kind"] == "read_only_caller"


def test_anonymous_caller_can_invoke_read_only_tool(
    client, anonymous_env_token, monkeypatch
):
    """ST1 read-only tools are NOT cap-gated by default — Mode-1
    anonymous can still call them."""
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)

    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {anonymous_env_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "kora__get_recent_ledger_entries"},
        },
    )
    assert r.status_code == 200
    assert "result" in r.json()


# ---------------------------------------------------------------------------
# kora__request_state_transition
# ---------------------------------------------------------------------------


def test_request_state_transition_valid(
    client, fully_authorized_caller_token, monkeypatch
):
    from agent.operational_state import (
        ClaimPermission,
        OperationalState,
        PrimaryState,
    )
    from agent.operational_state_holder import OperationalStateHolder
    from agent import operational_state_holder as h_mod

    initial = OperationalState(
        primary_state=PrimaryState.READY,
        claim_permission=ClaimPermission.NORMAL,
    )
    monkeypatch.setattr(h_mod, "_HOLDER", OperationalStateHolder(initial))

    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {fully_authorized_caller_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "kora__request_state_transition",
                "arguments": {
                    "target_state": "paused",
                    "reason": "operator pause from PR",
                },
            },
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert "result" in body
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["success"] is True
    assert payload["from_state"] == "ready"
    assert payload["to_state"] == "paused"
    assert payload["caller_actor_kind"] == "claude_pm_isokron"


def test_request_state_transition_invalid_target_state(
    client, fully_authorized_caller_token, monkeypatch
):
    from agent.operational_state import OperationalState, PrimaryState
    from agent.operational_state_holder import OperationalStateHolder
    from agent import operational_state_holder as h_mod

    monkeypatch.setattr(
        h_mod,
        "_HOLDER",
        OperationalStateHolder(
            OperationalState(primary_state=PrimaryState.READY)
        ),
    )

    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {fully_authorized_caller_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "kora__request_state_transition",
                "arguments": {"target_state": "WHATEVER", "reason": "x"},
            },
        },
    )
    body = r.json()
    assert body["error"]["code"] == -32602
    assert "WHATEVER" in body["error"]["message"]


def test_request_state_transition_invalid_table_edge(
    client, fully_authorized_caller_token, monkeypatch
):
    """A valid PrimaryState that's NOT a valid edge from current
    state → caller gets -32603 with the InvalidStateTransitionError
    message."""
    from agent.operational_state import OperationalState, PrimaryState
    from agent.operational_state_holder import OperationalStateHolder
    from agent import operational_state_holder as h_mod

    monkeypatch.setattr(
        h_mod,
        "_HOLDER",
        OperationalStateHolder(
            OperationalState(primary_state=PrimaryState.STOPPED)
        ),
    )

    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {fully_authorized_caller_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "kora__request_state_transition",
                "arguments": {"target_state": "active", "reason": "noop"},
            },
        },
    )
    body = r.json()
    # STOPPED → ACTIVE is not in the transition table → tool raises
    # InvalidStateTransitionError → mcp.py maps to -32603.
    assert body["error"]["code"] == -32603


# ---------------------------------------------------------------------------
# kora__create_sea_ticket
# ---------------------------------------------------------------------------


def test_create_sea_ticket_no_provider(
    client, fully_authorized_caller_token, monkeypatch
):
    """No active provider → tool returns -32602 (invalid params with
    'no active IsoKron provider' message)."""
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)

    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {fully_authorized_caller_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "kora__create_sea_ticket",
                "arguments": {"title": "test"},
            },
        },
    )
    body = r.json()
    assert body["error"]["code"] == -32602
    assert "provider" in body["error"]["message"].lower()


def test_create_sea_ticket_forwards_to_substrate(
    client, fully_authorized_caller_token, monkeypatch
):
    from kora_cli.listeners import mcp_tools

    mock_invoke = AsyncMock(
        return_value={"ticket_id": "tkt-xyz-001", "status": "created"}
    )
    mock_client = MagicMock()
    mock_client.invoke = mock_invoke
    mock_connection = MagicMock()
    mock_connection.get_mcp_client.return_value = mock_client
    mock_provider = MagicMock()
    mock_provider._connection = mock_connection
    monkeypatch.setattr(
        mcp_tools, "_get_active_provider", lambda: mock_provider
    )

    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {fully_authorized_caller_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "kora__create_sea_ticket",
                "arguments": {
                    "title": "Help with deployment",
                    "body": "Please review the new deploy",
                    "priority": "normal",
                },
            },
        },
    )
    body = r.json()
    assert "result" in body
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["success"] is True
    assert payload["ticket_id"] == "tkt-xyz-001"
    assert payload["caller_actor_kind"] == "claude_pm_isokron"

    # Verify the substrate invoke was called with kind=sea injected.
    mock_invoke.assert_awaited_once()
    call_args = mock_invoke.await_args
    assert call_args[0][0] == "sea__create_ticket"
    forwarded = call_args[0][1]
    assert forwarded["title"] == "Help with deployment"
    assert forwarded["kind"] == "sea"
    assert forwarded["origin_actor_kind"] == "claude_pm_isokron"


def test_create_sea_ticket_empty_title_rejected(
    client, fully_authorized_caller_token, monkeypatch
):
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {fully_authorized_caller_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "kora__create_sea_ticket",
                "arguments": {"title": "   "},
            },
        },
    )
    body = r.json()
    assert body["error"]["code"] == -32602
    assert "title" in body["error"]["message"].lower()


# ---------------------------------------------------------------------------
# kora__send_webhook_test_event
# ---------------------------------------------------------------------------


def test_webhook_test_event_refuses_on_prd(
    client, fully_authorized_caller_token, monkeypatch
):
    monkeypatch.setenv("KORA_DEPLOY_ENV", "prd")
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {fully_authorized_caller_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "kora__send_webhook_test_event",
                "arguments": {
                    "endpoint": "slack",
                    "payload": {"type": "url_verification"},
                },
            },
        },
    )
    body = r.json()
    assert body["error"]["code"] == -32001
    assert "dev_only" in body["error"]["message"]


def test_webhook_test_event_works_in_dev(
    client, fully_authorized_caller_token, monkeypatch
):
    monkeypatch.setenv("KORA_DEPLOY_ENV", "dev")
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {fully_authorized_caller_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "kora__send_webhook_test_event",
                "arguments": {
                    "endpoint": "slack",
                    "payload": {"type": "url_verification"},
                },
            },
        },
    )
    body = r.json()
    assert "result" in body
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["success"] is True
    assert payload["endpoint"] == "slack"
    assert payload["deploy_env"] == "dev"


def test_webhook_test_event_works_in_staging(
    client, fully_authorized_caller_token, monkeypatch
):
    """Staging env (KORA_DEPLOY_ENV=stg) also works — only prd refuses."""
    monkeypatch.setenv("KORA_DEPLOY_ENV", "stg")
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {fully_authorized_caller_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {
                "name": "kora__send_webhook_test_event",
                "arguments": {
                    "endpoint": "email",
                    "payload": {"from": "alice@example.com"},
                },
            },
        },
    )
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert payload["deploy_env"] == "stg"


def test_webhook_test_event_invalid_endpoint(
    client, fully_authorized_caller_token, monkeypatch
):
    monkeypatch.setenv("KORA_DEPLOY_ENV", "dev")
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {fully_authorized_caller_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 13,
            "method": "tools/call",
            "params": {
                "name": "kora__send_webhook_test_event",
                "arguments": {
                    "endpoint": "telegram",  # not slack/email
                    "payload": {},
                },
            },
        },
    )
    body = r.json()
    assert body["error"]["code"] == -32602


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------


def test_audit_log_emitted_on_successful_call(
    client, fully_authorized_caller_token, monkeypatch, caplog
):
    """Successful mutating call → [kora.mcp.tool_called] log line
    with the caller actor_kind."""
    caplog.set_level(logging.INFO)
    from agent.operational_state import OperationalState, PrimaryState
    from agent.operational_state_holder import OperationalStateHolder
    from agent import operational_state_holder as h_mod

    monkeypatch.setattr(
        h_mod,
        "_HOLDER",
        OperationalStateHolder(
            OperationalState(primary_state=PrimaryState.READY)
        ),
    )

    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {fully_authorized_caller_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 14,
            "method": "tools/call",
            "params": {
                "name": "kora__request_state_transition",
                "arguments": {"target_state": "paused", "reason": "audit"},
            },
        },
    )
    assert "result" in r.json()
    audit_lines = [
        r for r in caplog.records if "kora.mcp.tool_called" in r.getMessage()
    ]
    assert len(audit_lines) >= 1
    msg = audit_lines[-1].getMessage()
    assert "tool=kora__request_state_transition" in msg
    assert "caller_actor_kind=claude_pm_isokron" in msg
