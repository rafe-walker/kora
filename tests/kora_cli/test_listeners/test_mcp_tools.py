"""Tests for ``kora_cli.listeners.mcp_tools`` — KR-MCP-RUNTIME-SURFACE ST1.

Read-only tool surface — 5 new tools wired through the JSON-RPC
dispatch in ``mcp.py``. Tests cover:

  - All 5 tools appear in ``tools/list``.
  - Each tool's POST ``/mcp`` call returns the expected Pydantic shape.
  - Holder-unavailable + provider-unavailable paths return honest
    placeholders (not crashes).
  - Bearer auth still enforced (negative test).
  - Unknown tool → JSON-RPC -32602.
  - Tool-execution exception → JSON-RPC -32603 (not 500).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from kora_cli.listeners.mcp_tools import (
    TOOL_DESCRIPTORS,
    TOOL_DISPATCH,
    _execute_get_health_rollup,
    _execute_get_operational_state,
    _execute_get_recent_chain_events,
    _execute_get_recent_ledger_entries,
    _execute_list_active_sea_tickets,
)

BEARER = "test-bearer-token-ST1"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("KORA_MCP_BEARER_TOKEN", BEARER)
    from kora_cli.web_server import app

    return TestClient(app)


def _headers():
    return {"Authorization": f"Bearer {BEARER}"}


# ---------------------------------------------------------------------------
# Descriptor surface
# ---------------------------------------------------------------------------


def test_all_5_tools_in_descriptor_list():
    names = [t["name"] for t in TOOL_DESCRIPTORS]
    assert sorted(names) == sorted(
        [
            "kora__get_operational_state",
            "kora__get_health_rollup",
            "kora__get_recent_ledger_entries",
            "kora__get_recent_chain_events",
            "kora__list_active_sea_tickets",
        ]
    )


def test_dispatch_table_covers_all_descriptors():
    descriptor_names = {t["name"] for t in TOOL_DESCRIPTORS}
    dispatch_names = set(TOOL_DISPATCH.keys())
    assert descriptor_names == dispatch_names


def test_get_tools_list_returns_daemon_status_plus_st1_tools(client):
    r = client.get("/mcp/tools/list", headers=_headers())
    assert r.status_code == 200
    names = [t["name"] for t in r.json()["tools"]]
    # ST2 of KR-D-DAEMON (#101) shipped kora__daemon_status; this ST
    # extends with 5 more. Order isn't part of the contract but the
    # set is.
    assert "kora__daemon_status" in names
    for new_name in (
        "kora__get_operational_state",
        "kora__get_health_rollup",
        "kora__get_recent_ledger_entries",
        "kora__get_recent_chain_events",
        "kora__list_active_sea_tickets",
    ):
        assert new_name in names, f"missing: {new_name}"


def test_tools_list_descriptors_have_required_fields():
    """Every tool descriptor must carry name + description + inputSchema."""
    for tool in TOOL_DESCRIPTORS:
        assert "name" in tool and tool["name"].startswith("kora__")
        assert "description" in tool and tool["description"]
        assert "inputSchema" in tool
        assert tool["inputSchema"]["type"] == "object"


# ---------------------------------------------------------------------------
# kora__get_operational_state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operational_state_no_holder_returns_placeholder():
    from agent import operational_state_holder

    operational_state_holder._reset_holder_for_tests = (
        lambda: setattr(operational_state_holder, "_HOLDER", None)
    )
    # Force the holder to be None.
    operational_state_holder._HOLDER = None

    result = await _execute_get_operational_state()
    assert result.holder_available is False
    assert result.primary_state == "unknown"


@pytest.mark.asyncio
async def test_operational_state_with_holder_returns_current(monkeypatch):
    from agent.operational_state import (
        ClaimPermission,
        DegradationReason,
        OperationalState,
        PrimaryState,
    )
    from agent.operational_state_holder import OperationalStateHolder
    from agent import operational_state_holder as holder_mod

    initial = OperationalState(
        primary_state=PrimaryState.READY,
        degradation_reasons=frozenset({DegradationReason.COST}),
        claim_permission=ClaimPermission.CRITICAL_ONLY,
    )
    h = OperationalStateHolder(initial)
    monkeypatch.setattr(holder_mod, "_HOLDER", h)

    result = await _execute_get_operational_state()
    assert result.holder_available is True
    assert result.primary_state == "ready"
    assert result.degradation_reasons == ["cost"]
    assert result.claim_permission == "critical_only"


def test_operational_state_via_jsonrpc(client, monkeypatch):
    from agent.operational_state import (
        ClaimPermission,
        OperationalState,
        PrimaryState,
    )
    from agent.operational_state_holder import OperationalStateHolder
    from agent import operational_state_holder as holder_mod

    initial = OperationalState(
        primary_state=PrimaryState.ACTIVE,
        claim_permission=ClaimPermission.NORMAL,
    )
    monkeypatch.setattr(holder_mod, "_HOLDER", OperationalStateHolder(initial))

    r = client.post(
        "/mcp",
        headers=_headers(),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "kora__get_operational_state"},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert "result" in body
    text = body["result"]["content"][0]["text"]
    payload = json.loads(text)
    assert payload["primary_state"] == "active"
    assert payload["claim_permission"] == "normal"
    assert payload["holder_available"] is True


# ---------------------------------------------------------------------------
# kora__get_health_rollup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_rollup_no_holder_returns_placeholder(monkeypatch):
    from agent import health_rollup_holder as hh_mod

    monkeypatch.setattr(hh_mod, "_HOLDER", None)
    result = await _execute_get_health_rollup()
    assert result.holder_available is False
    assert result.overall_status == "unknown"


def test_health_rollup_via_jsonrpc_no_holder(client, monkeypatch):
    """Smoke test that the JSON-RPC round-trip survives an absent
    health holder gracefully (real one isn't initialized in tests)."""
    from agent import health_rollup_holder as hh_mod

    monkeypatch.setattr(hh_mod, "_HOLDER", None)
    r = client.post(
        "/mcp",
        headers=_headers(),
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "kora__get_health_rollup"},
        },
    )
    assert r.status_code == 200
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert payload["holder_available"] is False


# ---------------------------------------------------------------------------
# Substrate-touching tools: provider unavailable path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ledger_entries_no_provider_returns_empty(monkeypatch):
    from kora_cli.listeners import mcp_tools as t_mod

    monkeypatch.setattr(t_mod, "_get_active_provider", lambda: None)
    result = await _execute_get_recent_ledger_entries(limit=50, status=None)
    assert result.provider_unavailable is True
    assert result.entries == []
    assert result.limit_applied == 50


@pytest.mark.asyncio
async def test_chain_events_no_provider_returns_empty(monkeypatch):
    from kora_cli.listeners import mcp_tools as t_mod

    monkeypatch.setattr(t_mod, "_get_active_provider", lambda: None)
    result = await _execute_get_recent_chain_events(limit=50, event_kind=None)
    assert result.provider_unavailable is True
    assert result.events == []


@pytest.mark.asyncio
async def test_active_sea_tickets_no_provider_returns_empty(monkeypatch):
    from kora_cli.listeners import mcp_tools as t_mod

    monkeypatch.setattr(t_mod, "_get_active_provider", lambda: None)
    result = await _execute_list_active_sea_tickets(limit=50)
    assert result.provider_unavailable is True


@pytest.mark.asyncio
async def test_ledger_entries_provider_no_connection_returns_empty(monkeypatch):
    from kora_cli.listeners import mcp_tools as t_mod

    fake = MagicMock()
    fake._connection = None
    monkeypatch.setattr(t_mod, "_get_active_provider", lambda: fake)
    result = await _execute_get_recent_ledger_entries(limit=10, status=None)
    assert result.provider_unavailable is True


# ---------------------------------------------------------------------------
# Limit clamping
# ---------------------------------------------------------------------------


def test_limit_clamping_via_jsonrpc_oversized(client, monkeypatch):
    """A caller passing limit=1000 gets clamped to 200 (max)."""
    from kora_cli.listeners import mcp_tools as t_mod

    monkeypatch.setattr(t_mod, "_get_active_provider", lambda: None)
    r = client.post(
        "/mcp",
        headers=_headers(),
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "kora__get_recent_ledger_entries",
                "arguments": {"limit": 1000},
            },
        },
    )
    assert r.status_code == 200
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert payload["limit_applied"] == 200


def test_limit_clamping_via_jsonrpc_negative(client, monkeypatch):
    """A caller passing limit=-5 gets clamped to 1."""
    from kora_cli.listeners import mcp_tools as t_mod

    monkeypatch.setattr(t_mod, "_get_active_provider", lambda: None)
    r = client.post(
        "/mcp",
        headers=_headers(),
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "kora__get_recent_chain_events",
                "arguments": {"limit": -5},
            },
        },
    )
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert payload["limit_applied"] == 1


def test_limit_default_when_omitted(client, monkeypatch):
    from kora_cli.listeners import mcp_tools as t_mod

    monkeypatch.setattr(t_mod, "_get_active_provider", lambda: None)
    r = client.post(
        "/mcp",
        headers=_headers(),
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "kora__list_active_sea_tickets",
                "arguments": {},
            },
        },
    )
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert payload["limit_applied"] == 50


# ---------------------------------------------------------------------------
# Bearer auth still enforced on the new tools
# ---------------------------------------------------------------------------


def test_new_tools_require_bearer(client):
    """No Authorization header → 401 on POST /mcp (gate is on the
    route, not per-tool)."""
    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "kora__get_operational_state"},
        },
    )
    assert r.status_code == 401


def test_new_tools_reject_wrong_bearer(client):
    r = client.post(
        "/mcp",
        headers={"Authorization": "Bearer WRONG"},
        json={
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "kora__get_health_rollup"},
        },
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Error envelopes
# ---------------------------------------------------------------------------


def test_unknown_tool_returns_jsonrpc_neg32602(client):
    r = client.post(
        "/mcp",
        headers=_headers(),
        json={
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "kora__does_not_exist"},
        },
    )
    body = r.json()
    assert body["error"]["code"] == -32602


def test_tool_executor_exception_returns_jsonrpc_neg32603(client, monkeypatch):
    """If a dispatcher raises, the response is a JSON-RPC -32603
    (internal error) — not a 500."""
    from kora_cli.listeners import mcp_tools as t_mod

    async def _boom(params):
        raise RuntimeError("simulated tool failure")

    monkeypatch.setitem(t_mod.TOOL_DISPATCH, "kora__get_health_rollup", _boom)

    r = client.post(
        "/mcp",
        headers=_headers(),
        json={
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "kora__get_health_rollup"},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["error"]["code"] == -32603
    assert "RuntimeError" in body["error"]["message"]
