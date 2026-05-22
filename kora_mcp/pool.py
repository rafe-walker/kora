"""MCPClientPool — lazy-connecting multi-MCP client manager (KR-MCP-1 ST1).

Holds N :class:`MCPEndpointConfig` entries; opens a single
:class:`mcp.ClientSession` per endpoint on first :meth:`call_tool`
or :meth:`list_tools_all` access; caches the session for subsequent
calls; closes everything via :meth:`close_all`.

# Connection-error policy

  - Transport open failure (subprocess crash, HTTP DNS failure,
    auth failure) → :exc:`MCPCallFailed` from
    :meth:`call_tool`. The pool does NOT auto-retry — callers
    decide the retry policy.
  - Mid-call transport error → :exc:`MCPCallFailed`. The cached
    session is closed + dropped from the cache so the next call
    re-opens (matches the existing IsoKronMCPClient pattern).
  - Allowlist rejection → :exc:`MCPToolNotAllowed`. No transport
    activity at all.

# Allowlist matching

When :attr:`MCPEndpointConfig.allowed_tools_regex` is set, the
pool compiles it with ``re.IGNORECASE`` (per §4 Q2 default) and
calls :func:`re.search` against the bare ``tool_name`` (NOT the
qualified ``<prefix>__<tool_name>``). Anchors are operator's
responsibility — write ``"^create_.*$"`` to allow only
``create_*`` tools.

# Concurrency

The pool's cache is guarded by per-prefix locks so concurrent
:meth:`call_tool` invocations for the same endpoint share one
session (only the first opens; subsequent await the same connect).
Different prefixes open in parallel without contention.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Optional

from kora_mcp.registry import MCPEndpointConfig, MCPRegistryConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MCPCallFailed(RuntimeError):
    """Transport-tier or protocol-tier failure during an MCP call.

    Raised by :meth:`MCPClientPool.call_tool` and
    :meth:`MCPClientPool.list_tools_all` when the underlying MCP
    session can't open or the call itself raises.
    """


class MCPToolNotAllowed(RuntimeError):
    """The endpoint's ``allowed_tools_regex`` rejected the
    ``tool_name``. No transport call is attempted.

    Operator approval required: either edit the regex, or grant
    out-of-band.
    """


class MCPEndpointNotFound(KeyError):
    """No endpoint with the given ``prefix`` is registered."""


# ---------------------------------------------------------------------------
# Tool descriptor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    """Projection of one MCP ``Tool`` for the operator-UI surface.

    Mirrors the fields the MCP spec pins on the wire (``name``,
    ``description``, ``inputSchema``); kept as a plain dataclass
    so consumers don't have to import the mcp library to consume
    the listing.
    """

    name: str  # bare name, NOT prefixed (use qualify_tool_name to prefix)
    description: Optional[str]
    input_schema: Optional[dict[str, Any]]


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------


@dataclass
class _CachedClient:
    """Internal per-endpoint session cache entry."""

    session: Any  # mcp.ClientSession
    exit_stack: AsyncExitStack
    allowed_pattern: Optional[re.Pattern[str]]


class MCPClientPool:
    """Lazy-connecting multi-MCP client manager.

    Construct with a populated :class:`MCPRegistryConfig`; the pool
    does NOT open any connections until :meth:`call_tool` or
    :meth:`list_tools_all` is invoked. Use :meth:`close_all` on
    shutdown to release all cached sessions.

    Thread-safe via per-prefix :class:`asyncio.Lock` instances —
    concurrent calls to the same endpoint share one session;
    concurrent calls to different endpoints open in parallel.
    """

    def __init__(self, config: MCPRegistryConfig) -> None:
        self._config = config
        self._by_name: dict[str, MCPEndpointConfig] = {
            endpoint.name: endpoint for endpoint in config.endpoints
        }
        # Per-prefix lock — guards the lazy-open + cache populate.
        # Different prefixes get different locks → independent opens.
        self._open_locks: dict[str, asyncio.Lock] = {}
        # Cached sessions, keyed by prefix. None entry = open in
        # progress; populated entry = ready to use.
        self._cache: dict[str, _CachedClient] = {}

    # ------------------------------------------------------------------
    # Public read surface
    # ------------------------------------------------------------------

    @property
    def config(self) -> MCPRegistryConfig:
        return self._config

    def endpoint_names(self) -> list[str]:
        """Routing prefixes for all registered endpoints."""
        return list(self._by_name.keys())

    def has_open_connection(self, prefix: str) -> bool:
        """True if ``prefix``'s session is currently cached open."""
        return prefix in self._cache

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------

    async def call_tool(
        self,
        prefix: str,
        tool_name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """Route a tool call to the endpoint at ``prefix``.

        Lazily opens the session on first use, caches it, applies
        the allowlist, awaits the MCP ``call_tool`` response.

        Args:
            prefix: Endpoint routing prefix (``name`` field on
                :class:`MCPEndpointConfig`).
            tool_name: Bare tool name (no prefix). E.g.
                ``"create_issue"`` for the GitHub MCP's
                ``github__create_issue``.
            args: Tool arguments forwarded verbatim. Caller is
                responsible for the shape; the substrate-side MCP
                tool's schema validates on dispatch.

        Returns:
            The structured tool response as a dict. Shape depends
            on the tool; the pool doesn't re-validate.

        Raises:
            MCPEndpointNotFound: ``prefix`` not in registry.
            MCPToolNotAllowed: ``allowed_tools_regex`` rejected.
            MCPCallFailed: transport open or call raised; cached
                session (if any) is dropped before raising so the
                next call re-opens cleanly.
        """
        client = await self._ensure_open(prefix)
        if client.allowed_pattern is not None:
            if not client.allowed_pattern.search(tool_name):
                raise MCPToolNotAllowed(
                    f"tool {tool_name!r} at endpoint {prefix!r} is "
                    f"not in the allowlist "
                    f"(regex={self._by_name[prefix].allowed_tools_regex!r}). "
                    f"Operator must update the allowlist."
                )
        try:
            timeout = self._by_name[prefix].timeout_seconds
            result = await asyncio.wait_for(
                client.session.call_tool(tool_name, args), timeout=timeout
            )
        except Exception as exc:
            # Drop the cached client — next call re-opens.
            await self._close_one(prefix)
            raise MCPCallFailed(
                f"call_tool({prefix}/{tool_name}) failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return _project_call_result(result)

    async def list_tools(self, prefix: str) -> list[ToolDescriptor]:
        """Per-endpoint ``list_tools`` with full error surfacing.

        Mirrors :meth:`call_tool`'s lazy-open + per-call timeout +
        cache-drop-on-error pattern. Returns the projected tool
        descriptors on success; raises :exc:`MCPCallFailed` on any
        transport / protocol error.

        Used by callers that need per-endpoint health detail (e.g.
        KR-MCP-CONSUMPTION ST2's health-check task populating
        ``last_error`` per prefix). Distinct from
        :meth:`list_tools_all` which is best-effort across the
        whole catalog + maps failed endpoints to empty lists.
        """
        client = await self._ensure_open(prefix)
        try:
            timeout = self._by_name[prefix].timeout_seconds
            result = await asyncio.wait_for(
                client.session.list_tools(), timeout=timeout
            )
        except Exception as exc:
            await self._close_one(prefix)
            raise MCPCallFailed(
                f"list_tools({prefix}) failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return _project_tools_result(result)

    async def list_tools_all(self) -> dict[str, list[ToolDescriptor]]:
        """Open EVERY configured endpoint + return its tool catalog.

        Returns ``{prefix: [tool_descriptor, ...]}``. An endpoint
        that fails to open (transport error, auth missing) maps to
        an empty list + a WARN log line — the operator-UI surfaces
        endpoint health separately; this method is best-effort
        for the catalog read.
        """
        catalog: dict[str, list[ToolDescriptor]] = {}
        for prefix in self.endpoint_names():
            try:
                client = await self._ensure_open(prefix)
                timeout = self._by_name[prefix].timeout_seconds
                result = await asyncio.wait_for(
                    client.session.list_tools(), timeout=timeout
                )
            except Exception as exc:
                logger.warning(
                    "[kora_mcp.pool] list_tools(%s) failed: %r — "
                    "returning empty list for this endpoint",
                    prefix,
                    exc,
                )
                catalog[prefix] = []
                continue
            catalog[prefix] = _project_tools_result(result)
        return catalog

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close_all(self) -> None:
        """Close every cached session. Idempotent — safe to call
        multiple times. Best-effort: errors closing individual
        sessions log WARN + continue."""
        for prefix in list(self._cache.keys()):
            await self._close_one(prefix)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _ensure_open(self, prefix: str) -> _CachedClient:
        """Return a cached open client for ``prefix``, opening one
        if needed. Per-prefix lock prevents concurrent re-opens."""
        if prefix not in self._by_name:
            raise MCPEndpointNotFound(
                f"no MCP endpoint registered with prefix {prefix!r}; "
                f"registered: {sorted(self._by_name.keys())}"
            )
        cached = self._cache.get(prefix)
        if cached is not None:
            return cached

        lock = self._open_locks.setdefault(prefix, asyncio.Lock())
        async with lock:
            cached = self._cache.get(prefix)
            if cached is not None:
                return cached
            cached = await self._open_one(self._by_name[prefix])
            self._cache[prefix] = cached
            return cached

    async def _open_one(self, endpoint: MCPEndpointConfig) -> _CachedClient:
        """Open the MCP transport + session for ``endpoint``.

        Wraps the mcp lib's ``stdio_client`` /
        ``streamablehttp_client`` + ``ClientSession`` in an
        :class:`AsyncExitStack` so :meth:`_close_one` unwinds
        cleanly. Mirrors the existing IsoKronMCPClient pattern.
        """
        from mcp import ClientSession

        stack = AsyncExitStack()
        try:
            if endpoint.transport == "stdio":
                streams = await asyncio.wait_for(
                    self._open_stdio_transport(stack, endpoint),
                    timeout=endpoint.startup_timeout_seconds,
                )
            else:  # streamable_http
                streams = await asyncio.wait_for(
                    self._open_http_transport(stack, endpoint),
                    timeout=endpoint.startup_timeout_seconds,
                )
            read, write, *_ = streams  # streamable_http returns 3-tuple
            session: Any = await stack.enter_async_context(
                ClientSession(read, write)
            )
            await asyncio.wait_for(
                session.initialize(),
                timeout=endpoint.startup_timeout_seconds,
            )
        except Exception:
            await stack.aclose()
            raise

        allowed_pattern: Optional[re.Pattern[str]] = None
        if endpoint.allowed_tools_regex is not None:
            allowed_pattern = re.compile(
                endpoint.allowed_tools_regex, re.IGNORECASE
            )

        return _CachedClient(
            session=session,
            exit_stack=stack,
            allowed_pattern=allowed_pattern,
        )

    async def _open_stdio_transport(
        self, stack: AsyncExitStack, endpoint: MCPEndpointConfig
    ) -> Any:
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        argv = shlex.split(endpoint.endpoint)
        if not argv:
            raise MCPCallFailed(
                f"endpoint {endpoint.name!r} stdio endpoint is empty"
            )
        env = self._build_subprocess_env(endpoint)
        params = StdioServerParameters(
            command=argv[0],
            args=argv[1:],
            env=env,
        )
        return await stack.enter_async_context(stdio_client(params))

    async def _open_http_transport(
        self, stack: AsyncExitStack, endpoint: MCPEndpointConfig
    ) -> Any:
        try:
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError:  # pragma: no cover — older mcp lib
            from mcp.client.streamable_http import (  # type: ignore[no-redef]
                streamable_http_client as streamablehttp_client,
            )

        headers: dict[str, str] = {}
        if endpoint.auth_token_env:
            token = os.environ.get(endpoint.auth_token_env, "").strip()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return await stack.enter_async_context(
            streamablehttp_client(endpoint.endpoint, headers=headers or None)
        )

    @staticmethod
    def _build_subprocess_env(
        endpoint: MCPEndpointConfig,
    ) -> Optional[dict[str, str]]:
        """Build the env dict for a stdio subprocess.

        Pass PATH / HOME / XDG_* through (subprocesses need them),
        inject the auth token if configured, drop everything else.
        Same hygiene as IsoKronMCPClient's stdio path.
        """
        safe_passthrough = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")
        env: dict[str, str] = {
            key: os.environ[key]
            for key in safe_passthrough
            if key in os.environ
        }
        for key, value in os.environ.items():
            if key.startswith("XDG_"):
                env[key] = value
        if endpoint.auth_token_env:
            token = os.environ.get(endpoint.auth_token_env, "").strip()
            if token:
                # The MCP server reads the token from a conventional
                # env var name; the operator-supplied env var is
                # plumbed through with that name so the server picks
                # it up. The convention is server-specific (e.g.
                # GITHUB_PERSONAL_ACCESS_TOKEN for the GitHub MCP);
                # operators set BOTH KORA_MCP_<name>_TOKEN +
                # whatever the server needs in the env, OR rely on
                # the catalog (ST2) to bridge.
                env[endpoint.auth_token_env] = token
        return env if env else None

    async def _close_one(self, prefix: str) -> None:
        cached = self._cache.pop(prefix, None)
        if cached is None:
            return
        try:
            await cached.exit_stack.aclose()
        except Exception:
            logger.warning(
                "[kora_mcp.pool] close(%s) raised; cache entry already "
                "dropped",
                prefix,
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Result projection (mcp.ClientSession returns lib-typed objects;
# project to plain dicts/dataclasses so consumers don't import mcp)
# ---------------------------------------------------------------------------


def _project_call_result(result: Any) -> dict[str, Any]:
    """Project an MCP ``CallToolResult`` to a dict.

    MCP returns ``CallToolResult`` with ``content`` (list of content
    parts) + optional ``structuredContent`` + ``isError``. We surface
    the structured content as the primary payload + carry the text
    content as a fallback for tools that don't return JSON.
    """
    is_error = bool(getattr(result, "isError", False))
    if is_error:
        text = _extract_text(result) or "<no error text>"
        raise MCPCallFailed(f"tool returned error: {text}")
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    text = _extract_text(result)
    return {"_text": text} if text is not None else {}


def _project_tools_result(result: Any) -> list[ToolDescriptor]:
    """Project an MCP ``ListToolsResult`` to ``[ToolDescriptor, ...]``."""
    tools = getattr(result, "tools", None) or []
    out: list[ToolDescriptor] = []
    for tool in tools:
        out.append(
            ToolDescriptor(
                name=getattr(tool, "name", ""),
                description=getattr(tool, "description", None),
                input_schema=getattr(tool, "inputSchema", None),
            )
        )
    return out


def _extract_text(result: Any) -> Optional[str]:
    """Best-effort text extraction from a content-block list."""
    blocks = getattr(result, "content", None) or []
    parts: list[str] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts) if parts else None
