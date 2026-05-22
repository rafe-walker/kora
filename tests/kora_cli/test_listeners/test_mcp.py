"""Tests for ``kora_cli.listeners.mcp`` — KR-D-DAEMON ST2.

Covers:
  - /mcp/tools/list returns kora__daemon_status with valid bearer.
  - /mcp/tools/list rejects 401 without bearer.
  - /mcp/tools/list rejects 401 with wrong bearer.
  - /mcp/tools/list rejects 401 if token env unset / empty.
  - POST /mcp tools/list JSON-RPC envelope.
  - POST /mcp tools/call kora__daemon_status returns status JSON.
  - POST /mcp unknown method → JSON-RPC -32601.
  - MCPListener.startup fail-CLOSED on unset env.
  - Coordinator state surfaced through the tool.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from kora_cli import daemon as daemon_mod
from kora_cli.daemon import DaemonCoordinator
from kora_cli.listeners.mcp import (
    BEARER_TOKEN_ENV,
    DAEMON_STATUS_TOOL,
    MCPListener,
    _execute_daemon_status,
)


# ---------------------------------------------------------------------------
# HTTP surface (importing here ensures the router is mounted on the app)
# ---------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    """Test client over the admin FastAPI app with the MCP router
    mounted (mount happens at module import; importing the listener
    above is sufficient)."""
    monkeypatch.setenv(BEARER_TOKEN_ENV, "test-bearer-token-1234")
    from kora_cli.web_server import app

    return TestClient(app)


def test_tools_list_with_valid_bearer_returns_kora_daemon_status(client):
    r = client.get(
        "/mcp/tools/list",
        headers={"Authorization": "Bearer test-bearer-token-1234"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "tools" in body
    names = [t["name"] for t in body["tools"]]
    assert "kora__daemon_status" in names


def test_tools_list_without_bearer_returns_401(client):
    r = client.get("/mcp/tools/list")
    assert r.status_code == 401


def test_tools_list_with_wrong_bearer_returns_401(client):
    r = client.get(
        "/mcp/tools/list",
        headers={"Authorization": "Bearer WRONG"},
    )
    assert r.status_code == 401


def test_tools_list_with_malformed_authorization_returns_401(client):
    r = client.get(
        "/mcp/tools/list",
        headers={"Authorization": "NotBearer x"},
    )
    assert r.status_code == 401


def test_tools_list_with_env_unset_returns_401(monkeypatch):
    """Env-var gone post-startup → 401, not pass-through."""
    monkeypatch.delenv(BEARER_TOKEN_ENV, raising=False)
    from kora_cli.web_server import app

    c = TestClient(app)
    r = c.get(
        "/mcp/tools/list",
        headers={"Authorization": "Bearer anything"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# JSON-RPC POST surface
# ---------------------------------------------------------------------------


def test_post_jsonrpc_tools_list(client):
    r = client.post(
        "/mcp",
        headers={"Authorization": "Bearer test-bearer-token-1234"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    assert "result" in body
    assert any(
        t["name"] == "kora__daemon_status" for t in body["result"]["tools"]
    )


def test_post_jsonrpc_tools_call_daemon_status(client):
    r = client.post(
        "/mcp",
        headers={"Authorization": "Bearer test-bearer-token-1234"},
        json={
            "jsonrpc": "2.0",
            "id": 42,
            "method": "tools/call",
            "params": {"name": "kora__daemon_status", "arguments": {}},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == 42
    assert "result" in body
    content = body["result"]["content"]
    assert isinstance(content, list) and content[0]["type"] == "text"
    status_payload = json.loads(content[0]["text"])
    # The "not running" path: no coordinator currently active.
    assert status_payload["state"] in (
        "booting",
        "running",
        "shutting_down",
        "not_running",
    )


def test_post_jsonrpc_unknown_method(client):
    r = client.post(
        "/mcp",
        headers={"Authorization": "Bearer test-bearer-token-1234"},
        json={"jsonrpc": "2.0", "id": 7, "method": "tools/run_in_circles"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["error"]["code"] == -32601


def test_post_jsonrpc_unknown_tool(client):
    r = client.post(
        "/mcp",
        headers={"Authorization": "Bearer test-bearer-token-1234"},
        json={
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "kora__does_not_exist"},
        },
    )
    assert r.status_code == 200
    assert r.json()["error"]["code"] == -32602


def test_post_jsonrpc_parse_error(client):
    r = client.post(
        "/mcp",
        headers={"Authorization": "Bearer test-bearer-token-1234"},
        content=b"not-json",
    )
    assert r.status_code == 200
    assert r.json()["error"]["code"] == -32700


# ---------------------------------------------------------------------------
# MCPListener.startup fail-CLOSED behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_listener_startup_raises_when_env_unset(monkeypatch):
    monkeypatch.delenv(BEARER_TOKEN_ENV, raising=False)
    listener = MCPListener()
    with pytest.raises(RuntimeError, match=BEARER_TOKEN_ENV):
        await listener.startup()


@pytest.mark.asyncio
async def test_mcp_listener_startup_raises_when_env_empty(monkeypatch):
    monkeypatch.setenv(BEARER_TOKEN_ENV, "   ")
    listener = MCPListener()
    with pytest.raises(RuntimeError, match=BEARER_TOKEN_ENV):
        await listener.startup()


@pytest.mark.asyncio
async def test_mcp_listener_startup_ok_with_env(monkeypatch):
    monkeypatch.setenv(BEARER_TOKEN_ENV, "ok")
    listener = MCPListener()
    await listener.startup()  # no raise


# ---------------------------------------------------------------------------
# Coordinator status surface
# ---------------------------------------------------------------------------


def test_execute_daemon_status_without_coordinator():
    """When no coordinator is current, surface honestly."""
    assert daemon_mod._CURRENT_COORDINATOR is None
    status = _execute_daemon_status()
    assert status["state"] == "not_running"
    assert status["uptime_seconds"] is None
    assert status["listeners"] == []


def test_execute_daemon_status_with_active_coordinator(monkeypatch):
    """Mount a coordinator + verify the tool surfaces its state."""
    coord = DaemonCoordinator()

    async def noop():
        pass

    coord.register_listener("fake", noop, noop)
    monkeypatch.setattr(daemon_mod, "_CURRENT_COORDINATOR", coord)
    status = _execute_daemon_status()
    assert status["state"] == "booting"  # startup() not yet called
    assert any(l["name"] == "fake" for l in status["listeners"])


def test_daemon_status_tool_schema_shape():
    """Tool descriptor format is part of the MCP wire contract."""
    assert DAEMON_STATUS_TOOL["name"] == "kora__daemon_status"
    assert "description" in DAEMON_STATUS_TOOL
    assert DAEMON_STATUS_TOOL["inputSchema"]["type"] == "object"
