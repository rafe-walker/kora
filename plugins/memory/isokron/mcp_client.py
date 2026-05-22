"""IsoKron MCP client transport wiring (KR-7a).

Outgoing-fixed-tools MCP client for the Kora runtime — connects to the
Sea MCP server (stdio subprocess or HTTP endpoint, config-driven) and
exposes :meth:`IsoKronMCPClient.invoke` for the ``kora__*`` tool calls
shipped by K-7 / K-8 / K-9 / K-10.

# Composition over subclassing

KR-7a's pre-flight recon found that ``tools/mcp_tool.py`` is the
canonical stdio/HTTP MCP client pattern in this repo, but
``MCPServerTask`` (line 1012) is the *incoming-tools* shape (discover
external server's tool catalog → register each tool back into the
agent's tool-use surface → forward calls through). The wrong shape for
subclassing — Kora needs the *outgoing-fixed-tools* shape (connect to
a known endpoint → invoke a known list of ``kora__*`` tools by name,
no discovery).

The underlying transport plumbing in ``tools/mcp_tool.py`` IS exactly
what we need. KR-7a composes its pure helpers:

* :func:`tools.mcp_tool._validate_remote_mcp_url` — HTTP URL validation
* :func:`tools.mcp_tool._resolve_stdio_command` — stdio command resolution
* :func:`tools.mcp_tool._build_safe_env` — subprocess env hygiene (PATH /
  HOME / XDG_*) so we don't leak secrets to spawned MCP servers
* :func:`tools.mcp_tool._sanitize_error` — credential redaction in
  error text before it surfaces to logs / the model
* :func:`tools.mcp_tool._exc_str` — exception-to-string with repr fallback
* :class:`tools.mcp_tool.InvalidMcpUrlError` — typed URL validation error

# Auth (service-token)

PM-side coordination filed at
``coordination/from_kora_pm/24_kora_runtime_service_token_provisioning_request.md``
for substrate-team to provision a long-lived service token for Kora.
KR-7a wires the auth-injection plumbing:

* HTTP transport: ``Authorization: Bearer <token>`` request header
* Stdio transport: ``KORA_SERVICE_TOKEN`` env var injected into the
  subprocess (so the Sea MCP server reads it via its own env-loading
  path, matching the ``service-token-auth.ts`` middleware shape from
  ``apps/api``)

Token-not-yet-issued is non-blocking: tests use a mock token and the
client correctness is testable independently. Production deploys wait
on the substrate-team K-N for token provisioning.

# Lifecycle

The mcp SDK's transport context managers (``stdio_client`` /
``streamablehttp_client``) yield ``(read, write)`` streams that must
stay open for the lifetime of the ``ClientSession``. KR-7a uses
``contextlib.AsyncExitStack`` to enter both async context managers at
``start()`` and exit them in reverse order at ``close()``. The session
stays open across invocations — same pattern as ``MCPServerTask`` but
without the queue indirection (the mcp SDK's request-id machinery
serializes ``call_tool`` internally; multiple awaiters are safe).

All async operations run on :class:`IsoKronConnection`'s dedicated
event loop. There's no need for ``tools/mcp_tool.py``'s separate
``_mcp_loop`` — the IsoKron loop is already a long-lived asyncio
event loop running on a daemon thread.
"""

from __future__ import annotations

import logging
import shlex
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from typing import Any, Optional

from .config import STDIO_SCHEME, IsoKronProviderConfig, parse_mcp_transport

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class IsoKronMCPInvocationError(RuntimeError):
    """Raised when a Sea MCP tool call returns an error response.

    Carries the tool name + sanitized error text so callers can
    decide whether to retry, surface to the model, or fail-closed.
    Error text passes through ``tools.mcp_tool._sanitize_error`` so
    no credentials leak into logs.
    """

    def __init__(self, tool_name: str, message: str):
        self.tool_name = tool_name
        self.message = message
        super().__init__(f"MCP tool {tool_name!r} failed: {message}")


class IsoKronMCPNotStartedError(RuntimeError):
    """Raised when ``invoke`` is called before ``start`` (or after ``close``)."""


# ---------------------------------------------------------------------------
# Helpers — re-exports from tools.mcp_tool with lazy import
# ---------------------------------------------------------------------------


def _import_mcp_helpers():
    """Lazy import the helpers from ``tools.mcp_tool``.

    Lazy so this module loads cleanly even when ``mcp`` SDK isn't
    installed (``is_available()`` on the provider already checks).
    """
    from tools.mcp_tool import (
        _build_safe_env,
        _exc_str,
        _resolve_stdio_command,
        _sanitize_error,
        _validate_remote_mcp_url,
    )

    return {
        "build_safe_env": _build_safe_env,
        "exc_str": _exc_str,
        "resolve_stdio_command": _resolve_stdio_command,
        "sanitize_error": _sanitize_error,
        "validate_remote_mcp_url": _validate_remote_mcp_url,
    }


# ---------------------------------------------------------------------------
# IsoKronMCPClient
# ---------------------------------------------------------------------------


class IsoKronMCPClient:
    """Long-lived outgoing MCP client for the Sea MCP server.

    Lifecycle (driven by :class:`IsoKronConnection`):

        client = IsoKronMCPClient(config)
        await client.start()                 # opens transport + session
        result = await client.invoke('kora__append_event', {...})
        await client.close()                 # tears down in reverse order

    Both ``start()`` and ``close()`` are idempotent — double-start /
    double-close are no-ops, matching the connection-lifecycle contract
    upstream callers rely on.
    """

    def __init__(self, config: IsoKronProviderConfig):
        self._config = config
        self._transport = parse_mcp_transport(config.mcp_endpoint)
        self._exit_stack: Optional[AsyncExitStack] = None
        self._session: Any = None  # mcp.ClientSession when started

        # KR-P2-L ST1 — health-rollup subsignal timestamps. Updated on
        # successful `.invoke()` so the dispatch_reachable /
        # last_successful_write / last_heartbeat subsignals have a
        # ground-truth read source. Three cuts because the cockpit
        # needs them broken out separately per R4.1 §9.7 freshness
        # thresholds (60s / 300s / 90s respectively).
        self._last_invoke_at: Optional[datetime] = None
        self._last_successful_append_event_at: Optional[datetime] = None
        self._last_successful_refresh_claim_at: Optional[datetime] = None

    @property
    def is_started(self) -> bool:
        return self._session is not None

    @property
    def transport(self) -> str:
        return self._transport

    @property
    def last_invoke_at(self) -> Optional[datetime]:
        """KR-P2-L ST1 — last successful MCP invoke timestamp.

        Source for the ``dispatch_reachable`` subsignal — if no
        successful invoke has happened recently the substrate is
        considered unreachable. Includes ALL tool invocations
        (append_event, claim, refresh, release, etc.).
        """
        return self._last_invoke_at

    @property
    def last_successful_append_event_at(self) -> Optional[datetime]:
        """KR-P2-L ST1 — last successful ``kora__append_event``
        invocation timestamp.

        Source for the ``last_successful_write`` subsignal — chain
        writes are the canonical evidence Kora is making observable
        progress against the substrate.
        """
        return self._last_successful_append_event_at

    @property
    def last_successful_refresh_claim_at(self) -> Optional[datetime]:
        """KR-P2-L ST1 — last successful ``kora__refresh_claim``
        invocation timestamp.

        Source for the ``last_heartbeat`` subsignal — a stale
        heartbeat means the worker leg has stalled mid-claim.
        """
        return self._last_successful_refresh_claim_at

    async def start(self) -> None:
        """Open the MCP transport + initialize the client session.

        Surfaces transport open errors directly (Rule-6: don't swallow
        connection failures). The exit stack records both context
        managers so ``close()`` unwinds them in reverse on shutdown.
        """
        if self.is_started:
            return
        helpers = _import_mcp_helpers()
        stack = AsyncExitStack()
        try:
            read_write = await self._open_transport(stack, helpers)
            session = await self._open_session(stack, read_write)
            self._exit_stack = stack
            self._session = session
            logger.info(
                "[isokron.mcp] client started (transport=%s, endpoint=%s)",
                self._transport,
                _redacted_endpoint(self._config.mcp_endpoint),
            )
        except BaseException:
            # On any failure during open, unwind the partial stack so
            # we don't leave a subprocess or socket dangling.
            await stack.aclose()
            raise

    async def _open_transport(self, stack: AsyncExitStack, helpers: dict):
        """Enter the transport async context manager, return ``(read, write)``."""
        if self._transport == "stdio":
            return await self._open_stdio_transport(stack, helpers)
        return await self._open_http_transport(stack, helpers)

    async def _open_stdio_transport(
        self, stack: AsyncExitStack, helpers: dict
    ):
        """Spawn the Sea MCP server as a subprocess + open stdio streams."""
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        remainder = self._config.mcp_endpoint[len(STDIO_SCHEME):].strip()
        if not remainder:
            raise ValueError(
                f"[isokron.mcp] stdio endpoint is empty after {STDIO_SCHEME!r} "
                f"prefix; got {self._config.mcp_endpoint!r}"
            )
        parts = shlex.split(remainder)
        if not parts:
            raise ValueError(
                f"[isokron.mcp] stdio command could not be parsed; got {remainder!r}"
            )
        command_raw, *args = parts

        env = helpers["build_safe_env"](None)
        # Inject the service token so the substrate-side server can
        # authenticate via its env-loading path. None-token still
        # spawns (tests + dev); production deploy waits on real token.
        token = self._config.resolve_service_token()
        if token:
            env["KORA_SERVICE_TOKEN"] = token

        command, env = helpers["resolve_stdio_command"](command_raw, env)
        params = StdioServerParameters(command=command, args=args, env=env)
        return await stack.enter_async_context(stdio_client(params))

    async def _open_http_transport(
        self, stack: AsyncExitStack, helpers: dict
    ):
        """Open the HTTP (Streamable HTTP) transport with auth header."""
        try:
            from mcp.client.streamable_http import (
                streamablehttp_client,
            )
        except ImportError:  # pragma: no cover — older mcp SDKs
            from mcp.client.streamable_http import (
                streamable_http_client as streamablehttp_client,
            )

        url = helpers["validate_remote_mcp_url"](
            "isokron", self._config.mcp_endpoint
        )
        http_kwargs: dict[str, Any] = {}
        token = self._config.resolve_service_token()
        if token:
            http_kwargs["headers"] = {"Authorization": f"Bearer {token}"}

        # The mcp SDK's streamablehttp_client yields a 3-tuple
        # ``(read, write, get_session_id)``; we drop the session-id
        # accessor since Kora's runtime doesn't need it. **kwargs
        # spreading matches the ``tools/mcp_tool.py`` pattern (line
        # 1477) and sidesteps ty's union-overload signature ambiguity
        # across mcp SDK versions.
        cm = streamablehttp_client(url, **http_kwargs)
        result = await stack.enter_async_context(cm)
        # Normalize to (read, write) — drop session_id getter if present.
        if isinstance(result, tuple) and len(result) >= 2:
            return result[0], result[1]
        return result

    async def _open_session(self, stack: AsyncExitStack, read_write):
        """Wrap the transport streams in an MCP ClientSession + initialize."""
        from mcp import ClientSession

        read, write = read_write
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    async def close(self) -> None:
        """Tear down the session + transport. Idempotent."""
        if self._exit_stack is None:
            return
        stack = self._exit_stack
        self._exit_stack = None
        self._session = None
        try:
            await stack.aclose()
            logger.info("[isokron.mcp] client closed")
        except Exception:  # pragma: no cover — log + swallow on teardown
            logger.exception("[isokron.mcp] error during close — continuing")

    async def invoke(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Call a substrate-side MCP tool by name.

        Returns the tool's structured content dict (e.g.
        ``{"event_id": "<uuid>"}`` for ``kora__append_event``).
        Raises :class:`IsoKronMCPInvocationError` if the tool returns
        an MCP-error response or a non-dict payload.

        Caller is responsible for the ``args`` shape — KR-7a doesn't
        validate per-tool; the substrate-side Zod schemas validate
        when the call lands.
        """
        if not self.is_started:
            raise IsoKronMCPNotStartedError(
                f"[isokron.mcp] invoke({tool_name!r}) before start()"
            )
        helpers = _import_mcp_helpers()
        try:
            result = await self._session.call_tool(tool_name, args)
        except Exception as exc:
            raise IsoKronMCPInvocationError(
                tool_name,
                helpers["sanitize_error"](helpers["exc_str"](exc)),
            ) from exc
        if getattr(result, "isError", False):
            text = _extract_text_content(result) or "<no error text>"
            raise IsoKronMCPInvocationError(tool_name, helpers["sanitize_error"](text))
        payload = _extract_structured_content(result)
        if payload is None:
            raise IsoKronMCPInvocationError(
                tool_name,
                f"tool returned no structured content "
                f"(content blocks: {_describe_content(result)!r})",
            )
        # KR-P2-L ST1 — health-rollup subsignal timestamps. Set AFTER
        # the success check (failed invokes don't bump these — they
        # would corrupt the dispatch_reachable signal).
        now = datetime.now(timezone.utc)
        self._last_invoke_at = now
        if tool_name == "kora__append_event":
            self._last_successful_append_event_at = now
        elif tool_name == "kora__refresh_claim":
            self._last_successful_refresh_claim_at = now
        return payload


# ---------------------------------------------------------------------------
# Result projection helpers
# ---------------------------------------------------------------------------


def _extract_structured_content(result: Any) -> Optional[dict[str, Any]]:
    """Return ``result.structuredContent`` if present + dict-shaped.

    Modern mcp SDKs return tool results with a ``structuredContent``
    field for JSON-typed outputs. Fall back to parsing the first
    text-content block as JSON for older / text-only tools.
    """
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    text = _extract_text_content(result)
    if text is None:
        return None
    import json as _json

    try:
        parsed = _json.loads(text)
    except _json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def _extract_text_content(result: Any) -> Optional[str]:
    """Concatenate text content blocks from an MCP CallToolResult."""
    blocks = getattr(result, "content", None) or []
    parts: list[str] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts) if parts else None


def _describe_content(result: Any) -> list[str]:
    """Short list-of-type-names for content blocks. Used in error messages."""
    blocks = getattr(result, "content", None) or []
    return [type(block).__name__ for block in blocks]


def _redacted_endpoint(endpoint: str) -> str:
    """Render the endpoint for logs without leaking embedded credentials.

    Operator config typically puts ``stdio://`` commands or ``https://``
    URLs here — neither should contain a token directly, but defensive
    redaction matches the ``_sanitize_error`` discipline.
    """
    if endpoint.startswith(STDIO_SCHEME):
        return endpoint  # stdio commands don't carry credentials inline
    try:
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(endpoint)
        # Strip userinfo if any (rare but possible in test fixtures).
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))
    except Exception:  # pragma: no cover — defensive
        return "<endpoint>"
