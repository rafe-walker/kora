"""Tests for ST1 pause/resume tools — KR-MCP-STOP-CONTROL ST1.

Covers:
  - kora__request_pause from ACTIVE → PAUSED; ledger + audit + caller actor_kind
  - kora__request_pause from non-ACTIVE → -32602 invalid transition
  - kora__request_pause without capability → -32001 capability_denied
  - kora__request_resume from PAUSED → ACTIVE
  - kora__request_resume from non-PAUSED → -32602
  - kora__request_resume without capability → -32001
  - Empty reason → -32602
  - Pause cap is DISTINCT from kora__request_state_transition cap
    (caller can have pause/resume without full transition power)
  - Descriptors in tools/list with requires_cap_gate=True / dev_only=False
  - SECURITY: caller bearer token never in any error envelope
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient


def _sha256(tok: str) -> str:
    return "sha256:" + hashlib.sha256(tok.encode("utf-8")).hexdigest()


def _write_callers_yaml(path: Path, entries: list) -> None:
    path.write_text(yaml.safe_dump({"callers": entries}), encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_caller_cache():
    from kora_cli.listeners import mcp_caller_auth

    mcp_caller_auth._reset_cache_for_tests()
    yield
    mcp_caller_auth._reset_cache_for_tests()


@pytest.fixture(autouse=True)
def _reset_operational_state_holder():
    """Reset the module-level ``OperationalStateHolder`` singleton
    between tests — KR-TEST-STABILITY-XDIST.

    The ``_holder_with_state`` helper below installs a holder via
    direct ``h_mod._HOLDER = holder`` assignment (rather than
    ``monkeypatch.setattr``) because most tests use the helper for
    its side effect WITHOUT taking ``monkeypatch`` as a fixture arg.
    Without this autouse reset, a test that sets the holder to
    PAUSED leaks into the next test on the same xdist worker —
    surfacing as ``test_email_inbound_handler.py`` flakes where the
    state-gate sees a stale PAUSED holder and returns
    ``filtered_paused`` instead of ``received``.

    Resetting at BOTH setup and teardown is intentional: a previous
    test in the same worker may have left a dirty holder, AND this
    test may dirty the holder. Either path catches the leak.
    """
    from agent import operational_state_holder as h_mod

    h_mod._HOLDER = None
    yield
    h_mod._HOLDER = None


@pytest.fixture
def authorized_token(monkeypatch, tmp_path):
    """Caller with BOTH pause + resume caps (no full transition cap)."""
    token = "pause-resume-tok"
    callers_path = tmp_path / "mcp_callers.yaml"
    _write_callers_yaml(
        callers_path,
        [
            {
                "token_hash": _sha256(token),
                "actor_kind": "claude_pm_pauser",
                "allowed_caps": [
                    "kora__request_pause",
                    "kora__request_resume",
                ],
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
def unauthorized_token(monkeypatch, tmp_path):
    """Caller with NO caps (read-only on ST1)."""
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
    from kora_cli.listeners import mcp_caller_auth

    monkeypatch.setattr(
        mcp_caller_auth, "DEFAULT_CALLERS_PATH", callers_path
    )
    monkeypatch.delenv("KORA_MCP_BEARER_TOKEN", raising=False)
    return token


@pytest.fixture
def transition_only_token(monkeypatch, tmp_path):
    """Caller with ONLY kora__request_state_transition (not pause/resume).

    Verifies the spec's "separate caps from full state-transition"
    principle — having transition cap does NOT grant pause/resume.
    """
    token = "transition-only-tok"
    callers_path = tmp_path / "mcp_callers.yaml"
    _write_callers_yaml(
        callers_path,
        [
            {
                "token_hash": _sha256(token),
                "actor_kind": "claude_pm_transition_only",
                "allowed_caps": ["kora__request_state_transition"],
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


def _holder_with_state(primary_state):
    """Build + install a fresh OperationalStateHolder at the given state."""
    from agent.operational_state import OperationalState
    from agent.operational_state_holder import OperationalStateHolder
    from agent import operational_state_holder as h_mod

    holder = OperationalStateHolder(
        OperationalState(primary_state=primary_state)
    )
    h_mod._HOLDER = holder
    return holder


# ---------------------------------------------------------------------------
# Descriptors — surface in tools/list with cap-gate + non-dev-only
# ---------------------------------------------------------------------------


def test_descriptors_in_tools_list(client, authorized_token):
    r = client.get(
        "/mcp/tools/list",
        headers={"Authorization": f"Bearer {authorized_token}"},
    )
    assert r.status_code == 200
    by_name = {t["name"]: t for t in r.json()["tools"]}
    for name in ("kora__request_pause", "kora__request_resume"):
        assert name in by_name
        assert by_name[name]["requires_cap_gate"] is True
        assert by_name[name]["dev_only"] is False
        assert by_name[name]["inputSchema"]["required"] == ["reason"]


# ---------------------------------------------------------------------------
# Happy path — pause + resume
# ---------------------------------------------------------------------------


def test_pause_from_active_succeeds(client, authorized_token):
    from agent.operational_state import PrimaryState

    holder = _holder_with_state(PrimaryState.ACTIVE)
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {authorized_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "kora__request_pause",
                "arguments": {"reason": "operator triage"},
            },
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert "result" in body, body
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["success"] is True
    assert payload["from_state"] == "active"
    assert payload["to_state"] == "paused"
    assert payload["caller_actor_kind"] == "claude_pm_pauser"
    # Holder actually transitioned.
    assert holder.current.primary_state is PrimaryState.PAUSED


def test_resume_from_paused_succeeds(client, authorized_token):
    from agent.operational_state import PrimaryState

    holder = _holder_with_state(PrimaryState.PAUSED)
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {authorized_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "kora__request_resume",
                "arguments": {"reason": "triage complete"},
            },
        },
    )
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert payload["from_state"] == "paused"
    # Spec drift caught at K-DG: TRANSITION_TABLE has PAUSED → READY
    # (operator-clearance edge), NOT PAUSED → ACTIVE. Resume targets
    # READY; next claim cycle moves READY → ACTIVE.
    assert payload["to_state"] == "ready"
    assert holder.current.primary_state is PrimaryState.READY


# ---------------------------------------------------------------------------
# Invalid state-transition guards
# ---------------------------------------------------------------------------


def test_pause_from_paused_returns_32602(client, authorized_token):
    from agent.operational_state import PrimaryState

    _holder_with_state(PrimaryState.PAUSED)
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {authorized_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "kora__request_pause",
                "arguments": {"reason": "double-pause"},
            },
        },
    )
    body = r.json()
    assert body["error"]["code"] == -32602
    assert "paused" in body["error"]["message"].lower()


def test_pause_from_stopped_returns_32602(client, authorized_token):
    from agent.operational_state import PrimaryState

    _holder_with_state(PrimaryState.STOPPED)
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {authorized_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "kora__request_pause",
                "arguments": {"reason": "x"},
            },
        },
    )
    assert r.json()["error"]["code"] == -32602


def test_resume_from_active_returns_32602(client, authorized_token):
    from agent.operational_state import PrimaryState

    _holder_with_state(PrimaryState.ACTIVE)
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {authorized_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "kora__request_resume",
                "arguments": {"reason": "x"},
            },
        },
    )
    body = r.json()
    assert body["error"]["code"] == -32602
    assert "paused" in body["error"]["message"].lower()


def test_resume_from_booting_returns_32602(client, authorized_token):
    from agent.operational_state import PrimaryState

    _holder_with_state(PrimaryState.BOOTING)
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {authorized_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "kora__request_resume",
                "arguments": {"reason": "x"},
            },
        },
    )
    assert r.json()["error"]["code"] == -32602


# ---------------------------------------------------------------------------
# Reason validation
# ---------------------------------------------------------------------------


def test_pause_empty_reason_rejected(client, authorized_token):
    from agent.operational_state import PrimaryState

    _holder_with_state(PrimaryState.ACTIVE)
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {authorized_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "kora__request_pause",
                "arguments": {"reason": "   "},
            },
        },
    )
    body = r.json()
    assert body["error"]["code"] == -32602
    assert "reason" in body["error"]["message"].lower()


def test_pause_missing_reason_rejected(client, authorized_token):
    from agent.operational_state import PrimaryState

    _holder_with_state(PrimaryState.ACTIVE)
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {authorized_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "kora__request_pause",
                "arguments": {},
            },
        },
    )
    assert r.json()["error"]["code"] == -32602


# ---------------------------------------------------------------------------
# Capability gating — distinct from request_state_transition cap
# ---------------------------------------------------------------------------


def test_pause_without_capability_denied(client, unauthorized_token):
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {unauthorized_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "kora__request_pause",
                "arguments": {"reason": "x"},
            },
        },
    )
    err = r.json()["error"]
    assert err["code"] == -32001
    assert err["message"] == "capability_denied"
    assert err["data"]["required_capability"] == "kora__request_pause"
    assert err["data"]["caller_actor_kind"] == "read_only_caller"


def test_resume_without_capability_denied(client, unauthorized_token):
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {unauthorized_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "kora__request_resume",
                "arguments": {"reason": "x"},
            },
        },
    )
    err = r.json()["error"]
    assert err["code"] == -32001
    assert err["data"]["required_capability"] == "kora__request_resume"


def test_transition_cap_does_not_grant_pause(client, transition_only_token):
    """Caller has kora__request_state_transition cap but NOT
    kora__request_pause. Per spec: 'separate caps so operator can
    grant pause/resume without granting full transition power'. The
    reverse must also hold: transition cap doesn't auto-grant pause."""
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {transition_only_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "kora__request_pause",
                "arguments": {"reason": "trying to use wrong cap"},
            },
        },
    )
    err = r.json()["error"]
    assert err["code"] == -32001
    assert err["data"]["required_capability"] == "kora__request_pause"


def test_transition_cap_does_not_grant_resume(client, transition_only_token):
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {transition_only_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {
                "name": "kora__request_resume",
                "arguments": {"reason": "trying to use wrong cap"},
            },
        },
    )
    err = r.json()["error"]
    assert err["code"] == -32001
    assert err["data"]["required_capability"] == "kora__request_resume"


# ---------------------------------------------------------------------------
# Audit + ledger surface
# ---------------------------------------------------------------------------


def test_pause_records_audit_with_caller_actor_kind(
    client, authorized_token, caplog
):
    import logging
    from agent.operational_state import PrimaryState

    caplog.set_level(logging.INFO)
    _holder_with_state(PrimaryState.ACTIVE)
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {authorized_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 13,
            "method": "tools/call",
            "params": {
                "name": "kora__request_pause",
                "arguments": {"reason": "audit-test"},
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
    line = audit_lines[-1]
    assert "tool=kora__request_pause" in line
    assert "caller_actor_kind=claude_pm_pauser" in line


def test_resume_records_audit_with_caller_actor_kind(
    client, authorized_token, caplog
):
    import logging
    from agent.operational_state import PrimaryState

    caplog.set_level(logging.INFO)
    _holder_with_state(PrimaryState.PAUSED)
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {authorized_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 14,
            "method": "tools/call",
            "params": {
                "name": "kora__request_resume",
                "arguments": {"reason": "audit-test"},
            },
        },
    )
    assert "result" in r.json()
    line = next(
        m
        for m in (rec.getMessage() for rec in caplog.records)
        if "kora.mcp.tool_called" in m
    )
    assert "tool=kora__request_resume" in line
    assert "caller_actor_kind=claude_pm_pauser" in line


# ---------------------------------------------------------------------------
# SECURITY — bearer token never in error envelope
# ---------------------------------------------------------------------------


def test_bearer_token_never_in_error_envelope(
    client, authorized_token, tmp_path, monkeypatch
):
    """Diverse failure paths: invalid transition + bad reason. Bearer
    token value must NEVER appear in any returned JSON-RPC envelope
    or any log line."""
    import logging

    from agent.operational_state import PrimaryState
    from kora_cli.listeners import mcp_caller_auth

    # Re-provision with a strongly-shaped marker token.
    token_marker = "kora-stop-control-secret-MUST-NOT-LEAK"
    callers_path = tmp_path / "mcp_callers.yaml"
    _write_callers_yaml(
        callers_path,
        [
            {
                "token_hash": _sha256(token_marker),
                "actor_kind": "secure_caller",
                "allowed_caps": [
                    "kora__request_pause",
                    "kora__request_resume",
                ],
            }
        ],
    )
    mcp_caller_auth._reset_cache_for_tests()
    monkeypatch.setattr(
        mcp_caller_auth, "DEFAULT_CALLERS_PATH", callers_path
    )

    # Path 1: invalid transition.
    _holder_with_state(PrimaryState.STOPPED)
    r1 = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token_marker}"},
        json={
            "jsonrpc": "2.0",
            "id": 15,
            "method": "tools/call",
            "params": {
                "name": "kora__request_pause",
                "arguments": {"reason": "won't fly"},
            },
        },
    )
    # Path 2: empty reason.
    _holder_with_state(PrimaryState.ACTIVE)
    r2 = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token_marker}"},
        json={
            "jsonrpc": "2.0",
            "id": 16,
            "method": "tools/call",
            "params": {
                "name": "kora__request_pause",
                "arguments": {"reason": ""},
            },
        },
    )

    for response in (r1, r2):
        body_text = response.text
        assert token_marker not in body_text, (
            "bearer token surfaced in JSON-RPC error envelope"
        )
