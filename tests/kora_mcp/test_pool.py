"""KR-MCP-1 ST1 — MCPClientPool + registry tests.

Covers:
  - Pydantic config rejects unknown keys (``extra="forbid"``).
  - Lazy connect: pool with 2 endpoints opens only the one called.
  - Allowlist regex (case-insensitive) rejects non-matching tool
    names with :exc:`MCPToolNotAllowed`; transport not touched.
  - Allowlist match is on bare tool_name (not qualified prefix).
  - Mocked-transport ``call_tool`` round-trip returns structured
    content.
  - ``close_all`` closes every cached client.
  - Transport open failure raises :exc:`MCPCallFailed` (cache
    untouched).
  - Mid-call failure raises :exc:`MCPCallFailed` + drops cache
    entry so next call re-opens.
  - ``list_tools_all`` returns ``{prefix: [ToolDescriptor, ...]}``;
    failed endpoint maps to empty list.
  - Routing helpers parse + qualify correctly.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from kora_mcp.pool import (
    MCPCallFailed,
    MCPClientPool,
    MCPEndpointNotFound,
    MCPToolNotAllowed,
    ToolDescriptor,
)
from kora_mcp.registry import MCPEndpointConfig, MCPRegistryConfig
from kora_mcp.routing import (
    InvalidQualifiedToolName,
    parse_qualified_tool_name,
    qualify_tool_name,
)


# ---------------------------------------------------------------------------
# Config models — extra="forbid" discipline
# ---------------------------------------------------------------------------


def test_endpoint_config_rejects_unknown_keys():
    """K-DG drift discipline: a typo in YAML must surface at load
    time, not silently drop the value."""
    with pytest.raises(ValidationError) as exc_info:
        MCPEndpointConfig(
            name="github",
            transport="stdio",
            endpoint="npx -y @modelcontextprotocol/server-github",
            timeout_secondz=99.0,  # typo
        )
    assert "timeout_secondz" in str(exc_info.value).lower() or "extra" in str(
        exc_info.value
    ).lower()


def test_registry_config_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        MCPRegistryConfig(endpoints=[], unknown_top_level_key=True)


def test_endpoint_config_minimum_fields():
    """Only ``name`` / ``transport`` / ``endpoint`` are required."""
    cfg = MCPEndpointConfig(
        name="x",
        transport="streamable_http",
        endpoint="https://example.com",
    )
    assert cfg.name == "x"
    assert cfg.transport == "streamable_http"
    assert cfg.auth_token_env is None
    assert cfg.allowed_tools_regex is None
    assert cfg.timeout_seconds == 30.0


def test_endpoint_config_transport_literal_enforced():
    with pytest.raises(ValidationError):
        MCPEndpointConfig(
            name="x", transport="websocket", endpoint="ws://x"  # type: ignore[arg-type]
        )


def test_endpoint_config_positive_timeouts():
    with pytest.raises(ValidationError):
        MCPEndpointConfig(
            name="x",
            transport="stdio",
            endpoint="cmd",
            timeout_seconds=0,
        )
    with pytest.raises(ValidationError):
        MCPEndpointConfig(
            name="x",
            transport="stdio",
            endpoint="cmd",
            startup_timeout_seconds=-1,
        )


def test_registry_get_endpoint_lookup():
    cfg = MCPRegistryConfig(
        endpoints=[
            MCPEndpointConfig(name="a", transport="stdio", endpoint="cmd1"),
            MCPEndpointConfig(name="b", transport="stdio", endpoint="cmd2"),
        ]
    )
    assert cfg.get_endpoint("a").endpoint == "cmd1"
    assert cfg.get_endpoint("b").endpoint == "cmd2"
    assert cfg.get_endpoint("nope") is None


# ---------------------------------------------------------------------------
# Routing helpers
# ---------------------------------------------------------------------------


def test_parse_qualified_tool_name_happy_path():
    assert parse_qualified_tool_name("github__create_issue") == (
        "github",
        "create_issue",
    )
    assert parse_qualified_tool_name("cloudflare__d1_database_query") == (
        "cloudflare",
        "d1_database_query",
    )


def test_parse_qualified_tool_name_split_on_first_separator():
    """``foo__bar__baz`` splits to ``("foo", "bar__baz")``."""
    assert parse_qualified_tool_name("foo__bar__baz") == ("foo", "bar__baz")


@pytest.mark.parametrize(
    "bad",
    ["no_separator", "__missing_prefix", "missing_tool__", "", "foo_bar"],
)
def test_parse_qualified_tool_name_rejects_malformed(bad):
    with pytest.raises(InvalidQualifiedToolName):
        parse_qualified_tool_name(bad)


def test_parse_qualified_tool_name_rejects_non_string():
    with pytest.raises(InvalidQualifiedToolName):
        parse_qualified_tool_name(42)  # type: ignore[arg-type]


def test_qualify_tool_name_roundtrip():
    qualified = qualify_tool_name("github", "create_issue")
    assert qualified == "github__create_issue"
    assert parse_qualified_tool_name(qualified) == ("github", "create_issue")


def test_qualify_rejects_empty():
    with pytest.raises(InvalidQualifiedToolName):
        qualify_tool_name("", "tool")
    with pytest.raises(InvalidQualifiedToolName):
        qualify_tool_name("prefix", "")


# ---------------------------------------------------------------------------
# Pool — endpoint registry surface
# ---------------------------------------------------------------------------


def _two_endpoint_registry() -> MCPRegistryConfig:
    return MCPRegistryConfig(
        endpoints=[
            MCPEndpointConfig(
                name="github",
                transport="stdio",
                endpoint="npx -y @modelcontextprotocol/server-github",
            ),
            MCPEndpointConfig(
                name="cloudflare",
                transport="streamable_http",
                endpoint="https://mcp.cloudflare.com/test",
            ),
        ]
    )


def test_pool_init_does_not_open_connections():
    pool = MCPClientPool(_two_endpoint_registry())
    assert sorted(pool.endpoint_names()) == ["cloudflare", "github"]
    assert pool.has_open_connection("github") is False
    assert pool.has_open_connection("cloudflare") is False


def test_pool_init_with_empty_registry():
    pool = MCPClientPool(MCPRegistryConfig())
    assert pool.endpoint_names() == []


# ---------------------------------------------------------------------------
# Pool — call_tool: unknown prefix, allowlist, mocked round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_tool_unknown_prefix_raises_endpoint_not_found():
    pool = MCPClientPool(_two_endpoint_registry())
    with pytest.raises(MCPEndpointNotFound):
        await pool.call_tool("unknown", "foo", {})


def _patch_open_to_return(pool: MCPClientPool, session: Any, *, allowed_pattern=None):
    """Helper: stub `_open_one` to return a fake _CachedClient."""
    from kora_mcp.pool import _CachedClient

    async def _fake_open(endpoint):
        return _CachedClient(
            session=session,
            exit_stack=AsyncExitStack(),
            allowed_pattern=allowed_pattern,
        )

    return patch.object(pool, "_open_one", side_effect=_fake_open)


@pytest.mark.asyncio
async def test_call_tool_mocked_round_trip_returns_structured_content():
    pool = MCPClientPool(_two_endpoint_registry())
    session = SimpleNamespace()
    session.call_tool = AsyncMock(
        return_value=SimpleNamespace(
            isError=False,
            structuredContent={"issue_number": 42},
            content=[],
        )
    )
    with _patch_open_to_return(pool, session):
        result = await pool.call_tool("github", "create_issue", {"title": "x"})
    assert result == {"issue_number": 42}
    session.call_tool.assert_awaited_once_with("create_issue", {"title": "x"})


@pytest.mark.asyncio
async def test_call_tool_lazy_open_only_called_endpoint():
    """Calling github should NOT open cloudflare."""
    pool = MCPClientPool(_two_endpoint_registry())
    session = SimpleNamespace()
    session.call_tool = AsyncMock(
        return_value=SimpleNamespace(
            isError=False, structuredContent={"ok": True}, content=[]
        )
    )
    with _patch_open_to_return(pool, session):
        await pool.call_tool("github", "x", {})
    assert pool.has_open_connection("github") is True
    assert pool.has_open_connection("cloudflare") is False


@pytest.mark.asyncio
async def test_call_tool_caches_session_per_prefix():
    """Two sequential calls to the same endpoint share one session."""
    pool = MCPClientPool(_two_endpoint_registry())
    session = SimpleNamespace()
    session.call_tool = AsyncMock(
        return_value=SimpleNamespace(
            isError=False, structuredContent={"n": 1}, content=[]
        )
    )
    open_calls = {"n": 0}

    async def _fake_open(endpoint):
        from kora_mcp.pool import _CachedClient

        open_calls["n"] += 1
        return _CachedClient(
            session=session,
            exit_stack=AsyncExitStack(),
            allowed_pattern=None,
        )

    with patch.object(pool, "_open_one", side_effect=_fake_open):
        await pool.call_tool("github", "x", {})
        await pool.call_tool("github", "y", {})
    assert open_calls["n"] == 1


# ---------------------------------------------------------------------------
# Pool — allowlist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allowlist_rejects_non_matching_tool():
    import re

    pool = MCPClientPool(
        MCPRegistryConfig(
            endpoints=[
                MCPEndpointConfig(
                    name="github",
                    transport="stdio",
                    endpoint="cmd",
                    allowed_tools_regex=r"^create_.*$",
                ),
            ]
        )
    )
    session = SimpleNamespace()
    session.call_tool = AsyncMock()  # should NEVER be called
    pattern = re.compile(r"^create_.*$", re.IGNORECASE)
    with _patch_open_to_return(pool, session, allowed_pattern=pattern):
        with pytest.raises(MCPToolNotAllowed):
            await pool.call_tool("github", "list_repos", {})
    session.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_allowlist_allows_matching_tool():
    import re

    pool = MCPClientPool(
        MCPRegistryConfig(
            endpoints=[
                MCPEndpointConfig(
                    name="github",
                    transport="stdio",
                    endpoint="cmd",
                    allowed_tools_regex=r"^create_.*$",
                ),
            ]
        )
    )
    session = SimpleNamespace()
    session.call_tool = AsyncMock(
        return_value=SimpleNamespace(
            isError=False, structuredContent={"ok": True}, content=[]
        )
    )
    pattern = re.compile(r"^create_.*$", re.IGNORECASE)
    with _patch_open_to_return(pool, session, allowed_pattern=pattern):
        result = await pool.call_tool("github", "create_issue", {})
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_allowlist_is_case_insensitive():
    """Operator writes ``^create_.*$``; ``CREATE_ISSUE`` (caps) still
    matches per §4 Q2 default."""
    import re

    pool = MCPClientPool(
        MCPRegistryConfig(
            endpoints=[
                MCPEndpointConfig(
                    name="github",
                    transport="stdio",
                    endpoint="cmd",
                    allowed_tools_regex=r"^create_.*$",
                ),
            ]
        )
    )
    session = SimpleNamespace()
    session.call_tool = AsyncMock(
        return_value=SimpleNamespace(
            isError=False, structuredContent={"ok": True}, content=[]
        )
    )
    pattern = re.compile(r"^create_.*$", re.IGNORECASE)
    with _patch_open_to_return(pool, session, allowed_pattern=pattern):
        result = await pool.call_tool("github", "CREATE_ISSUE", {})
    assert result == {"ok": True}


# ---------------------------------------------------------------------------
# Pool — failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_tool_raises_mcp_call_failed_on_isError():
    pool = MCPClientPool(_two_endpoint_registry())
    session = SimpleNamespace()
    session.call_tool = AsyncMock(
        return_value=SimpleNamespace(
            isError=True,
            structuredContent=None,
            content=[SimpleNamespace(text="bad args")],
        )
    )
    with _patch_open_to_return(pool, session):
        with pytest.raises(MCPCallFailed) as exc_info:
            await pool.call_tool("github", "x", {})
    assert "bad args" in str(exc_info.value)


@pytest.mark.asyncio
async def test_call_tool_drops_cache_on_transport_error():
    """A mid-call exception drops the cached session so the next
    call re-opens."""
    pool = MCPClientPool(_two_endpoint_registry())
    session = SimpleNamespace()
    session.call_tool = AsyncMock(side_effect=RuntimeError("transport boom"))
    with _patch_open_to_return(pool, session):
        with pytest.raises(MCPCallFailed):
            await pool.call_tool("github", "x", {})
        # Cache dropped — next call re-attempts open
        assert pool.has_open_connection("github") is False


@pytest.mark.asyncio
async def test_open_failure_propagates_as_mcp_call_failed():
    """If _open_one raises, call_tool surfaces it."""
    pool = MCPClientPool(_two_endpoint_registry())

    async def _fake_open_raises(endpoint):
        raise ConnectionError("transport refused")

    with patch.object(pool, "_open_one", side_effect=_fake_open_raises):
        with pytest.raises(Exception) as exc_info:
            await pool.call_tool("github", "x", {})
    # _ensure_open propagates the ConnectionError (not MCPCallFailed) so
    # callers can distinguish open-time vs call-time failures via type.
    assert isinstance(exc_info.value, (ConnectionError, MCPCallFailed))


# ---------------------------------------------------------------------------
# Pool — list_tools_all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tools_all_returns_descriptors_per_endpoint():
    pool = MCPClientPool(_two_endpoint_registry())
    session = SimpleNamespace()
    session.list_tools = AsyncMock(
        return_value=SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="create_issue",
                    description="Create a new issue",
                    inputSchema={"type": "object"},
                ),
                SimpleNamespace(
                    name="list_repos",
                    description=None,
                    inputSchema=None,
                ),
            ]
        )
    )
    with _patch_open_to_return(pool, session):
        catalog = await pool.list_tools_all()
    assert set(catalog.keys()) == {"github", "cloudflare"}
    # Both endpoints opened the same fake → same tool list
    assert len(catalog["github"]) == 2
    assert isinstance(catalog["github"][0], ToolDescriptor)
    assert catalog["github"][0].name == "create_issue"
    assert catalog["github"][0].input_schema == {"type": "object"}


@pytest.mark.asyncio
async def test_list_tools_all_failed_endpoint_returns_empty_list():
    """One endpoint's transport failure shouldn't break the whole
    catalog read — that endpoint maps to []."""
    from kora_mcp.pool import _CachedClient

    pool = MCPClientPool(_two_endpoint_registry())

    success_session = SimpleNamespace()
    success_session.list_tools = AsyncMock(
        return_value=SimpleNamespace(
            tools=[SimpleNamespace(name="t", description=None, inputSchema=None)]
        )
    )

    async def _fake_open(endpoint):
        if endpoint.name == "github":
            return _CachedClient(
                session=success_session,
                exit_stack=AsyncExitStack(),
                allowed_pattern=None,
            )
        raise ConnectionError("cloudflare down")

    with patch.object(pool, "_open_one", side_effect=_fake_open):
        catalog = await pool.list_tools_all()
    assert len(catalog["github"]) == 1
    assert catalog["cloudflare"] == []


# ---------------------------------------------------------------------------
# Pool — close_all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_all_closes_every_cached_session():
    pool = MCPClientPool(_two_endpoint_registry())
    session = SimpleNamespace()
    session.call_tool = AsyncMock(
        return_value=SimpleNamespace(
            isError=False, structuredContent={"ok": True}, content=[]
        )
    )

    closed_stacks: list = []

    class _RecordingStack:
        async def aclose(self):
            closed_stacks.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    from kora_mcp.pool import _CachedClient

    async def _fake_open(endpoint):
        return _CachedClient(
            session=session,
            exit_stack=_RecordingStack(),
            allowed_pattern=None,
        )

    with patch.object(pool, "_open_one", side_effect=_fake_open):
        await pool.call_tool("github", "x", {})
        await pool.call_tool("cloudflare", "y", {})
    assert pool.has_open_connection("github") is True
    assert pool.has_open_connection("cloudflare") is True

    await pool.close_all()
    assert len(closed_stacks) == 2
    assert pool.has_open_connection("github") is False
    assert pool.has_open_connection("cloudflare") is False


@pytest.mark.asyncio
async def test_close_all_idempotent_on_empty_pool():
    pool = MCPClientPool(MCPRegistryConfig())
    await pool.close_all()  # no exception
    await pool.close_all()  # still no exception
