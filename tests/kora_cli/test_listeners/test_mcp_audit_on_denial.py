"""Tests for KR-MCP-AUDIT-ON-DENIAL — emit JSONL audit row at cap-gate denials.

Two denial paths land in the audit stream:
  - cap-gate denial → ``details.result == "capability_denied"``
  - actor_id-required denial (kora__request_stop only) →
    ``details.result == "actor_id_required"``

The two ``result`` literals are CC#1's alert-rule discriminators.

Covers spec §2(c):
  - cap-gate denial: AuditEntry written with result=capability_denied
    + required_capability + tool_name + caller_actor_kind +
    caller_actor_id
  - actor_id-required denial: AuditEntry written with result=
    actor_id_required
  - Successful tool call: no denial-result audit (only success-path)
  - Audit emit happens BEFORE the JSON-RPC envelope is returned
  - Audit-sink failure does NOT mask the denial response
  - Walk-payload security: bearer tokens never in audit details
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient


def _sha256(tok: str) -> str:
    return "sha256:" + hashlib.sha256(tok.encode("utf-8")).hexdigest()


def _write_callers_yaml(path: Path, entries: list) -> None:
    path.write_text(yaml.safe_dump({"callers": entries}), encoding="utf-8")


_OPERATOR_ACTOR_UUID = "8d50b3aa-1111-4222-9333-cafebabe1234"


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

    ``test_successful_call_does_not_emit_denial_audit`` installs an
    ACTIVE holder via direct ``h_mod._HOLDER = ...`` so the pause
    executor can transition. Without this autouse reset, that
    holder persists across worker boundaries to subsequent tests in
    the same xdist worker — surfacing as ``test_email_inbound_
    handler.py`` flakes where the state-gate sees an unexpected
    holder.
    """
    from agent import operational_state_holder as h_mod

    h_mod._HOLDER = None
    yield
    h_mod._HOLDER = None


@pytest.fixture
def empty_caps_token(monkeypatch, tmp_path):
    """Caller authenticated but with NO caps — every mutating tool denies."""
    token = "no-caps-tok"
    callers_path = tmp_path / "mcp_callers.yaml"
    _write_callers_yaml(
        callers_path,
        [
            {
                "token_hash": _sha256(token),
                "actor_kind": "claude_pm_uncfg",
                "actor_id": _OPERATOR_ACTOR_UUID,
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
def stop_cap_no_actor_id_token(monkeypatch, tmp_path):
    """Caller has stop cap but NO actor_id — actor_id_required path."""
    token = "stop-no-aid-tok"
    callers_path = tmp_path / "mcp_callers.yaml"
    _write_callers_yaml(
        callers_path,
        [
            {
                "token_hash": _sha256(token),
                "actor_kind": "claude_pm_no_actor_id",
                # actor_id omitted
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
def full_caps_token(monkeypatch, tmp_path):
    """Caller fully authorized — success path (no denial audit)."""
    token = "full-caps-tok"
    callers_path = tmp_path / "mcp_callers.yaml"
    _write_callers_yaml(
        callers_path,
        [
            {
                "token_hash": _sha256(token),
                "actor_kind": "claude_pm_full",
                "actor_id": _OPERATOR_ACTOR_UUID,
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
def client():
    from kora_cli.web_server import app

    return TestClient(app)


@pytest.fixture
def captured_audit_calls(monkeypatch):
    """Patch the lazy ``from kora_cli.audit import emit_audit`` inside
    the denial helpers. The helpers do ``from kora_cli.audit import
    emit_audit`` at call time; the import resolves via
    kora_cli/audit/__init__.py which re-exports from jsonl_sink.

    We patch the underlying ``emit_audit`` in the jsonl_sink module
    + the re-export. Either resolves into the same callable in this
    process."""
    calls: list[dict] = []

    def fake_emit_audit(
        seam: str,
        details: dict,
        *,
        caller_session_id: Any = None,
        source: Any = None,
        log_path: Any = None,
    ) -> None:
        calls.append(
            {
                "seam": seam,
                "details": dict(details),
                "caller_session_id": caller_session_id,
                "source": source,
            }
        )

    monkeypatch.setattr(
        "kora_cli.audit.jsonl_sink.emit_audit", fake_emit_audit
    )
    monkeypatch.setattr("kora_cli.audit.emit_audit", fake_emit_audit)
    return calls


# ---------------------------------------------------------------------------
# Cap-gate denial path
# ---------------------------------------------------------------------------


def test_cap_gate_denial_writes_audit_row(
    client, empty_caps_token, captured_audit_calls
):
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {empty_caps_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "kora__request_pause",
                "arguments": {"reason": "denied-test"},
            },
        },
    )
    # Denial envelope returned to caller.
    err = r.json()["error"]
    assert err["code"] == -32001
    assert err["message"] == "capability_denied"

    # Exactly one denial audit row written.
    denial_calls = [
        c
        for c in captured_audit_calls
        if c["details"].get("result") == "capability_denied"
    ]
    assert len(denial_calls) == 1
    row = denial_calls[0]
    assert row["seam"] == "mcp.tool_called"
    assert row["source"] == "mcp_http"
    d = row["details"]
    assert d["tool_name"] == "kora__request_pause"
    assert d["tool_kind"] == "mutating"
    assert d["caller_actor_kind"] == "claude_pm_uncfg"
    assert d["caller_actor_id"] == _OPERATOR_ACTOR_UUID
    assert d["required_capability"] == "kora__request_pause"
    assert d["duration_ms"] == 0
    assert d["tool_status"] == "not_allowed"
    assert d["result"] == "capability_denied"


def test_cap_gate_denial_caller_actor_id_none_when_absent(
    client, monkeypatch, tmp_path, captured_audit_calls
):
    """Caller without actor_id field still gets a clean audit row
    (caller_actor_id: None) — actor_id absence is its own state and
    shouldn't be masked into 'no audit'."""
    token = "no-aid-tok"
    callers_path = tmp_path / "mcp_callers.yaml"
    _write_callers_yaml(
        callers_path,
        [
            {
                "token_hash": _sha256(token),
                "actor_kind": "claude_pm_legacy",
                # actor_id absent
                "allowed_caps": [],
            }
        ],
    )
    from kora_cli.listeners import mcp_caller_auth

    monkeypatch.setattr(
        mcp_caller_auth, "DEFAULT_CALLERS_PATH", callers_path
    )
    monkeypatch.delenv("KORA_MCP_BEARER_TOKEN", raising=False)

    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "kora__request_pause",
                "arguments": {"reason": "x"},
            },
        },
    )
    assert r.json()["error"]["code"] == -32001
    denial_calls = [
        c
        for c in captured_audit_calls
        if c["details"].get("result") == "capability_denied"
    ]
    assert len(denial_calls) == 1
    assert denial_calls[0]["details"]["caller_actor_id"] is None
    assert denial_calls[0]["details"]["caller_actor_kind"] == "claude_pm_legacy"


def test_cap_gate_denial_audit_emitted_before_envelope(
    client, empty_caps_token, monkeypatch
):
    """Verify ordering: audit emit MUST run BEFORE the envelope returns.

    The audit fixture isn't enough — it captures regardless of order.
    Patch emit_audit to raise inside; the response should still be
    a -32001 envelope (audit failure must not mask the denial)."""
    from kora_cli.listeners import mcp as mcp_mod

    def _failing_emit(*args, **kwargs):
        raise RuntimeError("audit sink down")

    # Patch BOTH bound emit_audit references so the lazy import inside
    # the helper resolves to the failing function.
    monkeypatch.setattr(
        "kora_cli.audit.jsonl_sink.emit_audit", _failing_emit
    )
    monkeypatch.setattr(
        "kora_cli.audit.emit_audit", _failing_emit
    )

    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {empty_caps_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "kora__request_pause",
                "arguments": {"reason": "x"},
            },
        },
    )
    # Denial response still surfaces cleanly.
    assert r.status_code == 200
    assert r.json()["error"]["code"] == -32001
    assert r.json()["error"]["message"] == "capability_denied"


def test_cap_gate_denial_does_not_run_executor(
    client, empty_caps_token, captured_audit_calls, monkeypatch
):
    """Denied calls must NOT reach _ST2_DISPATCH executors. Verify by
    spying on the dispatch table."""
    from kora_cli.listeners import mcp_tools

    dispatched: list = []
    original = mcp_tools.ST2_TOOL_DISPATCH["kora__request_pause"]

    async def _spy(params, caller):
        dispatched.append((params, caller))
        return await original(params, caller)

    monkeypatch.setitem(
        mcp_tools.ST2_TOOL_DISPATCH, "kora__request_pause", _spy
    )

    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {empty_caps_token}"},
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
    assert r.json()["error"]["code"] == -32001
    assert dispatched == [], (
        "denied call must not reach the executor; cap-gate fires first"
    )


# ---------------------------------------------------------------------------
# actor_id_required denial path
# ---------------------------------------------------------------------------


def test_actor_id_required_denial_writes_audit_row(
    client,
    stop_cap_no_actor_id_token,
    captured_audit_calls,
    monkeypatch,
):
    """Caller with kora__request_stop cap but no actor_id triggers
    the actor_id-required gate — second denial discriminator."""
    # Install a dummy coordinator so confirm_token can be read.
    from kora_cli.daemon import DaemonCoordinator
    from kora_cli.listeners import mcp as mcp_mod

    coord = DaemonCoordinator()
    monkeypatch.setattr(mcp_mod, "current_coordinator", lambda: coord)

    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {stop_cap_no_actor_id_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "kora__request_stop",
                "arguments": {
                    "reason": "trying without actor_id",
                    "level": 1,
                    "confirm_token": coord.daemon_session_id,
                },
            },
        },
    )
    err = r.json()["error"]
    assert err["code"] == -32001
    assert err["message"] == "actor_id_required_for_stop"

    denial_calls = [
        c
        for c in captured_audit_calls
        if c["details"].get("result") == "actor_id_required"
    ]
    assert len(denial_calls) == 1
    d = denial_calls[0]["details"]
    assert d["tool_name"] == "kora__request_stop"
    assert d["tool_kind"] == "mutating"
    assert d["caller_actor_kind"] == "claude_pm_no_actor_id"
    assert d["caller_actor_id"] is None  # caller has no actor_id field
    assert d["required_capability"] == "kora__request_stop"
    assert d["duration_ms"] == 0
    assert d["tool_status"] == "not_allowed"
    # The two denial discriminators are MUTUALLY EXCLUSIVE so the
    # alert rule's detail_match doesn't conflate them.
    cap_denials = [
        c
        for c in captured_audit_calls
        if c["details"].get("result") == "capability_denied"
    ]
    assert cap_denials == []


# ---------------------------------------------------------------------------
# Success path — no denial audit
# ---------------------------------------------------------------------------


def test_successful_call_does_not_emit_denial_audit(
    client, full_caps_token, captured_audit_calls, monkeypatch
):
    """Authorized call → pause executor runs → success-path audit only.

    Verify the cap-gate doesn't accidentally fire on authorized calls."""
    # Put the holder into ACTIVE so the pause succeeds without
    # exercising the InvalidStateTransitionError path.
    from agent.operational_state import OperationalState, PrimaryState
    from agent.operational_state_holder import OperationalStateHolder
    from agent import operational_state_holder as h_mod

    h_mod._HOLDER = OperationalStateHolder(
        OperationalState(primary_state=PrimaryState.ACTIVE)
    )

    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {full_caps_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 20,
            "method": "tools/call",
            "params": {
                "name": "kora__request_pause",
                "arguments": {"reason": "authorized"},
            },
        },
    )
    assert "result" in r.json(), r.json()

    # No capability_denied / actor_id_required entries.
    denial_calls = [
        c
        for c in captured_audit_calls
        if c["details"].get("result") in ("capability_denied", "actor_id_required")
    ]
    assert denial_calls == []
    # There SHOULD be at least one success-path audit row from the
    # pause executor's _emit_audit (its result is "active->paused").
    success_calls = [
        c
        for c in captured_audit_calls
        if "->" in str(c["details"].get("result", ""))
    ]
    assert len(success_calls) >= 1


# ---------------------------------------------------------------------------
# Security sweep — bearer tokens never in audit details
# ---------------------------------------------------------------------------


def test_audit_details_never_contain_bearer_token(
    client, monkeypatch, tmp_path, captured_audit_calls
):
    """Walk every key+value in every captured audit row; the bearer
    token marker MUST NOT appear anywhere."""
    token_marker = "kora-mcp-bearer-MUST-NOT-LEAK-IN-AUDIT-987"
    callers_path = tmp_path / "mcp_callers.yaml"
    _write_callers_yaml(
        callers_path,
        [
            {
                "token_hash": _sha256(token_marker),
                "actor_kind": "secure_caller",
                "actor_id": _OPERATOR_ACTOR_UUID,
                "allowed_caps": [],
            }
        ],
    )
    from kora_cli.listeners import mcp_caller_auth

    monkeypatch.setattr(
        mcp_caller_auth, "DEFAULT_CALLERS_PATH", callers_path
    )
    monkeypatch.delenv("KORA_MCP_BEARER_TOKEN", raising=False)

    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token_marker}"},
        json={
            "jsonrpc": "2.0",
            "id": 30,
            "method": "tools/call",
            "params": {
                "name": "kora__request_pause",
                "arguments": {"reason": "token-leak-check"},
            },
        },
    )
    assert r.json()["error"]["code"] == -32001

    for call in captured_audit_calls:
        # Serialize the entire captured row + walk for the marker.
        serialized = json.dumps(call, default=str)
        assert token_marker not in serialized, (
            f"bearer token surfaced in audit row: {call!r}"
        )
