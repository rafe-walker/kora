"""MCP HTTP listener (KR-D-DAEMON ST2 — listener "mcp").

Mounts MCP-protocol routes onto the existing admin FastAPI app at
``/mcp``. Distinct from:

  - ``mcp_serve.py`` (top-level) — the EXISTING stdio messaging-bridge
    that ``kora mcp serve`` runs today. ST2 does NOT touch it.
  - ``agent/transports/kora_tools_mcp_server.py`` — the codex-subprocess
    Hermes-tools bridge. ST2 does NOT touch it.

# Scope (ST2 — MINIMAL)

A single tool: ``kora__daemon_status`` returning the coordinator's
state via ``DaemonCoordinator.get_status()``. This proves the
``/mcp`` routing + bearer-auth + JSON-RPC envelope. The real
agent-facing tool surface (``kora__trigger_action``, ``kora__pause``,
``kora__get_ledger_entries``, ``kora__heartbeat``) lands in the
follow-on bucket ``KR-MCP-RUNTIME-SURFACE`` after the daemon is live
+ we've decided which tools to expose.

# Auth

Bearer-token via the ``Authorization`` header. Token from
``KORA_MCP_BEARER_TOKEN`` env (Doppler-injected from
``kora-runtime-substrate``). If the env var is missing OR empty at
startup, the listener REFUSES TO REGISTER — fail-CLOSED per
``feedback_fail_closed_by_default_security_infra``. A daemon that
serves MCP traffic without auth is worse than one that refuses to
expose MCP at all.

# Wire format

Two surfaces:

  - ``GET /mcp/tools/list`` — convenience endpoint matching the
    bucket-spec test (``curl -H "Authorization: Bearer $TOK"
    localhost:9119/mcp/tools/list``). Returns the tools array
    directly: ``{"tools": [{"name": "...", "description": "...",
    "inputSchema": {...}}]}``.
  - ``POST /mcp`` — JSON-RPC 2.0 envelope for full MCP-client
    compatibility. Handles ``tools/list`` + ``tools/call`` methods.
    Other methods return JSON-RPC ``-32601 method not found``.

Streamable-HTTP transport (chunked SSE) is NOT implemented this ST —
the minimal surface is request/response only. Adding streamable
comes with the real tool surface in the follow-on bucket.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from kora_cli.daemon import (
    DEFAULT_SHUTDOWN_TIMEOUT,
    current_coordinator,
    register_daemon_listener,
)

logger = logging.getLogger(__name__)


BEARER_TOKEN_ENV = "KORA_MCP_BEARER_TOKEN"


# ---------------------------------------------------------------------------
# Tool definition: kora__daemon_status
# ---------------------------------------------------------------------------

DAEMON_STATUS_TOOL: Dict[str, Any] = {
    "name": "kora__daemon_status",
    "description": (
        "Return the Kora daemon coordinator's current state: lifecycle "
        "(booting/running/shutting_down), uptime in seconds since "
        "startup completed, the per-listener registration + started "
        "state, and the shutdown reason if applicable."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

TOOLS: list = [DAEMON_STATUS_TOOL]


def _execute_daemon_status() -> Dict[str, Any]:
    """Body for ``kora__daemon_status``. Returns the JSON dict."""
    coord = current_coordinator()
    if coord is None:
        # Reachable only via test paths or if the listener somehow
        # serves traffic outside cmd_daemon. Surface honestly.
        return {
            "state": "not_running",
            "uptime_seconds": None,
            "shutdown_reason": None,
            "listeners": [],
        }
    return coord.get_status()


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------


def _require_bearer(
    authorization: Optional[str] = Header(default=None),
) -> None:
    """FastAPI dependency — raise 401 unless Bearer matches env token.

    The token is re-read per-request so a Doppler-rotated value picks
    up without a daemon restart. The startup() gate guarantees the
    env was set when the daemon registered the listener; a later
    unset is treated as "auth misconfigured → 401" rather than
    "auth disabled".
    """
    expected = os.environ.get(BEARER_TOKEN_ENV, "").strip()
    if not expected:
        # Env was set at startup, now empty — treat as auth failure,
        # NOT as a pass-through.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="MCP bearer auth misconfigured",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or malformed Authorization header",
        )
    presented = authorization[len("Bearer ") :].strip()
    # Constant-time comparison to avoid timing side-channels on the
    # bearer token. Both sides are str; encode for compare_digest.
    import hmac

    if not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
        )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/mcp", tags=["mcp"])


@router.get("/tools/list", dependencies=[Depends(_require_bearer)])
def get_tools_list() -> Dict[str, Any]:
    """Convenience endpoint matching the bucket-spec smoke test."""
    return {"tools": TOOLS}


@router.post("", dependencies=[Depends(_require_bearer)])
async def post_jsonrpc(request: Request) -> Dict[str, Any]:
    """JSON-RPC 2.0 entry. Handles ``tools/list`` + ``tools/call``."""
    try:
        body = await request.json()
    except Exception:
        return _jsonrpc_error(None, -32700, "Parse error")

    if not isinstance(body, dict):
        return _jsonrpc_error(None, -32600, "Invalid Request")

    req_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}

    if method == "tools/list":
        return _jsonrpc_result(req_id, {"tools": TOOLS})

    if method == "tools/call":
        tool_name = params.get("name")
        if tool_name == "kora__daemon_status":
            return _jsonrpc_result(
                req_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": _execute_daemon_status_text(),
                        }
                    ]
                },
            )
        return _jsonrpc_error(
            req_id, -32602, f"unknown tool: {tool_name!r}"
        )

    return _jsonrpc_error(req_id, -32601, f"method not found: {method!r}")


def _jsonrpc_result(req_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(req_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


def _execute_daemon_status_text() -> str:
    """JSON-stringified status for the MCP ``content[].text`` field."""
    import json

    return json.dumps(_execute_daemon_status(), default=str, sort_keys=True)


# ---------------------------------------------------------------------------
# Listener factory + registration
# ---------------------------------------------------------------------------


class MCPListener:
    """Holds the post-startup state. The router is added to the web
    app at import time (below); startup() validates the bearer-token
    env. shutdown() is a no-op — the web listener tears down the
    serving uvicorn process for us.
    """

    async def startup(self) -> None:
        token = os.environ.get(BEARER_TOKEN_ENV, "").strip()
        if not token:
            # Fail-CLOSED per security-infra default. The coordinator
            # interprets this raise as a startup failure + aborts the
            # daemon.
            raise RuntimeError(
                f"mcp listener refuses to register: {BEARER_TOKEN_ENV} "
                "is unset or empty. Set it in Doppler "
                "(kora-runtime-substrate) and redeploy. Daemon-without-MCP "
                "is supported via `kora daemon --listener heartbeat "
                "--listener web`."
            )
        logger.info(
            "[kora.mcp] listener active; bearer auth from %s; %d tool(s)",
            BEARER_TOKEN_ENV,
            len(TOOLS),
        )

    async def shutdown(self) -> None:
        # Routes live on the shared FastAPI app, which the web listener
        # owns. The web listener's uvicorn-stop tears down request
        # serving for us. Nothing to do here.
        pass


def _factory():
    listener = MCPListener()
    return (listener.startup, listener.shutdown, DEFAULT_SHUTDOWN_TIMEOUT)


register_daemon_listener("mcp", _factory)


# ---------------------------------------------------------------------------
# Mount routes onto the shared admin app (import-time side effect)
# ---------------------------------------------------------------------------

# The web listener uses ``kora_cli.web_server.app``. Mounting at
# import time means by the time uvicorn binds, the MCP routes are
# already registered. If web_server isn't importable (e.g. fastapi
# extra not installed), the daemon can't run anyway, so let the
# ImportError surface.
#
# IMPORTANT ordering note: ``kora_cli/web_server.py`` mounts a SPA
# catch-all route ``/{full_path:path}`` at module-import time (line
# 6412 in current main). FastAPI matches routes in list order, so a
# plain ``app.include_router(router)`` adds AFTER the catch-all and
# the catch-all wins for ``/mcp/*``. We insert MCP routes at the
# FRONT of ``app.routes`` so they match before the SPA fallback.

from kora_cli.web_server import app as _admin_app  # noqa: E402


def _mount_router_ahead_of_catchall() -> None:
    """Insert our router's routes at the start of ``app.routes``."""
    # First gather the router's routes via include_router into a
    # disposable sub-app; this gives FastAPI a chance to apply
    # any dependency-resolution wiring. Then move them to the front.
    from fastapi import FastAPI

    _scratch = FastAPI()
    _scratch.include_router(router)
    # The scratch app now has the fully-resolved route objects we want.
    for new_route in reversed(_scratch.routes):
        # Skip default scratch routes (openapi, redoc, etc.)
        path = getattr(new_route, "path", "")
        if not path.startswith("/mcp"):
            continue
        _admin_app.routes.insert(0, new_route)


_mount_router_ahead_of_catchall()
