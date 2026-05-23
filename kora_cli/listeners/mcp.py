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
# Caller auth (KR-MCP-RUNTIME-SURFACE ST2)
# ---------------------------------------------------------------------------

from kora_cli.listeners.mcp_caller_auth import (  # noqa: E402
    Caller,
    resolve_caller,
)


def _resolve_caller_dep(
    authorization: Optional[str] = Header(default=None),
) -> Caller:
    """FastAPI dependency: resolve presented bearer → ``Caller``.

    Replaces the legacy ``_require_bearer`` single-token check. The
    resolver supports two modes (see ``mcp_caller_auth``):

      - Mode 2 — file ACL at ``~/.kora/mcp_callers.yaml`` (per-caller
        identity + per-caller allowed-caps).
      - Mode 1 — ``KORA_MCP_BEARER_TOKEN`` env (comma-separated for
        multi-token rotation; resolves to anonymous caller — only
        ungated read-only tools work).

    Any unresolved presented token → 401. Tool-level cap-gate
    enforcement is downstream (in ``post_jsonrpc``); this dep only
    determines whether the caller is identifiable at all.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or malformed Authorization header",
        )
    presented = authorization[len("Bearer ") :].strip()
    caller = resolve_caller(presented)
    if caller is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
        )
    return caller


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

# KR-MCP-RUNTIME-SURFACE ST1 — extend with the 5 read-only tools.
# Imported lazily inside this list-extension so the import side-effect
# happens at module-load time but doesn't ripple to test fixtures that
# need to patch the dispatch table.
from kora_cli.listeners.mcp_tools import (  # noqa: E402
    ST2_TOOL_DESCRIPTORS as _ST2_DESCRIPTORS,
    ST2_TOOL_DISPATCH as _ST2_DISPATCH,
    TOOL_DESCRIPTORS as _ST1_DESCRIPTORS,
    TOOL_DISPATCH as _ST1_DISPATCH,
    _ST2_ActorIdRequired,  # noqa: F401
    _ST2_DevOnlyError,  # noqa: F401
    _ST2_ToolInputError,  # noqa: F401
)

TOOLS.extend(_ST1_DESCRIPTORS)
TOOLS.extend(_ST2_DESCRIPTORS)


# Tool registry — per-tool gating metadata.
# ``requires_cap_gate``: True → caller.can_invoke(tool_name) must pass.
# Default False for read-only tools; True for mutating tools.
# Operator can opt-in to gate a read tool by adding its name to a caller's
# allowed_caps + setting the flag here (not exposed in ST2 — future bucket).
_TOOL_FLAGS: Dict[str, Dict[str, bool]] = {}
# Set defaults from descriptors (each ST2 descriptor carries the flags).
for _tool in TOOLS:
    _TOOL_FLAGS[_tool["name"]] = {
        "requires_cap_gate": bool(_tool.get("requires_cap_gate", False)),
        "dev_only": bool(_tool.get("dev_only", False)),
    }


def _check_cap_gate(
    req_id: Any, tool_name: Optional[str], caller: Caller
) -> Optional[Dict[str, Any]]:
    """Return a JSON-RPC error envelope if the cap gate denies; else None.

    Decision tree:

      - Unknown tool: pass through; the downstream "unknown tool"
        branch handles it.
      - Tool's ``requires_cap_gate`` is False: allow regardless of
        caller identity.
      - Tool's ``requires_cap_gate`` is True + caller can_invoke
        tool: allow.
      - Tool's ``requires_cap_gate`` is True + caller cannot:
        return -32001 ``capability_denied`` with the required cap
        in the error data so the caller can self-diagnose.
    """
    if tool_name is None:
        return None
    flags = _TOOL_FLAGS.get(tool_name)
    if flags is None:
        return None  # unknown tool — downstream handles -32602
    if not flags.get("requires_cap_gate", False):
        return None
    if caller.can_invoke(tool_name):
        return None
    # Denied.
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": -32001,
            "message": "capability_denied",
            "data": {
                "required_capability": tool_name,
                "caller_actor_kind": caller.actor_kind,
            },
        },
    }


# ---------------------------------------------------------------------------
# KR-MCP-AUDIT-ON-DENIAL — JSONL audit emit for the two denial paths
# ---------------------------------------------------------------------------
#
# Both helpers write to the same ``mcp.tool_called`` seam used by the
# success-path audit in ``mcp_tools._emit_audit``. They run BEFORE the
# JSON-RPC error envelope is returned so a denied call leaves a JSONL
# row even though no executor ran. The ``details.result`` field is the
# alert-rule discriminator — values match the patterns CC#1's
# ``capability_denied_24h`` rule (+ future actor_id_required rules)
# already grep for.
#
# Why not call into ``mcp_tools._emit_audit``: that helper takes a
# ``result`` STRING the executor builds from its own state-change
# vocabulary (e.g. ``"active->paused"``) and embeds ``args_keys`` from
# the executor's input. Denial paths don't have either — the request
# never reached the executor. Keeping a separate helper here avoids
# pretzeling _emit_audit's contract for two callers with different
# inputs.


def _emit_capability_denied_audit(
    *, tool_name: Optional[str], caller: Caller
) -> None:
    """Write a JSONL audit row for a cap-gate denial.

    Reason text NEVER in the audit (caller-supplied; might leak
    sensitive context — same posture as the stop-tool's audit
    omission). Captures only operator-actionable fields: which tool,
    which caller actor_kind, which capability was required, and the
    discriminator literal CC#1's alert rule matches on.
    """
    # Best-effort emit — never raise from this path. An audit-sink
    # failure must NOT mask the denial response to the caller.
    try:
        from kora_cli.audit import emit_audit

        emit_audit(
            seam="mcp.tool_called",
            details={
                "tool_name": tool_name or "<unknown>",
                "tool_kind": "mutating",
                "caller_actor_kind": caller.actor_kind,
                "caller_actor_id": caller.actor_id,
                "required_capability": tool_name or "<unknown>",
                "duration_ms": 0,
                "tool_status": "not_allowed",
                "result": "capability_denied",
            },
            source="mcp_http",
        )
        # Also emit the structured-log line for operator grep — mirrors
        # the dual-write pattern in mcp_tools._emit_audit.
        logger.info(
            "[kora.mcp.tool_denied] tool=%s caller_actor_kind=%s "
            "result=capability_denied",
            tool_name,
            caller.actor_kind,
        )
    except Exception:  # pragma: no cover — sink failure must not mask denial
        logger.exception(
            "[kora.mcp.tool_denied] emit_audit failed for "
            "tool=%s caller_actor_kind=%s — denial response still "
            "returned, but JSONL row missing",
            tool_name,
            caller.actor_kind,
        )


def _emit_actor_id_required_audit(
    *, tool_name: Optional[str], caller: Caller
) -> None:
    """Write a JSONL audit row for an actor_id-required denial.

    Distinct ``result`` literal so CC#1's ``capability_denied_24h``
    alert rule's ``detail_match`` doesn't conflate this with a cap
    denial — different operator-fix path (the cap IS granted; the
    actor_id field is missing in mcp_callers.yaml). A future
    ``actor_id_required_24h`` alert rule can grep this separately.
    """
    try:
        from kora_cli.audit import emit_audit

        emit_audit(
            seam="mcp.tool_called",
            details={
                "tool_name": tool_name or "<unknown>",
                "tool_kind": "mutating",
                "caller_actor_kind": caller.actor_kind,
                "caller_actor_id": caller.actor_id,
                "required_capability": tool_name or "<unknown>",
                "duration_ms": 0,
                "tool_status": "not_allowed",
                "result": "actor_id_required",
            },
            source="mcp_http",
        )
        logger.info(
            "[kora.mcp.tool_denied] tool=%s caller_actor_kind=%s "
            "result=actor_id_required",
            tool_name,
            caller.actor_kind,
        )
    except Exception:  # pragma: no cover
        logger.exception(
            "[kora.mcp.tool_denied] emit_audit failed for "
            "tool=%s caller_actor_kind=%s — denial response still "
            "returned, but JSONL row missing",
            tool_name,
            caller.actor_kind,
        )


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
            "daemon_session_id": None,
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


@router.get("/tools/list")
def get_tools_list(
    caller: Caller = Depends(_resolve_caller_dep),
) -> Dict[str, Any]:
    """Convenience endpoint matching the bucket-spec smoke test.

    Tool descriptors include ``requires_cap_gate`` + ``dev_only``
    flags so callers can predict which tools their identity
    permits and which are env-restricted.
    """
    return {"tools": TOOLS}


@router.post("")
async def post_jsonrpc(
    request: Request,
    caller: Caller = Depends(_resolve_caller_dep),
) -> Dict[str, Any]:
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
        tool_args = params.get("arguments") or {}

        # Capability gate (ST2) — runs BEFORE any dispatch.
        # tool registry flags `requires_cap_gate` per tool; if true,
        # caller's allowed_caps must include the tool name.
        gate_err = _check_cap_gate(req_id, tool_name, caller)
        if gate_err is not None:
            # KR-MCP-AUDIT-ON-DENIAL — emit a JSONL audit row for the
            # denial BEFORE returning. The cap-gate is the only gate
            # that needs this explicit emit; the success-path audit
            # at mcp_tools._emit_audit runs from inside each executor
            # AFTER dispatch, so it never sees denied calls. CC#1's
            # capability_denied_24h alert rule consumes these rows.
            _emit_capability_denied_audit(
                tool_name=tool_name, caller=caller
            )
            return gate_err

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
        # KR-MCP-RUNTIME-SURFACE ST1 — route into the mcp_tools dispatch
        # table for the read-only tools. Each dispatcher returns a
        # Pydantic model; we serialize via model_dump_json for the
        # content[].text field.
        if tool_name in _ST1_DISPATCH:
            try:
                model = await _ST1_DISPATCH[tool_name](tool_args)
            except Exception as exc:
                logger.exception(
                    "[mcp] tool %s raised %r", tool_name, exc
                )
                return _jsonrpc_error(
                    req_id, -32603, f"tool error: {type(exc).__name__}"
                )
            return _jsonrpc_result(
                req_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": model.model_dump_json(),
                        }
                    ]
                },
            )
        # KR-MCP-RUNTIME-SURFACE ST2 — mutating tools. Dispatchers
        # receive the resolved Caller for audit-logging the actor_kind.
        if tool_name in _ST2_DISPATCH:
            try:
                model = await _ST2_DISPATCH[tool_name](tool_args, caller)
            except _ST2_DevOnlyError as exc:
                # Surface dev-only refusal as a distinct JSON-RPC error
                # so callers don't confuse it with a generic capability
                # denial (caps could be granted but env still refuses).
                return _jsonrpc_error(
                    req_id,
                    -32001,
                    f"dev_only_tool: {exc}",
                )
            except _ST2_ToolInputError as exc:
                # Args validation failed (bad target_state, etc.) —
                # -32602 invalid params.
                return _jsonrpc_error(
                    req_id, -32602, f"invalid params: {exc}"
                )
            except _ST2_ActorIdRequired as exc:
                # KR-MCP-STOP-CONTROL ST2 — distinct -32001 code from
                # capability_denied. The cap may be granted but the
                # caller still lacks the actor_id field needed for
                # substrate attribution. Operator-fix path is in the
                # error message.
                #
                # KR-MCP-AUDIT-ON-DENIAL — emit symmetrically with the
                # cap-gate denial so the alerts panel can surface
                # actor_id-required misconfigurations the same way.
                # Same seam + same tool_kind; result discriminator is
                # "actor_id_required" (distinct from
                # "capability_denied" so the alert rule's
                # detail_match doesn't conflate the two — they're
                # different operator-fix paths).
                _emit_actor_id_required_audit(
                    tool_name=tool_name, caller=caller
                )
                return _jsonrpc_error(
                    req_id,
                    -32001,
                    "actor_id_required_for_stop",
                    data={
                        "caller_actor_kind": caller.actor_kind,
                        "tool": tool_name,
                        "remediation": str(exc),
                    },
                )
            except Exception as exc:
                logger.exception(
                    "[mcp] tool %s raised %r", tool_name, exc
                )
                return _jsonrpc_error(
                    req_id, -32603, f"tool error: {type(exc).__name__}"
                )
            return _jsonrpc_result(
                req_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": model.model_dump_json(),
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


def _jsonrpc_error(
    req_id: Any,
    code: int,
    message: str,
    *,
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    error: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": error}


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
