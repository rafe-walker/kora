"""KR-7a — IsoKronMCPClient transport wiring tests.

Covers:
- Config-level: transport-mode discrimination + service-token resolution
- Client lifecycle: start/close idempotency + invoke-before-start guard
- Stdio transport: env injection + command resolution composition
- HTTP transport: Authorization header injection
- Result projection: ``structuredContent`` happy-path; text-fallback JSON
- Error surfacing: substrate-side ``isError`` → IsoKronMCPInvocationError;
  raw exceptions → IsoKronMCPInvocationError with sanitized message
- Connection integration: ``get_mcp_client()`` lazy-opens; pre-start guard

No live MCP server needed — we mock the SDK boundary (``stdio_client``,
``streamablehttp_client``, ``ClientSession``) so the test runs hermetically.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pydantic import SecretStr

from plugins.memory.isokron.config import (
    IsoKronProviderConfig,
    parse_mcp_transport,
)
from plugins.memory.isokron.mcp_client import (
    IsoKronMCPClient,
    IsoKronMCPInvocationError,
    IsoKronMCPNotStartedError,
)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _stdio_config(*, token: str | None = None) -> IsoKronProviderConfig:
    return IsoKronProviderConfig(
        isokron_dsn="postgres://kora:secret@localhost:5432/isokron",
        mcp_endpoint="stdio://node /path/to/sea-mcp-server.js --port 0",
        mcp_service_token=SecretStr(token) if token is not None else None,
    )


def _http_config(*, token: str | None = None) -> IsoKronProviderConfig:
    return IsoKronProviderConfig(
        isokron_dsn="postgres://kora:secret@localhost:5432/isokron",
        mcp_endpoint="https://sea.isokron.local/mcp",
        mcp_service_token=SecretStr(token) if token is not None else None,
    )


# ---------------------------------------------------------------------------
# Transport discrimination + service-token resolution
# ---------------------------------------------------------------------------


def test_parse_mcp_transport_stdio_vs_http():
    assert parse_mcp_transport("stdio://node ./sea.js") == "stdio"
    assert parse_mcp_transport("http://sea.local/mcp") == "http"
    assert parse_mcp_transport("https://sea.isokron.local/mcp") == "http"


def test_config_mcp_transport_property_matches_endpoint_scheme():
    assert _stdio_config().mcp_transport == "stdio"
    assert _http_config().mcp_transport == "http"


def test_config_resolve_service_token_prefers_explicit_field(monkeypatch):
    monkeypatch.setenv("KORA_SERVICE_TOKEN", "from-env")
    cfg = _http_config(token="from-config")
    assert cfg.resolve_service_token() == "from-config"


def test_config_resolve_service_token_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("KORA_SERVICE_TOKEN", "env-token-xyz")
    cfg = _http_config(token=None)
    assert cfg.resolve_service_token() == "env-token-xyz"


def test_config_resolve_service_token_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("KORA_SERVICE_TOKEN", raising=False)
    cfg = _http_config(token=None)
    assert cfg.resolve_service_token() is None


# ---------------------------------------------------------------------------
# Client lifecycle — pre-start guard + transport property
# ---------------------------------------------------------------------------


def test_client_transport_property_reflects_config():
    assert IsoKronMCPClient(_stdio_config()).transport == "stdio"
    assert IsoKronMCPClient(_http_config()).transport == "http"


def test_client_is_started_false_before_start():
    client = IsoKronMCPClient(_stdio_config())
    assert client.is_started is False


def test_invoke_before_start_raises_not_started_error():
    client = IsoKronMCPClient(_stdio_config())

    async def _run():
        await client.invoke("kora__append_event", {})

    with pytest.raises(IsoKronMCPNotStartedError):
        asyncio.run(_run())


def test_close_before_start_is_noop():
    """Idempotent close — pre-start ``close`` returns silently."""
    client = IsoKronMCPClient(_stdio_config())

    async def _run():
        await client.close()

    asyncio.run(_run())  # no exception


# ---------------------------------------------------------------------------
# Stdio transport — env injection + command resolution path
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _fake_stdio_cm(_params):
    yield (MagicMock(name="read"), MagicMock(name="write"))


@asynccontextmanager
async def _fake_session_cm(_read, _write):
    session = MagicMock()
    session.initialize = AsyncMock()
    session.call_tool = AsyncMock(
        return_value=_fake_call_tool_result({"event_id": "evt-abc"})
    )
    yield session


def _fake_call_tool_result(structured: dict | None, *, is_error: bool = False):
    """Build a fake CallToolResult mirroring the mcp SDK shape."""
    result = MagicMock()
    result.isError = is_error
    result.structuredContent = structured
    result.content = []
    return result


def test_stdio_start_injects_service_token_into_subprocess_env(monkeypatch):
    """Token is injected as KORA_SERVICE_TOKEN env into the spawned subprocess."""
    captured_params: dict = {}

    @asynccontextmanager
    async def capturing_stdio_cm(params):
        captured_params["env"] = dict(params.env)
        captured_params["command"] = params.command
        captured_params["args"] = list(params.args)
        yield (MagicMock(), MagicMock())

    client = IsoKronMCPClient(_stdio_config(token="svc-token-123"))

    with patch(
        "mcp.client.stdio.stdio_client", side_effect=capturing_stdio_cm
    ), patch("mcp.ClientSession", side_effect=_fake_session_cm):
        asyncio.run(client.start())

    assert captured_params["env"]["KORA_SERVICE_TOKEN"] == "svc-token-123"
    # Command resolution via _resolve_stdio_command should have left the
    # bare "node" or resolved it to an absolute path; either way the
    # original first-token shouldn't be empty.
    assert captured_params["command"]
    assert captured_params["args"] == ["/path/to/sea-mcp-server.js", "--port", "0"]

    asyncio.run(client.close())


def test_stdio_start_omits_token_env_when_none(monkeypatch):
    """No service token → no KORA_SERVICE_TOKEN entry in subprocess env."""
    monkeypatch.delenv("KORA_SERVICE_TOKEN", raising=False)
    captured_env: dict = {}

    @asynccontextmanager
    async def capturing_stdio_cm(params):
        captured_env.update(params.env)
        yield (MagicMock(), MagicMock())

    client = IsoKronMCPClient(_stdio_config(token=None))

    with patch(
        "mcp.client.stdio.stdio_client", side_effect=capturing_stdio_cm
    ), patch("mcp.ClientSession", side_effect=_fake_session_cm):
        asyncio.run(client.start())

    assert "KORA_SERVICE_TOKEN" not in captured_env
    asyncio.run(client.close())


def test_stdio_start_rejects_empty_command():
    """``stdio://`` with no remainder is a config bug; raise loudly."""
    cfg = IsoKronProviderConfig(
        isokron_dsn="postgres://x@y/z",
        mcp_endpoint="stdio://",
    )
    client = IsoKronMCPClient(cfg)
    with pytest.raises(ValueError) as excinfo:
        asyncio.run(client.start())
    assert "empty" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# HTTP transport — Authorization header injection
# ---------------------------------------------------------------------------


def test_http_start_injects_authorization_bearer_header():
    captured: dict = {}

    @asynccontextmanager
    async def capturing_http_cm(url, headers=None):
        captured["url"] = url
        captured["headers"] = dict(headers or {})
        yield (MagicMock(), MagicMock(), MagicMock())  # 3-tuple per SDK

    client = IsoKronMCPClient(_http_config(token="bearer-xyz"))

    with patch(
        "mcp.client.streamable_http.streamablehttp_client",
        side_effect=capturing_http_cm,
    ), patch("mcp.ClientSession", side_effect=_fake_session_cm):
        asyncio.run(client.start())

    assert captured["url"] == "https://sea.isokron.local/mcp"
    assert captured["headers"]["Authorization"] == "Bearer bearer-xyz"
    asyncio.run(client.close())


def test_http_start_omits_authorization_when_no_token(monkeypatch):
    monkeypatch.delenv("KORA_SERVICE_TOKEN", raising=False)
    captured: dict = {}

    @asynccontextmanager
    async def capturing_http_cm(url, headers=None):
        captured["headers"] = dict(headers or {})
        yield (MagicMock(), MagicMock(), MagicMock())

    client = IsoKronMCPClient(_http_config(token=None))

    with patch(
        "mcp.client.streamable_http.streamablehttp_client",
        side_effect=capturing_http_cm,
    ), patch("mcp.ClientSession", side_effect=_fake_session_cm):
        asyncio.run(client.start())

    assert "Authorization" not in captured["headers"]
    asyncio.run(client.close())


# ---------------------------------------------------------------------------
# invoke — result projection
# ---------------------------------------------------------------------------


def test_invoke_returns_structured_content_dict():
    client = IsoKronMCPClient(_stdio_config(token="t"))
    session = MagicMock()
    session.initialize = AsyncMock()
    session.call_tool = AsyncMock(
        return_value=_fake_call_tool_result({"event_id": "evt-001"})
    )

    @asynccontextmanager
    async def fake_session_cm(_read, _write):
        yield session

    with patch(
        "mcp.client.stdio.stdio_client", side_effect=_fake_stdio_cm
    ), patch("mcp.ClientSession", side_effect=fake_session_cm):
        asyncio.run(client.start())
        result = asyncio.run(client.invoke("kora__append_event", {"x": 1}))
        asyncio.run(client.close())

    assert result == {"event_id": "evt-001"}
    session.call_tool.assert_awaited_once_with("kora__append_event", {"x": 1})


def test_invoke_falls_back_to_text_json_when_no_structured_content():
    """Older mcp tools return text-only content; parse first text block as JSON."""
    text_block = MagicMock()
    text_block.text = json.dumps({"event_id": "evt-text"})
    raw_result = MagicMock()
    raw_result.isError = False
    raw_result.structuredContent = None
    raw_result.content = [text_block]

    session = MagicMock()
    session.initialize = AsyncMock()
    session.call_tool = AsyncMock(return_value=raw_result)

    @asynccontextmanager
    async def fake_session_cm(_read, _write):
        yield session

    client = IsoKronMCPClient(_stdio_config(token="t"))
    with patch(
        "mcp.client.stdio.stdio_client", side_effect=_fake_stdio_cm
    ), patch("mcp.ClientSession", side_effect=fake_session_cm):
        asyncio.run(client.start())
        result = asyncio.run(client.invoke("kora__some_tool", {}))
        asyncio.run(client.close())

    assert result == {"event_id": "evt-text"}


def test_invoke_substrate_error_response_raises_invocation_error():
    """``isError=True`` from the substrate surfaces as IsoKronMCPInvocationError."""
    err_block = MagicMock()
    err_block.text = "tenant not found for workspace_id=org_test"
    err_result = MagicMock()
    err_result.isError = True
    err_result.structuredContent = None
    err_result.content = [err_block]

    session = MagicMock()
    session.initialize = AsyncMock()
    session.call_tool = AsyncMock(return_value=err_result)

    @asynccontextmanager
    async def fake_session_cm(_read, _write):
        yield session

    client = IsoKronMCPClient(_stdio_config(token="t"))
    with patch(
        "mcp.client.stdio.stdio_client", side_effect=_fake_stdio_cm
    ), patch("mcp.ClientSession", side_effect=fake_session_cm):
        asyncio.run(client.start())
        with pytest.raises(IsoKronMCPInvocationError) as excinfo:
            asyncio.run(client.invoke("kora__append_event", {}))
        asyncio.run(client.close())

    assert excinfo.value.tool_name == "kora__append_event"
    assert "tenant not found" in excinfo.value.message


def test_invoke_call_tool_raising_propagates_as_invocation_error():
    """Any exception from session.call_tool surfaces sanitized."""
    session = MagicMock()
    session.initialize = AsyncMock()
    session.call_tool = AsyncMock(side_effect=RuntimeError("transport reset"))

    @asynccontextmanager
    async def fake_session_cm(_read, _write):
        yield session

    client = IsoKronMCPClient(_stdio_config(token="t"))
    with patch(
        "mcp.client.stdio.stdio_client", side_effect=_fake_stdio_cm
    ), patch("mcp.ClientSession", side_effect=fake_session_cm):
        asyncio.run(client.start())
        with pytest.raises(IsoKronMCPInvocationError) as excinfo:
            asyncio.run(client.invoke("kora__append_event", {}))
        asyncio.run(client.close())

    assert excinfo.value.tool_name == "kora__append_event"
    assert "transport reset" in excinfo.value.message


# ---------------------------------------------------------------------------
# Lifecycle — idempotency
# ---------------------------------------------------------------------------


def test_double_start_is_noop():
    """Calling start() twice doesn't re-open the transport."""
    open_count = {"n": 0}

    @asynccontextmanager
    async def counting_stdio_cm(_params):
        open_count["n"] += 1
        yield (MagicMock(), MagicMock())

    client = IsoKronMCPClient(_stdio_config(token="t"))
    with patch(
        "mcp.client.stdio.stdio_client", side_effect=counting_stdio_cm
    ), patch("mcp.ClientSession", side_effect=_fake_session_cm):
        asyncio.run(client.start())
        asyncio.run(client.start())  # second call should be a no-op
        asyncio.run(client.close())

    assert open_count["n"] == 1


def test_double_close_is_noop():
    client = IsoKronMCPClient(_stdio_config(token="t"))
    with patch(
        "mcp.client.stdio.stdio_client", side_effect=_fake_stdio_cm
    ), patch("mcp.ClientSession", side_effect=_fake_session_cm):
        asyncio.run(client.start())
        asyncio.run(client.close())
        asyncio.run(client.close())  # second close — no exception


# ---------------------------------------------------------------------------
# Connection-level integration
# ---------------------------------------------------------------------------


def test_connection_get_mcp_client_raises_before_start():
    """Pre-start access raises ``IsoKronConnectionError`` (not NotImplementedError)."""
    from plugins.memory.isokron.connection import (
        IsoKronConnection,
        IsoKronConnectionError,
    )

    conn = IsoKronConnection(_stdio_config(token="t"))
    with pytest.raises(IsoKronConnectionError):
        conn.get_mcp_client()
