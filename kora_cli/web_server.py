"""
Hermes Agent — Web UI server.

Provides a FastAPI backend serving the Vite/React frontend and REST API
endpoints for managing configuration, environment variables, and sessions.

Usage:
    python -m kora_cli.main web          # Start on http://127.0.0.1:9119
    python -m kora_cli.main web --port 8080
"""

import asyncio
import hmac
import importlib.util
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from kora_cli import __version__, __release_date__
from kora_cli.config import (
    cfg_get,
    DEFAULT_CONFIG,
    OPTIONAL_ENV_VARS,
    get_config_path,
    get_env_path,
    get_kora_home,
    load_config,
    load_env,
    save_config,
    save_env_value,
    remove_env_value,
    check_config_version,
    redact_key,
)
from gateway.status import get_running_pid, read_runtime_status

try:
    from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel
except ImportError:
    # First try lazy-installing the dashboard extras. Only the user actually
    # running `hermes dashboard` needs fastapi+uvicorn; lazy install keeps
    # them out of every other install path. After install, re-import.
    try:
        from tools.lazy_deps import ensure as _lazy_ensure
        _lazy_ensure("tool.dashboard", prompt=False)
        from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
        from fastapi.staticfiles import StaticFiles
        from pydantic import BaseModel
    except Exception:
        raise SystemExit(
            "Web UI requires fastapi and uvicorn.\n"
            f"Install with: {sys.executable} -m pip install 'fastapi' 'uvicorn[standard]'"
        )

WEB_DIST = Path(os.environ["HERMES_WEB_DIST"]) if "HERMES_WEB_DIST" in os.environ else Path(__file__).parent / "web_dist"
_log = logging.getLogger(__name__)

app = FastAPI(title="Hermes Agent", version=__version__)

# ---------------------------------------------------------------------------
# Session token for protecting sensitive endpoints (reveal).
# Generated fresh on every server start — dies when the process exits.
# Injected into the SPA HTML so only the legitimate web UI can use it.
# ---------------------------------------------------------------------------
_SESSION_TOKEN = secrets.token_urlsafe(32)
_SESSION_HEADER_NAME = "X-Hermes-Session-Token"

# In-browser Chat tab (/chat, /api/pty, …).  Off unless ``hermes dashboard --tui``
# or HERMES_DASHBOARD_TUI=1.  Set from :func:`start_server`.
_DASHBOARD_EMBEDDED_CHAT_ENABLED = False

# Simple rate limiter for the reveal endpoint
_reveal_timestamps: List[float] = []
_REVEAL_MAX_PER_WINDOW = 5
_REVEAL_WINDOW_SECONDS = 30

# CORS: restrict to localhost origins only.  The web UI is intended to run
# locally; binding to 0.0.0.0 with allow_origins=["*"] would let any website
# read/modify config and secrets.

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Endpoints that do NOT require the session token.  Everything else under
# /api/ is gated by the auth middleware below.  Keep this list minimal —
# only truly non-sensitive, read-only endpoints belong here.
# ---------------------------------------------------------------------------
_PUBLIC_API_PATHS: frozenset = frozenset({
    "/api/status",
    "/api/config/defaults",
    "/api/config/schema",
    "/api/model/info",
    "/api/dashboard/themes",
    "/api/dashboard/plugins",
    "/api/dashboard/plugins/rescan",
})


def _has_valid_session_token(request: Request) -> bool:
    """True if the request carries a valid dashboard session token.

    The dedicated session header avoids collisions with reverse proxies that
    already use ``Authorization`` (for example Caddy ``basic_auth``). We still
    accept the legacy Bearer path for backward compatibility with older
    dashboard bundles.
    """
    session_header = request.headers.get(_SESSION_HEADER_NAME, "")
    if session_header and hmac.compare_digest(
        session_header.encode(),
        _SESSION_TOKEN.encode(),
    ):
        return True

    auth = request.headers.get("authorization", "")
    expected = f"Bearer {_SESSION_TOKEN}"
    return hmac.compare_digest(auth.encode(), expected.encode())


def _require_token(request: Request) -> None:
    """Validate the ephemeral session token.  Raises 401 on mismatch."""
    if not _has_valid_session_token(request):
        raise HTTPException(status_code=401, detail="Unauthorized")


# Accepted Host header values for loopback binds. DNS rebinding attacks
# point a victim browser at an attacker-controlled hostname (evil.test)
# which resolves to 127.0.0.1 after a TTL flip — bypassing same-origin
# checks because the browser now considers evil.test and our dashboard
# "same origin". Validating the Host header at the app layer rejects any
# request whose Host isn't one we bound for. See GHSA-ppp5-vxwm-4cf7.
_LOOPBACK_HOST_VALUES: frozenset = frozenset({
    "localhost", "127.0.0.1", "::1",
})


def _is_accepted_host(host_header: str, bound_host: str) -> bool:
    """True if the Host header targets the interface we bound to.

    Accepts:
    - Exact bound host (with or without port suffix)
    - Loopback aliases when bound to loopback
    - Any host when bound to 0.0.0.0 (explicit opt-in to non-loopback,
      no protection possible at this layer)
    """
    if not host_header:
        return False
    # Strip port suffix. IPv6 addresses use bracket notation:
    #   [::1]         — no port
    #   [::1]:9119    — with port
    # Plain hosts/v4:
    #   localhost:9119
    #   127.0.0.1:9119
    h = host_header.strip()
    if h.startswith("["):
        # IPv6 bracketed — port (if any) follows "]:"
        close = h.find("]")
        if close != -1:
            host_only = h[1:close]  # strip brackets
        else:
            host_only = h.strip("[]")
    else:
        host_only = h.rsplit(":", 1)[0] if ":" in h else h
    host_only = host_only.lower()

    # 0.0.0.0 bind means operator explicitly opted into all-interfaces
    # (requires --insecure per web_server.start_server). No Host-layer
    # defence can protect that mode; rely on operator network controls.
    if bound_host in {"0.0.0.0", "::"}:
        return True

    # Loopback bind: accept the loopback names
    bound_lc = bound_host.lower()
    if bound_lc in _LOOPBACK_HOST_VALUES:
        return host_only in _LOOPBACK_HOST_VALUES

    # Explicit non-loopback bind: require exact host match
    return host_only == bound_lc


@app.middleware("http")
async def host_header_middleware(request: Request, call_next):
    """Reject requests whose Host header doesn't match the bound interface.

    Defends against DNS rebinding: a victim browser on a localhost
    dashboard is tricked into fetching from an attacker hostname that
    TTL-flips to 127.0.0.1. CORS and same-origin checks don't help —
    the browser now treats the attacker origin as same-origin with the
    dashboard. Host-header validation at the app layer catches it.

    See GHSA-ppp5-vxwm-4cf7.
    """
    # Store the bound host on app.state so this middleware can read it —
    # set by start_server() at listen time.
    bound_host = getattr(app.state, "bound_host", None)
    if bound_host:
        host_header = request.headers.get("host", "")
        if not _is_accepted_host(host_header, bound_host):
            return JSONResponse(
                status_code=400,
                content={
                    "detail": (
                        "Invalid Host header. Dashboard requests must use "
                        "the hostname the server was bound to."
                    ),
                },
            )
    return await call_next(request)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Require the session token on all /api/ routes except the public list."""
    path = request.url.path
    if path.startswith("/api/") and path not in _PUBLIC_API_PATHS:
        if not _has_valid_session_token(request):
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized"},
            )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Config schema — auto-generated from DEFAULT_CONFIG
# ---------------------------------------------------------------------------

# Manual overrides for fields that need select options or custom types
_SCHEMA_OVERRIDES: Dict[str, Dict[str, Any]] = {
    "model": {
        "type": "string",
        "description": "Default model (e.g. anthropic/claude-sonnet-4.6)",
        "category": "general",
    },
    "model_context_length": {
        "type": "number",
        "description": "Context window override (0 = auto-detect from model metadata)",
        "category": "general",
    },
    "terminal.backend": {
        "type": "select",
        "description": "Terminal execution backend",
        "options": ["local", "docker", "ssh", "modal", "daytona", "vercel_sandbox", "singularity"],
    },
    "terminal.vercel_runtime": {
        "type": "select",
        "description": "Vercel Sandbox runtime",
        "options": ["node24", "node22", "python3.13"],  # sync with _SUPPORTED_VERCEL_RUNTIMES in terminal_tool.py
    },
    "terminal.modal_mode": {
        "type": "select",
        "description": "Modal sandbox mode",
        "options": ["sandbox", "function"],
    },
    "tts.provider": {
        "type": "select",
        "description": "Text-to-speech provider",
        "options": ["edge", "elevenlabs", "openai", "neutts"],
    },
    "stt.provider": {
        "type": "select",
        "description": "Speech-to-text provider",
        # "mistral" temporarily removed — mistralai PyPI package quarantined
        # (malicious 2.4.6 release on 2026-05-12). Restore once available.
        "options": ["local", "openai"],
    },
    "display.skin": {
        "type": "select",
        "description": "CLI visual theme",
        "options": ["default", "ares", "mono", "slate"],
    },
    "dashboard.theme": {
        "type": "select",
        "description": "Web dashboard visual theme",
        "options": ["default", "midnight", "ember", "mono", "cyberpunk", "rose"],
    },
    "display.resume_display": {
        "type": "select",
        "description": "How resumed sessions display history",
        "options": ["minimal", "full", "off"],
    },
    "display.busy_input_mode": {
        "type": "select",
        "description": "Input behavior while agent is running",
        "options": ["interrupt", "queue", "steer"],
    },
    "memory.provider": {
        "type": "select",
        "description": "Memory provider plugin",
        "options": ["builtin", "honcho"],
    },
    "approvals.mode": {
        "type": "select",
        "description": "Dangerous command approval mode",
        "options": ["ask", "yolo", "deny"],
    },
    "context.engine": {
        "type": "select",
        "description": "Context management engine",
        "options": ["default", "custom"],
    },
    "human_delay.mode": {
        "type": "select",
        "description": "Simulated typing delay mode",
        "options": ["off", "typing", "fixed"],
    },
    "logging.level": {
        "type": "select",
        "description": "Log level for agent.log",
        "options": ["DEBUG", "INFO", "WARNING", "ERROR"],
    },
    "agent.service_tier": {
        "type": "select",
        "description": "API service tier (OpenAI/Anthropic)",
        "options": ["", "auto", "default", "flex"],
    },
    "delegation.reasoning_effort": {
        "type": "select",
        "description": "Reasoning effort for delegated subagents",
        "options": ["", "low", "medium", "high"],
    },
}

# Categories with fewer fields get merged into "general" to avoid tab sprawl.
_CATEGORY_MERGE: Dict[str, str] = {
    "privacy": "security",
    "context": "agent",
    "skills": "agent",
    "cron": "agent",
    "network": "agent",
    "checkpoints": "agent",
    "approvals": "security",
    "human_delay": "display",
    "dashboard": "display",
    "code_execution": "agent",
    "prompt_caching": "agent",
    "goals": "agent",
    # Only `telegram.reactions` currently lives under telegram — fold it in
    # with the other messaging-platform config (discord) so it isn't an
    # orphan tab of one field.
    "telegram": "discord",
}

# Display order for tabs — unlisted categories sort alphabetically after these.
_CATEGORY_ORDER = [
    "general", "agent", "terminal", "display", "delegation",
    "memory", "compression", "security", "browser", "voice",
    "tts", "stt", "logging", "discord", "auxiliary",
]


def _infer_type(value: Any) -> str:
    """Infer a UI field type from a Python value."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "number"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return "string"


def _build_schema_from_config(
    config: Dict[str, Any],
    prefix: str = "",
) -> Dict[str, Dict[str, Any]]:
    """Walk DEFAULT_CONFIG and produce a flat dot-path → field schema dict."""
    schema: Dict[str, Dict[str, Any]] = {}
    for key, value in config.items():
        full_key = f"{prefix}.{key}" if prefix else key

        # Skip internal / version keys
        if full_key in {"_config_version",}:
            continue

        # Category is the first path component for nested keys, or "general"
        # for top-level scalar fields (model, toolsets, timezone, etc.).
        if prefix:
            category = prefix.split(".")[0]
        elif isinstance(value, dict):
            category = key
        else:
            category = "general"

        if isinstance(value, dict):
            # Recurse into nested dicts
            schema.update(_build_schema_from_config(value, full_key))
        else:
            entry: Dict[str, Any] = {
                "type": _infer_type(value),
                "description": full_key.replace(".", " → ").replace("_", " ").title(),
                "category": category,
            }
            # Apply manual overrides
            if full_key in _SCHEMA_OVERRIDES:
                entry.update(_SCHEMA_OVERRIDES[full_key])
            # Merge small categories
            entry["category"] = _CATEGORY_MERGE.get(entry["category"], entry["category"])
            schema[full_key] = entry
    return schema


CONFIG_SCHEMA = _build_schema_from_config(DEFAULT_CONFIG)

# Inject virtual fields that don't live in DEFAULT_CONFIG but are surfaced
# by the normalize/denormalize cycle.  Insert model_context_length right after
# the "model" key so it renders adjacent in the frontend.
_mcl_entry = _SCHEMA_OVERRIDES["model_context_length"]
_ordered_schema: Dict[str, Dict[str, Any]] = {}
for _k, _v in CONFIG_SCHEMA.items():
    _ordered_schema[_k] = _v
    if _k == "model":
        _ordered_schema["model_context_length"] = _mcl_entry
CONFIG_SCHEMA = _ordered_schema


class ConfigUpdate(BaseModel):
    config: dict


class EnvVarUpdate(BaseModel):
    key: str
    value: str


class EnvVarDelete(BaseModel):
    key: str


class EnvVarReveal(BaseModel):
    key: str


class ModelAssignment(BaseModel):
    """Payload for POST /api/model/set — assign a provider/model to a slot.

    scope="main"        → writes model.provider + model.default
    scope="auxiliary"   → writes auxiliary.<task>.provider + auxiliary.<task>.model
    scope="auxiliary" with task=""  → applied to every auxiliary.* slot
    scope="auxiliary" with task="__reset__"  → resets every slot to provider="auto"
    """
    scope: str
    provider: str
    model: str
    task: str = ""


_GATEWAY_HEALTH_URL = os.getenv("GATEWAY_HEALTH_URL")
try:
    _GATEWAY_HEALTH_TIMEOUT = float(os.getenv("GATEWAY_HEALTH_TIMEOUT", "3"))
except (ValueError, TypeError):
    _log.warning(
        "Invalid GATEWAY_HEALTH_TIMEOUT value %r — using default 3.0s",
        os.getenv("GATEWAY_HEALTH_TIMEOUT"),
    )
    _GATEWAY_HEALTH_TIMEOUT = 3.0

# DEPRECATED (scheduled for removal): GATEWAY_HEALTH_URL / GATEWAY_HEALTH_TIMEOUT.
# Cross-container / cross-host gateway liveness detection will be folded into a
# first-class dashboard config key so it's no longer Docker-adjacent lore buried
# in env vars.  The env vars still work for now so existing Compose deployments
# don't break.  Do not add new callers — wire new uses through the planned
# config surface.


def _probe_gateway_health() -> tuple[bool, dict | None]:
    """Probe the gateway via its HTTP health endpoint (cross-container).

    .. deprecated::
        Driven by the deprecated ``GATEWAY_HEALTH_URL`` /
        ``GATEWAY_HEALTH_TIMEOUT`` env vars.  Scheduled for removal alongside
        a move to a first-class dashboard config key.  See
        :data:`_GATEWAY_HEALTH_URL` for context.

    Uses ``/health/detailed`` first (returns full state), falling back to
    the simpler ``/health`` endpoint.  Returns ``(is_alive, body_dict)``.

    Accepts any of these as ``GATEWAY_HEALTH_URL``:
    - ``http://gateway:8642``                (base URL — recommended)
    - ``http://gateway:8642/health``         (explicit health path)
    - ``http://gateway:8642/health/detailed`` (explicit detailed path)

    This is a **blocking** call — run via ``run_in_executor`` from async code.
    """
    if not _GATEWAY_HEALTH_URL:
        return False, None

    # Normalise to base URL so we always probe the right paths regardless of
    # whether the user included /health or /health/detailed in the env var.
    base = _GATEWAY_HEALTH_URL.rstrip("/")
    if base.endswith("/health/detailed"):
        base = base[: -len("/health/detailed")]
    elif base.endswith("/health"):
        base = base[: -len("/health")]

    for path in (f"{base}/health/detailed", f"{base}/health"):
        try:
            req = urllib.request.Request(path, method="GET")
            with urllib.request.urlopen(req, timeout=_GATEWAY_HEALTH_TIMEOUT) as resp:
                if resp.status == 200:
                    body = json.loads(resp.read())
                    return True, body
        except Exception:
            continue
    return False, None


@app.get("/api/status")
async def get_status():
    current_ver, latest_ver = check_config_version()

    # --- Gateway liveness detection ---
    # Try local PID check first (same-host).  If that fails and a remote
    # GATEWAY_HEALTH_URL is configured, probe the gateway over HTTP so the
    # dashboard works when the gateway runs in a separate container.
    gateway_pid = get_running_pid()
    gateway_running = gateway_pid is not None
    remote_health_body: dict | None = None

    if not gateway_running and _GATEWAY_HEALTH_URL:
        loop = asyncio.get_running_loop()
        alive, remote_health_body = await loop.run_in_executor(
            None, _probe_gateway_health
        )
        if alive:
            gateway_running = True
            # PID from the remote container (display only — not locally valid)
            if remote_health_body:
                gateway_pid = remote_health_body.get("pid")

    gateway_state = None
    gateway_platforms: dict = {}
    gateway_exit_reason = None
    gateway_updated_at = None
    configured_gateway_platforms: set[str] | None = None
    try:
        from gateway.config import load_gateway_config

        gateway_config = load_gateway_config()
        configured_gateway_platforms = {
            platform.value for platform in gateway_config.get_connected_platforms()
        }
    except Exception:
        configured_gateway_platforms = None

    # Prefer the detailed health endpoint response (has full state) when the
    # local runtime status file is absent or stale (cross-container).
    runtime = read_runtime_status()
    if runtime is None and remote_health_body and remote_health_body.get("gateway_state"):
        runtime = remote_health_body

    if runtime:
        gateway_state = runtime.get("gateway_state")
        gateway_platforms = runtime.get("platforms") or {}
        if configured_gateway_platforms is not None:
            gateway_platforms = {
                key: value
                for key, value in gateway_platforms.items()
                if key in configured_gateway_platforms
            }
        gateway_exit_reason = runtime.get("exit_reason")
        gateway_updated_at = runtime.get("updated_at")
        if not gateway_running:
            gateway_state = gateway_state if gateway_state in {"stopped", "startup_failed"} else "stopped"
            gateway_platforms = {}
        elif gateway_running and remote_health_body is not None:
            # The health probe confirmed the gateway is alive, but the local
            # runtime status file may be stale (cross-container).  Override
            # stopped/None state so the dashboard shows the correct badge.
            if gateway_state in {None, "stopped"}:
                gateway_state = "running"

    # If there was no runtime info at all but the health probe confirmed alive,
    # ensure we still report the gateway as running (no shared volume scenario).
    if gateway_running and gateway_state is None and remote_health_body is not None:
        gateway_state = "running"

    active_sessions = 0
    try:
        from kora_state import SessionDB
        db = SessionDB()
        try:
            sessions = db.list_sessions_rich(limit=50)
            now = time.time()
            active_sessions = sum(
                1 for s in sessions
                if s.get("ended_at") is None
                and (now - s.get("last_active", s.get("started_at", 0))) < 300
            )
        finally:
            db.close()
    except Exception:
        pass

    return {
        "version": __version__,
        "release_date": __release_date__,
        "hermes_home": str(get_kora_home()),
        "config_path": str(get_config_path()),
        "env_path": str(get_env_path()),
        "config_version": current_ver,
        "latest_config_version": latest_ver,
        "gateway_running": gateway_running,
        "gateway_pid": gateway_pid,
        "gateway_health_url": _GATEWAY_HEALTH_URL,
        "gateway_state": gateway_state,
        "gateway_platforms": gateway_platforms,
        "gateway_exit_reason": gateway_exit_reason,
        "gateway_updated_at": gateway_updated_at,
        "active_sessions": active_sessions,
    }


# ---------------------------------------------------------------------------
# Gateway + update actions (invoked from the Status page).
#
# Both commands are spawned as detached subprocesses so the HTTP request
# returns immediately.  stdin is closed (``DEVNULL``) so any stray ``input()``
# calls fail fast with EOF rather than hanging forever.  stdout/stderr are
# streamed to a per-action log file under ``~/.kora/logs/<action>.log`` so
# the dashboard can tail them back to the user.
# ---------------------------------------------------------------------------

_ACTION_LOG_DIR: Path = get_kora_home() / "logs"

# Short ``name`` (from the URL) → absolute log file path.
_ACTION_LOG_FILES: Dict[str, str] = {
    "gateway-restart": "gateway-restart.log",
    "hermes-update": "hermes-update.log",
}

# ``name`` → most recently spawned Popen handle.  Used so ``status`` can
# report liveness and exit code without shelling out to ``ps``.
_ACTION_PROCS: Dict[str, subprocess.Popen] = {}


def _spawn_hermes_action(subcommand: List[str], name: str) -> subprocess.Popen:
    """Spawn ``hermes <subcommand>`` detached and record the Popen handle.

    Uses the running interpreter's ``kora_cli.main`` module so the action
    inherits the same venv/PYTHONPATH the web server is using.
    """
    log_file_name = _ACTION_LOG_FILES[name]
    _ACTION_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = _ACTION_LOG_DIR / log_file_name
    log_file = open(log_path, "ab", buffering=0)
    log_file.write(
        f"\n=== {name} started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode()
    )

    cmd = [sys.executable, "-m", "kora_cli.main", *subcommand]

    popen_kwargs: Dict[str, Any] = {
        "cwd": str(PROJECT_ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": log_file,
        "stderr": subprocess.STDOUT,
        "env": {**os.environ, "HERMES_NONINTERACTIVE": "1"},
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)
    _ACTION_PROCS[name] = proc
    return proc


def _tail_lines(path: Path, n: int) -> List[str]:
    """Return the last ``n`` lines of ``path``.  Reads the whole file — fine
    for our small per-action logs.  Binary-decoded with ``errors='replace'``
    so log corruption doesn't 500 the endpoint."""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    return lines[-n:] if n > 0 else lines


@app.post("/api/gateway/restart")
async def restart_gateway():
    """Kick off a ``hermes gateway restart`` in the background."""
    try:
        proc = _spawn_hermes_action(["gateway", "restart"], "gateway-restart")
    except Exception as exc:
        _log.exception("Failed to spawn gateway restart")
        raise HTTPException(status_code=500, detail=f"Failed to restart gateway: {exc}")
    return {
        "ok": True,
        "pid": proc.pid,
        "name": "gateway-restart",
    }


@app.post("/api/hermes/update")
async def update_hermes():
    """Kick off ``hermes update`` in the background."""
    try:
        proc = _spawn_hermes_action(["update"], "hermes-update")
    except Exception as exc:
        _log.exception("Failed to spawn hermes update")
        raise HTTPException(status_code=500, detail=f"Failed to start update: {exc}")
    return {
        "ok": True,
        "pid": proc.pid,
        "name": "hermes-update",
    }


@app.get("/api/actions/{name}/status")
async def get_action_status(name: str, lines: int = 200):
    """Tail an action log and report whether the process is still running."""
    log_file_name = _ACTION_LOG_FILES.get(name)
    if log_file_name is None:
        raise HTTPException(status_code=404, detail=f"Unknown action: {name}")

    log_path = _ACTION_LOG_DIR / log_file_name
    tail = _tail_lines(log_path, min(max(lines, 1), 2000))

    proc = _ACTION_PROCS.get(name)
    if proc is None:
        running = False
        exit_code: Optional[int] = None
        pid: Optional[int] = None
    else:
        exit_code = proc.poll()
        running = exit_code is None
        pid = proc.pid

    return {
        "name": name,
        "running": running,
        "exit_code": exit_code,
        "pid": pid,
        "lines": tail,
    }


@app.get("/api/sessions")
async def get_sessions(limit: int = 20, offset: int = 0):
    try:
        from kora_state import SessionDB
        db = SessionDB()
        try:
            sessions = db.list_sessions_rich(limit=limit, offset=offset)
            total = db.session_count()
            now = time.time()
            for s in sessions:
                s["is_active"] = (
                    s.get("ended_at") is None
                    and (now - s.get("last_active", s.get("started_at", 0))) < 300
                )
            return {"sessions": sessions, "total": total, "limit": limit, "offset": offset}
        finally:
            db.close()
    except Exception:
        _log.exception("GET /api/sessions failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/sessions/search")
async def search_sessions(q: str = "", limit: int = 20):
    """Full-text search across session message content using FTS5."""
    if not q or not q.strip():
        return {"results": []}
    try:
        from kora_state import SessionDB
        db = SessionDB()
        try:
            # Auto-add prefix wildcards so partial words match
            # e.g. "nimb" → "nimb*" matches "nimby"
            # Preserve quoted phrases and existing wildcards as-is
            import re
            terms = []
            for token in re.findall(r'"[^"]*"|\S+', q.strip()):
                if token.startswith('"') or token.endswith("*"):
                    terms.append(token)
                else:
                    terms.append(token + "*")
            prefix_query = " ".join(terms)
            matches = db.search_messages(query=prefix_query, limit=limit)
            # Group by session_id — return unique sessions with their best snippet
            seen: dict = {}
            for m in matches:
                sid = m["session_id"]
                if sid not in seen:
                    seen[sid] = {
                        "session_id": sid,
                        "snippet": m.get("snippet", ""),
                        "role": m.get("role"),
                        "source": m.get("source"),
                        "model": m.get("model"),
                        "session_started": m.get("session_started"),
                    }
            return {"results": list(seen.values())}
        finally:
            db.close()
    except Exception:
        _log.exception("GET /api/sessions/search failed")
        raise HTTPException(status_code=500, detail="Search failed")


def _normalize_config_for_web(config: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize config for the web UI.

    Hermes supports ``model`` as either a bare string (``"anthropic/claude-sonnet-4"``)
    or a dict (``{default: ..., provider: ..., base_url: ...}``).  The schema is built
    from DEFAULT_CONFIG where ``model`` is a string, but user configs often have the
    dict form.  Normalize to the string form so the frontend schema matches.

    Also surfaces ``model_context_length`` as a top-level field so the web UI can
    display and edit it.  A value of 0 means "auto-detect".
    """
    config = dict(config)  # shallow copy
    model_val = config.get("model")
    if isinstance(model_val, dict):
        # Extract context_length before flattening the dict
        ctx_len = model_val.get("context_length", 0)
        config["model"] = model_val.get("default", model_val.get("name", ""))
        config["model_context_length"] = ctx_len if isinstance(ctx_len, int) else 0
    else:
        config["model_context_length"] = 0
    return config


@app.get("/api/config")
async def get_config():
    config = _normalize_config_for_web(load_config())
    # Strip internal keys that the frontend shouldn't see or send back
    return {k: v for k, v in config.items() if not k.startswith("_")}


@app.get("/api/config/defaults")
async def get_defaults():
    return DEFAULT_CONFIG


@app.get("/api/config/schema")
async def get_schema():
    return {"fields": CONFIG_SCHEMA, "category_order": _CATEGORY_ORDER}


_EMPTY_MODEL_INFO: dict = {
    "model": "",
    "provider": "",
    "auto_context_length": 0,
    "config_context_length": 0,
    "effective_context_length": 0,
    "capabilities": {},
}


@app.get("/api/model/info")
def get_model_info():
    """Return resolved model metadata for the currently configured model.

    Calls the same context-length resolution chain the agent uses, so the
    frontend can display "Auto-detected: 200K" alongside the override field.
    Also returns model capabilities (vision, reasoning, tools) when available.
    """
    try:
        cfg = load_config()
        model_cfg = cfg.get("model", "")

        # Extract model name and provider from the config
        if isinstance(model_cfg, dict):
            model_name = model_cfg.get("default", model_cfg.get("name", ""))
            provider = model_cfg.get("provider", "")
            base_url = model_cfg.get("base_url", "")
            config_ctx = model_cfg.get("context_length")
        else:
            model_name = str(model_cfg) if model_cfg else ""
            provider = ""
            base_url = ""
            config_ctx = None

        if not model_name:
            return dict(_EMPTY_MODEL_INFO, provider=provider)

        # Resolve auto-detected context length (pass config_ctx=None to get
        # purely auto-detected value, then separately report the override)
        try:
            from agent.model_metadata import get_model_context_length
            auto_ctx = get_model_context_length(
                model=model_name,
                base_url=base_url,
                provider=provider,
                config_context_length=None,  # ignore override — we want auto value
            )
        except Exception:
            auto_ctx = 0

        config_ctx_int = 0
        if isinstance(config_ctx, int) and config_ctx > 0:
            config_ctx_int = config_ctx

        # Effective is what the agent actually uses
        effective_ctx = config_ctx_int if config_ctx_int > 0 else auto_ctx

        # Try to get model capabilities from models.dev
        caps = {}
        try:
            from agent.models_dev import get_model_capabilities
            mc = get_model_capabilities(provider=provider, model=model_name)
            if mc is not None:
                caps = {
                    "supports_tools": mc.supports_tools,
                    "supports_vision": mc.supports_vision,
                    "supports_reasoning": mc.supports_reasoning,
                    "context_window": mc.context_window,
                    "max_output_tokens": mc.max_output_tokens,
                    "model_family": mc.model_family,
                }
        except Exception:
            pass

        return {
            "model": model_name,
            "provider": provider,
            "auto_context_length": auto_ctx,
            "config_context_length": config_ctx_int,
            "effective_context_length": effective_ctx,
            "capabilities": caps,
        }
    except Exception:
        _log.exception("GET /api/model/info failed")
        return dict(_EMPTY_MODEL_INFO)


# ---------------------------------------------------------------------------
# Model assignment — pick provider+model for main slot or auxiliary slots.
# Mirrors the model.options JSON-RPC from tui_gateway but uses REST so the
# Models page (which has no chat PTY open) can drive it.
# ---------------------------------------------------------------------------

# Canonical auxiliary task slots. Keep in sync with DEFAULT_CONFIG["auxiliary"]
# in kora_cli/config.py — listed here for deterministic ordering in the UI.
_AUX_TASK_SLOTS: Tuple[str, ...] = (
    "vision",
    "web_extract",
    "compression",
    "session_search",
    "skills_hub",
    "approval",
    "mcp",
    "title_generation",
    "curator",
)


@app.get("/api/model/options")
def get_model_options():
    """Return authenticated providers + their curated model lists.

    REST equivalent of the ``model.options`` JSON-RPC on tui_gateway, so the
    dashboard Models page can render the picker without a live chat session.
    The response shape matches ``model.options`` 1:1 so ``ModelPickerDialog``
    can share the same types.
    """
    try:
        from kora_cli.inventory import build_models_payload, load_picker_context

        return build_models_payload(load_picker_context(), max_models=50)
    except Exception:
        _log.exception("GET /api/model/options failed")
        raise HTTPException(status_code=500, detail="Failed to list model options")


@app.get("/api/model/auxiliary")
def get_auxiliary_models():
    """Return current auxiliary task assignments.

    Shape:
      {
        "tasks": [
          {"task": "vision", "provider": "auto", "model": "", "base_url": ""},
          ...
        ],
        "main": {"provider": "openrouter", "model": "anthropic/claude-opus-4.7"},
      }
    """
    try:
        cfg = load_config()
        aux_cfg = cfg.get("auxiliary", {})
        if not isinstance(aux_cfg, dict):
            aux_cfg = {}

        tasks = []
        for slot in _AUX_TASK_SLOTS:
            slot_cfg = aux_cfg.get(slot, {}) if isinstance(aux_cfg.get(slot), dict) else {}
            tasks.append({
                "task": slot,
                "provider": str(slot_cfg.get("provider", "auto") or "auto"),
                "model": str(slot_cfg.get("model", "") or ""),
                "base_url": str(slot_cfg.get("base_url", "") or ""),
            })

        model_cfg = cfg.get("model", {})
        if isinstance(model_cfg, dict):
            main = {
                "provider": str(model_cfg.get("provider", "") or ""),
                "model": str(model_cfg.get("default", model_cfg.get("name", "")) or ""),
            }
        else:
            main = {"provider": "", "model": str(model_cfg) if model_cfg else ""}

        return {"tasks": tasks, "main": main}
    except Exception:
        _log.exception("GET /api/model/auxiliary failed")
        raise HTTPException(status_code=500, detail="Failed to read auxiliary config")


@app.post("/api/model/set")
async def set_model_assignment(body: ModelAssignment):
    """Assign a model to the main slot or an auxiliary task slot.

    Writes to ``~/.kora/config.yaml`` — applies to **new** sessions only.
    The currently running chat PTY (if any) is not affected; use the
    ``/model`` slash command inside a chat to hot-swap that specific session.
    """
    scope = (body.scope or "").strip().lower()
    provider = (body.provider or "").strip()
    model = (body.model or "").strip()
    task = (body.task or "").strip().lower()

    if scope not in {"main", "auxiliary"}:
        raise HTTPException(status_code=400, detail="scope must be 'main' or 'auxiliary'")

    try:
        cfg = load_config()

        if scope == "main":
            if not provider or not model:
                raise HTTPException(status_code=400, detail="provider and model required for main")
            model_cfg = cfg.get("model", {})
            if not isinstance(model_cfg, dict):
                model_cfg = {}
            model_cfg["provider"] = provider
            model_cfg["default"] = model
            # Clear stale base_url so the resolver picks the provider's own default.
            if "base_url" in model_cfg and model_cfg.get("base_url"):
                model_cfg["base_url"] = ""
            # Also clear hardcoded context_length override — new model may have
            # a different context window.
            if "context_length" in model_cfg:
                model_cfg.pop("context_length", None)
            cfg["model"] = model_cfg
            save_config(cfg)
            return {"ok": True, "scope": "main", "provider": provider, "model": model}

        # scope == "auxiliary"
        aux = cfg.get("auxiliary")
        if not isinstance(aux, dict):
            aux = {}

        if task == "__reset__":
            # Reset every slot to provider="auto", model="" — keeps other fields intact.
            for slot in _AUX_TASK_SLOTS:
                slot_cfg = aux.get(slot)
                if not isinstance(slot_cfg, dict):
                    slot_cfg = {}
                slot_cfg["provider"] = "auto"
                slot_cfg["model"] = ""
                aux[slot] = slot_cfg
            cfg["auxiliary"] = aux
            save_config(cfg)
            return {"ok": True, "scope": "auxiliary", "reset": True}

        if not provider:
            raise HTTPException(status_code=400, detail="provider required for auxiliary")

        targets = [task] if task else list(_AUX_TASK_SLOTS)
        for slot in targets:
            if slot not in _AUX_TASK_SLOTS:
                raise HTTPException(status_code=400, detail=f"unknown auxiliary task: {slot}")
            slot_cfg = aux.get(slot)
            if not isinstance(slot_cfg, dict):
                slot_cfg = {}
            slot_cfg["provider"] = provider
            slot_cfg["model"] = model
            aux[slot] = slot_cfg

        cfg["auxiliary"] = aux
        save_config(cfg)
        return {
            "ok": True,
            "scope": "auxiliary",
            "tasks": targets,
            "provider": provider,
            "model": model,
        }
    except HTTPException:
        raise
    except Exception:
        _log.exception("POST /api/model/set failed")
        raise HTTPException(status_code=500, detail="Failed to save model assignment")




def _denormalize_config_from_web(config: Dict[str, Any]) -> Dict[str, Any]:
    """Reverse _normalize_config_for_web before saving.

    Reconstructs ``model`` as a dict by reading the current on-disk config
    to recover model subkeys (provider, base_url, api_mode, etc.) that were
    stripped from the GET response.  The frontend only sees model as a flat
    string; the rest is preserved transparently.

    Also handles ``model_context_length`` — writes it back into the model dict
    as ``context_length``.  A value of 0 or absent means "auto-detect" (omitted
    from the dict so get_model_context_length() uses its normal resolution).
    """
    config = dict(config)
    # Remove any _model_meta that might have leaked in (shouldn't happen
    # with the stripped GET response, but be defensive)
    config.pop("_model_meta", None)

    # Extract and remove model_context_length before processing model
    ctx_override = config.pop("model_context_length", 0)
    if not isinstance(ctx_override, int):
        try:
            ctx_override = int(ctx_override)
        except (TypeError, ValueError):
            ctx_override = 0

    model_val = config.get("model")
    if isinstance(model_val, str) and model_val:
        # Read the current disk config to recover model subkeys
        try:
            disk_config = load_config()
            disk_model = disk_config.get("model")
            if isinstance(disk_model, dict):
                # Preserve all subkeys, update default with the new value
                disk_model["default"] = model_val
                # Write context_length into the model dict (0 = remove/auto)
                if ctx_override > 0:
                    disk_model["context_length"] = ctx_override
                else:
                    disk_model.pop("context_length", None)
                config["model"] = disk_model
            # Model was previously a bare string — upgrade to dict if
            # user is setting a context_length override
            elif ctx_override > 0:
                config["model"] = {
                    "default": model_val,
                    "context_length": ctx_override,
                }
        except Exception:
            pass  # can't read disk config — just use the string form
    return config


@app.put("/api/config")
async def update_config(body: ConfigUpdate):
    try:
        save_config(_denormalize_config_from_web(body.config))
        return {"ok": True}
    except Exception:
        _log.exception("PUT /api/config failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/env")
async def get_env_vars():
    env_on_disk = load_env()
    result = {}
    for var_name, info in OPTIONAL_ENV_VARS.items():
        value = env_on_disk.get(var_name)
        result[var_name] = {
            "is_set": bool(value),
            "redacted_value": redact_key(value) if value else None,
            "description": info.get("description", ""),
            "url": info.get("url"),
            "category": info.get("category", ""),
            "is_password": info.get("password", False),
            "tools": info.get("tools", []),
            "advanced": info.get("advanced", False),
        }
    return result


@app.put("/api/env")
async def set_env_var(body: EnvVarUpdate):
    try:
        save_env_value(body.key, body.value)
        return {"ok": True, "key": body.key}
    except Exception:
        _log.exception("PUT /api/env failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.delete("/api/env")
async def remove_env_var(body: EnvVarDelete):
    try:
        removed = remove_env_value(body.key)
        if not removed:
            raise HTTPException(status_code=404, detail=f"{body.key} not found in .env")
        return {"ok": True, "key": body.key}
    except HTTPException:
        raise
    except Exception:
        _log.exception("DELETE /api/env failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/env/reveal")
async def reveal_env_var(body: EnvVarReveal, request: Request):
    """Return the real (unredacted) value of a single env var.

    Protected by:
    - Ephemeral session token (generated per server start, injected into SPA)
    - Rate limiting (max 5 reveals per 30s window)
    - Audit logging
    """
    # --- Token check ---
    _require_token(request)

    # --- Rate limit ---
    now = time.time()
    cutoff = now - _REVEAL_WINDOW_SECONDS
    _reveal_timestamps[:] = [t for t in _reveal_timestamps if t > cutoff]
    if len(_reveal_timestamps) >= _REVEAL_MAX_PER_WINDOW:
        raise HTTPException(status_code=429, detail="Too many reveal requests. Try again shortly.")
    _reveal_timestamps.append(now)

    # --- Reveal ---
    env_on_disk = load_env()
    value = env_on_disk.get(body.key)
    if value is None:
        raise HTTPException(status_code=404, detail=f"{body.key} not found in .env")

    _log.info("env/reveal: %s", body.key)
    return {"key": body.key, "value": value}


# ---------------------------------------------------------------------------
# OAuth provider endpoints — status + disconnect (Phase 1)
# ---------------------------------------------------------------------------
#
# Phase 1 surfaces *which OAuth providers exist* and whether each is
# connected, plus a disconnect button. The actual login flow (PKCE for
# Anthropic, device-code for Nous/Codex) still runs in the CLI for now;
# Phase 2 will add in-browser flows. For unconnected providers we return
# the canonical ``hermes auth add <provider>`` command so the dashboard
# can surface a one-click copy.


def _truncate_token(value: Optional[str], visible: int = 6) -> str:
    """Return ``...XXXXXX`` (last N chars) for safe display in the UI.

    We never expose more than the trailing ``visible`` characters of an
    OAuth access token. JWT prefixes (the part before the first dot) are
    stripped first when present so the visible suffix is always part of
    the signing region rather than a meaningless header chunk.

    Returns the Entra-ID placeholder when handed a callable (Azure Foundry
    bearer provider) — the callable is NEVER invoked here.
    """
    if not value:
        return ""
    if callable(value) and not isinstance(value, str):
        # Entra ID bearer provider — never reveal a minted token in the UI.
        return "<entra-id-bearer>"
    s = str(value)
    if "." in s and s.count(".") >= 2:
        # Looks like a JWT — show the trailing piece of the signature only.
        s = s.rsplit(".", 1)[-1]
    if len(s) <= visible:
        return s
    return f"…{s[-visible:]}"


def _anthropic_oauth_status() -> Dict[str, Any]:
    """Combined status across the three Anthropic credential sources we read.

    Hermes resolves Anthropic creds in this order at runtime:
    1. ``~/.kora/.anthropic_oauth.json`` — Hermes-managed PKCE flow
    2. ``~/.claude/.credentials.json`` — Claude Code CLI credentials (auto)
    3. ``ANTHROPIC_TOKEN`` / ``ANTHROPIC_API_KEY`` env vars
    The dashboard reports the highest-priority source that's actually present.
    """
    try:
        from agent.anthropic_adapter import (
            read_hermes_oauth_credentials,
            read_claude_code_credentials,
            _HERMES_OAUTH_FILE,
        )
    except ImportError:
        read_claude_code_credentials = None  # type: ignore
        read_hermes_oauth_credentials = None  # type: ignore
        _HERMES_OAUTH_FILE = None  # type: ignore

    hermes_creds = None
    if read_hermes_oauth_credentials:
        try:
            hermes_creds = read_hermes_oauth_credentials()
        except Exception:
            hermes_creds = None
    if hermes_creds and hermes_creds.get("accessToken"):
        return {
            "logged_in": True,
            "source": "hermes_pkce",
            "source_label": f"Hermes PKCE ({_HERMES_OAUTH_FILE})",
            "token_preview": _truncate_token(hermes_creds.get("accessToken")),
            "expires_at": hermes_creds.get("expiresAt"),
            "has_refresh_token": bool(hermes_creds.get("refreshToken")),
        }

    cc_creds = None
    if read_claude_code_credentials:
        try:
            cc_creds = read_claude_code_credentials()
        except Exception:
            cc_creds = None
    if cc_creds and cc_creds.get("accessToken"):
        return {
            "logged_in": True,
            "source": "claude_code",
            "source_label": "Claude Code (~/.claude/.credentials.json)",
            "token_preview": _truncate_token(cc_creds.get("accessToken")),
            "expires_at": cc_creds.get("expiresAt"),
            "has_refresh_token": bool(cc_creds.get("refreshToken")),
        }

    env_token = os.getenv("ANTHROPIC_TOKEN") or os.getenv("CLAUDE_CODE_OAUTH_TOKEN")
    if env_token:
        return {
            "logged_in": True,
            "source": "env_var",
            "source_label": "ANTHROPIC_TOKEN environment variable",
            "token_preview": _truncate_token(env_token),
            "expires_at": None,
            "has_refresh_token": False,
        }
    return {"logged_in": False, "source": None}


def _claude_code_only_status() -> Dict[str, Any]:
    """Surface Claude Code CLI credentials as their own provider entry.

    Independent of the Anthropic entry above so users can see whether their
    Claude Code subscription tokens are actively flowing into Hermes even
    when they also have a separate Hermes-managed PKCE login.
    """
    try:
        from agent.anthropic_adapter import read_claude_code_credentials
        creds = read_claude_code_credentials()
    except Exception:
        creds = None
    if creds and creds.get("accessToken"):
        return {
            "logged_in": True,
            "source": "claude_code_cli",
            "source_label": "~/.claude/.credentials.json",
            "token_preview": _truncate_token(creds.get("accessToken")),
            "expires_at": creds.get("expiresAt"),
            "has_refresh_token": bool(creds.get("refreshToken")),
        }
    return {"logged_in": False, "source": None}


# Provider catalog. The order matters — it's how we render the UI list.
# ``cli_command`` is what the dashboard surfaces as the copy-to-clipboard
# fallback while Phase 2 (in-browser flows) isn't built yet.
# ``flow`` describes the OAuth shape so the future modal can pick the
# right UI: ``pkce`` = open URL + paste callback code, ``device_code`` =
# show code + verification URL + poll, ``external`` = read-only (delegated
# to a third-party CLI like Claude Code or Qwen).
_OAUTH_PROVIDER_CATALOG: tuple[Dict[str, Any], ...] = (
    {
        "id": "anthropic",
        "name": "Anthropic (Claude API)",
        "flow": "pkce",
        "cli_command": "hermes auth add anthropic",
        "docs_url": "https://docs.claude.com/en/api/getting-started",
        "status_fn": _anthropic_oauth_status,
    },
    {
        "id": "claude-code",
        "name": "Claude Code (subscription)",
        "flow": "external",
        "cli_command": "claude setup-token",
        "docs_url": "https://docs.claude.com/en/docs/claude-code",
        "status_fn": _claude_code_only_status,
    },
    {
        "id": "nous",
        "name": "Nous Portal",
        "flow": "device_code",
        "cli_command": "hermes auth add nous",
        "docs_url": "https://portal.nousresearch.com",
        "status_fn": None,  # dispatched via auth.get_nous_auth_status
    },
    {
        "id": "openai-codex",
        "name": "OpenAI Codex (ChatGPT)",
        "flow": "device_code",
        "cli_command": "hermes auth add openai-codex",
        "docs_url": "https://platform.openai.com/docs",
        "status_fn": None,  # dispatched via auth.get_codex_auth_status
    },
    {
        "id": "qwen-oauth",
        "name": "Qwen (via Qwen CLI)",
        "flow": "external",
        "cli_command": "hermes auth add qwen-oauth",
        "docs_url": "https://github.com/QwenLM/qwen-code",
        "status_fn": None,  # dispatched via auth.get_qwen_auth_status
    },
    {
        "id": "minimax-oauth",
        "name": "MiniMax (OAuth)",
        # MiniMax's flow is structurally device-code (verification URI +
        # user code, backend polls the token endpoint) with a PKCE
        # extension for code-binding. The dashboard renders the same UX
        # as Nous's device-code flow; the PKCE bit is a security
        # extension that doesn't change the operator experience.
        "flow": "device_code",
        "cli_command": "hermes auth add minimax-oauth",
        "docs_url": "https://www.minimax.io",
        "status_fn": None,  # dispatched via auth.get_minimax_oauth_auth_status
    },
)


def _resolve_provider_status(provider_id: str, status_fn) -> Dict[str, Any]:
    """Dispatch to the right status helper for an OAuth provider entry."""
    if status_fn is not None:
        try:
            return status_fn()
        except Exception as e:
            return {"logged_in": False, "error": str(e)}
    try:
        from kora_cli import auth as hauth
        if provider_id == "nous":
            raw = hauth.get_nous_auth_status()
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": "nous_portal",
                "source_label": raw.get("portal_base_url") or "Nous Portal",
                "token_preview": _truncate_token(raw.get("access_token")),
                "expires_at": raw.get("access_expires_at"),
                "has_refresh_token": bool(raw.get("has_refresh_token")),
            }
        if provider_id == "openai-codex":
            raw = hauth.get_codex_auth_status()
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": raw.get("source") or "openai_codex",
                "source_label": raw.get("auth_mode") or "OpenAI Codex",
                "token_preview": _truncate_token(raw.get("api_key")),
                "expires_at": None,
                "has_refresh_token": False,
                "last_refresh": raw.get("last_refresh"),
            }
        if provider_id == "qwen-oauth":
            raw = hauth.get_qwen_auth_status()
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": "qwen_cli",
                "source_label": raw.get("auth_store_path") or "Qwen CLI",
                "token_preview": _truncate_token(raw.get("access_token")),
                "expires_at": raw.get("expires_at"),
                "has_refresh_token": bool(raw.get("has_refresh_token")),
            }
        if provider_id == "minimax-oauth":
            raw = hauth.get_minimax_oauth_auth_status()
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": "minimax_oauth",
                "source_label": f"MiniMax ({raw.get('region', 'global')})",
                "token_preview": None,
                "expires_at": raw.get("expires_at"),
                "has_refresh_token": True,
            }
    except Exception as e:
        return {"logged_in": False, "error": str(e)}
    return {"logged_in": False}


@app.get("/api/providers/oauth")
async def list_oauth_providers():
    """Enumerate every OAuth-capable LLM provider with current status.

    Response shape (per provider):
        id              stable identifier (used in DELETE path)
        name            human label
        flow            "pkce" | "device_code" | "external"
        cli_command     fallback CLI command for users to run manually
        docs_url        external docs/portal link for the "Learn more" link
        status:
          logged_in        bool — currently has usable creds
          source           short slug ("hermes_pkce", "claude_code", ...)
          source_label     human-readable origin (file path, env var name)
          token_preview    last N chars of the token, never the full token
          expires_at       ISO timestamp string or null
          has_refresh_token bool
    """
    providers = []
    for p in _OAUTH_PROVIDER_CATALOG:
        status = _resolve_provider_status(p["id"], p.get("status_fn"))
        providers.append({
            "id": p["id"],
            "name": p["name"],
            "flow": p["flow"],
            "cli_command": p["cli_command"],
            "docs_url": p["docs_url"],
            "status": status,
        })
    return {"providers": providers}


@app.delete("/api/providers/oauth/{provider_id}")
async def disconnect_oauth_provider(provider_id: str, request: Request):
    """Disconnect an OAuth provider. Token-protected (matches /env/reveal)."""
    _require_token(request)

    valid_ids = {p["id"] for p in _OAUTH_PROVIDER_CATALOG}
    if provider_id not in valid_ids:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider: {provider_id}. "
                   f"Available: {', '.join(sorted(valid_ids))}",
        )

    # Anthropic and claude-code clear the same Hermes-managed PKCE file
    # AND forget the Claude Code import. We don't touch ~/.claude/* directly
    # — that's owned by the Claude Code CLI; users can re-auth there if they
    # want to undo a disconnect.
    if provider_id in {"anthropic", "claude-code"}:
        try:
            from agent.anthropic_adapter import _HERMES_OAUTH_FILE
            if _HERMES_OAUTH_FILE.exists():
                _HERMES_OAUTH_FILE.unlink()
        except Exception:
            pass
        # Also clear the credential pool entry if present.
        try:
            from kora_cli.auth import clear_provider_auth
            clear_provider_auth("anthropic")
        except Exception:
            pass
        _log.info("oauth/disconnect: %s", provider_id)
        return {"ok": True, "provider": provider_id}

    try:
        from kora_cli.auth import clear_provider_auth
        cleared = clear_provider_auth(provider_id)
        _log.info("oauth/disconnect: %s (cleared=%s)", provider_id, cleared)
        return {"ok": bool(cleared), "provider": provider_id}
    except Exception as e:
        _log.exception("disconnect %s failed", provider_id)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# OAuth Phase 2 — in-browser PKCE & device-code flows
# ---------------------------------------------------------------------------
#
# Two flow shapes are supported:
#
#   PKCE (Anthropic):
#     1. POST /api/providers/oauth/anthropic/start
#          → server generates code_verifier + challenge, builds claude.ai
#            authorize URL, stashes verifier in _oauth_sessions[session_id]
#          → returns { session_id, flow: "pkce", auth_url }
#     2. UI opens auth_url in a new tab. User authorizes, copies code.
#     3. POST /api/providers/oauth/anthropic/submit { session_id, code }
#          → server exchanges (code + verifier) → tokens at console.anthropic.com
#          → persists to ~/.kora/.anthropic_oauth.json AND credential pool
#          → returns { ok: true, status: "approved" }
#
#   Device code (Nous, OpenAI Codex):
#     1. POST /api/providers/oauth/{nous|openai-codex}/start
#          → server hits provider's device-auth endpoint
#          → gets { user_code, verification_url, device_code, interval, expires_in }
#          → spawns background poller thread that polls the token endpoint
#            every `interval` seconds until approved/expired
#          → stores poll status in _oauth_sessions[session_id]
#          → returns { session_id, flow: "device_code", user_code,
#                      verification_url, expires_in, poll_interval }
#     2. UI opens verification_url in a new tab and shows user_code.
#     3. UI polls GET /api/providers/oauth/{provider}/poll/{session_id}
#          every 2s until status != "pending".
#     4. On "approved" the background thread has already saved creds; UI
#        refreshes the providers list.
#
# Sessions are kept in-memory only (single-process FastAPI) and time out
# after 15 minutes. A periodic cleanup runs on each /start call to GC
# expired sessions so the dict doesn't grow without bound.

_OAUTH_SESSION_TTL_SECONDS = 15 * 60
_oauth_sessions: Dict[str, Dict[str, Any]] = {}
_oauth_sessions_lock = threading.Lock()

# Import OAuth constants from canonical source instead of duplicating.
# Guarded so hermes web still starts if anthropic_adapter is unavailable;
# Phase 2 endpoints will return 501 in that case.
try:
    from agent.anthropic_adapter import (
        _OAUTH_CLIENT_ID as _ANTHROPIC_OAUTH_CLIENT_ID,
        _OAUTH_TOKEN_URL as _ANTHROPIC_OAUTH_TOKEN_URL,
        _OAUTH_REDIRECT_URI as _ANTHROPIC_OAUTH_REDIRECT_URI,
        _OAUTH_SCOPES as _ANTHROPIC_OAUTH_SCOPES,
        _generate_pkce as _generate_pkce_pair,
    )
    _ANTHROPIC_OAUTH_AVAILABLE = True
except ImportError:
    _ANTHROPIC_OAUTH_AVAILABLE = False
_ANTHROPIC_OAUTH_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"


def _gc_oauth_sessions() -> None:
    """Drop expired sessions. Called opportunistically on /start."""
    cutoff = time.time() - _OAUTH_SESSION_TTL_SECONDS
    with _oauth_sessions_lock:
        stale = [sid for sid, sess in _oauth_sessions.items() if sess["created_at"] < cutoff]
        for sid in stale:
            _oauth_sessions.pop(sid, None)


def _new_oauth_session(provider_id: str, flow: str) -> tuple[str, Dict[str, Any]]:
    """Create + register a new OAuth session, return (session_id, session_dict)."""
    sid = secrets.token_urlsafe(16)
    sess = {
        "session_id": sid,
        "provider": provider_id,
        "flow": flow,
        "created_at": time.time(),
        "status": "pending",  # pending | approved | denied | expired | error
        "error_message": None,
    }
    with _oauth_sessions_lock:
        _oauth_sessions[sid] = sess
    return sid, sess


def _save_anthropic_oauth_creds(access_token: str, refresh_token: str, expires_at_ms: int) -> None:
    """Persist Anthropic PKCE creds to both Hermes file AND credential pool.

    Mirrors what auth_commands.add_command does so the dashboard flow leaves
    the system in the same state as ``hermes auth add anthropic``.
    """
    from agent.anthropic_adapter import _HERMES_OAUTH_FILE
    payload = {
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "expiresAt": expires_at_ms,
    }
    _HERMES_OAUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    _HERMES_OAUTH_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    # Best-effort credential-pool insert. Failure here doesn't invalidate
    # the file write — pool registration only matters for the rotation
    # strategy, not for runtime credential resolution.
    try:
        from agent.credential_pool import (
            PooledCredential,
            load_pool,
            AUTH_TYPE_OAUTH,
            SOURCE_MANUAL,
        )
        import uuid
        pool = load_pool("anthropic")
        # Avoid duplicate entries: delete any prior dashboard-issued OAuth entry
        existing = [e for e in pool.entries() if getattr(e, "source", "").startswith(f"{SOURCE_MANUAL}:dashboard_pkce")]
        for e in existing:
            try:
                pool.remove_entry(getattr(e, "id", ""))
            except Exception:
                pass
        entry = PooledCredential(
            provider="anthropic",
            id=uuid.uuid4().hex[:6],
            label="dashboard PKCE",
            auth_type=AUTH_TYPE_OAUTH,
            priority=0,
            source=f"{SOURCE_MANUAL}:dashboard_pkce",
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at_ms=expires_at_ms,
        )
        pool.add_entry(entry)
    except Exception as e:
        _log.warning("anthropic pool add (dashboard) failed: %s", e)


def _start_anthropic_pkce() -> Dict[str, Any]:
    """Begin PKCE flow. Returns the auth URL the UI should open."""
    if not _ANTHROPIC_OAUTH_AVAILABLE:
        raise HTTPException(status_code=501, detail="Anthropic OAuth not available (missing adapter)")
    verifier, challenge = _generate_pkce_pair()
    sid, sess = _new_oauth_session("anthropic", "pkce")
    sess["verifier"] = verifier
    sess["state"] = verifier  # Anthropic round-trips verifier as state
    params = {
        "code": "true",
        "client_id": _ANTHROPIC_OAUTH_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": _ANTHROPIC_OAUTH_REDIRECT_URI,
        "scope": _ANTHROPIC_OAUTH_SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": verifier,
    }
    auth_url = f"{_ANTHROPIC_OAUTH_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"
    return {
        "session_id": sid,
        "flow": "pkce",
        "auth_url": auth_url,
        "expires_in": _OAUTH_SESSION_TTL_SECONDS,
    }


def _submit_anthropic_pkce(session_id: str, code_input: str) -> Dict[str, Any]:
    """Exchange authorization code for tokens. Persists on success."""
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess or sess["provider"] != "anthropic" or sess["flow"] != "pkce":
        raise HTTPException(status_code=404, detail="Unknown or expired session")
    if sess["status"] != "pending":
        return {"ok": False, "status": sess["status"], "message": sess.get("error_message")}

    # Anthropic's redirect callback page formats the code as `<code>#<state>`.
    # Strip the state suffix if present (we already have the verifier server-side).
    parts = code_input.strip().split("#", 1)
    code = parts[0].strip()
    if not code:
        return {"ok": False, "status": "error", "message": "No code provided"}
    state_from_callback = parts[1] if len(parts) > 1 else ""

    exchange_data = json.dumps({
        "grant_type": "authorization_code",
        "client_id": _ANTHROPIC_OAUTH_CLIENT_ID,
        "code": code,
        "state": state_from_callback or sess["state"],
        "redirect_uri": _ANTHROPIC_OAUTH_REDIRECT_URI,
        "code_verifier": sess["verifier"],
    }).encode()
    req = urllib.request.Request(
        _ANTHROPIC_OAUTH_TOKEN_URL,
        data=exchange_data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "hermes-dashboard/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = f"Token exchange failed: {e}"
        return {"ok": False, "status": "error", "message": sess["error_message"]}

    access_token = result.get("access_token", "")
    refresh_token = result.get("refresh_token", "")
    expires_in = int(result.get("expires_in") or 3600)
    if not access_token:
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = "No access token returned"
        return {"ok": False, "status": "error", "message": sess["error_message"]}

    expires_at_ms = int(time.time() * 1000) + (expires_in * 1000)
    try:
        _save_anthropic_oauth_creds(access_token, refresh_token, expires_at_ms)
    except Exception as e:
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = f"Save failed: {e}"
        return {"ok": False, "status": "error", "message": sess["error_message"]}
    with _oauth_sessions_lock:
        sess["status"] = "approved"
    _log.info("oauth/pkce: anthropic login completed (session=%s)", session_id)
    return {"ok": True, "status": "approved"}


async def _start_device_code_flow(provider_id: str) -> Dict[str, Any]:
    """Initiate a device-code flow (Nous, OpenAI Codex, or MiniMax).

    Calls the provider's device-auth endpoint via the existing CLI helpers,
    then spawns a background poller. Returns the user-facing display fields
    so the UI can render the verification page link + user code.
    """
    if provider_id == "nous":
        from kora_cli.auth import (
            _nous_device_scope_with_env_override,
            _request_nous_device_code_with_scope_fallback,
            PROVIDER_REGISTRY,
        )
        import httpx
        pconfig = PROVIDER_REGISTRY["nous"]
        portal_base_url = (
            os.getenv("HERMES_PORTAL_BASE_URL")
            or os.getenv("NOUS_PORTAL_BASE_URL")
            or pconfig.portal_base_url
        ).rstrip("/")
        client_id = pconfig.client_id
        scope, explicit_scope = _nous_device_scope_with_env_override(
            None,
            default_scope=pconfig.scope,
        )

        def _do_nous_device_request():
            with httpx.Client(
                timeout=httpx.Timeout(15.0),
                headers={"Accept": "application/json"},
            ) as client:
                return _request_nous_device_code_with_scope_fallback(
                    client=client,
                    portal_base_url=portal_base_url,
                    client_id=client_id,
                    scope=scope,
                    allow_legacy_fallback=not explicit_scope,
                )

        device_data, effective_scope = await asyncio.get_running_loop().run_in_executor(
            None, _do_nous_device_request
        )
        sid, sess = _new_oauth_session("nous", "device_code")
        sess["device_code"] = str(device_data["device_code"])
        sess["interval"] = int(device_data["interval"])
        sess["expires_at"] = time.time() + int(device_data["expires_in"])
        sess["portal_base_url"] = portal_base_url
        sess["client_id"] = client_id
        sess["scope"] = effective_scope
        threading.Thread(
            target=_nous_poller, args=(sid,), daemon=True, name=f"oauth-poll-{sid[:6]}"
        ).start()
        return {
            "session_id": sid,
            "flow": "device_code",
            "user_code": str(device_data["user_code"]),
            "verification_url": str(device_data["verification_uri_complete"]),
            "expires_in": int(device_data["expires_in"]),
            "poll_interval": int(device_data["interval"]),
        }

    if provider_id == "openai-codex":
        # Codex uses fixed OpenAI device-auth endpoints; reuse the helper.
        sid, _ = _new_oauth_session("openai-codex", "device_code")
        # Use the helper but in a thread because it polls inline.
        # We can't extract just the start step without refactoring auth.py,
        # so we run the full helper in a worker and proxy the user_code +
        # verification_url back via the session dict. The helper prints
        # to stdout — we capture nothing here, just status.
        threading.Thread(
            target=_codex_full_login_worker, args=(sid,), daemon=True,
            name=f"oauth-codex-{sid[:6]}",
        ).start()
        # Block briefly until the worker has populated the user_code, OR error.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with _oauth_sessions_lock:
                s = _oauth_sessions.get(sid)
            if s and (s.get("user_code") or s["status"] != "pending"):
                break
            await asyncio.sleep(0.1)
        with _oauth_sessions_lock:
            s = _oauth_sessions.get(sid, {})
        if s.get("status") == "error":
            raise HTTPException(status_code=500, detail=s.get("error_message") or "device-auth failed")
        if not s.get("user_code"):
            raise HTTPException(status_code=504, detail="device-auth timed out before returning a user code")
        return {
            "session_id": sid,
            "flow": "device_code",
            "user_code": s["user_code"],
            "verification_url": s["verification_url"],
            "expires_in": int(s.get("expires_in") or 900),
            "poll_interval": int(s.get("interval") or 5),
        }

    if provider_id == "minimax-oauth":
        # MiniMax uses a device-code-style flow (verification URI + user
        # code + background poll) with a PKCE extension on top. From the
        # operator's perspective it's identical to Nous's device-code
        # flow; the PKCE bit (verifier + challenge from
        # _minimax_pkce_pair) is a security extension that binds the
        # token exchange to the original session.
        from kora_cli.auth import (
            _minimax_pkce_pair,
            _minimax_request_user_code,
            MINIMAX_OAUTH_CLIENT_ID,
            MINIMAX_OAUTH_GLOBAL_BASE,
        )
        import httpx
        verifier, challenge, state = _minimax_pkce_pair()
        portal_base_url = (
            os.getenv("MINIMAX_PORTAL_BASE_URL") or MINIMAX_OAUTH_GLOBAL_BASE
        ).rstrip("/")
        def _do_minimax_request():
            with httpx.Client(
                timeout=httpx.Timeout(15.0),
                headers={"Accept": "application/json"},
                follow_redirects=True,
            ) as client:
                return _minimax_request_user_code(
                    client=client,
                    portal_base_url=portal_base_url,
                    client_id=MINIMAX_OAUTH_CLIENT_ID,
                    code_challenge=challenge,
                    state=state,
                )
        device_data = await asyncio.get_event_loop().run_in_executor(
            None, _do_minimax_request
        )
        sid, sess = _new_oauth_session("minimax-oauth", "device_code")
        # The CLI flow names this `interval_ms` because MiniMax's
        # `interval` field is in milliseconds (defensive default 2000ms
        # in _minimax_poll_token).
        interval_raw = device_data.get("interval")
        sess["interval_ms"] = (
            int(interval_raw) if interval_raw is not None else None
        )
        sess["user_code"] = str(device_data["user_code"])
        sess["code_verifier"] = verifier
        sess["state"] = state
        sess["portal_base_url"] = portal_base_url
        sess["client_id"] = MINIMAX_OAUTH_CLIENT_ID
        sess["region"] = "global"
        # `expired_in` from MiniMax is overloaded — could be a unix-ms
        # timestamp OR a seconds-from-now duration. Mirror the heuristic
        # in _minimax_poll_token. Stash the raw value for the poller;
        # compute a derived expires_at + UI-friendly expires_in seconds.
        expired_in_raw = int(device_data["expired_in"])
        sess["expired_in_raw"] = expired_in_raw
        if expired_in_raw > 1_000_000_000_000:  # likely unix-ms
            expires_at_ts = expired_in_raw / 1000.0
            expires_in_seconds = max(0, int(expires_at_ts - time.time()))
        else:
            expires_at_ts = time.time() + expired_in_raw
            expires_in_seconds = expired_in_raw
        sess["expires_at"] = expires_at_ts
        threading.Thread(
            target=_minimax_poller,
            args=(sid,),
            daemon=True,
            name=f"oauth-poll-{sid[:6]}",
        ).start()
        return {
            "session_id": sid,
            "flow": "device_code",
            "user_code": str(device_data["user_code"]),
            "verification_url": str(device_data["verification_uri"]),
            "expires_in": expires_in_seconds,
            "poll_interval": max(2, (sess["interval_ms"] or 2000) // 1000),
        }

    raise HTTPException(status_code=400, detail=f"Provider {provider_id} does not support device-code flow")


def _nous_poller(session_id: str) -> None:
    """Background poller that drives a Nous device-code flow to completion."""
    from kora_cli.auth import (
        NOUS_INFERENCE_AUTH_MODE_FRESH,
        _poll_for_token,
        refresh_nous_oauth_from_state,
    )
    from datetime import datetime, timezone
    import httpx
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess:
        return
    portal_base_url = sess["portal_base_url"]
    client_id = sess["client_id"]
    device_code = sess["device_code"]
    interval = sess["interval"]
    scope = sess.get("scope")
    expires_in = max(60, int(sess["expires_at"] - time.time()))
    try:
        with httpx.Client(timeout=httpx.Timeout(15.0), headers={"Accept": "application/json"}) as client:
            token_data = _poll_for_token(
                client=client,
                portal_base_url=portal_base_url,
                client_id=client_id,
                device_code=device_code,
                expires_in=expires_in,
                poll_interval=interval,
            )
        # Same post-processing as _nous_device_code_login (mint agent key)
        now = datetime.now(timezone.utc)
        token_ttl = int(token_data.get("expires_in") or 0)
        auth_state = {
            "portal_base_url": portal_base_url,
            "inference_base_url": token_data.get("inference_base_url"),
            "client_id": client_id,
            "scope": token_data.get("scope") or scope,
            "token_type": token_data.get("token_type", "Bearer"),
            "access_token": token_data["access_token"],
            "refresh_token": token_data.get("refresh_token"),
            "obtained_at": now.isoformat(),
            "expires_at": (
                datetime.fromtimestamp(now.timestamp() + token_ttl, tz=timezone.utc).isoformat()
                if token_ttl else None
            ),
            "expires_in": token_ttl,
        }
        full_state = refresh_nous_oauth_from_state(
            auth_state,
            min_key_ttl_seconds=300,
            timeout_seconds=15.0,
            force_refresh=False,
            inference_auth_mode=NOUS_INFERENCE_AUTH_MODE_FRESH,
        )
        from kora_cli.auth import persist_nous_credentials
        persist_nous_credentials(full_state)
        with _oauth_sessions_lock:
            sess["status"] = "approved"
        _log.info("oauth/device: nous login completed (session=%s)", session_id)
    except Exception as e:
        _log.warning("nous device-code poll failed (session=%s): %s", session_id, e)
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = str(e)


def _minimax_poller(session_id: str) -> None:
    """Background poller that drives a MiniMax OAuth flow to completion.

    Mirrors `_nous_poller` but calls the MiniMax-specific token endpoint,
    which uses a PKCE-style ``code_verifier`` + ``user_code`` rather than
    the ``device_code`` field used by Nous. On success, builds the same
    auth_state dict that ``_minimax_oauth_login`` (the CLI flow) builds
    and persists via ``_minimax_save_auth_state`` — so the dashboard
    path leaves the system in the same state as
    ``hermes auth add minimax-oauth``.
    """
    from kora_cli.auth import (
        _minimax_poll_token,
        _minimax_resolve_token_expiry_unix,
        _minimax_save_auth_state,
        MINIMAX_OAUTH_GLOBAL_INFERENCE,
        MINIMAX_OAUTH_SCOPE,
    )
    from datetime import datetime, timezone
    import httpx
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess:
        return
    portal_base_url = sess["portal_base_url"]
    client_id = sess["client_id"]
    user_code = sess["user_code"]
    code_verifier = sess["code_verifier"]
    interval_ms = sess.get("interval_ms")
    expired_in_raw = sess["expired_in_raw"]
    try:
        with httpx.Client(
            timeout=httpx.Timeout(15.0),
            headers={"Accept": "application/json"},
            follow_redirects=True,
        ) as client:
            token_data = _minimax_poll_token(
                client=client,
                portal_base_url=portal_base_url,
                client_id=client_id,
                user_code=user_code,
                code_verifier=code_verifier,
                expired_in=expired_in_raw,
                interval_ms=interval_ms,
            )
        # Build the auth_state dict in the same shape as the CLI flow's
        # `_minimax_oauth_login` so `_minimax_save_auth_state` writes
        # the canonical record. Region is fixed to "global" for the
        # dashboard path; cn-region operators can still use the CLI
        # flow which supports `--region cn`.
        now = datetime.now(timezone.utc)
        expires_at_ts = _minimax_resolve_token_expiry_unix(
            int(token_data["expired_in"]), now=now,
        )
        expires_in_s = max(0, int(expires_at_ts - now.timestamp()))
        auth_state = {
            "provider": "minimax-oauth",
            "region": sess.get("region", "global"),
            "portal_base_url": portal_base_url,
            "inference_base_url": MINIMAX_OAUTH_GLOBAL_INFERENCE,
            "client_id": client_id,
            "scope": MINIMAX_OAUTH_SCOPE,
            "token_type": token_data.get("token_type", "Bearer"),
            "access_token": token_data["access_token"],
            "refresh_token": token_data["refresh_token"],
            "resource_url": token_data.get("resource_url"),
            "obtained_at": now.isoformat(),
            "expires_at": datetime.fromtimestamp(
                expires_at_ts, tz=timezone.utc
            ).isoformat(),
            "expires_in": expires_in_s,
        }
        _minimax_save_auth_state(auth_state)
        with _oauth_sessions_lock:
            sess["status"] = "approved"
        _log.info("oauth/device: minimax login completed (session=%s)", session_id)
    except Exception as e:
        _log.warning("minimax device-code poll failed (session=%s): %s", session_id, e)
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = str(e)


def _codex_full_login_worker(session_id: str) -> None:
    """Run the complete OpenAI Codex device-code flow.

    Codex doesn't use the standard OAuth device-code endpoints; it has its
    own ``/api/accounts/deviceauth/usercode`` (JSON body, returns
    ``device_auth_id``) and ``/api/accounts/deviceauth/token`` (JSON body
    polled until 200). On success the response carries an
    ``authorization_code`` + ``code_verifier`` that get exchanged at
    CODEX_OAUTH_TOKEN_URL with grant_type=authorization_code.

    The flow is replicated inline (rather than calling
    _codex_device_code_login) because that helper prints/blocks/polls in a
    single function — we need to surface the user_code to the dashboard the
    moment we receive it, well before polling completes.
    """
    try:
        import httpx
        from kora_cli.auth import (
            CODEX_OAUTH_CLIENT_ID,
            CODEX_OAUTH_TOKEN_URL,
            DEFAULT_CODEX_BASE_URL,
        )
        issuer = "https://auth.openai.com"

        # Step 1: request device code
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            resp = client.post(
                f"{issuer}/api/accounts/deviceauth/usercode",
                json={"client_id": CODEX_OAUTH_CLIENT_ID},
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code != 200:
            raise RuntimeError(f"deviceauth/usercode returned {resp.status_code}")
        device_data = resp.json()
        user_code = device_data.get("user_code", "")
        device_auth_id = device_data.get("device_auth_id", "")
        poll_interval = max(3, int(device_data.get("interval", "5")))
        if not user_code or not device_auth_id:
            raise RuntimeError("device-code response missing user_code or device_auth_id")
        verification_url = f"{issuer}/codex/device"
        with _oauth_sessions_lock:
            sess = _oauth_sessions.get(session_id)
            if not sess:
                return
            sess["user_code"] = user_code
            sess["verification_url"] = verification_url
            sess["device_auth_id"] = device_auth_id
            sess["interval"] = poll_interval
            sess["expires_in"] = 15 * 60  # OpenAI's effective limit
            sess["expires_at"] = time.time() + sess["expires_in"]

        # Step 2: poll until authorized
        deadline = time.monotonic() + sess["expires_in"]
        code_resp = None
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            while time.monotonic() < deadline:
                time.sleep(poll_interval)
                poll = client.post(
                    f"{issuer}/api/accounts/deviceauth/token",
                    json={"device_auth_id": device_auth_id, "user_code": user_code},
                    headers={"Content-Type": "application/json"},
                )
                if poll.status_code == 200:
                    code_resp = poll.json()
                    break
                if poll.status_code in {403, 404}:
                    continue  # user hasn't authorized yet
                raise RuntimeError(f"deviceauth/token poll returned {poll.status_code}")

        if code_resp is None:
            with _oauth_sessions_lock:
                sess["status"] = "expired"
                sess["error_message"] = "Device code expired before approval"
            return

        # Step 3: exchange authorization_code for tokens
        authorization_code = code_resp.get("authorization_code", "")
        code_verifier = code_resp.get("code_verifier", "")
        if not authorization_code or not code_verifier:
            raise RuntimeError("device-auth response missing authorization_code/code_verifier")
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            token_resp = client.post(
                CODEX_OAUTH_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": authorization_code,
                    "redirect_uri": f"{issuer}/deviceauth/callback",
                    "client_id": CODEX_OAUTH_CLIENT_ID,
                    "code_verifier": code_verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if token_resp.status_code != 200:
            raise RuntimeError(f"token exchange returned {token_resp.status_code}")
        tokens = token_resp.json()
        access_token = tokens.get("access_token", "")
        refresh_token = tokens.get("refresh_token", "")
        if not access_token:
            raise RuntimeError("token exchange did not return access_token")

        # Persist via credential pool — same shape as auth_commands.add_command
        from agent.credential_pool import (
            PooledCredential,
            load_pool,
            AUTH_TYPE_OAUTH,
            SOURCE_MANUAL,
        )
        import uuid as _uuid
        pool = load_pool("openai-codex")
        base_url = (
            os.getenv("HERMES_CODEX_BASE_URL", "").strip().rstrip("/")
            or DEFAULT_CODEX_BASE_URL
        )
        entry = PooledCredential(
            provider="openai-codex",
            id=_uuid.uuid4().hex[:6],
            label="dashboard device_code",
            auth_type=AUTH_TYPE_OAUTH,
            priority=0,
            source=f"{SOURCE_MANUAL}:dashboard_device_code",
            access_token=access_token,
            refresh_token=refresh_token,
            base_url=base_url,
        )
        pool.add_entry(entry)
        with _oauth_sessions_lock:
            sess["status"] = "approved"
        _log.info("oauth/device: openai-codex login completed (session=%s)", session_id)
    except Exception as e:
        _log.warning("codex device-code worker failed (session=%s): %s", session_id, e)
        with _oauth_sessions_lock:
            s = _oauth_sessions.get(session_id)
            if s:
                s["status"] = "error"
                s["error_message"] = str(e)


@app.post("/api/providers/oauth/{provider_id}/start")
async def start_oauth_login(provider_id: str, request: Request):
    """Initiate an OAuth login flow. Token-protected."""
    _require_token(request)
    _gc_oauth_sessions()
    valid = {p["id"] for p in _OAUTH_PROVIDER_CATALOG}
    if provider_id not in valid:
        raise HTTPException(status_code=400, detail=f"Unknown provider {provider_id}")
    catalog_entry = next(p for p in _OAUTH_PROVIDER_CATALOG if p["id"] == provider_id)
    if catalog_entry["flow"] == "external":
        raise HTTPException(
            status_code=400,
            detail=f"{provider_id} uses an external CLI; run `{catalog_entry['cli_command']}` manually",
        )
    try:
        # The pkce branch is gated on provider_id == "anthropic" because
        # `_start_anthropic_pkce()` is hardcoded to the Anthropic flow.
        # Routing any other future pkce-flagged provider through it would
        # silently launch the Anthropic OAuth flow (the bug fixed in this
        # change for MiniMax). New PKCE providers must add their own
        # start function and an explicit branch here.
        if catalog_entry["flow"] == "pkce" and provider_id == "anthropic":
            return _start_anthropic_pkce()
        if catalog_entry["flow"] == "device_code":
            return await _start_device_code_flow(provider_id)
    except HTTPException:
        raise
    except Exception as e:
        _log.exception("oauth/start %s failed", provider_id)
        raise HTTPException(status_code=500, detail=str(e))
    raise HTTPException(status_code=400, detail="Unsupported flow")


class OAuthSubmitBody(BaseModel):
    session_id: str
    code: str


@app.post("/api/providers/oauth/{provider_id}/submit")
async def submit_oauth_code(provider_id: str, body: OAuthSubmitBody, request: Request):
    """Submit the auth code for PKCE flows. Token-protected."""
    _require_token(request)
    if provider_id == "anthropic":
        return await asyncio.get_running_loop().run_in_executor(
            None, _submit_anthropic_pkce, body.session_id, body.code,
        )
    raise HTTPException(status_code=400, detail=f"submit not supported for {provider_id}")


@app.get("/api/providers/oauth/{provider_id}/poll/{session_id}")
async def poll_oauth_session(provider_id: str, session_id: str):
    """Poll a device-code session's status (no auth — read-only state)."""
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    if sess["provider"] != provider_id:
        raise HTTPException(status_code=400, detail="Provider mismatch for session")
    return {
        "session_id": session_id,
        "status": sess["status"],
        "error_message": sess.get("error_message"),
        "expires_at": sess.get("expires_at"),
    }


@app.delete("/api/providers/oauth/sessions/{session_id}")
async def cancel_oauth_session(session_id: str, request: Request):
    """Cancel a pending OAuth session. Token-protected."""
    _require_token(request)
    with _oauth_sessions_lock:
        sess = _oauth_sessions.pop(session_id, None)
    if sess is None:
        return {"ok": False, "message": "session not found"}
    return {"ok": True, "session_id": session_id}


# ---------------------------------------------------------------------------
# Session detail endpoints
# ---------------------------------------------------------------------------



def _session_latest_descendant(session_id: str):
    """Resolve a session id to the newest child leaf session.

    /model may create child sessions. Dashboard refresh should continue the
    newest child instead of reopening the old parent.
    """
    from kora_state import SessionDB

    def row_get(row, key, index):
        if isinstance(row, dict):
            return row.get(key)
        try:
            return row[key]
        except Exception:
            try:
                return row[index]
            except Exception:
                return None

    db = SessionDB()
    try:
        sid = db.resolve_session_id(session_id)
        if not sid or not db.get_session(sid):
            return None, []

        conn = (
            getattr(db, "conn", None)
            or getattr(db, "_conn", None)
            or getattr(db, "connection", None)
            or getattr(db, "_connection", None)
        )

        rows = []
        if conn is not None:
            raw_rows = conn.execute(
                "SELECT id, parent_session_id, started_at FROM sessions"
            ).fetchall()
            for row in raw_rows:
                rows.append({
                    "id": row_get(row, "id", 0),
                    "parent_session_id": row_get(row, "parent_session_id", 1),
                    "started_at": row_get(row, "started_at", 2),
                })
        else:
            rows = db.list_sessions_rich(limit=10000, offset=0)

        children = {}
        for row in rows:
            rid = row.get("id")
            parent = row.get("parent_session_id")
            if rid and parent:
                children.setdefault(parent, []).append(row)

        def started(row):
            try:
                return float(row.get("started_at") or 0)
            except Exception:
                return 0.0

        current = sid
        path = [sid]
        seen = {sid}

        while children.get(current):
            candidates = [r for r in children[current] if r.get("id") not in seen]
            if not candidates:
                break
            candidates.sort(key=started, reverse=True)
            current = candidates[0]["id"]
            path.append(current)
            seen.add(current)

        return current, path
    finally:
        db.close()

@app.get("/api/sessions/{session_id}")
async def get_session_detail(session_id: str):
    from kora_state import SessionDB
    db = SessionDB()
    try:
        sid = db.resolve_session_id(session_id)
        session = db.get_session(sid) if sid else None
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        return session
    finally:
        db.close()



@app.get("/api/sessions/{session_id}/latest-descendant")
async def get_session_latest_descendant(session_id: str):
    latest, path = _session_latest_descendant(session_id)
    if not latest:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "requested_session_id": path[0] if path else session_id,
        "session_id": latest,
        "path": path,
        "changed": bool(path and latest != path[0]),
    }

@app.get("/api/sessions/{session_id}/messages")
async def get_session_messages(session_id: str):
    from kora_state import SessionDB
    db = SessionDB()
    try:
        sid = db.resolve_session_id(session_id)
        if not sid:
            raise HTTPException(status_code=404, detail="Session not found")
        messages = db.get_messages(sid)
        return {"session_id": sid, "messages": messages}
    finally:
        db.close()


@app.delete("/api/sessions/{session_id}")
async def delete_session_endpoint(session_id: str):
    from kora_state import SessionDB
    db = SessionDB()
    try:
        if not db.delete_session(session_id):
            raise HTTPException(status_code=404, detail="Session not found")
        return {"ok": True}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Log viewer endpoint
# ---------------------------------------------------------------------------


@app.get("/api/logs")
async def get_logs(
    file: str = "agent",
    lines: int = 100,
    level: Optional[str] = None,
    component: Optional[str] = None,
    search: Optional[str] = None,
):
    from kora_cli.logs import _read_tail, LOG_FILES

    log_name = LOG_FILES.get(file)
    if not log_name:
        raise HTTPException(status_code=400, detail=f"Unknown log file: {file}")
    log_path = get_kora_home() / "logs" / log_name
    if not log_path.exists():
        return {"file": file, "lines": []}

    try:
        from kora_logging import COMPONENT_PREFIXES
    except ImportError:
        COMPONENT_PREFIXES = {}

    # Normalize "ALL" / "all" / empty → no filter. _matches_filters treats an
    # empty tuple as "must match a prefix" (startswith(()) is always False),
    # so passing () instead of None silently drops every line.
    min_level = level if level and level.upper() != "ALL" else None
    if component and component.lower() != "all":
        comp_prefixes = COMPONENT_PREFIXES.get(component)
        if comp_prefixes is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown component: {component}. "
                       f"Available: {', '.join(sorted(COMPONENT_PREFIXES))}",
            )
    else:
        comp_prefixes = None

    has_filters = bool(min_level or comp_prefixes or search)
    result = _read_tail(
        log_path, min(lines, 500) if not search else 2000,
        has_filters=has_filters,
        min_level=min_level,
        component_prefixes=comp_prefixes,
    )
    # Post-filter by search term (case-insensitive substring match).
    # _read_tail doesn't support free-text search, so we filter here and
    # trim to the requested line count afterward.
    if search:
        needle = search.lower()
        result = [l for l in result if needle in l.lower()][-min(lines, 500):]
    return {"file": file, "lines": result}


# ---------------------------------------------------------------------------
# Cron job management endpoints
# ---------------------------------------------------------------------------


class CronJobCreate(BaseModel):
    prompt: str
    schedule: str
    name: str = ""
    deliver: str = "local"


class CronJobUpdate(BaseModel):
    updates: dict


_CRON_PROFILE_LOCK = threading.RLock()


def _cron_profile_dicts() -> List[Dict[str, Any]]:
    """Return dashboard profile records, falling back to a directory scan."""
    from kora_cli import profiles as profiles_mod
    try:
        return [_profile_to_dict(p) for p in profiles_mod.list_profiles()]
    except Exception:
        _log.exception("Failed to list profiles for cron dashboard; falling back to directory scan")
        return _fallback_profile_dicts(profiles_mod)


def _cron_profile_home(profile: Optional[str]) -> Tuple[str, Path]:
    """Resolve a profile query value to (profile_name, HERMES_HOME)."""
    from kora_cli import profiles as profiles_mod

    raw = (profile or "default").strip() or "default"
    try:
        canon = profiles_mod.normalize_profile_name(raw)
        profiles_mod.validate_profile_name(canon)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not profiles_mod.profile_exists(canon):
        raise HTTPException(status_code=404, detail=f"Profile '{canon}' does not exist.")
    return canon, profiles_mod.get_profile_dir(canon)


def _annotate_cron_job(job: Dict[str, Any], profile: str, home: Path) -> Dict[str, Any]:
    annotated = dict(job)
    annotated["profile"] = profile
    annotated["profile_name"] = profile
    annotated["hermes_home"] = str(home)
    annotated["is_default_profile"] = profile == "default"
    return annotated


def _call_cron_for_profile(profile: Optional[str], func_name: str, *args, **kwargs):
    """Run cron.jobs helpers against the selected profile's cron directory.

    cron.jobs keeps CRON_DIR/JOBS_FILE/OUTPUT_DIR as module globals resolved
    from the process HERMES_HOME at import time. The dashboard is a single
    process that can inspect many profiles, so temporarily retarget those
    globals while holding a lock and restore them immediately after the call.
    """
    profile_name, home = _cron_profile_home(profile)
    with _CRON_PROFILE_LOCK:
        from cron import jobs as cron_jobs

        old_cron_dir = cron_jobs.CRON_DIR
        old_jobs_file = cron_jobs.JOBS_FILE
        old_output_dir = cron_jobs.OUTPUT_DIR
        cron_jobs.CRON_DIR = home / "cron"
        cron_jobs.JOBS_FILE = cron_jobs.CRON_DIR / "jobs.json"
        cron_jobs.OUTPUT_DIR = cron_jobs.CRON_DIR / "output"
        try:
            result = getattr(cron_jobs, func_name)(*args, **kwargs)
        finally:
            cron_jobs.CRON_DIR = old_cron_dir
            cron_jobs.JOBS_FILE = old_jobs_file
            cron_jobs.OUTPUT_DIR = old_output_dir

    if isinstance(result, list):
        return [_annotate_cron_job(j, profile_name, home) for j in result]
    if isinstance(result, dict):
        return _annotate_cron_job(result, profile_name, home)
    return result


def _find_cron_job_profile(job_id: str) -> Optional[str]:
    for profile in _cron_profile_dicts():
        name = str(profile.get("name") or "")
        if not name:
            continue
        jobs = _call_cron_for_profile(name, "list_jobs", True)
        if any(j.get("id") == job_id or j.get("name") == job_id for j in jobs):
            return name
    return None


@app.get("/api/cron/jobs")
async def list_cron_jobs(profile: str = "all"):
    requested = (profile or "all").strip()
    if requested.lower() != "all":
        return _call_cron_for_profile(requested, "list_jobs", True)

    jobs: List[Dict[str, Any]] = []
    for item in _cron_profile_dicts():
        name = str(item.get("name") or "")
        if not name:
            continue
        try:
            jobs.extend(_call_cron_for_profile(name, "list_jobs", True))
        except Exception:
            _log.exception("Failed to list cron jobs for profile %s", name)
    return jobs


@app.get("/api/cron/jobs/{job_id}")
async def get_cron_job(job_id: str, profile: Optional[str] = None):
    selected = profile or _find_cron_job_profile(job_id)
    if not selected:
        raise HTTPException(status_code=404, detail="Job not found")
    job = _call_cron_for_profile(selected, "get_job", job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/cron/jobs")
async def create_cron_job(body: CronJobCreate, profile: str = "default"):
    try:
        return _call_cron_for_profile(
            profile,
            "create_job",
            prompt=body.prompt,
            schedule=body.schedule,
            name=body.name,
            deliver=body.deliver,
        )
    except Exception as e:
        _log.exception("POST /api/cron/jobs failed")
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/cron/jobs/{job_id}")
async def update_cron_job(job_id: str, body: CronJobUpdate, profile: Optional[str] = None):
    selected = profile or _find_cron_job_profile(job_id)
    if not selected:
        raise HTTPException(status_code=404, detail="Job not found")
    job = _call_cron_for_profile(selected, "update_job", job_id, body.updates)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/cron/jobs/{job_id}/pause")
async def pause_cron_job(job_id: str, profile: Optional[str] = None):
    selected = profile or _find_cron_job_profile(job_id)
    if not selected:
        raise HTTPException(status_code=404, detail="Job not found")
    job = _call_cron_for_profile(selected, "pause_job", job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/cron/jobs/{job_id}/resume")
async def resume_cron_job(job_id: str, profile: Optional[str] = None):
    selected = profile or _find_cron_job_profile(job_id)
    if not selected:
        raise HTTPException(status_code=404, detail="Job not found")
    job = _call_cron_for_profile(selected, "resume_job", job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/cron/jobs/{job_id}/trigger")
async def trigger_cron_job(job_id: str, profile: Optional[str] = None):
    selected = profile or _find_cron_job_profile(job_id)
    if not selected:
        raise HTTPException(status_code=404, detail="Job not found")
    job = _call_cron_for_profile(selected, "trigger_job", job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.delete("/api/cron/jobs/{job_id}")
async def delete_cron_job(job_id: str, profile: Optional[str] = None):
    selected = profile or _find_cron_job_profile(job_id)
    if not selected:
        raise HTTPException(status_code=404, detail="Job not found")
    if not _call_cron_for_profile(selected, "remove_job", job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# MCP server management endpoints
# ---------------------------------------------------------------------------


class MCPToolsUpdate(BaseModel):
    enabled_tools: List[str]
    all_tools: List[str]


def _mcp_summarize_tools_cfg(tools_cfg: Any) -> Dict[str, Any]:
    """Normalise the tools.include/exclude block for the API response."""
    include: Optional[List[str]] = None
    exclude: Optional[List[str]] = None
    if isinstance(tools_cfg, dict):
        raw_include = tools_cfg.get("include")
        raw_exclude = tools_cfg.get("exclude")
        if isinstance(raw_include, list):
            include = [str(x) for x in raw_include]
        if isinstance(raw_exclude, list):
            exclude = [str(x) for x in raw_exclude]
    if include is not None:
        summary = f"{len(include)} selected"
    elif exclude is not None:
        summary = f"-{len(exclude)} excluded"
    else:
        summary = "all"
    return {"include": include, "exclude": exclude, "summary": summary}


def _mcp_server_to_dict(name: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Project a config.yaml ``mcp_servers`` entry into the API shape."""
    url = cfg.get("url")
    command = cfg.get("command")
    cmd_args = cfg.get("args") if isinstance(cfg.get("args"), list) else []

    if url:
        transport_type = "http"
        transport_display = str(url)
    elif command:
        transport_type = "stdio"
        joined_args = " ".join(str(a) for a in cmd_args[:3])
        transport_display = f"{command} {joined_args}".strip()
    else:
        transport_type = "unknown"
        transport_display = ""

    enabled_raw = cfg.get("enabled", True)
    if isinstance(enabled_raw, str):
        enabled = enabled_raw.lower() in {"true", "1", "yes"}
    else:
        enabled = bool(enabled_raw)

    auth_type = cfg.get("auth") or ("headers" if cfg.get("headers") else "none")

    return {
        "name": name,
        "transport_type": transport_type,
        "transport": transport_display,
        "url": url,
        "command": command,
        "args": list(cmd_args),
        "enabled": enabled,
        "auth_type": auth_type,
        "tools": _mcp_summarize_tools_cfg(cfg.get("tools")),
    }


def _mcp_get_server_or_404(name: str) -> Dict[str, Any]:
    from kora_cli import mcp_config as _mcp_config_mod

    servers = _mcp_config_mod._get_mcp_servers()
    if name not in servers:
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    return servers[name]


@app.get("/api/mcp/servers")
async def list_mcp_servers():
    from kora_cli import mcp_config as _mcp_config_mod

    servers = _mcp_config_mod._get_mcp_servers()
    return [_mcp_server_to_dict(name, cfg) for name, cfg in servers.items()]


@app.get("/api/mcp/servers/{name}")
async def get_mcp_server(name: str):
    cfg = _mcp_get_server_or_404(name)
    return _mcp_server_to_dict(name, cfg)


@app.post("/api/mcp/servers/{name}/probe")
async def probe_mcp_server(name: str):
    """Connect to the server, list its tools, disconnect.

    This is an interactive operation: it may block for several seconds and
    will trigger an OAuth flow for unauthenticated OAuth servers.
    """
    from kora_cli import mcp_config as _mcp_config_mod

    cfg = _mcp_get_server_or_404(name)
    start = time.monotonic()
    try:
        tools = await asyncio.get_event_loop().run_in_executor(
            None, _mcp_config_mod._probe_single_server, name, cfg
        )
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        raise HTTPException(
            status_code=502,
            detail={"error": str(exc), "elapsed_ms": elapsed_ms},
        )
    elapsed_ms = int((time.monotonic() - start) * 1000)
    return {
        "name": name,
        "elapsed_ms": elapsed_ms,
        "tools": [
            {"name": tool_name, "description": desc}
            for tool_name, desc in tools
        ],
    }


@app.put("/api/mcp/servers/{name}/tools")
async def set_mcp_server_tools(name: str, body: MCPToolsUpdate):
    """Update per-tool gating for an MCP server.

    Mirrors :func:`kora_cli.mcp_config.cmd_mcp_configure` behaviour:
    - if ``enabled_tools`` covers every tool in ``all_tools``, drop the
      ``tools`` block entirely (= "all enabled")
    - otherwise, write ``tools.include = enabled_tools`` and drop any
      stale ``tools.exclude``
    """
    _mcp_get_server_or_404(name)

    enabled = list(dict.fromkeys(body.enabled_tools))
    all_tools = list(dict.fromkeys(body.all_tools))
    unknown = [t for t in enabled if t not in all_tools]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown tool(s) for server '{name}': {', '.join(unknown)}",
        )

    config = load_config()
    server_entry = config.setdefault("mcp_servers", {}).setdefault(name, {})

    if len(enabled) == len(all_tools):
        server_entry.pop("tools", None)
    else:
        server_entry.setdefault("tools", {})
        server_entry["tools"]["include"] = enabled
        server_entry["tools"].pop("exclude", None)

    save_config(config)

    refreshed = config["mcp_servers"][name]
    return _mcp_server_to_dict(name, refreshed)


@app.post("/api/mcp/servers/{name}/enable")
async def enable_mcp_server(name: str):
    _mcp_get_server_or_404(name)
    config = load_config()
    server_entry = config.setdefault("mcp_servers", {}).setdefault(name, {})
    server_entry["enabled"] = True
    save_config(config)
    return _mcp_server_to_dict(name, server_entry)


@app.post("/api/mcp/servers/{name}/disable")
async def disable_mcp_server(name: str):
    _mcp_get_server_or_404(name)
    config = load_config()
    server_entry = config.setdefault("mcp_servers", {}).setdefault(name, {})
    server_entry["enabled"] = False
    save_config(config)
    return _mcp_server_to_dict(name, server_entry)


# ---------------------------------------------------------------------------
# Gateway platform identity endpoints (KR-P2-G)
# ---------------------------------------------------------------------------


_DISPLAY_NAME_MAX_LEN = 64


def _display_name_default() -> str:
    """Canonical default display_name — sourced from PlatformConfig so the
    UI never drifts from gateway/config.py's fallback. KR-P2-G §9 pre-push
    check enforces this (no new hardcoded ``"Kora"`` literals)."""
    from gateway.config import PlatformConfig
    return PlatformConfig.from_dict({}).display_name


class GatewayPlatformIdentityUpdate(BaseModel):
    display_name: Any  # validated explicitly so we can return field-tagged 400s


def _gateway_discover_supported_platforms() -> set[str]:
    """Return the set of platform_ids that have an installed adapter.

    Source: ``gateway/platforms/`` directory entries that match a member of
    the ``Platform`` enum (which already absorbs bundled-plugin and runtime
    plugin discovery via its ``_missing_()`` hook). Files like ``base.py``,
    ``helpers.py``, and ``signal_rate_limit.py`` exist in the dir but are
    not adapters; checking against ``Platform()`` filters them out.
    """
    from gateway.config import Platform

    platforms_root = Path(__file__).resolve().parent.parent / "gateway" / "platforms"
    found: set[str] = set()
    if not platforms_root.is_dir():
        return found

    for child in platforms_root.iterdir():
        if child.name.startswith((".", "_")):
            continue
        if child.is_file() and child.suffix == ".py":
            candidate = child.stem
        elif child.is_dir() and (child / "__init__.py").exists():
            candidate = child.name
        else:
            continue
        if candidate in {"base", "helpers"}:
            continue
        try:
            Platform(candidate)
        except ValueError:
            continue
        found.add(candidate)
    return found


def _gateway_platform_to_dict(
    platform_id: str,
    raw_entry: Dict[str, Any],
    supported: bool,
) -> Dict[str, Any]:
    """Project a platforms.<id> YAML block into the API identity shape.

    Only inspects the raw YAML dict (not a constructed ``PlatformConfig``)
    so we can preserve the distinction between "set in top-level" vs "set
    in extra:" vs "missing entirely" — the resolution logic itself lives
    in ``PlatformConfig.from_dict`` and we mirror its precedence here.
    """
    top_level = raw_entry.get("display_name")
    extra_block = raw_entry.get("extra") if isinstance(raw_entry.get("extra"), dict) else {}
    extra_value = extra_block.get("display_name") if isinstance(extra_block, dict) else None

    if isinstance(top_level, str) and top_level.strip():
        effective = top_level
        source = "config"
    elif isinstance(extra_value, str) and extra_value.strip():
        effective = extra_value
        source = "extra"
    else:
        effective = _display_name_default()
        source = "default"

    enabled_raw = raw_entry.get("enabled", False)
    if isinstance(enabled_raw, str):
        enabled = enabled_raw.lower() in {"true", "1", "yes"}
    else:
        enabled = bool(enabled_raw)

    token_raw = raw_entry.get("token")
    if isinstance(token_raw, str) and token_raw.strip():
        token_status = "env_referenced" if token_raw.strip().startswith("${") else "configured"
    else:
        token_status = "missing"

    extra_keys = sorted(extra_block.keys()) if isinstance(extra_block, dict) else []

    return {
        "platform_id": platform_id,
        "enabled": enabled,
        "display_name": effective,
        "display_name_source": source,
        "supported": supported,
        "token_status": token_status,
        "extra_keys": extra_keys,
    }


def _gateway_load_platforms_block() -> Dict[str, Any]:
    """Read the top-level ``platforms:`` block from config.yaml.

    Note: this is the load-bearing path that ``load_gateway_config()`` in
    gateway/config.py reads from (see ``yaml_cfg.get("platforms")``). The
    KR-P2-G bucket §3 pseudocode said ``config["gateway"]["platforms"]``,
    but the gateway config loader does not look under that path — writing
    there would be a silent no-op.
    """
    config = load_config()
    block = config.get("platforms")
    if not isinstance(block, dict):
        return {}
    return block


def _gateway_build_platform_listing() -> List[Dict[str, Any]]:
    supported = _gateway_discover_supported_platforms()
    configured = _gateway_load_platforms_block()
    all_ids = sorted(set(supported) | set(configured.keys()))
    return [
        _gateway_platform_to_dict(
            pid,
            configured.get(pid, {}) if isinstance(configured.get(pid), dict) else {},
            pid in supported,
        )
        for pid in all_ids
    ]


def _validate_display_name(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """Validate + normalise a display_name update.

    Returns ``(trimmed_value, error_message)``. A blank/whitespace value
    normalises to ``None`` (= "clear my override"); a violation returns
    ``error_message`` set to a human-readable reason.
    """
    if not isinstance(value, str):
        return None, "must be a string"
    trimmed = value.strip()
    if not trimmed:
        return None, None
    if len(trimmed.encode("utf-8")) > _DISPLAY_NAME_MAX_LEN:
        return None, f"must be {_DISPLAY_NAME_MAX_LEN} bytes or fewer (UTF-8)"
    if "\n" in trimmed or "\r" in trimmed or "\x00" in trimmed:
        return None, "must not contain newlines or null bytes"
    return trimmed, None


@app.get("/api/gateway/platforms")
async def list_gateway_platforms():
    return _gateway_build_platform_listing()


@app.get("/api/gateway/platforms/{platform_id}")
async def get_gateway_platform(platform_id: str):
    supported = _gateway_discover_supported_platforms()
    configured = _gateway_load_platforms_block()
    if platform_id not in supported and platform_id not in configured:
        raise HTTPException(
            status_code=404,
            detail=f"Gateway platform '{platform_id}' not found",
        )
    raw = configured.get(platform_id, {})
    if not isinstance(raw, dict):
        raw = {}
    return _gateway_platform_to_dict(platform_id, raw, platform_id in supported)


@app.put("/api/gateway/platforms/{platform_id}/identity")
async def set_gateway_platform_identity(
    platform_id: str, body: GatewayPlatformIdentityUpdate
):
    supported = _gateway_discover_supported_platforms()
    configured = _gateway_load_platforms_block()

    if platform_id not in supported and platform_id not in configured:
        raise HTTPException(
            status_code=404,
            detail=f"Gateway platform '{platform_id}' not found",
        )
    if platform_id not in supported:
        raise HTTPException(
            status_code=409,
            detail={
                "platform_id": platform_id,
                "error": (
                    "orphan config — no adapter installed for this platform. "
                    "Remove the entry from config.yaml or install the adapter."
                ),
            },
        )

    trimmed, error = _validate_display_name(body.display_name)
    if error is not None:
        raise HTTPException(
            status_code=400,
            detail={"field": "display_name", "error": error},
        )

    config = load_config()
    platforms_block = config.setdefault("platforms", {})
    if not isinstance(platforms_block, dict):
        platforms_block = {}
        config["platforms"] = platforms_block
    entry = platforms_block.setdefault(platform_id, {})
    if not isinstance(entry, dict):
        entry = {}
        platforms_block[platform_id] = entry

    # trimmed=None means "clear the override" — write null so the
    # PlatformConfig.from_dict fallback kicks in on next load.
    entry["display_name"] = trimmed
    save_config(config)

    return _gateway_platform_to_dict(platform_id, entry, True)


# ---------------------------------------------------------------------------
# Operational state read endpoint (KR-P2-OPS-PANEL)
# ---------------------------------------------------------------------------
#
# KR-P2-I-integration ST5: the endpoint now reads the live
# ``OperationalStateHolder`` singleton from
# ``agent.operational_state_holder`` (wired at IsoKron provider init by
# ST3). When the holder is initialized, the response carries the real
# state + the in-memory transition-history ring; the ``stub`` flag is
# dropped so the admin panel auto-stops rendering its stub banner.
#
# When the holder is NOT yet initialized (boot in progress, or the
# IsoKron provider failed to construct), the endpoint returns the
# stub-shape with ``stub: True`` PLUS an ``error`` field naming the
# specific cause — the panel renders a "holder not initialized" banner
# distinct from the cold-stub banner so operators can distinguish
# "no wire-in yet" from "real runtime is up".


@app.get("/api/operational-state")
async def get_operational_state():
    """Return Kora's current operational state.

    Live source: ``agent.operational_state_holder.get_holder()``.
    The OperationalStateHolder is initialized at IsoKron-provider
    boot (see ``agent.operational_state_wire.wire_operational_state``).

    Enum values pinned to R4.1 §9.1:
      primary_state     ∈ {booting, ready, active, paused, stopped}
      claim_permission  ∈ {none, critical_only, normal}
      degradation_reason∈ {cost, auth, dispatch, substrate, migration,
                           operator, token_expiring, retry_ceiling}
    """
    from agent.operational_state import transitions_from
    from agent.operational_state_holder import get_holder

    holder = get_holder()
    if holder is None:
        # Holder not yet initialized — boot incomplete or the IsoKron
        # provider failed to construct. Keep the stub shape so the
        # admin panel renders gracefully; surface the cause via
        # ``error`` so the operator knows it's not cold-stub state.
        return {
            "primary_state": "booting",
            "claim_permission": "none",
            "degradation_reasons": [],
            "is_degraded": False,
            "transition_history": [],
            "valid_next_states": [],
            "stub": True,
            "error": "OperationalStateHolder not yet initialized",
        }

    state = holder.current
    return {
        "primary_state": state.primary_state.value,
        "claim_permission": state.claim_permission.value,
        "degradation_reasons": sorted(
            r.value for r in state.degradation_reasons
        ),
        "is_degraded": state.is_degraded(),
        "transition_history": holder.history(limit=10),
        "valid_next_states": [
            {"to_state": t.to_state.value, "trigger": t.trigger}
            for t in transitions_from(state.primary_state)
        ],
        # ``stub`` field intentionally absent on the live-state path —
        # the admin panel renders its stub banner only when the field
        # is present and truthy.
    }


# ---------------------------------------------------------------------------
# Sea_Tickets — Kora-assigned read endpoint (KR-P2-SEA-PANEL)
# ---------------------------------------------------------------------------
#
# v1 returns a hardcoded grouped-by-status stub so the admin panel can
# ship before KR-P2-E (consumer loop) wires Kora as a real Sea_Tickets
# consumer. The ``stub: True`` flag is the explicit "this is sample
# data, not real tickets" signal — the frontend renders a banner when
# True so operators don't get misled during a real outage.
#
# Flip-over: when KR-P2-E lands and ``IsoKronMemoryProvider`` grows a
# ``get_assigned_sea_tickets(actor_id)`` helper, replace the body with
# a projection of that read and drop the ``stub`` flag. Page shape is
# unchanged.


@app.get("/api/sea-tickets/kora-assigned")
async def get_kora_assigned_sea_tickets():
    """Return Sea_Tickets currently assigned to the Kora actor.

    Live source (KR-P2-CLEANUP ST2): reads via
    ``plugins.memory.isokron.assigned_sea_tickets.get_assigned_sea_tickets_via_provider``
    against the gateway-level active ``IsoKronMemoryProvider``
    (registered at gateway boot by ``sea_ticket_poller_lifecycle``).

    Grouped by status:
      in_progress       — claim_fence_token is set (Kora's working it)
      queued            — assigned, waiting for claim
      recently_resolved — recently completed / released / failed_retryable
      failed_or_blocked — failed_terminal / blocked_needs_operator

    When the active provider isn't registered (early boot or
    isolated-test contexts), returns the stub-shape with
    ``stub: True`` + an ``error`` field — the cockpit panel renders
    a distinct banner so operators can tell "no wire-in yet" apart
    from "real runtime is up".

    Internal idempotency tokens (e.g. ``claim_fence_token``) are
    intentionally never included in the API shape.
    """
    from plugins.memory.isokron.active_provider import get_active_provider
    from plugins.memory.isokron.assigned_sea_tickets import (
        get_assigned_sea_tickets_via_provider,
    )

    provider = get_active_provider()
    if provider is None:
        return _kora_assigned_sea_tickets_stub(
            error=(
                "IsoKronMemoryProvider not yet registered as active "
                "(gateway boot in progress, or provider failed to "
                "initialize)"
            )
        )

    grouped = await get_assigned_sea_tickets_via_provider(provider=provider)
    if grouped is None:
        return _kora_assigned_sea_tickets_stub(
            error=(
                "substrate read returned None — see "
                "``[assigned_sea_tickets]`` log lines for the cause"
            )
        )

    return grouped


def _kora_assigned_sea_tickets_stub(*, error: str) -> dict:
    """Stub-shape returned on the uninitialized / read-failure branch.

    Same four buckets the live read returns + ``stub: True`` + an
    ``error`` field naming the cause. The panel's stub banner
    activates on ``stub`` truthiness; an ``error`` line is rendered
    underneath to distinguish "no provider yet" from cold-stub.
    """
    return {
        "in_progress": [
            {
                "id": "sea_ticket_stub_001",
                "title": "Stub ticket — currently in progress (sample data)",
                "criticality": "normal",
                "claimed_at": "2026-05-21T17:30:00Z",
                "claim_count": 1,
                "work_attempt_count": 1,
            },
        ],
        "queued": [
            {
                "id": "sea_ticket_stub_002",
                "title": "Stub ticket — queued (sample data)",
                "criticality": "low",
                "assigned_at": "2026-05-21T17:25:00Z",
                "next_eligible_at": None,
            },
            {
                "id": "sea_ticket_stub_003",
                "title": "Stub ticket — queued, deferred (sample data)",
                "criticality": "normal",
                "assigned_at": "2026-05-21T17:20:00Z",
                "next_eligible_at": "2026-05-21T18:00:00Z",
            },
        ],
        "recently_resolved": [
            {
                "id": "sea_ticket_stub_004",
                "title": "Stub ticket — resolved (sample data)",
                "criticality": "normal",
                "resolved_at": "2026-05-21T16:00:00Z",
                "resolution": "completed",
                "model_tier_used": "sonnet",
            },
        ],
        "failed_or_blocked": [
            {
                "id": "sea_ticket_stub_005",
                "title": "Stub ticket — blocked needs operator (sample data)",
                "criticality": "normal",
                "state": "blocked_needs_operator",
                "failure_count_by_reason": {
                    "agent_loop_timeout": 1,
                    "tool_exec_failure": 0,
                },
            },
        ],
        "stub": True,
        "error": error,
    }


# ---------------------------------------------------------------------------
# kora_control — runtime-observed-state read endpoint (KR-P2-CONTROL-PANEL)
# ---------------------------------------------------------------------------
#
# v1 returns a hardcoded sample-data stub grouping observed STOP-KORA
# commands by lifecycle position (active / recently_enforced / history)
# so the admin panel can ship before KR-P2-J wires the runtime to
# observe + act on the kora_control command log. The ``stub: True``
# flag drives the panel's STUB banner; flip it off when the body is
# replaced with a real KoraControlReader.get_all_observed_commands()
# projection (post-KR-P2-J).
#
# Command ISSUANCE happens cockpit-side (IsoKron-team's lane). This
# Kora-runtime panel is observation-only — operators check here to
# confirm the runtime saw + acked + enforced the commands they issued
# from the cockpit. No write surface lives here.


@app.get("/api/kora-control/observed-state")
async def get_kora_control_observed_state():
    """Return kora_control commands as observed by the runtime.

    Live source (KR-P2-CLEANUP ST3): reads via
    ``plugins.memory.isokron.observed_kora_control.get_observed_state_via_provider``
    against the gateway-level active ``IsoKronMemoryProvider`` (set
    at gateway boot by ``sea_ticket_poller_lifecycle``). Workspace-
    scoped via the substrate's ``app.workspace_id`` GUC.

    Grouped by lifecycle position:
      active             — open commands (lifecycle ∈ {created,
                           visible_to_runtime, acknowledged,
                           enforcing})
      recently_enforced  — last 10 enforced
      history            — older terminal-state commands (enforced
                           overflow, superseded, expired, failed,
                           escalated; capped at 30)

    When the active provider isn't registered (early boot, isolated-
    test contexts), returns the stub-shape with ``stub: True`` + an
    ``error`` field naming the cause.
    """
    from plugins.memory.isokron.active_provider import get_active_provider
    from plugins.memory.isokron.observed_kora_control import (
        get_observed_state_via_provider,
    )

    provider = get_active_provider()
    if provider is None:
        return _kora_control_observed_state_stub(
            error=(
                "IsoKronMemoryProvider not yet registered as active "
                "(gateway boot in progress, or provider failed to "
                "initialize)"
            )
        )

    grouped = await get_observed_state_via_provider(provider=provider)
    if grouped is None:
        return _kora_control_observed_state_stub(
            error=(
                "substrate read returned None — see "
                "``[observed_kora_control]`` log lines for the cause"
            )
        )

    return grouped


def _kora_control_observed_state_stub(*, error: str) -> dict:
    """Stub-shape returned on the uninitialized / read-failure branch.

    Same three buckets the live read produces + ``stub: True`` +
    ``error`` naming the cause. The panel's stub banner activates on
    ``stub`` truthiness; the ``error`` line is rendered underneath
    to distinguish "no provider yet" from cold-stub.
    """
    return {
        "active": [
            {
                "command_id": "kc_stub_001",
                "level": 1,
                "kind": "stop",
                "reason": (
                    "Sample STOP-KORA L1 (intake-stop) — runtime "
                    "acknowledged but not yet enforcing"
                ),
                "issuer": "operator@stormhaven (cockpit session stub-cs-001)",
                "sequence": 42,
                "created_at": "2026-05-21T18:00:00Z",
                "visible_to_runtime_at": "2026-05-21T18:00:02Z",
                "observed_at": "2026-05-21T18:00:05Z",
                "acknowledged_at": "2026-05-21T18:00:07Z",
                "enforced_at": None,
                "lifecycle_state": "acknowledged",
                "expires_at": "2026-05-21T19:00:00Z",
                "target_session": None,
            },
        ],
        "recently_enforced": [
            {
                "command_id": "kc_stub_002",
                "level": 0,
                "kind": "reset",
                "reason": "Sample L0 reset — clears lower commands; operator-cleared",
                "issuer": "operator@stormhaven (cockpit session stub-cs-002)",
                "sequence": 41,
                "created_at": "2026-05-21T17:30:00Z",
                "visible_to_runtime_at": "2026-05-21T17:30:01Z",
                "observed_at": "2026-05-21T17:30:03Z",
                "acknowledged_at": "2026-05-21T17:30:04Z",
                "enforced_at": "2026-05-21T17:30:05Z",
                "lifecycle_state": "enforced",
                "expires_at": None,
                "target_session": None,
            },
        ],
        "history": [
            {
                "command_id": "kc_stub_003",
                "level": 2,
                "kind": "stop",
                "reason": "Sample historical L2 drain — completed",
                "issuer": "operator@stormhaven (cockpit session stub-cs-003)",
                "sequence": 35,
                "created_at": "2026-05-21T15:00:00Z",
                "visible_to_runtime_at": "2026-05-21T15:00:01Z",
                "observed_at": "2026-05-21T15:00:03Z",
                "acknowledged_at": "2026-05-21T15:00:04Z",
                "enforced_at": "2026-05-21T15:00:08Z",
                "lifecycle_state": "enforced",
                "expires_at": None,
                "target_session": None,
            },
            {
                "command_id": "kc_stub_004",
                "level": 1,
                "kind": "stop",
                "reason": (
                    "Sample expired L1 — runtime ack'd but operator "
                    "never enforced + ran out window"
                ),
                "issuer": "operator@stormhaven (cockpit session stub-cs-004)",
                "sequence": 28,
                "created_at": "2026-05-21T12:00:00Z",
                "visible_to_runtime_at": "2026-05-21T12:00:01Z",
                "observed_at": "2026-05-21T12:00:03Z",
                "acknowledged_at": "2026-05-21T12:00:04Z",
                "enforced_at": None,
                "lifecycle_state": "expired",
                "expires_at": "2026-05-21T13:00:00Z",
                "target_session": None,
            },
        ],
        "stub": True,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Boot status — gate sequence outcome (KR-P2-BOOT-PANEL)
# ---------------------------------------------------------------------------
#
# v1 returns a hardcoded sample of a successful boot (current) plus one
# failed boot (history) so the admin panel renders meaningfully. Flips
# to real data via ``BootGateRunner.last_result()`` +
# ``BootGateRunner.recent_history(limit=20)`` once KR-P2-H lands.
#
# Operator intervention on a stuck boot is operator-side (flyctl restart,
# Doppler env-var fix, etc.); the panel is observation-only. No "force
# re-boot" or "skip gate" buttons live here.


@app.get("/api/boot-status")
async def get_boot_status():
    """Return the most-recent boot's gate-sequence outcome + history.

    Live source (KR-P2-CLEANUP ST4): reads ``BootGateRunner.last_result()``
    + ``BootGateRunner.recent_history(limit=20)``. The runner's
    in-memory ring is populated by ``agent.boot_coordinator.run_boot_sequence``
    on every terminal outcome (READY / STOPPED, including diagnostic
    runs). Ring wipes on process restart — durable history lives in
    the chain-event log (``kora.boot.ready`` / ``kora.boot.failed``).

    Outcome enum: booting | ready | failed.
    Gate outcome enum: pass | fail.
    Gate class enum: transient (re-runnable) | invariant (must hold).
    """
    from agent.boot_gates import BootGateRunner

    last = BootGateRunner.last_result()
    if last is None:
        return _boot_status_stub(
            error=(
                "no boot has completed this process lifetime — "
                "BootGateRunner._recent_history is empty"
            )
        )

    history_entries = BootGateRunner.recent_history(limit=20)
    # Most-recent first for the panel + drop the head (it's `current`).
    history_entries_oldest_first = history_entries[:-1]

    return {
        "current": _project_boot_entry_current(last),
        "history": [
            _project_boot_entry_history(entry)
            for entry in reversed(history_entries_oldest_first)
        ],
    }


def _project_boot_entry_current(entry) -> dict:
    """Project a BootHistoryEntry into the panel's `current` shape."""
    summary = entry.summary
    outcome = "ready" if summary.result.value == "ready" else "failed"
    gates = [
        {
            "gate_id": g.gate_id,
            "title": getattr(g, "title", g.gate_id),
            "gate_class": g.gate_class.value,
            "outcome": g.outcome.value,
            "elapsed_ms": int(getattr(g, "elapsed_ms", 0) or 0),
            "detail": getattr(g, "detail", "") or "",
        }
        for g in summary.gate_results
    ]
    return {
        "boot_id": entry.boot_id,
        "primary_state": outcome,
        "started_at": entry.started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "completed_at": entry.completed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_ms": entry.elapsed_ms,
        "outcome": outcome,
        "gates": gates,
    }


def _project_boot_entry_history(entry) -> dict:
    """Project a past BootHistoryEntry into the panel's `history` shape.

    History entries elide the per-gate detail (the panel only renders
    summary rows) but call out the failing gate when one exists.
    """
    summary = entry.summary
    outcome = "ready" if summary.result.value == "ready" else "failed"
    out: dict = {
        "boot_id": entry.boot_id,
        "started_at": entry.started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "completed_at": entry.completed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_ms": entry.elapsed_ms,
        "outcome": outcome,
    }
    if summary.failed_gate is not None:
        out["failed_gate_id"] = summary.failed_gate.gate_id
        out["failed_gate_title"] = getattr(
            summary.failed_gate, "title", summary.failed_gate.gate_id
        )
        out["detail"] = getattr(summary.failed_gate, "detail", "") or ""
    return out


def _boot_status_stub(*, error: str) -> dict:
    """Stub-shape returned when BootGateRunner's history is empty
    (process just started; no boot has completed yet)."""
    return {
        "current": {
            "boot_id": "boot_stub_001",
            "primary_state": "ready",
            "started_at": "2026-05-21T19:30:00Z",
            "completed_at": "2026-05-21T19:30:08Z",
            "elapsed_ms": 8120,
            "outcome": "ready",
            "gates": [
                {
                    "gate_id": "1",
                    "title": "Claude auth valid",
                    "gate_class": "transient",
                    "outcome": "pass",
                    "elapsed_ms": 412,
                    "detail": "claude auth status: ok",
                },
                {
                    "gate_id": "4",
                    "title": "kora_runtime role perms",
                    "gate_class": "transient",
                    "outcome": "pass",
                    "elapsed_ms": 89,
                    "detail": "expected pass + expected deny both confirmed",
                },
                {
                    "gate_id": "5",
                    "title": "kronicle-mcp reachable",
                    "gate_class": "transient",
                    "outcome": "pass",
                    "elapsed_ms": 64,
                    "detail": "kronicle-mcp.internal:8443/health 200",
                },
                {
                    "gate_id": "6",
                    "title": "wsk_* token valid",
                    "gate_class": "transient",
                    "outcome": "pass",
                    "elapsed_ms": 220,
                    "detail": "kora__read_kora_capability_row probe ok",
                },
                {
                    "gate_id": "7",
                    "title": "canonical kora actor row exists",
                    "gate_class": "invariant",
                    "outcome": "pass",
                    "elapsed_ms": 41,
                    "detail": "actor_registry row present for kora actor_kind",
                },
                {
                    "gate_id": "8",
                    "title": "Charter + capability matrix load",
                    "gate_class": "transient",
                    "outcome": "pass",
                    "elapsed_ms": 720,
                    "detail": "constitution_cache populated; 1 active revision",
                },
                {
                    "gate_id": "10",
                    "title": "KR-7 boot smoke check (read-only)",
                    "gate_class": "invariant",
                    "outcome": "pass",
                    "elapsed_ms": 95,
                    "detail": "dispatch tier would attribute canonical 0076 actor",
                },
            ],
        },
        "history": [
            {
                "boot_id": "boot_stub_000",
                "started_at": "2026-05-21T19:00:00Z",
                "completed_at": "2026-05-21T19:00:12Z",
                "elapsed_ms": 12450,
                "outcome": "failed",
                "failed_gate_id": "5",
                "failed_gate_title": "kronicle-mcp reachable",
                "detail": "kronicle-mcp.internal:8443 connection timeout after 3 retries",
            },
        ],
        "stub": True,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Cost ladder — current burn + rung + deferred (KR-P2-COST-PANEL → KR-P2-COST-FLIP)
# ---------------------------------------------------------------------------
#
# KR-P2-COST-FLIP (this commit) flips the body from stub to live read.
# Two-branch shape mirroring DR-FLIP: live path drops the stub flag;
# uninit (no CostStateHolder or no IsoKronMemoryProvider) and failure
# (substrate read raises) both return the documented fallback shape
# with stub:true + an error field so the operator sees the cause in
# the FE without server-log digging.
#
# Read-only by design: rung transitions happen automatically in
# KR-P2-K's runtime; the "extra-usage OFF" hard cap is an Anthropic-
# console toggle (out-of-process). No control surface lives here.
#
# Security: response shape carries dollar amounts + model tier + ticket
# IDs only. Never includes raw credentials. The §8 grep check from PR
# #49 still applies.


_COST_STATE_FALLBACK: Dict[str, Any] = {
    "current": {
        "billing_period_start": "1970-01-01T00:00:00Z",
        "billing_period_end": "1970-01-31T23:59:59Z",
        "days_remaining": 0,
        "credit_pool_usd": 0.00,
        "spent_to_date_usd": 0.00,
        "burn_rate_usd_per_day": 0.00,
        "projected_end_of_period_usd": 0.00,
        "active_rung": "normal",
        "active_rung_threshold_pct": 75,
        "current_pct_used": 0.00,
        "effective_model_tier": "opus",
        "downshift_active": False,
        "downshift_reason": None,
        "extra_usage_off": True,
    },
    "rate_limit_pulse": None,
    "deferred_tickets": [],
    "reconciliation_history": [],
}


@app.get("/api/cost-state")
async def get_cost_state():
    """Return Kora's current cost-ladder state.

    Live read via ``get_cost_state_summary`` (KR-P2-COST-FLIP). When
    the CostStateHolder isn't initialised, the IsoKron provider isn't
    registered, or the substrate read fails, returns the same shape
    with ``stub: True`` + an ``error`` field so the FE keeps rendering
    and the operator sees the cause.
    """
    try:
        from agent.cost_state_holder import get_cost_holder
        from agent.cost_state_summary import get_cost_state_summary
        from plugins.memory.isokron import get_last_active_provider

        cost_holder = get_cost_holder()
        provider = get_last_active_provider()
        if cost_holder is None:
            return {
                **_COST_STATE_FALLBACK,
                "stub": True,
                "error": "CostStateHolder not initialised",
            }
        # provider may be None — get_cost_state_summary degrades the
        # deferred-tickets list to [] in that case, but the holder
        # snapshot still produces useful current/rate_limit fields.
        summary = await get_cost_state_summary(cost_holder, provider)
    except Exception as exc:
        _log.exception("[kora.cost_panel] live read failed")
        return {
            **_COST_STATE_FALLBACK,
            "stub": True,
            "error": f"{type(exc).__name__}: {exc}",
        }

    return {
        "current": summary.current,
        "rate_limit_pulse": summary.rate_limit_pulse,
        "deferred_tickets": list(summary.deferred_tickets),
        "reconciliation_history": list(summary.reconciliation_history),
        "stub": False,
    }


# ---------------------------------------------------------------------------
# Capabilities inspector — live read (KR-P2-CAP-PANEL)
# ---------------------------------------------------------------------------
#
# Real data out of the gate (no stub flag): reads the 64-tool
# ``TOOL_CAPABILITY_MAP`` shipped by KR-P2-A and resolves each unique
# ``cap_*`` via ``actor_has_capability``. ``KeyError`` from the cap
# helper means the cap isn't in the C2 mirror yet — that's the
# documented fail-CLOSED state per
# D-krp2a-st1-infra-tier-caps-missing-from-c2-mirror; the verdict
# surfaces as ``unmapped_in_c2_mirror`` so the operator sees real
# permission shape rather than a swallowed error.
#
# The four substrate-tier ``kora__*`` tools (K-7/8/9/10) are NOT in
# TOOL_CAPABILITY_MAP — the pre-screen short-circuits any name
# starting with ``kora__`` with PASS because substrate-side dispatch
# is the authoritative gate. They surface in their own ``substrate_tier``
# array so the panel can render the full surface as "always PASS".

# Substrate-tier (always-PASS) tools. Pinned by name to match the
# bucket §3 contract + the test_substrate_tier_contains_exactly_four
# guard. If a 5th ``kora__*`` tool ever ships, both this list and the
# test need updating in lockstep with the bucket spec.
_CAP_PANEL_SUBSTRATE_TIER_TOOLS: List[str] = [
    "kora__append_event",
    "kora__write_agent_scratchpad",
    "kora__create_relationlink",
    "kora__read_kora_capability_row",
]


@app.get("/api/capabilities")
async def get_capabilities():
    """Return the 64-tool capability map + live per-cap verdict.

    Live read — no stub flag. Reflects current C2 mirror state.
    """
    from agent.tool_capability_map import TOOL_CAPABILITY_MAP
    from plugins.memory.isokron.capability_check import actor_has_capability

    # Resolve each unique cap once (17 lookups, not 64).
    caps_seen: Dict[str, str] = {}
    for cap in set(TOOL_CAPABILITY_MAP.values()):
        try:
            granted = actor_has_capability(cap)
            caps_seen[cap] = "granted" if granted else "denied"
        except KeyError:
            caps_seen[cap] = "unmapped_in_c2_mirror"
        except Exception:
            _log.exception("Unexpected error resolving capability %s", cap)
            caps_seen[cap] = "error"

    # Build per-cap-group entries (cap_name → {cap_name, verdict, tools[]})
    groups: Dict[str, Dict[str, Any]] = {}
    for tool_name, cap_name in TOOL_CAPABILITY_MAP.items():
        if cap_name not in groups:
            groups[cap_name] = {
                "cap_name": cap_name,
                "verdict": caps_seen[cap_name],
                "tools": [],
            }
        groups[cap_name]["tools"].append(tool_name)

    # Sort tools within each group; sort groups by cap_name alphabetical
    for grp in groups.values():
        grp["tools"].sort()
    sorted_groups = sorted(groups.values(), key=lambda g: g["cap_name"])

    return {
        "groups": sorted_groups,
        "substrate_tier": list(_CAP_PANEL_SUBSTRATE_TIER_TOOLS),
        "total_tools": len(TOOL_CAPABILITY_MAP) + len(_CAP_PANEL_SUBSTRATE_TIER_TOOLS),
        "total_caps": len(caps_seen),
        "unmapped_count": sum(
            1 for v in caps_seen.values() if v == "unmapped_in_c2_mirror"
        ),
    }


# ---------------------------------------------------------------------------
# Health rollup — R4.1 §9.7 (KR-P2-HEALTH-PANEL)
# ---------------------------------------------------------------------------
#
# v1 returns a hardcoded all-fresh stub so the admin panel can ship
# before KR-P2-L wires the runtime ``kora.health.probe`` emitter +
# subsignal collectors. Flips to real data via the future
# HealthRollupHolder.current() accessor — page is unchanged.
#
# Read-only by design. Alerting integration lives cockpit-side (the
# IsoKron-team's lane); this is observation-only.
#
# Top-level enum per R4.1 §9.7:
#   healthy | degraded | stopped | outage
# Per-subsignal status:
#   fresh | stale | missing | degraded
# stopped_reason: non-null only when overall ∈ {stopped, outage} — the
# distinction lets operators tell "intentionally stopped" apart from
# "outage" without guessing.
#
# P6 surface (R4.1 §9.7): the frontend renders a red top-of-page banner
# whenever ``escalation_watcher_liveness.status == "stale"`` so the
# operator knows control-plane escalation is unavailable and they must
# use manual L4. The endpoint just surfaces the subsignal honestly; the
# banner is FE-rendered.


# KR-P2-L ST4 — fallback payload returned when the live read fails.
# Same shape the v1 stub returned so the FE renders unchanged on
# either branch; the ``stub: True`` + ``error`` fields tell the
# operator why the live read isn't engaged.
_HEALTH_ROLLUP_FALLBACK: Dict[str, Any] = {
    "overall": "healthy",
    "control_plane": "healthy",
    "worker": "healthy",
    "stopped_reason": None,
    "subsignals": {
        "last_successful_write": {
            "status": "fresh",
            "value_at": "2026-05-21T22:30:00Z",
            "threshold_seconds": 300,
            "elapsed_seconds": 45,
        },
        "claim_state": {
            "status": "fresh",
            "value": "active",
            "claim_id": "stub_claim_001",
        },
        "credit_burn": {
            "status": "fresh",
            "value_pct": 43.7,
            "threshold_pct": 90,
            "rung": "warn_75",
        },
        "breaker_state": {
            "status": "fresh",
            "value": "closed",
        },
        "auth_validity_window": {
            "status": "fresh",
            "expires_at": "2027-04-18T00:00:00Z",
            "threshold_days": 30,
            "days_remaining": 332,
        },
        "dispatch_reachable": {
            "status": "fresh",
            "value_at": "2026-05-21T22:30:00Z",
            "threshold_seconds": 60,
            "elapsed_seconds": 5,
        },
        "last_heartbeat": {
            "status": "fresh",
            "value_at": "2026-05-21T22:29:58Z",
            "threshold_seconds": 90,
            "elapsed_seconds": 7,
        },
        "escalation_watcher_liveness": {
            "status": "fresh",
            "value_at": "2026-05-21T22:29:55Z",
            "threshold_seconds": 15,
            "elapsed_seconds": 10,
        },
    },
}


@app.get("/api/health-rollup")
async def get_health_rollup():
    """Return Kora's health rollup with 8 R4.1 §9.7 subsignals.

    KR-P2-L ST4: flipped from stub to live read via
    :func:`agent.health_rollup_holder.HealthRollupHolder.current`.

    Two-branch shape (mirrors KR-P2-DR-FLIP):
      - Live path → projects ``HealthRollup`` via
        :func:`rollup_to_panel_payload`; includes ``stub: False``.
      - Fallback (holder uninit / collect raises / projection raises)
        → returns ``_HEALTH_ROLLUP_FALLBACK`` shape + ``stub: True``
        + ``error`` field naming the underlying cause, so the
        operator sees why the live read isn't engaged rather than a
        500.

    The holder is lazy-initialized on first request if the gateway
    boot didn't already (keeps the endpoint usable in
    agent-session-only contexts where no explicit init runs).
    """
    try:
        from agent.health_rollup_holder import (
            get_health_rollup_holder,
            init_health_rollup_holder,
            rollup_to_panel_payload,
        )

        holder = get_health_rollup_holder()
        if holder is None:
            holder = init_health_rollup_holder()
        rollup = holder.current()
        payload = rollup_to_panel_payload(rollup)
        payload["stub"] = False
        return payload
    except Exception as exc:
        _log.exception("[kora.health_panel] live read failed")
        return {
            **_HEALTH_ROLLUP_FALLBACK,
            "stub": True,
            "error": f"{type(exc).__name__}: {exc}",
        }


# ---------------------------------------------------------------------------
# Disaster recovery / substrate epoch (KR-P2-DR-PANEL → KR-P2-DR-FLIP)
# ---------------------------------------------------------------------------
#
# R4.1 §9.8: when a PITR happens, the substrate_epoch bumps; gate 3b
# detects the mismatch with kora_known_epoch and Kora transitions to
# PAUSED{substrate} until the operator runs the post-PITR runbook to
# clear. This endpoint surfaces:
#
#   * current epoch state (substrate_epoch vs kora_known_epoch + match)
#   * epoch_history (intentionally empty in v1 — no kora_known_epoch
#     history table exists; populates when substrate-team ships a
#     history view, no FE change needed)
#   * recent_dr_events (kora.dr.observed payloads from event_log,
#     last 10)
#   * runbook_pending — derived flag the FE uses to render the red
#     top-of-page DR alert
#
# KR-P2-DR-FLIP (this commit) flips the body from stub to live read.
# Two-branch shape: live path drops the stub flag; uninit/failure
# returns the stub fallback shape with stub:true + error field so
# the operator sees the underlying cause rather than a 500. Mirrors
# the KR-P2-CLEANUP ST2/3/4 pattern CC#3 just used for SEA / CONTROL
# / BOOT.
#
# Read-only: the post-PITR substrate_epoch bump is an OS-level
# operator action (Fly secret + flyctl restart). This panel SURFACES
# the need; it does not execute the runbook.


_DR_STATE_FALLBACK: Dict[str, Any] = {
    "current": {
        "substrate_epoch": 0,
        "kora_known_epoch": None,
        "match_status": "unknown",
        "last_check_at": "1970-01-01T00:00:00Z",
        "kora_paused_substrate": False,
    },
    "epoch_history": [],
    "recent_dr_events": [],
    "runbook_pending": False,
}


@app.get("/api/dr-state")
async def get_dr_state():
    """Return Kora's DR / substrate-epoch state.

    Live read via ``get_dr_state_summary`` (KR-P2-DR-FLIP). When the
    IsoKron provider isn't registered, the workspace_id can't be
    resolved, or the substrate read fails, returns the same shape with
    ``stub: True`` + an ``error`` field so the FE keeps rendering and
    the operator sees why the live read failed.

    Enum reference:
      match_status   ∈ {clean, mismatch_detected, pending_runbook, unknown}
      event_type     == "kora.dr.observed"
    """
    try:
        from plugins.memory.isokron import get_last_active_provider
        from plugins.memory.isokron.dr_epoch import get_dr_state_summary

        provider = get_last_active_provider()
        if provider is None:
            return {
                **_DR_STATE_FALLBACK,
                "stub": True,
                "error": "IsoKronMemoryProvider not initialised",
            }
        ws = provider._resolve_workspace_id()
        if ws is None:
            return {
                **_DR_STATE_FALLBACK,
                "stub": True,
                "error": "no workspace_id resolvable",
            }
        summary = await get_dr_state_summary(provider, ws)
    except Exception as exc:
        _log.exception("[kora.dr_panel] live read failed")
        return {
            **_DR_STATE_FALLBACK,
            "stub": True,
            "error": f"{type(exc).__name__}: {exc}",
        }

    runbook_pending = summary.kora_paused_substrate or summary.match_status in {
        "mismatch_detected",
        "pending_runbook",
    }

    return {
        "current": {
            "substrate_epoch": summary.substrate_epoch,
            "kora_known_epoch": summary.kora_known_epoch,
            "match_status": summary.match_status,
            "last_check_at": summary.last_check_at,
            "kora_paused_substrate": summary.kora_paused_substrate,
        },
        "epoch_history": list(summary.epoch_history),
        "recent_dr_events": list(summary.recent_dr_events),
        "runbook_pending": runbook_pending,
        "stub": False,
    }


# ---------------------------------------------------------------------------
# Charter / Constitution viewer (KR-P2-CHARTER-PANEL)
# ---------------------------------------------------------------------------
#
# Live read — no stub flag. Reads:
#   * Active (revision_id, rules_hash, loaded_at) from
#     ``IsoKronMemoryProvider._constitution_cache`` via the new
#     ``get_active_constitution_summary`` helper (cache-only; never
#     triggers a substrate fetch).
#   * Capability matrix (cap_name → [tools]) projected from KR-P2-A's
#     ``TOOL_CAPABILITY_MAP`` — same source CAP-PANEL uses, surfaced
#     as a simpler group shape (no per-cap verdict, that's CAP-PANEL's
#     job).
#   * Substrate-tier (always-PASS) ``kora__*`` tools.
#
# v1 fallback mode (KR-P2-CHARTER-PANEL §1, PM-approved):
# substrate doesn't expose rule CONTENT via a Kora-tier read; so the
# response always carries ``rules_available=False`` + empty
# ``rules=[]``. Frontend renders the subdued amber banner pointing at
# the cockpit. When substrate-team adds the rule-content read SECDEF,
# the helper grows; this endpoint body and the FE both stay unchanged.
#
# When no provider is registered (CI / dev runs without substrate
# config / a different memory provider selected), ``active`` is
# ``null`` — the capability matrix still renders so the panel remains
# operationally useful.


def _cap_panel_simple_groups() -> List[Dict[str, Any]]:
    """Project ``TOOL_CAPABILITY_MAP`` into ``[{cap_name, tools[]}]``.

    Shared with :func:`get_capabilities`'s ``groups`` shape minus the
    per-cap verdict: CHARTER-PANEL wants the policy *map* (which cap_*
    covers which tools), not the per-cap allow/deny resolution. The
    KR-P2-CHARTER-PANEL §5 regression guard pins that both endpoints
    surface identical (cap_name, sorted-tools) pairs.
    """
    from agent.tool_capability_map import TOOL_CAPABILITY_MAP

    grouped: Dict[str, List[str]] = {}
    for tool_name, cap_name in TOOL_CAPABILITY_MAP.items():
        grouped.setdefault(cap_name, []).append(tool_name)
    return [
        {"cap_name": cap, "tools": sorted(tools)}
        for cap, tools in sorted(grouped.items(), key=lambda kv: kv[0])
    ]


@app.get("/api/charter")
async def get_charter():
    """Return active Constitution summary + capability matrix.

    v1 fallback mode: rule content not exposed via Kora-tier read;
    only revision_id + rules_hash are surfaced. See module docstring.
    """
    active: Optional[Dict[str, Any]] = None
    try:
        from plugins.memory.isokron import get_last_active_provider

        provider = get_last_active_provider()
        if provider is not None:
            active = provider.get_active_constitution_summary()
    except Exception:
        # IsoKron plugin not importable in this environment — leave
        # active=None; the capability matrix still renders so the panel
        # is operationally useful.
        _log.exception(
            "Charter endpoint: failed to read constitution summary"
        )
        active = None

    return {
        "active": active,
        "capability_groups": _cap_panel_simple_groups(),
        "substrate_tier_tools": list(_CAP_PANEL_SUBSTRATE_TIER_TOOLS),
        "stub": False,
    }


# ---------------------------------------------------------------------------
# Chain-events live tail (KR-P2-CHAIN-EVENTS-PANEL)
# ---------------------------------------------------------------------------
#
# Live read — operator-facing tail of recent event_log rows for the
# active workspace. Pairs with the #kora-firehose Slack channel — same
# data, different surface; this panel works without Slack open.
#
# Manual reload only (per bucket §1 verification 2, option a): no SSE,
# no polling. Consistent UX with every other admin panel.
#
# Two-branch shape mirroring DR-FLIP / COST-FLIP: live drops stub;
# uninit/failure returns the fallback shape with stub:true + error.


_CHAIN_EVENTS_FALLBACK: Dict[str, Any] = {
    "events": [],
    "next_before_ts": None,
}


def _chain_event_envelope(event_type: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract operator-relevant envelope fields per event family.

    Constitution events carry ``revision_id`` + ``rules_hash`` — surface
    them so operators can spot a revision-flip at a glance. Other
    families don't have a documented envelope yet; return None and the
    FE skips the envelope display.
    """
    if event_type.startswith("kora.constitution."):
        envelope: Dict[str, Any] = {}
        for key in ("revision_id", "rules_hash"):
            if key in payload:
                envelope[key] = payload[key]
        return envelope or None
    return None


@app.get("/api/chain-events")
async def get_chain_events(
    prefix: str = "kora.",
    limit: int = 100,
    before_ts: Optional[str] = None,
):
    """Return recent event_log rows for the active workspace.

    Query params:
      prefix: event_type filter (default "kora."). Empty string = no filter.
      limit: max events to return (default 100, hard-capped at 500).
      before_ts: ISO-8601 cursor; returns events strictly older than this.
                 FE feeds previous page's last occurred_at to load older.
    """
    try:
        from plugins.memory.isokron import get_last_active_provider
        from plugins.memory.isokron.events import read_recent_events

        provider = get_last_active_provider()
        if provider is None:
            return {
                **_CHAIN_EVENTS_FALLBACK,
                "stub": True,
                "error": "IsoKronMemoryProvider not initialised",
            }
        ws = provider._resolve_workspace_id()
        if ws is None:
            return {
                **_CHAIN_EVENTS_FALLBACK,
                "stub": True,
                "error": "no workspace_id resolvable",
            }
        connection = getattr(provider, "_connection", None)
        if connection is None:
            return {
                **_CHAIN_EVENTS_FALLBACK,
                "stub": True,
                "error": "provider has no _connection",
            }
        pool = connection.get_pg_pool()

        rows = await read_recent_events(
            workspace_id=ws,
            pool=pool,
            event_type_prefix=prefix or None,
            limit=limit,
            before_ts=before_ts,
        )
    except Exception as exc:
        _log.exception("[kora.chain_events_panel] live read failed")
        return {
            **_CHAIN_EVENTS_FALLBACK,
            "stub": True,
            "error": f"{type(exc).__name__}: {exc}",
        }

    projected_events: List[Dict[str, Any]] = []
    for row in rows:
        envelope = _chain_event_envelope(row.event_type, row.payload)
        projected_events.append(
            {
                "event_id": row.event_id,
                "event_type": row.event_type,
                "actor_id": row.actor_id,
                # actor_kind requires a JOIN to actor_registry we don't
                # do in v1. Set to None; FE renders as "—".
                "actor_kind": None,
                "workspace_id": ws,
                "occurred_at": row.occurred_at,
                "payload": row.payload,
                "envelope": envelope,
            }
        )

    # Pagination cursor: the oldest event in this batch is the cursor
    # for the next "Load older" page. None when the batch is empty
    # (no more older events to load).
    next_before_ts = (
        projected_events[-1]["occurred_at"] if projected_events else None
    )

    return {
        "events": projected_events,
        "next_before_ts": next_before_ts,
        "stub": False,
    }


# ---------------------------------------------------------------------------
# Operator runbooks viewer (KR-P2-RUNBOOKS-PANEL)
# ---------------------------------------------------------------------------
#
# When DR fires at 2am, operator opens /runbooks and reads inline. No
# tabbing to docs. Read-only — runbooks are markdown files; operators
# update via kora-docs + redeploy.
#
# The manifest is EXPLICIT — pinned tuples of (id, title, repo-relative
# path). Auto-discovery rejected: ops want a known stable index of
# "these are the runbooks I might need at 2am", not a wandering scan
# of every .md in kora_docs/.
#
# Path-traversal defense is structural: the user-supplied {id} is only
# ever used as a dict key against _RUNBOOK_MANIFEST. The path values
# themselves are pinned strings, never concatenated with input.
# Belt+braces: an extra id validation regex rejects anything outside
# [a-z0-9_].
#
# Some manifest entries reference files in kora_docs/ which is a
# separate repo (rafe-walker/kora-docs) — not vendored into this
# repo. Those surface as ``available: false`` placeholders. Operator
# sees the runbook is supposed to exist but isn't authored yet;
# manifest entry serves as the "documented but pending" pointer.

import re as _re_runbooks


# Manifest: (title, repo-relative path)
_RUNBOOK_MANIFEST: Dict[str, Tuple[str, str]] = {
    "dr_runbook": (
        "Disaster Recovery — post-PITR substrate_epoch bump",
        "kora_docs/15_status_and_roadmap/dr_runbook.md",
    ),
    "token_rotation_runbook": (
        "Token rotation — wsk_* + CLAUDE_CODE_OAUTH_TOKEN unified procedure",
        "kora_docs/15_status_and_roadmap/token_rotation_runbook.md",
    ),
    "deploy_runbook_canonical": (
        "Deploy — canonical (post-KR-P2-F-pre)",
        "kora_docs/15_status_and_roadmap/deploy_runbook.md",
    ),
    "kora_dna": (
        "Kora DNA reference",
        "kora_docs/00_canonical_current_state/kora_dna.md",
    ),
    "deploy_fly_io": (
        "Fly deploy + Doppler secret refresh",
        "docs/deploy-fly-io.md",
    ),
}

_RUNBOOK_ID_RE = _re_runbooks.compile(r"^[a-z][a-z0-9_]*$")
_RUNBOOK_MAX_BYTES = 1_048_576  # 1 MiB hard cap


def _runbook_repo_root() -> Path:
    """Resolve the kora-repo root from this module's location.

    web_server.py lives at <root>/kora_cli/web_server.py, so the repo
    root is 2 parents up. Using __file__ rather than os.getcwd() keeps
    the resolution stable across uvicorn launch dirs.
    """
    return Path(__file__).resolve().parent.parent


def _runbook_resolved_path(rel_path: str) -> Path:
    """Resolve a manifest path against the repo root.

    Manifest paths are repo-relative + author-controlled — they are
    NOT user input. ``Path.resolve()`` here normalizes; we additionally
    check ``resolved.is_relative_to(root)`` as belt+braces so a future
    typo in the manifest can't accidentally point outside the repo.
    """
    return (_runbook_repo_root() / rel_path).resolve()


def _runbook_safe_stat(rel_path: str) -> Optional[Tuple[int, str]]:
    """Return (size_bytes, last_modified_iso) for an available runbook,
    or ``None`` when the file doesn't exist or sits outside the repo
    root. Never raises."""
    from datetime import datetime, timezone

    try:
        resolved = _runbook_resolved_path(rel_path)
        root = _runbook_repo_root()
        if not resolved.is_relative_to(root):
            return None
        if not resolved.is_file():
            return None
        stat = resolved.stat()
        last_modified = datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")
        return stat.st_size, last_modified
    except Exception:
        _log.exception(
            "[kora.runbooks] stat failed for %s", rel_path
        )
        return None


@app.get("/api/runbooks")
async def list_runbooks():
    """Return the explicit manifest of known operator runbooks.

    One entry per pinned (id, title, path) tuple; ``available`` reflects
    whether the file is present + readable on the deploy filesystem.
    Missing files surface as placeholders so operators see "this
    runbook is documented but not yet authored" rather than an empty
    panel.
    """
    runbooks: List[Dict[str, Any]] = []
    for runbook_id, (title, rel_path) in _RUNBOOK_MANIFEST.items():
        stat = _runbook_safe_stat(rel_path)
        if stat is None:
            runbooks.append(
                {
                    "id": runbook_id,
                    "title": title,
                    "path": rel_path,
                    "available": False,
                    "size_bytes": None,
                    "last_modified": None,
                }
            )
        else:
            size_bytes, last_modified = stat
            runbooks.append(
                {
                    "id": runbook_id,
                    "title": title,
                    "path": rel_path,
                    "available": True,
                    "size_bytes": size_bytes,
                    "last_modified": last_modified,
                }
            )
    return {"runbooks": runbooks}


@app.get("/api/runbooks/{runbook_id}/content")
async def get_runbook_content(runbook_id: str):
    """Return the raw markdown content for the named runbook.

    Path-traversal defense:
      1. ``runbook_id`` must match ``^[a-z][a-z0-9_]*$`` (rejects ``..``,
         ``/``, etc.).
      2. ``runbook_id`` is used only as a dict-key lookup against
         ``_RUNBOOK_MANIFEST``; the path string in the response is the
         manifest's pinned value, never composed from user input.
      3. After resolving, we verify the path stays inside the repo root
         (guards against a future manifest typo with ``..``).

    Caps:
      * File size > ``_RUNBOOK_MAX_BYTES`` (1 MiB) → 413.

    Errors:
      * Invalid id format → 404 (and log a WARN so operators can spot
        traversal attempts).
      * Unknown id → 404.
      * Manifest entry but file missing → 404 with a clear message
        (separately distinguishable from "unknown id" so the FE can
        render the "[runbook pending]" placeholder).
    """
    if not _RUNBOOK_ID_RE.match(runbook_id):
        _log.warning(
            "[kora.runbooks] rejecting malformed runbook_id=%r — "
            "possible path-traversal attempt",
            runbook_id,
        )
        raise HTTPException(status_code=404, detail="Runbook not found")

    entry = _RUNBOOK_MANIFEST.get(runbook_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Runbook not found")

    title, rel_path = entry
    resolved = _runbook_resolved_path(rel_path)
    root = _runbook_repo_root()

    if not resolved.is_relative_to(root):
        _log.warning(
            "[kora.runbooks] manifest entry %r resolves outside repo "
            "root (%s) — refusing to serve",
            runbook_id,
            resolved,
        )
        raise HTTPException(status_code=404, detail="Runbook not found")

    if not resolved.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Runbook '{runbook_id}' not yet authored",
        )

    size = resolved.stat().st_size
    if size > _RUNBOOK_MAX_BYTES:
        _log.warning(
            "[kora.runbooks] %r exceeds cap (%d > %d bytes); refusing",
            runbook_id,
            size,
            _RUNBOOK_MAX_BYTES,
        )
        raise HTTPException(
            status_code=413,
            detail=(
                f"Runbook '{runbook_id}' exceeds {_RUNBOOK_MAX_BYTES} "
                f"byte cap (actual {size}). Split the file or raise "
                f"the cap in web_server._RUNBOOK_MAX_BYTES."
            ),
        )

    text = resolved.read_text(encoding="utf-8")
    return Response(content=text, media_type="text/markdown; charset=utf-8")


# ---------------------------------------------------------------------------
# Diagnostic bundle (KR-P2-DIAG-BUNDLE)
# ---------------------------------------------------------------------------
#
# One click → zip containing all 10 panel data sources + manifest.
# Operator sends to substrate-team for triage instead of screenshotting
# panel-by-panel.
#
# Two firm contracts:
#   1. EXPLICIT allowlist (_PANEL_SOURCES). New sources don't auto-leak
#      into the bundle — adding one here is the explicit decision to
#      include it. Fail-CLOSED per bucket §1.
#   2. Credential-safe. Every source endpoint already excludes
#      credentials per its own design (cost-state has a credential-
#      leak guard test from PR #49; charter never returns tokens;
#      etc.). The DIAG-BUNDLE test #6 belt+braces grep against the
#      assembled bundle catches any aggregation accident.
#
# Per-endpoint try/except: bundle never fails entirely. A failed
# endpoint surfaces as an entry in manifest.errors[] with type +
# message; its JSON file is omitted (operator + substrate-team can
# tell from the manifest what's missing and why).


def _panel_sources() -> Dict[str, Any]:
    """Explicit (endpoint_name → async fetcher) mapping for the bundle.

    Kept as a function (not module-level dict) so the fetchers resolve
    to the current bound versions when the dict is built. Names match
    the bucket §3 documented zip-file names (without ``.json`` suffix).

    Adding a new source requires (a) appending to this dict and (b)
    confirming the source endpoint's response doesn't include
    credentials. Bucket §1 fail-CLOSED principle: no auto-discovery.
    """
    return {
        "operational_state": get_operational_state,
        "boot_status": get_boot_status,
        "cost_state": get_cost_state,
        "health_rollup": get_health_rollup,
        "dr_state": get_dr_state,
        "sea_tickets_kora_assigned": get_kora_assigned_sea_tickets,
        "kora_control_observed_state": get_kora_control_observed_state,
        "capabilities": get_capabilities,
        "charter": get_charter,
        # /api/chain-events takes query params; pre-bind the bucket §2
        # defaults (kora.* prefix, 500 events) into a small wrapper.
        "chain_events": lambda: get_chain_events(prefix="kora.", limit=500),
        # /api/runbooks (manifest only; content excluded — runbooks are
        # static docs, would bloat the bundle).
        "runbooks_manifest": list_runbooks,
    }


@app.get("/api/diag-bundle")
async def get_diag_bundle():
    """Stream a zip aggregating all 10 panel data sources for operator triage.

    Defensive: per-endpoint try/except — bundle never fails entirely.
    Credential-safe: never includes raw tokens or secrets.
    """
    import io as _io
    import json as _json
    import zipfile as _zipfile
    from datetime import datetime, timezone

    from fastapi.responses import StreamingResponse

    now = datetime.now(timezone.utc)
    bundle_id = f"kora-diag-bundle-{now.strftime('%Y%m%d-%H%M%S')}"

    buf = _io.BytesIO()
    errors: List[Dict[str, str]] = []
    included: List[str] = []

    sources = _panel_sources()

    with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
        for endpoint_name, fetcher in sources.items():
            try:
                data = await fetcher()
                zf.writestr(
                    f"{endpoint_name}.json",
                    _json.dumps(data, default=str, indent=2),
                )
                included.append(endpoint_name)
            except Exception as exc:
                # Don't fail the whole bundle for one bad endpoint —
                # surface in manifest so operator + substrate-team can
                # tell from the bundle what's missing and why.
                _log.exception(
                    "[kora.diag_bundle] %s fetch failed", endpoint_name
                )
                errors.append(
                    {
                        "endpoint": endpoint_name,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

        manifest = {
            "bundle_id": bundle_id,
            "bundle_at": now.isoformat().replace("+00:00", "Z"),
            "version": "1.0",
            "endpoints_included": included,
            "errors": errors,
        }
        zf.writestr("manifest.json", _json.dumps(manifest, indent=2))

    payload = buf.getvalue()

    async def _iter():
        # Single-chunk yield is the right shape here (~10 small JSONs,
        # well under a MB). StreamingResponse still gets us the
        # Content-Disposition + media_type plumbing.
        yield payload

    return StreamingResponse(
        _iter(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{bundle_id}.zip"'
        },
    )


# ---------------------------------------------------------------------------
# Backend service heartbeat (KR-HB-PANEL)
# ---------------------------------------------------------------------------
#
# v1 stub: hardcoded sample of 5 backend services (Vercel / Sentry /
# Doppler / Supabase / Fly) so the operator-facing dashboard can ship
# before the Python heartbeat module that talks to each service's API
# lands (KR-FEAT-HEARTBEAT follow-on, post-KR-D-DAEMON ST2).
#
# The ``stub: True`` flag is the explicit "this is sample data, not
# real polling" signal — the frontend renders a banner when True so
# operators never get misled during a real outage.
#
# Flip-over: when KR-FEAT-HEARTBEAT lands and a HeartbeatPoller
# emits per-service status, replace this body with a projection of
# the live state and drop the ``stub`` flag. Page UI is unchanged.


@app.get("/api/heartbeat/services")
async def get_heartbeat_services():
    """Return per-service heartbeat status for Joshua's backend stack.

    KR-FEAT-HEARTBEAT ST2: flipped from stub to live read via
    :func:`kora_cli.heartbeat_probes.current_service_snapshots`.
    The heartbeat scheduler populates the snapshot cache every
    ``KORA_HEARTBEAT_PROBE_INTERVAL_SEC`` seconds (default 300).

    Two-branch shape:

      - Live path: ``stub=False`` + ``cache_warming=False`` +
        ``services`` projected from the snapshot cache.
      - Cache-warming path: ``stub=False`` + ``cache_warming=True``
        + ``services=[]``. Returned when the daemon has just
        started and the first probe cycle hasn't completed —
        FE renders "Probes warming up..." instead of an empty
        state. Suppresses any false "all services down" alert
        heuristic.

    Service status enum: ``healthy`` | ``degraded`` | ``unhealthy``
    | ``unknown`` (the latter added in this flip; see TS
    ``HeartbeatStatus`` in ``web/src/lib/api.ts``).
    """
    from datetime import datetime, timezone

    from kora_cli.heartbeat_probes import current_service_snapshots

    snapshots = current_service_snapshots()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if not snapshots:
        return {
            "services": [],
            "generated_at": now_iso,
            "stub": False,
            "cache_warming": True,
        }

    # Stable ordering so FE doesn't re-shuffle cards between
    # refreshes: render in the default-probe registration order
    # (vercel → sentry → doppler → supabase → fly), then any
    # extras (operator-added probes via a future config-driven
    # extension) by alphabetical name.
    canonical_order = ("vercel", "sentry", "doppler", "supabase", "fly")
    ordered_names = [n for n in canonical_order if n in snapshots] + sorted(
        name for name in snapshots if name not in canonical_order
    )

    services: list[dict[str, Any]] = []
    for name in ordered_names:
        snapshot = snapshots[name]
        services.append({
            "name": snapshot.name,
            "status": snapshot.status,
            "last_check_at": snapshot.last_check_at.strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "latency_ms": snapshot.latency_ms,
            "details": dict(snapshot.details),
            "error": snapshot.error,
        })

    return {
        "services": services,
        "generated_at": now_iso,
        "stub": False,
        "cache_warming": False,
    }


# ---------------------------------------------------------------------------
# MCP client picker (KR-MCP-3) — Phase 2 Feature 1
# ---------------------------------------------------------------------------
#
# Operator-facing view of EXTERNAL MCPs Kora consumes (Kora-as-MCP-client).
# Distinct from KR-P2-C ST2's /api/mcp/servers (Kora-as-MCP-server admin
# at /mcp); this is /api/mcp/clients/list at /mcp-clients.
#
# v1 stub: 2 hardcoded clients (github + cloudflare) per bucket §3.
# CC#1's KR-MCP-1 ST2 replaces this with the real catalog read using
# the same payload shape. The ``stub: True`` flag is the explicit
# "this is sample data" signal; FE renders a banner when True.
#
# HARD CONSTRAINT (bucket §5 + ship-checklist): NEVER include token
# VALUES in the response. ``auth_token_env`` carries only the env-var
# NAME (e.g. ``KORA_MCP_GITHUB_TOKEN``); ``auth_token_present`` is a
# bool. Tokens live in Doppler — the cockpit never receives them.
# The §4 test guards against any future drift that leaks a value-
# shaped field.


@app.get("/api/mcp/clients/list")
async def list_mcp_clients():
    """Return the catalog of external MCPs Kora is configured to consume.

    KR-MCP-CLIENTS-FLIP: live read via
    :func:`kora_mcp.catalog.load_effective_catalog` (default github
    + cloudflare entries + operator overrides from
    ``~/.kora/config.yaml``). Replaces the v1 stub body from
    KR-MCP-3 (PR #106).

    Per-client fields (pinned by TS ``MCPClient`` interface in
    ``web/src/lib/api.ts``):

      name                  — short id (github, cloudflare, etc.)
      transport             — "stdio" | "streamable_http"
      endpoint              — command line or URL (UI truncates)
      status                — connected / configured_but_unconnected /
                              error / unhealthy
      auth_token_env        — Doppler env-var NAME (never the value)
      auth_token_present    — bool: env-var resolves to non-empty?
      allowed_tools_regex   — null = all tools; string = filter
      tools_count           — int when status=connected; null otherwise

    HARD CONSTRAINT (carried forward from KR-MCP-3 §5): token VALUES
    never appear in this response. ``auth_token_env`` is the env-var
    NAME; ``auth_token_present`` is a bool. The walk-all-keys
    security test in ``test_web_server_mcp_clients.py`` pins this
    invariant.

    Status mapping (post-KR-MCP-CONSUMPTION ST2):

      auth env unset / empty                              → ``unhealthy``
      auth env set, no snapshot OR stale OR not connected → ``configured_but_unconnected``
      auth env set + fresh snapshot + connected           → ``connected``

    A snapshot is "stale" when ``last_check_at`` is older than the
    health-check cadence (``KORA_MCP_HEALTH_CHECK_INTERVAL_SEC``,
    default 300s). A missed heartbeat cycle surfaces as
    ``configured_but_unconnected`` instead of falsely reporting
    ``connected``.

    ST2 additive payload fields:
      last_check_at  — ISO string when snapshot was taken (or null)
      last_error     — operator-readable failure string (or null)
      tools_count    — real value from snapshot (null when not connected)
    """
    from datetime import datetime, timedelta, timezone

    from kora_cli.listeners.mcp_consumption import (
        DEFAULT_HEALTH_CHECK_INTERVAL_SEC,
        _read_health_check_interval,
        current_health_snapshots,
    )
    from kora_mcp.catalog import check_endpoint_health, load_effective_catalog

    registry = load_effective_catalog()
    snapshots = current_health_snapshots()
    now = datetime.now(timezone.utc)
    try:
        cadence_seconds = _read_health_check_interval()
    except Exception:
        cadence_seconds = DEFAULT_HEALTH_CHECK_INTERVAL_SEC
    staleness_threshold = timedelta(seconds=cadence_seconds)

    clients: list[dict[str, Any]] = []
    for endpoint in registry.endpoints:
        health = check_endpoint_health(endpoint)
        snapshot = snapshots.get(endpoint.name)

        # Status derivation per KR-MCP-CONSUMPTION ST2.
        if not health.healthy:
            status = "unhealthy"
            tools_count = None
            last_check_at = None
            last_error = None
        elif snapshot is None:
            status = "configured_but_unconnected"
            tools_count = None
            last_check_at = None
            last_error = None
        else:
            is_stale = (now - snapshot.last_check_at) > staleness_threshold
            if snapshot.connected and not is_stale:
                status = "connected"
            else:
                status = "configured_but_unconnected"
            tools_count = (
                snapshot.tools_count if snapshot.connected else None
            )
            last_check_at = snapshot.last_check_at.strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            last_error = snapshot.last_error

        clients.append({
            "name": endpoint.name,
            "transport": endpoint.transport,
            "endpoint": endpoint.endpoint,
            "status": status,
            "auth_token_env": endpoint.auth_token_env or "",
            "auth_token_present": health.auth_env_set
            and endpoint.auth_token_env is not None,
            "allowed_tools_regex": endpoint.allowed_tools_regex,
            "tools_count": tools_count,
            "last_check_at": last_check_at,
            "last_error": last_error,
        })
    return {
        "clients": clients,
        "stub": False,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ---------------------------------------------------------------------------
# Webhook events lens (KR-WEBHOOK-EVENTS-PANEL)
# ---------------------------------------------------------------------------
#
# Operator-facing observability for public-port traffic on
# /api/webhooks/* (Slack events, email inbound, future panels).
# Anticipates Phase 2 Features 3 + 5; once the daemon deploys to
# Fly, Joshua needs to see "what's hitting the public port" without
# `flyctl logs`.
#
# v1 stub: 4 representative events per bucket §3 verbatim (verified
# slack message, verified slack url_verification, dead-letter email
# bad signature, slack rate-limited). CC#3 will wire real per-event
# recording via either chain-event emission or a substrate
# webhook_events table — blocked on substrate-team coord ask for
# the dead-letter ledger shape. Until then the stub:true flag keeps
# the FE banner visible.
#
# SECURITY: source_ip values are OCTET-MASKED in the response
# (e.g. "54.203.x.x" not "54.203.99.142") — operator gets
# geolocation hint without full PII exposure. CC#3 will enforce
# the same mask when real data flips in. The §4 test regex-pins
# the mask format so any future drift that emits a full IP gets
# caught at the endpoint layer (3-layer security contract pattern
# from KR-MCP-3 #106: backend shape + TS interface + test regex).


# Mask the last two octets of an IPv4 address to match the
# panel's source_ip contract (e.g. "54.203.99.142" → "54.203.x.x").
# Non-IPv4 strings (IPv6, "-", empty) pass through with a single
# "x.x" suffix replacing the last segment for any dotted form, else
# return "—" — defensive so the FE always sees a stringable value.
def _mask_ipv4_last_two_octets(value: str) -> str:
    if not value or value == "-":
        return "—"
    parts = value.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return f"{parts[0]}.{parts[1]}.x.x"
    # IPv6 / unexpected shape: don't try to mask; surface as "—" so
    # the panel doesn't leak an unmasked form. The mask-format test
    # below pins this defensive shape.
    return "—"


def _endpoint_for_webhook_source(source: str) -> str:
    """Map audit details.source → public webhook route. CC#3's
    emit_audit at webhook_dead_letter.py:139 passes "slack" / "email";
    map back to the routes the FE knows."""
    if source == "slack":
        return "/api/webhooks/slack/events"
    if source == "email":
        return "/api/webhooks/email/inbound"
    return f"/api/webhooks/{source}"  # forward-compat


def _project_webhook_dead_letter(
    entry: "AuditEntry", lineno: int
) -> Dict[str, Any]:
    """Project a webhook.dead_letter AuditEntry to WebhookEvent shape."""
    d = entry.details
    raw_peer_ip = str(d.get("peer_ip", "-"))
    return {
        "id": f"audit-{lineno}",
        "endpoint": _endpoint_for_webhook_source(str(d.get("source", ""))),
        "received_at": entry.emitted_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "dead_letter",  # this seam ONLY produces dead-letters
        "source_ip": _mask_ipv4_last_two_octets(raw_peer_ip),
        "event_type": str(d.get("reason", "")) or None,
        # Subset the audit details to FE-shaped detail. NEVER pass
        # the full audit details through verbatim — that could
        # surface internal fields the FE doesn't expect.
        "details": {
            "reason": str(d.get("reason", "")),
            "header_present": bool(d.get("header_present", False)),
        },
    }


@app.get("/api/webhooks/events/recent")
async def list_recent_webhook_events(limit: int = 50):
    """Return recent public-webhook events for the operator-facing lens.

    Reads ``${KORA_HOME}/kora_audit_log.jsonl`` filtered to
    ``seam=webhook.dead_letter`` (written by
    ``kora_cli/listeners/webhook_dead_letter.py:136-146``) and
    projects each row to the FE's ``WebhookEvent`` shape.

    Limitations until follow-on buckets land:
      * Verified happy-path events are NOT in the audit log
        (audit is attention-events only; verified events emit
        via the existing chain-log seams). Panel shows only
        dead-letters for now.
      * Rate-limited events come from the slowapi middleware
        which doesn't currently call emit_audit. Follow-on bucket
        can wire that.

    SECURITY (3-layer contract carry-forward from PR #109):
      1. source_ip is OCTET-MASKED in the response
         (``_mask_ipv4_last_two_octets``) — the audit writer passes
         the raw peer_ip; THIS endpoint enforces the mask. Backend
         test asserts no full 4-octet IPv4 leaks anywhere.
      2. details is sub-set to FE-shaped fields (reason +
         header_present) — never the raw audit details dict, which
         could carry future fields the panel hasn't vetted.
      3. event_type derived from details.reason (machine code,
         never user content).
    """
    from datetime import datetime, timedelta, timezone
    from kora_cli.audit.jsonl_reader import read_audit_entries

    capped_limit = max(1, min(limit, 200))
    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)

    all_rows = read_audit_entries(seam="webhook.dead_letter")
    projected = [
        _project_webhook_dead_letter(e, lineno=i + 1)
        for i, e in enumerate(all_rows)
    ]

    in_window = [e for e in all_rows if e.emitted_at >= cutoff_24h]

    return {
        "events": projected[:capped_limit],
        "stub": False,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_recent_24h": len(in_window),
    }


# ---------------------------------------------------------------------------
# Agent activity lens (KR-AGENT-ACTIVITY-PANEL) — KR-AUDIT-PANEL-ENDPOINTS flip
# ---------------------------------------------------------------------------
#
# Operator-facing observability for OTHER agents calling Kora via the
# /mcp endpoint (Feature 4). Reads the live ``kora_audit_log.jsonl``
# written by CC#3's KR-AUDIT-JSONL-SINK (PR #139), filtered to
# ``seam=mcp.tool_called`` rows.
#
# K-DG drift caught (spec §2 Flip 1 vs actual emit_audit call site at
# kora_cli/listeners/mcp_tools.py:714-724):
#   * spec said details.duration_ms — NOT in actual writer
#   * spec said details.tool_status — NOT in actual writer
#   * spec said details.result_summary — actual key is `result`
#   * Audit only fires on success path; failed mutating-tool calls
#     don't currently emit_audit. So all logged rows are status=ok.
#     KR-MCP-RUNTIME-SURFACE follow-on (read tools + failure path)
#     will extend the audit writer; until then duration_ms is set
#     to 0 (FE renders as "0 ms" — accurate to the data we have).
#
# SECURITY (3-layer contract carry-forward from PR #114):
#   1. result_summary is a SHORT TEXTUAL summary — projection takes
#      details.result (the writer's docstring confirms this is a
#      short stringified result; full bodies NEVER hit audit).
#      Backend test sweeps for raw-JSON shapes in the field.
#   2. caller_actor_kind is a LABEL — derived from
#      details.caller_actor_kind (writer pre-validates per
#      mcp_callers.yaml). Backend test sweeps for token shapes.
#   3. TS interface (AgentCall) enforces both contracts at compile
#      time. No FE changes for this flip; TS shape matches the
#      projection exactly.


def _project_mcp_tool_called(entry: "AuditEntry", lineno: int) -> Dict[str, Any]:
    """Project a ``mcp.tool_called`` AuditEntry to AgentCall shape."""
    d = entry.details
    return {
        "id": f"audit-{lineno}",
        "tool_name": d.get("tool_name", ""),
        "caller_actor_kind": d.get("caller_actor_kind", ""),
        "called_at": entry.emitted_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        # Not in audit writer today; KR-MCP-RUNTIME-SURFACE follow-on
        # will add. FE renders "0 ms" rather than NaN until then.
        "duration_ms": 0,
        # Audit only fires on success path today; see K-DG note above.
        "status": "ok",
        "result_summary": str(d.get("result", "")),
    }


@app.get("/api/agent-activity/recent")
async def list_recent_agent_activity(limit: int = 50):
    """Return recent agent-driven MCP tool calls for the operator lens.

    Reads ``${KORA_HOME}/kora_audit_log.jsonl`` (written by
    CC#3's KR-AUDIT-JSONL-SINK, PR #139), filters to the
    ``mcp.tool_called`` seam, projects each row to the FE's
    ``AgentCall`` shape, and returns the newest first.

    Query params:
      limit — number of newest entries to return; default 50,
              capped at 200.
    """
    from datetime import datetime, timedelta, timezone
    from kora_cli.audit.jsonl_reader import read_audit_entries

    capped_limit = max(1, min(limit, 200))
    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)

    all_rows = read_audit_entries(seam="mcp.tool_called")
    projected = [
        _project_mcp_tool_called(e, lineno=i + 1)
        for i, e in enumerate(all_rows)
    ]

    in_window = [e for e in all_rows if e.emitted_at >= cutoff_24h]
    by_caller: Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    for e in in_window:
        caller = e.details.get("caller_actor_kind", "unknown")
        by_caller[caller] = by_caller.get(caller, 0) + 1
        # All audit-logged calls are status=ok today (K-DG note above);
        # tally still uses the projection's status so the dict shape
        # matches what real-failure-path data will look like.
        by_status["ok"] = by_status.get("ok", 0) + 1

    return {
        "calls": projected[:capped_limit],
        "stub": False,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_recent_24h": len(in_window),
        "by_caller_24h": by_caller,
    }


# ---------------------------------------------------------------------------
# Slack DM conversation lens (KR-SLACK-DM-PANEL)
# ---------------------------------------------------------------------------
#
# Operator-facing view of Kora ↔ Joshua DM exchanges. Pairs with
# CC#3's KR-FEAT-SLACK-DM bucket (Feature 5 backend) — ST2 will swap
# this stub for a real read of ${HERMES_HOME}/slack_dm_log.jsonl.
#
# v1 stub: 4 representative messages per bucket §2(a) verbatim,
# spanning inbound (received) + outbound (sent_ok) + filtered
# (filtered_non_joshua) so the operator's first look surfaces the
# filtering posture as well as the happy path.
#
# SECURITY (3-layer contract, this iteration's specific risks):
#   1. user_id_label is a LABEL (joshua / kora_bot / unknown_user)
#      — never a raw Slack user ID (which has the shape U[A-Z0-9]{8,}).
#      Backend test pins via regex.
#   2. channel_id is a STUB label (D_STUB1 / D_STUB2). When CC#3
#      flips real data the real channel IDs must be hashed or
#      truncated (PII-adjacent). Backend test pins the stub shape.
#   3. text content is rendered as plain text by the FE — React's
#      default child escaping defangs any HTML/markdown/script in
#      the message body. FE pins via dangerouslySetInnerHTML grep.
#   4. Walk-the-whole-payload guard catching xoxb-/xoxp-/Slack
#      signing-secret token shapes anywhere in the response —
#      backend bug or future log entry that leaks creds gets caught
#      at the API edge, not in the operator's browser.


# KR-SLACK-DM-PANEL-FLIP constants.
_SLACK_DM_LOG_FILENAME = "slack_dm_log.jsonl"
_SLACK_DM_DEFAULT_LIMIT = 50
_SLACK_DM_MAX_LIMIT = 200
# Channel-mask shape: first 4 + last 4 chars; backend exposes the
# field, the small follow-on bucket KR-SLACK-DM-PANEL-CHANNEL-MASK
# handles FE rendering (kept decoupled to keep this PR small).
_CHANNEL_MASK_HEAD = 4
_CHANNEL_MASK_TAIL = 4


def _mask_channel_id(channel_id: str) -> str:
    """First 4 + last 4 chars + literal ellipsis. Short channel IDs
    (≤ head+tail+1) pass through unchanged — masking a shorter string
    would expose more of it via implication than just returning it."""
    if len(channel_id) <= _CHANNEL_MASK_HEAD + _CHANNEL_MASK_TAIL + 1:
        return channel_id
    return f"{channel_id[:_CHANNEL_MASK_HEAD]}…{channel_id[-_CHANNEL_MASK_TAIL:]}"


def _project_slack_dm_entry(
    entry: Dict[str, Any],
    lineno: int,
    expected_joshua: str,
) -> Optional[Dict[str, Any]]:
    """Project a single JSONL entry to the FE's SlackDMMessage shape.

    Returns None when the entry can't be classified as inbound or
    outbound (defensive — malformed entries skip rather than crash).

    Direction discrimination follows the actual writer shape in
    ``kora_cli/handlers/slack_dm_handler.py:302-310`` (inbound — has
    ``received_at``/``handled_status``) vs ``slack_dm_handler.py:775-805``
    (outbound — has ``sent_at``/``send_status``). The bucket spec's
    K-DG block mentioned a ``direction: outbound`` key but the actual
    writer does NOT include one; we discriminate on the keys that DO
    exist.

    SECURITY: ``user_id_label`` is a label only (joshua / kora_bot /
    unknown_user) — never the raw U... Slack user ID. Resolution
    mirrors the handler's own check at ``slack_dm_handler.py:241``
    against ``KORA_SLACK_JOSHUA_USER_ID``.
    """
    channel_id_raw = entry.get("channel_id", "") or ""

    if "received_at" in entry and "handled_status" in entry:
        # Inbound
        user_id = entry.get("user_id") or ""
        if expected_joshua and user_id == expected_joshua:
            label = "joshua"
        else:
            # Non-Joshua inbound: label as unknown_user even if the
            # raw user_id is present in the JSONL; we never echo it.
            label = "unknown_user"
        return {
            "id": f"line-{lineno}",
            "direction": "inbound",
            "timestamp": entry.get("received_at", ""),
            "channel_id": channel_id_raw,
            "channel_id_truncated": _mask_channel_id(channel_id_raw),
            "thread_ts": entry.get("thread_ts"),
            "user_id_label": label,
            "text": entry.get("text", ""),
            "handled_status": entry.get("handled_status", ""),
        }

    if "sent_at" in entry and "send_status" in entry:
        # Outbound — always Kora's bot identity. send_status is
        # "ok" / "failed"; FE expects "sent_ok" / "sent_failed".
        send_status = entry.get("send_status", "")
        return {
            "id": f"line-{lineno}",
            "direction": "outbound",
            "timestamp": entry.get("sent_at", ""),
            "channel_id": channel_id_raw,
            "channel_id_truncated": _mask_channel_id(channel_id_raw),
            "thread_ts": entry.get("thread_ts"),
            "user_id_label": "kora_bot",
            "text": entry.get("text", ""),
            "handled_status": f"sent_{send_status}",
        }

    return None


@app.get("/api/slack-dm/recent")
async def list_recent_slack_dm(limit: int = _SLACK_DM_DEFAULT_LIMIT):
    """Return recent Kora ↔ Joshua DM messages for the operator lens.

    Reads ``${KORA_HOME}/slack_dm_log.jsonl`` (written by
    ``kora_cli/handlers/slack_dm_handler.py`` per PR #119/#122) and
    projects each entry to the FE's ``SlackDMMessage`` shape.

    Query params:
      ``limit`` — number of newest entries to return; default 50,
                  capped at 200 to bound large-file reads.

    Per-message fields (matches FE TS interface from PR #120):
      id                    — derived line-{N} id, stable within file
      direction             — "inbound" | "outbound"
      timestamp             — ISO-8601 (from received_at or sent_at)
      channel_id            — raw channel ID (D... for DM channels)
      channel_id_truncated  — masked form (head…tail) for the
                              upcoming FE channel-mask bucket; FE
                              consumers ignore this field today
      thread_ts             — Slack thread parent ts (or null)
      user_id_label         — LABEL only (joshua / kora_bot /
                              unknown_user); NEVER the raw U... ID
      text                  — message body (FE renders plain text)
      handled_status        — received / filtered_non_joshua /
                              filtered_bot / filtered_subtype /
                              handler_error / dropped_paused
                              (inbound) OR sent_ok / sent_failed
                              (outbound; derived from send_status)

    SECURITY (4-layer contract preserved from PR #120):
      1. user_id_label is a derived LABEL; raw user_id never reaches
         the wire. Tests sweep payload for U[A-Z0-9]{8,} shape.
      2. channel_id starts with "D" (Slack DM channel) — backend
         test asserts. channel_id_truncated companion field lets
         the FE follow-on render a masked view.
      3. text rendered as PLAIN TEXT by FE (React default escape +
         dangerouslySetInnerHTML ban pinned in
         test_web_server_slack_dm.py).
      4. Walk-payload guard for xoxb-/xoxp- Slack token shapes.

    Behaviour:
      * Missing JSONL file → empty list + stub:false (fresh daemon
        with no DMs yet — empty-state UI; no error).
      * Malformed JSONL line → logged + skipped; other entries
        still parsed (defensive against partial-write corruption).
      * Sort: newest first by timestamp descending.
      * stub: false always — this endpoint no longer serves stub
        data, even on empty file (FE's STUB banner stays hidden).
    """
    from datetime import datetime, timedelta, timezone
    import json as _json
    import os as _os

    capped_limit = max(1, min(limit, _SLACK_DM_MAX_LIMIT))
    log_path = get_kora_home() / _SLACK_DM_LOG_FILENAME
    expected_joshua = _os.environ.get(
        "KORA_SLACK_JOSHUA_USER_ID", ""
    ).strip()
    now = datetime.now(timezone.utc)
    generated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    cutoff_24h = now - timedelta(hours=24)

    projected: List[Dict[str, Any]] = []

    if log_path.is_file():
        try:
            with log_path.open("r", encoding="utf-8") as f:
                for lineno, raw_line in enumerate(f, start=1):
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        entry = _json.loads(raw_line)
                    except _json.JSONDecodeError as exc:
                        _log.warning(
                            "[kora.slack_dm_panel] line %d malformed JSON, "
                            "skipped: %r",
                            lineno,
                            exc,
                        )
                        continue
                    if not isinstance(entry, dict):
                        _log.warning(
                            "[kora.slack_dm_panel] line %d not a JSON object, "
                            "skipped",
                            lineno,
                        )
                        continue
                    msg = _project_slack_dm_entry(entry, lineno, expected_joshua)
                    if msg is not None:
                        projected.append(msg)
        except OSError as exc:
            _log.warning(
                "[kora.slack_dm_panel] failed to read %s: %r",
                log_path,
                exc,
            )

    # Newest-first sort. ISO-8601 lex order == chronological order
    # for UTC Z-suffixed timestamps (same shape the writer uses).
    projected.sort(key=lambda m: m.get("timestamp", ""), reverse=True)

    # Aggregate counts use the FULL projected set (not the limited
    # slice) so the headline numbers reflect the whole 24h window.
    def _within_24h(ts_str: str) -> bool:
        if not ts_str:
            return False
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= cutoff_24h

    in_window = [m for m in projected if _within_24h(m["timestamp"])]
    by_direction: Dict[str, int] = {"inbound": 0, "outbound": 0}
    by_status: Dict[str, int] = {}
    for m in in_window:
        by_direction[m["direction"]] = by_direction.get(m["direction"], 0) + 1
        status = m["handled_status"]
        by_status[status] = by_status.get(status, 0) + 1

    return {
        "messages": projected[:capped_limit],
        "stub": False,
        "generated_at": generated_at,
        "total_recent_24h": len(in_window),
        "by_direction_24h": by_direction,
        "by_status_24h": by_status,
    }


# ---------------------------------------------------------------------------
# Email inbox/outbox lens (KR-EMAIL-PANEL)
# ---------------------------------------------------------------------------
#
# Operator-facing view of recent inbound + outbound email exchanges.
# Pairs with CC#1's KR-FEAT-EMAIL bucket (Feature 3 backend) — ST2
# will swap this stub for a real read of
# ${HERMES_HOME}/email_inbound_log.jsonl +
# ${HERMES_HOME}/email_outbound_log.jsonl (Purelymail-backed).
#
# v1 stub: 4 representative messages per bucket §2(a) verbatim,
# spanning inbound (received) + outbound (sent_ok) + filtered
# (filtered_non_allowlist) + inbound-with-attachment so the
# operator's first look surfaces the filtering posture + the
# attachment-count affordance.
#
# 4-layer SECURITY contract (extending the now-standard 3-layer
# pattern with an email-specific token sweep):
#   1. from_label / to_label are LABELS (joshua / kora /
#      unknown_sender) — never raw email addresses. Backend test
#      asserts no `[^\s]+@[^\s]+\.[^\s]+` match anywhere in payload.
#   2. message_id is a label-shaped placeholder in v1. Real
#      Purelymail message IDs are PII-adjacent — CC#1 will
#      hash/truncate when real data flips. Backend test pins the
#      stub-shape.
#   3. body_text_truncated_400 is rendered as PLAIN TEXT by the FE.
#      Real bodies may contain HTML/markdown/scripts — React
#      default escaping defangs them; FE pins via
#      dangerouslySetInnerHTML grep. has_html is metadata only.
#   4. Walk-the-whole-payload guard against Purelymail API token
#      shapes + HMAC-secret-shape (32/64-char hex) + bearer-token
#      shapes — backend bug or future log-entry edit that leaks
#      creds gets caught at the API edge.


# KR-EMAIL-PANEL-FLIP constants (PR #138 inbound writer + #124
# outbound writer feed this endpoint).
_EMAIL_INBOUND_LOG_FILENAME = "email_inbound_log.jsonl"
_EMAIL_OUTBOUND_LOG_FILENAME = "email_outbound_log.jsonl"
_EMAIL_DEFAULT_LIMIT = 50
_EMAIL_MAX_LIMIT = 200
_EMAIL_BODY_TRUNCATE_LIMIT = 400

# The handler's HANDLED_* taxonomy in
# ``kora_cli/handlers/email_inbound_handler.py`` is more granular
# than the FE's ``EmailHandledStatus`` union in
# ``web/src/lib/api.ts``. Map down to the FE-allowed values
# (lossy on purpose — operator-facing status is coarser than the
# internal handler taxonomy; the JSONL itself remains canonical).
_INBOUND_STATUS_TO_FE: Dict[str, str] = {
    "received": "received",
    "filtered_paused": "dropped_paused",
    "filtered_stopped": "dropped_paused",
    "filtered_non_allowlist": "filtered_non_allowlist",
    "filtered_wrong_recipient": "filtered_wrong_recipient",
    # filtered_non_joshua collapses into filtered_non_allowlist for
    # the operator — both are "sender wasn't allowed" from the
    # panel's perspective; the JSONL extra-field carries the
    # finer-grained reason for triage.
    "filtered_non_joshua": "filtered_non_allowlist",
    "handler_error": "handler_error",
}


def _project_email_inbound(
    entry: Dict[str, Any],
    lineno: int,
    expected_joshua_lc: str,
    expected_kora_lc: str,
) -> Optional[Dict[str, Any]]:
    """Project one inbound JSONL entry to the FE's ``EmailMessage`` shape.

    Inbound entry schema is set by
    ``kora_cli/handlers/email_inbound_handler.py`` (KR-FEAT-EMAIL-
    INBOUND-IMAP ST2 / PR #138). Returns ``None`` for entries the
    handler couldn't fully classify (no message_id / no from /
    unknown handled_status).

    SECURITY: raw ``entry['from']`` and ``entry['to']`` ARE email
    addresses — those are NEVER written to the returned dict;
    instead from_label / to_label resolve to "joshua" / "kora" /
    "unknown_sender" / "other" via env comparison.
    """
    handled_raw = entry.get("handled_status")
    if not isinstance(handled_raw, str):
        return None
    fe_status = _INBOUND_STATUS_TO_FE.get(handled_raw)
    if fe_status is None:
        # Unknown handled_status — skip defensively rather than
        # surfacing an enum value the FE doesn't know how to render.
        return None

    sender_raw = entry.get("from") or ""
    sender_lc = sender_raw.strip().lower() if isinstance(sender_raw, str) else ""
    if expected_joshua_lc and sender_lc == expected_joshua_lc:
        from_label = "joshua"
    else:
        from_label = "unknown_sender"

    recipients = entry.get("to") or []
    if not isinstance(recipients, list):
        recipients = []
    recipients_lc = {
        str(r).strip().lower() for r in recipients if isinstance(r, str)
    }
    if expected_kora_lc and expected_kora_lc in recipients_lc:
        to_label = "kora"
    else:
        to_label = "other"

    body_raw = entry.get("body_text_truncated_2k") or ""
    if not isinstance(body_raw, str):
        body_raw = ""
    body_truncated_400 = body_raw[:_EMAIL_BODY_TRUNCATE_LIMIT]

    has_html_raw = entry.get("has_html")
    has_html = bool(has_html_raw) if isinstance(has_html_raw, bool) else False

    attachments_raw = entry.get("attachments_count")
    attachments_count = (
        int(attachments_raw) if isinstance(attachments_raw, int) else 0
    )

    # Semantic flip per bucket §2(a): spoofing_warning is what the
    # FE renders. When the handler skipped the spoofing check
    # (spoofing_check_skipped=True), there's no warning to raise.
    # When/if a future bucket adds real envelope-based detection
    # and finds a mismatch, that entry will have
    # spoofing_check_skipped=False AND a handled_status of
    # filtered_spoofing — which collapses to filtered_non_allowlist
    # in the FE enum, with spoofing_warning=True carrying the signal.
    spoofing_check_skipped = bool(entry.get("spoofing_check_skipped"))
    spoofing_warning = not spoofing_check_skipped

    message_id = entry.get("message_id") or f"inbound-no-id-line-{lineno}"

    return {
        "id": f"inbound-{lineno}",
        "direction": "inbound",
        "timestamp": entry.get("received_at", ""),
        "message_id": message_id,
        "from_label": from_label,
        "to_label": to_label,
        "subject": entry.get("subject", "") or "",
        "body_text_truncated_400": body_truncated_400,
        "has_html": has_html,
        "attachments_count": attachments_count,
        "handled_status": fe_status,
        "spoofing_warning": spoofing_warning,
    }


def _project_email_outbound(
    entry: Dict[str, Any],
    lineno: int,
    expected_joshua_lc: str,
) -> Optional[Dict[str, Any]]:
    """Project one outbound JSONL entry to the FE's ``EmailMessage`` shape.

    Outbound entry schema is set by
    ``kora_cli/clients/purelymail_client.py`` (KR-FEAT-EMAIL ST1
    + KR-MCP-SEND-TOOLS / PRs #124 + #130). Body text is NEVER in
    the outbound JSONL by design — the FE shows a placeholder.

    ``send_status`` in the JSONL is ``"ok"`` / ``"failed"``; the
    FE consumes ``"sent_ok"`` / ``"sent_failed"`` — derive here.
    """
    send_status = entry.get("send_status")
    if send_status not in {"ok", "failed"}:
        return None

    recipients = entry.get("to") or []
    if not isinstance(recipients, list) or not recipients:
        return None
    first_recipient_lc = (
        str(recipients[0]).strip().lower()
        if isinstance(recipients[0], str)
        else ""
    )
    if expected_joshua_lc and first_recipient_lc == expected_joshua_lc:
        to_label = "joshua"
    else:
        to_label = "other"

    message_id = entry.get("message_id") or f"outbound-no-id-line-{lineno}"

    return {
        "id": f"outbound-{lineno}",
        "direction": "outbound",
        "timestamp": entry.get("sent_at", ""),
        "message_id": message_id,
        "from_label": "kora",
        "to_label": to_label,
        "subject": entry.get("subject", "") or "",
        # Body not logged outbound-side per PR #124's security
        # contract (subject + recipients only). FE renders this
        # placeholder; if/when a follow-on bucket adds outbound-
        # body retention, the FE consumes the field unchanged.
        "body_text_truncated_400": (
            "(outbound body not logged for size + privacy)"
        ),
        "has_html": False,
        "attachments_count": 0,
        "handled_status": f"sent_{send_status}",
        "in_reply_to": entry.get("in_reply_to") or None,
    }


def _read_email_jsonl_lines(
    path: Path,
) -> List[Tuple[int, Dict[str, Any]]]:
    """Read + parse a JSONL file. Returns ``[(lineno, entry), ...]``.

    Missing file → empty list (the daemon may not have written to
    one or both files yet). Malformed lines logged + skipped so a
    partial-write corruption doesn't break the whole panel.
    """
    out: List[Tuple[int, Dict[str, Any]]] = []
    if not path.is_file():
        return out
    try:
        with path.open("r", encoding="utf-8") as f:
            for lineno, raw_line in enumerate(f, start=1):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    _log.warning(
                        "[kora.email_panel] %s line %d malformed JSON, "
                        "skipped: %r",
                        path,
                        lineno,
                        exc,
                    )
                    continue
                if not isinstance(entry, dict):
                    _log.warning(
                        "[kora.email_panel] %s line %d not a JSON object, "
                        "skipped",
                        path,
                        lineno,
                    )
                    continue
                out.append((lineno, entry))
    except OSError as exc:
        _log.warning(
            "[kora.email_panel] failed to read %s: %r",
            path,
            exc,
        )
    return out


@app.get("/api/email/recent")
async def list_recent_email(limit: int = _EMAIL_DEFAULT_LIMIT):
    """Return recent email exchanges for the operator lens.

    KR-EMAIL-PANEL-FLIP flips this endpoint from the v1 stub to a
    live read of BOTH email JSONLs:
      * ``${KORA_HOME}/email_inbound_log.jsonl`` (PR #138 writer)
      * ``${KORA_HOME}/email_outbound_log.jsonl`` (PR #124 writer)

    Both files may be missing on a fresh deploy — that's fine,
    the endpoint returns an empty list with ``stub: false``.

    Query params:
      ``limit`` — number of newest entries to return; default 50,
                  capped at 200 to bound large-file reads.

    Per-message fields match ``EmailMessage`` in
    ``web/src/lib/api.ts``. The handler's HANDLED_* taxonomy is
    coarsened to the FE's ``EmailHandledStatus`` union via
    ``_INBOUND_STATUS_TO_FE`` — JSONL stays canonical; the panel
    sees the operator-facing rollup.

    4-layer SECURITY contract (preserved from PR #121 stub):
      1. from_label / to_label are LABELS (joshua / kora /
         unknown_sender / other) — never raw email addresses. The
         walk-payload regex sweep in tests catches drift.
      2. message_id passes through from the JSONL. RFC 5322
         message-ids contain the operator's domain (e.g.,
         ``<id@stormhavenenterprises.com>``) which IS legitimate —
         FE consumers need it for threading. The walk-payload
         email-address guard EXCLUDES the message_id field from
         its sweep to allow this legitimate pattern; everywhere
         else, no raw addresses.
      3. body_text_truncated_400 is plain text (truncated from
         the inbound JSONL's body_text_truncated_2k or the
         outbound placeholder string). FE renders as JSX child;
         dangerouslySetInnerHTML banned in EmailPanel.tsx.
      4. Walk-payload guards for Purelymail-token env-var-name
         hints + HMAC-secret hex shapes + Bearer/Authorization
         header shapes catch any future log-entry edit that leaks
         credential material.

    Behaviour:
      * Either or both JSONLs missing → empty list + stub:false.
      * Malformed JSONL line → logged + skipped; other lines
        still parsed (defensive against partial-write corruption).
      * Unknown handled_status / send_status → entry skipped
        (defensive; keeps the FE enum union clean).
      * Sort: newest first by timestamp descending.
      * stub: false always.
      * Aggregate counts (``total_recent_24h`` /
        ``by_direction_24h`` / ``by_status_24h``) use the FULL
        projected set within the 24h window — NOT the limited
        slice — so dashboard headlines reconcile to the panel
        view.
    """
    from datetime import datetime, timedelta, timezone

    capped_limit = max(1, min(limit, _EMAIL_MAX_LIMIT))

    home = get_kora_home()
    inbound_path = home / _EMAIL_INBOUND_LOG_FILENAME
    outbound_path = home / _EMAIL_OUTBOUND_LOG_FILENAME

    expected_joshua_lc = (
        os.environ.get("KORA_EMAIL_JOSHUA_ADDRESS", "").strip().lower()
    )
    expected_kora_lc = (
        os.environ.get("KORA_EMAIL_KORA_ADDRESS", "").strip().lower()
    )

    now = datetime.now(timezone.utc)
    generated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    cutoff_24h = now - timedelta(hours=24)

    projected: List[Dict[str, Any]] = []
    for lineno, entry in _read_email_jsonl_lines(inbound_path):
        msg = _project_email_inbound(
            entry,
            lineno,
            expected_joshua_lc=expected_joshua_lc,
            expected_kora_lc=expected_kora_lc,
        )
        if msg is not None:
            projected.append(msg)
    for lineno, entry in _read_email_jsonl_lines(outbound_path):
        msg = _project_email_outbound(
            entry,
            lineno,
            expected_joshua_lc=expected_joshua_lc,
        )
        if msg is not None:
            projected.append(msg)

    # Newest-first sort. JSONL timestamps are ISO-8601 UTC — lex
    # order == chronological order for matching formats.
    projected.sort(key=lambda m: m.get("timestamp", ""), reverse=True)

    def _within_24h(ts_str: str) -> bool:
        if not ts_str:
            return False
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= cutoff_24h

    in_window = [m for m in projected if _within_24h(m["timestamp"])]
    by_direction: Dict[str, int] = {"inbound": 0, "outbound": 0}
    by_status: Dict[str, int] = {}
    for m in in_window:
        by_direction[m["direction"]] = by_direction.get(m["direction"], 0) + 1
        status = m["handled_status"]
        by_status[status] = by_status.get(status, 0) + 1

    return {
        "messages": projected[:capped_limit],
        "stub": False,
        "generated_at": generated_at,
        "total_recent_24h": len(in_window),
        "by_direction_24h": by_direction,
        "by_status_24h": by_status,
    }


# ---------------------------------------------------------------------------
# Kora reasoning activity lens (KR-REASONING-PANEL)
# ---------------------------------------------------------------------------
#
# Operator-facing view of Kora's recent ReasoningEngine calls.
# Pairs with CC#3's KR-FEAT-AI-RESPONSE-LOOP ST2 (in flight) —
# ST2 extends the slack_dm_log.jsonl outbound entries with the
# model_used / input_tokens / output_tokens / reasoning_duration_ms
# / reasoning_error fields, and a small follow-on bucket reads
# those into this endpoint.
#
# v1 stub: 4 representative calls per bucket §3(a), spanning
#   * ok @ NORMAL on opus      — happy path
#   * ok @ WARN_75 on sonnet   — cost-downshift in action
#   * halted @ HARD_STOP_100   — budget-locked refusal
#   * failed sdk_timeout       — transport failure
#
# K-DG drift caught (bucket spec used uppercase enum NAMES but
# the wire format is the lowercase Enum VALUES per
# ``agent/cost_state_holder.py:114-117``: NORMAL = "normal" etc):
# stub uses the lowercase ``.value`` strings to match what CC#3
# real data will emit. cost_rung_at_call is a literal value
# string; the FE pill-color map keys on these.
#
# 4-layer SECURITY contract (extending the established pattern
# with reasoning-specific guards):
#   1. response_text_truncated_200 rendered as PLAIN TEXT by the
#      FE — React's default child escaping defangs any HTML /
#      markdown / script in Kora's generated text. FE pins via
#      dangerouslySetInnerHTML grep.
#   2. NO Anthropic-key shapes anywhere in payload — walk-payload
#      regex sweeps for ``sk-ant-`` prefix + base64-like 32+ char
#      runs. Catches a future log-entry edit or error-projection
#      bug that leaks credential material into the operator view.
#   3. NO PII from message context: response_text_truncated_200
#      must never contain the inbound user's identifying patterns
#      (email regex / Slack user-ID regex). Backend test sweeps.
#   4. TS interface declares all fields with documented contracts;
#      no ``raw_prompt`` / ``auth_token`` companion fields exist.


# Audit-derived reasoning panel — KR-AUDIT-PANEL-ENDPOINTS flip.
#
# Reads ``kora_audit_log.jsonl`` rows where ``seam ==
# "reasoning.tool_called"`` (written by
# ``kora_cli/reasoning/anthropic_engine.py:921-938``) and GROUPS
# them by ``caller_session_id`` so a multi-tool reasoning iteration
# collapses into one ReasoningCall row.
#
# K-DG: the audit writer details only includes tool_name +
# triggered_by + tool_duration_ms + tool_status (+ optional
# exc_type). It does NOT have model_used / tokens / response_text —
# those live in ``slack_dm_log.jsonl`` outbound entries. For this
# flip, those fields are null; the cross-reference happens in the
# follow-on bucket KR-REASONING-PANEL-MODEL-XREF.
#
# Status derivation: ok if every grouped tool has tool_status==ok;
# otherwise the dominant non-ok status (capability_denied →
# halted; execution_error → handler_error).
#
# KR-REASONING-PANEL-MODEL-XREF (this file's update): model_used /
# input_tokens / output_tokens / cost_rung_at_call /
# response_text_truncated_200 are populated by cross-referencing
# kora_audit_log.jsonl reasoning rows with slack_dm_log.jsonl
# outbound entries via the channel_id + thread_ts/timestamp join
# in ``kora_cli/audit/reasoning_xref.py``. Graceful degradation:
# when no slack_dm match is found, those fields remain null (same
# behavior as the pre-xref projection from PR #141).


@app.get("/api/reasoning/recent")
async def list_recent_reasoning(limit: int = 50):
    """Return recent Kora ReasoningEngine calls for the operator lens.

    Reads ``${KORA_HOME}/kora_audit_log.jsonl`` filtered to
    ``seam=reasoning.tool_called``, groups consecutive tool calls
    by ``caller_session_id``, and cross-references each group with
    ``slack_dm_log.jsonl`` outbound entries (via
    ``kora_cli/audit/reasoning_xref.py``) to populate
    ``model_used`` / ``input_tokens`` / ``output_tokens`` /
    ``cost_rung_at_call`` / ``response_text_truncated_200``.

    Graceful degradation: when the xref lookup fails for a group
    (slack_dm entry missing or outside the ±60s correlation
    window), those fields render as null. Same shape as the
    pre-xref behavior from PR #141, so the FE handles both.

    Aggregates (total_recent_24h, by_status_24h, by_model_24h,
    tokens_total_24h) operate on INDIVIDUAL audit rows + xref'd
    outbound entries — NOT groups — so headline counts reflect
    activity volume.
    """
    from datetime import datetime, timedelta, timezone
    from kora_cli.audit.jsonl_reader import read_audit_entries
    from kora_cli.audit.reasoning_xref import (
        load_reasoning_calls_with_xref,
    )

    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)

    projected, raw_in_window_count = load_reasoning_calls_with_xref(
        limit=limit,
    )

    # Per-status aggregate (individual rows). Re-read audit since
    # the xref helper returns groups; we count INDIVIDUAL rows
    # within the 24h window for the headline (per PR #141 rationale).
    audit_rows = read_audit_entries(seam="reasoning.tool_called")
    by_status: Dict[str, int] = {"ok": 0, "failed": 0, "halted": 0}
    for e in audit_rows:
        if e.emitted_at < cutoff_24h:
            continue
        st = e.details.get("tool_status", "ok")
        if st == "ok":
            by_status["ok"] += 1
        elif st == "not_allowed":
            by_status["halted"] += 1
        else:
            by_status["failed"] += 1

    # Model + token aggregates from the xref'd groups within window.
    # Falls back to empty / zero if no xref enrichment happened.
    by_model: Dict[str, int] = {}
    tokens_total = {"input": 0, "output": 0}
    for call in projected:
        # Per-group emitted_at corresponds to started_at; only count
        # those within the 24h window.
        ts_str = call.get("started_at", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            continue
        if ts < cutoff_24h:
            continue
        model = call.get("model_used")
        if model:
            by_model[model] = by_model.get(model, 0) + 1
        elif call.get("status") == "halted":
            by_model["halted_no_model"] = by_model.get("halted_no_model", 0) + 1
        tokens_total["input"] += int(call.get("input_tokens") or 0)
        tokens_total["output"] += int(call.get("output_tokens") or 0)

    return {
        # projected already capped by the helper per its limit arg.
        "calls": projected,
        "stub": False,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_recent_24h": raw_in_window_count,
        "by_model_24h": by_model,
        "by_status_24h": by_status,
        "tokens_total_24h": tokens_total,
    }


# ---------------------------------------------------------------------------
# Unified operator-attention lens (KR-ALERTS-PANEL)
# ---------------------------------------------------------------------------
#
# Aggregates "needs operator attention" signals from across the 12
# existing panels into one place — operator scans ONE banner instead
# of 12 destructive-tone cards. Each panel already has its own
# headline-destructive trigger; this endpoint will (when real-data
# flips) collect those triggers from a central alert-collector.
#
# v1 stub: 4 representative alerts per bucket §1(a) verbatim,
# spanning all three severity tiers (critical / warning / info) and
# four distinct categories so the operator's first look exercises
# the severity sort + category icon mapping + click-through nav.
#
# Real alert generation is DEFERRED: needs source panels to expose
# their alert state to a central collector (separate backend bucket).
# Same stub-then-real pattern as HB-PANEL / MCP-3 / WEBHOOK-EVENTS /
# AGENT-ACTIVITY / SLACK-DM / EMAIL / REASONING.
#
# 3-layer SECURITY contract (same shape as prior panels):
#   1. ``title`` + ``detail`` rendered as PLAIN TEXT by the FE —
#      React's default child escaping defangs HTML/markdown/script.
#      FE pins via dangerouslySetInnerHTML grep. Real alert text
#      may eventually quote source-panel state which could in
#      theory contain user content.
#   2. NO PII / secret patterns: walk-payload regex catches
#      Anthropic key shapes, Slack token shapes, email addresses,
#      raw Slack user IDs. Defense-in-depth even though alert
#      strings are operator-authored at the source-panel level.
#   3. TS interface declares typed severity + category enums; no
#      ``raw_payload`` / ``user_message`` companion fields exist
#      on the Alert type.


@app.get("/api/alerts/current")
async def list_current_alerts():
    """Return currently-active operator-attention alerts.

    KR-ALERTS-PANEL-FLIP swaps the v1 stub (PR #134) for a real
    aggregator that pulls from 5 sources: OperationalStateHolder +
    cost-ladder holder + HealthRollup + audit JSONL +
    heartbeat-probe snapshots. See
    ``kora_cli/alerts/aggregator.py`` for the rule taxonomy +
    fail-soft contract.

    Per-alert fields (matches FE ``Alert`` in ``web/src/lib/api.ts``):
      id, severity, category, title, detail, source_panel,
      source_panel_route, first_seen_at.

    Behaviour:
      * Any per-source failure (holder uninitialized, JSONL
        unreadable, probe import error, etc.) is caught inside
        the aggregator + that source's rules drop silently; other
        rules still emit. Operator NEVER sees a 500.
      * stub: false always — the endpoint reads live state even
        when no alerts are active (empty list, stub:false).
      * Sort: severity rank (critical → warning → info), then by
        alert id for stable intra-tier ordering.
    """
    from kora_cli.alerts import compute_active_alerts

    alerts = compute_active_alerts()
    by_severity: Dict[str, int] = {"critical": 0, "warning": 0, "info": 0}
    for a in alerts:
        by_severity[a.severity] = by_severity.get(a.severity, 0) + 1

    from datetime import datetime, timezone

    return {
        "alerts": [a.to_dict() for a in alerts],
        "stub": False,
        "generated_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "total_active": len(alerts),
        "by_severity": by_severity,
    }


# ---------------------------------------------------------------------------
# Pre-warmed daemon snapshot (KR-CHEAP-PRE-WARMED-SNAPSHOT)
# ---------------------------------------------------------------------------
#
# Surfaces the snapshot file written every 5 min by the snapshot
# listener's periodic task. Reading the snapshot is $0 LLM cost
# AND $0 holder-read cost — the projection has already been done.
# Cockpit + reasoning-engine routing layer (separate bucket) can
# consume this for status queries without paying per-source fan-out.


@app.get("/api/snapshot")
async def get_daemon_snapshot():
    """Return the most-recent fresh daemon snapshot.

    Behavior:
      * Fresh snapshot on disk (≤10 min old) → returns the snapshot
        dict verbatim (schema_version + computed_at + the per-
        source sections).
      * No snapshot OR snapshot stale → returns
        ``{"error": "no_snapshot", "stale": true}`` so consumers
        can branch on presence without crashing.

    The snapshot itself is fail-soft per
    :mod:`kora_cli.snapshot.state_snapshot` — missing accessors
    degrade individual fields to ``"unknown"`` rather than failing
    the whole snapshot.
    """
    from kora_cli.snapshot import read_snapshot

    snap = read_snapshot()
    if snap is None:
        return {"error": "no_snapshot", "stale": True}
    return snap

# Panel-view instrumentation sink (KR-PANEL-USE-INSTRUMENTATION)
# ---------------------------------------------------------------------------
#
# Per Council R3 lock + sub-cut (c): records which top-level pages /
# panels the operator opens. Over time the JSONL accretes usage data
# that informs any future panel-design decisions — no shape changes
# happen blind.
#
# Path B chosen (PM confirmed): separate ``${KORA_HOME}/panel_views.jsonl``
# file rather than extending the audit log's SeamName Literal. The
# audit log is a forensic/compliance surface (Pydantic ``extra="forbid"``
# + tight SeamName Literal kept intentionally narrow); panel_views are
# operator-UX telemetry with a different lifecycle, different
# consumers, and likely different retention semantics. Mixing them
# would muddle both contracts (e.g., a future
# ``read_audit_entries(seam=None)`` query would surface panel-views
# unexpectedly — a latent contract violation).
#
# Write discipline mirrors ``kora_cli/audit/jsonl_sink.py``:
#   * Best-effort: OSError → WARN-log + return (never crash the
#     frontend; instrumentation must never break operator UX)
#   * mkdir(parents=True, exist_ok=True) before append (KORA_HOME
#     may not exist on fresh installs)
#   * Atomic single-line append per request
#
# Reader is out-of-scope for this bucket — we just write; consumers
# come later when we have data to act on.

PANEL_VIEWS_LOG_FILENAME = "panel_views.jsonl"
_PANEL_NAME_MAX = 128
_SESSION_ID_MAX = 64


def _resolve_panel_views_log_path() -> Path:
    """Resolve to ``<KORA_HOME>/panel_views.jsonl``. Re-resolves on
    every call so monkeypatch in tests works without ContextVar
    plumbing (per the #137 fixture-isolation lesson)."""
    return get_kora_home() / PANEL_VIEWS_LOG_FILENAME


@app.post("/api/panel_view")
async def emit_panel_view(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Operator-UX telemetry sink: record a single panel view.

    Fire-and-forget from the FE ``usePanelView`` hook (zero
    operator-visible latency target). Validation:

      * ``panel_name`` — required non-empty string, truncated to
        ``_PANEL_NAME_MAX`` chars. Empty → 400 since an unattributed
        view event has no analytical value.
      * ``session_id`` — optional, truncated to ``_SESSION_ID_MAX``
        chars. Missing → recorded as ``"unknown"`` so cold-tab
        emits still produce countable rows.

    Returns ``{"ok": True}`` on accepted writes. Returns ``{"ok": True,
    "warning": "write_failed"}`` on JSONL append failure so the FE's
    fire-and-forget POST doesn't surface an error and confuse the
    operator (instrumentation MUST NOT break UX).
    """
    panel_name_raw = payload.get("panel_name", "")
    panel_name = (
        str(panel_name_raw).strip()[:_PANEL_NAME_MAX]
        if panel_name_raw is not None
        else ""
    )
    if not panel_name:
        # 400 here is intentional — empty panel_name is a FE bug, not
        # a transient runtime condition. Surfaces in dev quickly.
        raise HTTPException(status_code=400, detail="panel_name required")

    session_id_raw = payload.get("session_id")
    if not session_id_raw:
        session_id = "unknown"
    else:
        session_id = str(session_id_raw).strip()[:_SESSION_ID_MAX] or "unknown"

    from datetime import datetime, timezone

    entry = {
        "kind": "panel_view",
        "panel_name": panel_name,
        "session_id": session_id,
        "emitted_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }

    log_path = _resolve_panel_views_log_path()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        _log.warning(
            "[kora.panel_view] write failed (%s): %r — FE caller "
            "swallows; instrumentation must never break UX",
            log_path,
            exc,
        )
        return {"ok": True, "warning": "write_failed"}

    return {"ok": True}


# ---------------------------------------------------------------------------
# Per-route cost telemetry (KR-CHEAP-COST-TELEMETRY)
# ---------------------------------------------------------------------------


@app.get("/api/cost_telemetry")
async def get_cost_telemetry():
    """Per-route cost counters across all 3 windows.

    Source-of-truth for any Kora-cost decisions (escalation rate,
    route shape, classifier tuning, etc.). Reads the in-memory
    telemetry singleton directly — no disk roundtrip, no LLM
    cost.

    Shape:

    .. code-block:: json

        {
          "process_lifetime": {"slack_dm": {...}, "unknown": {...}, ...},
          "rolling_24h":      {"slack_dm": {...}, ...},
          "monthly":          {"slack_dm": {...}, ...}
        }

    Per-route counter shape comes from
    :class:`kora_cli.telemetry.cost_telemetry._RouteCounters.to_dict`.
    """
    from kora_cli.telemetry import get_telemetry

    return get_telemetry().snapshot()


# ---------------------------------------------------------------------------
# DM phrasebook viewer + tester (KR-FE-PHRASEBOOK-VIEWER)
# ---------------------------------------------------------------------------
#
# Read-only v1 — operator can SEE the phrasebook + test patterns
# interactively. Edit/write path is a follow-on bucket
# (KR-FE-PHRASEBOOK-EDITOR + KR-API-PHRASEBOOK-CRUD).
#
# Why surface this in the cockpit at all:
#   * Phrasebook + snapshot interpolation is the cheap-substrate
#     thesis applied to DM handling — operator should be able to
#     audit which patterns short-circuit AND test which would
#     fall through to the reasoning engine given the current
#     snapshot (e.g., "cost_ladder.model_default is unknown
#     today so the burn-query entry will fall through")
#   * Same row shape will be reused by the eventual promotion-
#     loop review panel (proposals add phrasebook entries)
#
# Pattern: ALL reads go through dm_phrasebook.load_phrasebook()
# which honors the operator override at
# ${KORA_HOME}/phrasebook/slack_dm.yml — same source-of-truth as
# the live DM handler, so the cockpit can't drift from runtime.

# Snapshot-placeholder regex (mirrors dm_phrasebook.py:252) so the
# extracted-fields list matches what render_reply will actually
# walk at runtime. Kept in sync via the test-pin in
# tests/kora_cli/test_phrasebook_endpoints.py.
import re as _phrasebook_re

_PHRASEBOOK_PLACEHOLDER_RE = _phrasebook_re.compile(
    r"\{snapshot\.([a-zA-Z0-9_.]+)\}"
)


def _extract_phrasebook_snapshot_refs(template: str) -> list:
    """Return the sorted, deduped list of snapshot field paths the
    template references via ``{snapshot.X.Y.Z}`` placeholders."""
    return sorted(set(_PHRASEBOOK_PLACEHOLDER_RE.findall(template or "")))


def _phrasebook_override_path_or_none():
    """Public-ish accessor for the override path WITHOUT requiring
    the file to exist (the private helper inside dm_phrasebook
    returns None when the file is absent; for the viewer we want
    the candidate path even when it doesn't exist yet — operator
    needs to know where to create it)."""
    try:
        from kora_constants import get_kora_home

        return get_kora_home() / "phrasebook" / "slack_dm.yml"
    except Exception:
        return None


@app.get("/api/phrasebook/slack_dm")
async def get_slack_dm_phrasebook() -> Dict[str, Any]:
    """Read-only view of the current Slack DM phrasebook.

    Returns the same entries the live handler would match against
    (via dm_phrasebook.load_phrasebook with no override path —
    honors ``${KORA_HOME}/phrasebook/slack_dm.yml`` when present;
    falls back to bundled default otherwise).

    Each entry's ``referenced_snapshot_fields`` is the list of
    snapshot paths the reply_template references — operator can
    see at a glance which fields each entry depends on.
    """
    from kora_cli.short_circuit import dm_phrasebook

    entries = dm_phrasebook.load_phrasebook()
    override_candidate = _phrasebook_override_path_or_none()
    override_exists = (
        override_candidate is not None and override_candidate.is_file()
    )
    return {
        "source": "override" if override_exists else "bundled_default",
        "source_path": str(override_candidate) if override_exists else "bundled",
        # Echoes the candidate path even when absent so operator
        # knows where to drop the YAML to start overriding.
        "override_candidate_path": str(override_candidate)
        if override_candidate is not None
        else None,
        "entries": [
            {
                "pattern": entry.pattern.pattern,
                "category": entry.category,
                "description": entry.description,
                "reply_template": entry.reply_template,
                "referenced_snapshot_fields": (
                    _extract_phrasebook_snapshot_refs(entry.reply_template)
                ),
            }
            for entry in entries
        ],
    }


@app.post("/api/phrasebook/slack_dm/test")
async def test_phrasebook_match(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Operator-supplied test text → matched entry + rendered reply.

    Read-only. Does NOT call the reasoning engine, does NOT send
    DMs, does NOT mutate any state. Pure preview of what the live
    handler would do RIGHT NOW for the given text.

    Result shape mirrors the operator's mental model:
      * matched=False → would fall through to reasoning (no entry
        matched)
      * matched=True + rendered_reply present → short-circuit reply
        (handler would skip the reasoning engine entirely)
      * matched=True + rendered_reply null → matched but the
        snapshot is stale / has degraded fields → would still
        fall through to reasoning

    The ``would_fall_through_to_reasoning_engine`` boolean is the
    single answer the operator usually wants ("does this text
    cost me $0 or cents right now?").
    """
    from kora_cli.short_circuit import dm_phrasebook
    from kora_cli.snapshot import read_snapshot

    test_text = str(payload.get("text", ""))[:1024]
    entries = dm_phrasebook.load_phrasebook()
    matched = dm_phrasebook.match_message(test_text, entries)

    if matched is None:
        return {
            "matched": False,
            "would_fall_through_to_reasoning_engine": True,
        }

    snap = read_snapshot()
    rendered = dm_phrasebook.render_reply(matched, snap)

    return {
        "matched": True,
        "category": matched.category,
        "description": matched.description,
        "pattern": matched.pattern.pattern,
        "reply_template": matched.reply_template,
        "referenced_snapshot_fields": _extract_phrasebook_snapshot_refs(
            matched.reply_template
        ),
        "rendered_reply": rendered,
        "would_fall_through_to_reasoning_engine": rendered is None,
        "snapshot_present": snap is not None,
    }


# ---------------------------------------------------------------------------
# DM phrasebook write path — KR-FE-PHRASEBOOK-EDITOR-AND-CRUD
# ---------------------------------------------------------------------------
#
# Read endpoints are above (PR #167). This block adds:
#
#   * PUT  /api/phrasebook/slack_dm           — replace entries
#   * POST /api/phrasebook/slack_dm/revert    — revert to backup
#   * GET  /api/phrasebook/slack_dm/backups   — list backups
#
# Validation, atomic write, backup rotation, and the static
# snapshot-schema allow-list live in
# kora_cli/short_circuit/phrasebook_editor.py — this block is
# request-handling + audit emission only.
#
# Audit: each successful PUT (or revert) emits seam=phrasebook.updated
# with entry_count_before / entry_count_after / backup_filename /
# actor="operator". Audit-panel consumers (KR-AUDIT-PANEL-ENDPOINTS
# / future KR-PROMOTION-REVIEW-PANEL) join on this seam to
# reconstruct edit history.


def _phrasebook_count_current_entries() -> int:
    """Best-effort count of currently-live entries (for the
    entry_count_before audit field). Returns 0 if load fails so a
    failure here doesn't block the write."""
    try:
        from kora_cli.short_circuit import dm_phrasebook

        return len(dm_phrasebook.load_phrasebook())
    except Exception:
        return 0


@app.put("/api/phrasebook/slack_dm")
async def put_phrasebook(payload: Dict[str, Any]) -> Any:
    """Replace the entire operator-override phrasebook.

    Atomic-semantic: if ANY entry fails validation, the whole
    payload is refused (422 with per-entry errors); the existing
    override is preserved. Successful writes go through
    backup-then-write-atomic so an in-flight crash can't leave
    the override half-written or back-up-less.

    Payload shape:
      {"entries": [
          {"pattern": "...", "category": "...",
           "description": "...", "reply_template": "..."}, ...
      ]}

    On success returns the new entries (echoed verbatim so the
    cockpit can refresh from the response) + the backup
    filename written (if any) + the source path.
    """
    from kora_cli.audit import emit_audit
    from kora_cli.short_circuit import phrasebook_editor

    entries = payload.get("entries") if isinstance(payload, dict) else None
    errors = phrasebook_editor.validate_entries(entries)
    if errors:
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_failed",
                "errors": [e.as_dict() for e in errors],
            },
        )

    typed_entries: List[Dict[str, Any]] = entries  # type: ignore[assignment]
    count_before = _phrasebook_count_current_entries()

    override_path = phrasebook_editor._override_path()
    backup_path = phrasebook_editor.write_backup_for(override_path)

    try:
        phrasebook_editor.write_phrasebook(typed_entries)
    except Exception as exc:
        logger.warning(
            "[kora.phrasebook] write_phrasebook raised %r — backup "
            "preserved at %s, override unchanged",
            exc,
            backup_path,
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": "write_failed",
                "detail": repr(exc),
                "backup_filename": (
                    backup_path.name if backup_path is not None else None
                ),
            },
        )

    keep = phrasebook_editor._backup_keep_count()
    try:
        rotated = phrasebook_editor.rotate_backups(keep)
    except Exception as exc:
        logger.warning(
            "[kora.phrasebook] backup rotation raised %r — write "
            "succeeded; rotation will catch up next write",
            exc,
        )
        rotated = []

    try:
        emit_audit(
            seam="phrasebook.updated",
            details={
                "actor": "operator",
                "action": "put",
                "entry_count_before": count_before,
                "entry_count_after": len(typed_entries),
                "backup_filename": (
                    backup_path.name if backup_path is not None else None
                ),
                "rotated_backup_count": len(rotated),
            },
            source=None,
        )
    except Exception as exc:
        logger.warning(
            "[kora.phrasebook] audit emit_audit raised %r — write "
            "still succeeded",
            exc,
        )

    return {
        "source_path": str(override_path),
        "entry_count": len(typed_entries),
        "backup_filename": (
            backup_path.name if backup_path is not None else None
        ),
        "rotated_backup_count": len(rotated),
        "entries": [
            {
                "pattern": e["pattern"],
                "category": e["category"],
                "description": e["description"],
                "reply_template": e["reply_template"],
                "referenced_snapshot_fields": (
                    _extract_phrasebook_snapshot_refs(e["reply_template"])
                ),
            }
            for e in typed_entries
        ],
    }


@app.post("/api/phrasebook/slack_dm/revert")
async def revert_phrasebook_endpoint(
    payload: Optional[Dict[str, Any]] = None,
) -> Any:
    """Revert the override to a specific backup OR (when no
    filename supplied) the most-recent backup OR (when no backups
    exist) remove the override entirely.

    Payload (all optional): ``{"filename": "slack_dm.YYYY-...Z.yml"}``

    Defense against path traversal lives in
    phrasebook_editor.revert_phrasebook — filename must match the
    slack_dm.*.yml shape with no path separators.
    """
    from kora_cli.audit import emit_audit
    from kora_cli.short_circuit import phrasebook_editor

    filename: Optional[str] = None
    if isinstance(payload, dict):
        raw = payload.get("filename")
        if isinstance(raw, str) and raw:
            filename = raw

    count_before = _phrasebook_count_current_entries()

    try:
        result = phrasebook_editor.revert_phrasebook(filename=filename)
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_filename", "detail": str(exc)},
        )
    except FileNotFoundError as exc:
        return JSONResponse(
            status_code=404,
            content={"error": "backup_not_found", "detail": str(exc)},
        )
    except Exception as exc:
        logger.warning("[kora.phrasebook] revert raised %r", exc)
        return JSONResponse(
            status_code=500,
            content={"error": "revert_failed", "detail": repr(exc)},
        )

    count_after = _phrasebook_count_current_entries()

    try:
        emit_audit(
            seam="phrasebook.updated",
            details={
                "actor": "operator",
                "action": "revert",
                "entry_count_before": count_before,
                "entry_count_after": count_after,
                "reverted_to": result.get("reverted_to"),
            },
            source=None,
        )
    except Exception as exc:
        logger.warning(
            "[kora.phrasebook] revert audit emit_audit raised %r", exc
        )

    return result


@app.get("/api/phrasebook/slack_dm/backups")
async def get_phrasebook_backups() -> Dict[str, Any]:
    """Newest-first list of available backups, for the cockpit
    revert dropdown. Each entry: filename / timestamp /
    size_bytes / entry_count (None when the backup can't be
    parsed, so the cockpit can grey out corrupt backups instead
    of pretending they're valid revert targets)."""
    from kora_cli.short_circuit import phrasebook_editor

    return {
        "backups": phrasebook_editor.list_backups(),
        "rotation_keep": phrasebook_editor._backup_keep_count(),
    }


# ---------------------------------------------------------------------------
# Phrasebook promotion review — KR-PROMOTE-PHRASEBOOK-FOUNDATION (Deliverable E)
# ---------------------------------------------------------------------------
#
# Three endpoints driving the operator-approval UX. CC#2's
# KR-FE-PROMOTION-REVIEW-PANEL follow-on reads/writes these.
#
#   * GET   /api/promotions/phrasebook/pending           — list pending
#   * POST  /api/promotions/phrasebook/{id}/approve      — approve + PUT phrasebook
#   * POST  /api/promotions/phrasebook/{id}/reject       — reject
#
# Drift-guard pin: ``_PROMOTION_STATUS_VALUES`` mirrors the proposer
# module's ``PROPOSAL_STATUS_VALUES``. The KR-FE-PROMOTION-REVIEW-PANEL
# follow-on adds the symmetric FE constant + a snapshot-pin test that
# fails CI if the two drift.


# Wire-stable status allowlist — paired with the FE constant added
# by KR-FE-PROMOTION-REVIEW-PANEL.
_PROMOTION_STATUS_VALUES: Tuple[str, ...] = (
    "pending",
    "approved",
    "rejected",
    "expired",
)


@app.get("/api/promotions/phrasebook/pending")
async def list_pending_phrasebook_proposals() -> Dict[str, Any]:
    """Return all pending phrasebook proposals, highest-confidence
    first. Each entry is the full PromotionProposal projection
    (see ``kora_cli.promote.phrasebook.proposer.proposal_to_dict``)
    so the cockpit panel has everything it needs to render the
    review surface in one round-trip.

    Sidebar-nav count is ``len(response["proposals"])``.
    """
    from kora_cli.promote.phrasebook.proposer import proposal_to_dict
    from kora_cli.promote.phrasebook.store import list_pending

    proposals = list_pending()
    return {
        "proposals": [proposal_to_dict(p) for p in proposals],
        "status_values": list(_PROMOTION_STATUS_VALUES),
    }


@app.post("/api/promotions/phrasebook/{proposal_id}/approve")
async def approve_phrasebook_proposal(
    proposal_id: str, payload: Optional[Dict[str, Any]] = None
) -> Any:
    """Approve a pending proposal. Optional payload override
    fields the operator edited at approve-time:

      {pattern_override?, reply_template_override?,
       category_override?, review_notes?}

    Workflow:
      1. Load the pending proposal (404 if missing / not
         pending).
      2. Build the post-override PhrasebookEntry shape.
      3. Validate via the existing phrasebook editor's validator
         (regex compiles, template references real fields, etc).
      4. PUT to the operator-override phrasebook with
         ``actor="kora_proposal_approved"`` per #177
         forward-compat.
      5. Transition the proposal to ``approved/`` directory.
      6. Emit ``promotion.approved`` audit row.
    """
    from kora_cli.audit import emit_audit
    from kora_cli.promote.phrasebook.proposer import proposal_to_dict
    from kora_cli.promote.phrasebook.store import (
        ProposalNotFound,
        load,
        transition,
    )
    from kora_cli.short_circuit import dm_phrasebook, phrasebook_editor

    overrides_dict: Dict[str, Any] = {}
    review_notes = ""
    if isinstance(payload, dict):
        pattern_override = payload.get("pattern_override")
        if isinstance(pattern_override, str):
            overrides_dict["pattern"] = pattern_override
        reply_template_override = payload.get("reply_template_override")
        if isinstance(reply_template_override, str):
            overrides_dict["reply_template"] = reply_template_override
        category_override = payload.get("category_override")
        if isinstance(category_override, str):
            overrides_dict["category"] = category_override
        notes_raw = payload.get("review_notes")
        if isinstance(notes_raw, str):
            review_notes = notes_raw

    try:
        existing = load(proposal_id)
    except ProposalNotFound:
        return JSONResponse(
            status_code=404,
            content={"error": "proposal_not_found", "proposal_id": proposal_id},
        )
    if existing.status != "pending":
        return JSONResponse(
            status_code=409,
            content={
                "error": "proposal_not_pending",
                "proposal_id": proposal_id,
                "current_status": existing.status,
            },
        )

    final_pattern = overrides_dict.get("pattern") or existing.proposed_pattern
    final_reply_template = (
        overrides_dict.get("reply_template")
        or existing.proposed_reply_template
    )
    final_category = (
        overrides_dict.get("category") or existing.proposed_category
    )

    new_entry = {
        "pattern": final_pattern,
        "category": final_category,
        "description": (
            f"Promoted from Kora proposal {proposal_id} "
            f"(cluster size {existing.cluster_size}, "
            f"confidence {existing.confidence:.2f})"
        ),
        "reply_template": final_reply_template,
    }

    # Merge into existing override entries — promotion ADDS, never
    # replaces. Operator edits the result later via the PUT
    # endpoint if they want different ordering.
    current_entries: List[Dict[str, Any]] = []
    for entry in dm_phrasebook.load_phrasebook():
        current_entries.append(
            {
                "pattern": entry.pattern.pattern,
                "category": entry.category,
                "description": entry.description,
                "reply_template": entry.reply_template,
            }
        )
    proposed_entries = current_entries + [new_entry]
    validation_errors = phrasebook_editor.validate_entries(
        proposed_entries
    )
    if validation_errors:
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_failed",
                "errors": [e.as_dict() for e in validation_errors],
                "proposal_id": proposal_id,
            },
        )

    count_before = len(current_entries)
    override_path = phrasebook_editor._override_path()
    backup_path = phrasebook_editor.write_backup_for(override_path)
    try:
        phrasebook_editor.write_phrasebook(proposed_entries)
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={
                "error": "phrasebook_write_failed",
                "detail": repr(exc),
                "backup_filename": (
                    backup_path.name if backup_path is not None else None
                ),
            },
        )

    # ``phrasebook.updated`` audit row uses the forward-compat
    # actor literal per PR #177 so the promotion-history view can
    # tell operator-edits apart from auto-approved promotions.
    try:
        emit_audit(
            seam="phrasebook.updated",
            details={
                "actor": "kora_proposal_approved",
                "action": "put",
                "entry_count_before": count_before,
                "entry_count_after": len(proposed_entries),
                "backup_filename": (
                    backup_path.name if backup_path is not None else None
                ),
                "proposal_id": proposal_id,
            },
            source=None,
        )
    except Exception as exc:
        logger.warning(
            "[kora.promote] phrasebook.updated emit raised %r — "
            "approval still succeeded",
            exc,
        )

    # Move the proposal to approved/ with operator overrides
    # baked into the persisted record so the audit JSONL +
    # on-disk file agree.
    updated = transition(
        proposal_id,
        new_status="approved",
        review_notes=review_notes,
        overrides=overrides_dict or None,
    )

    try:
        emit_audit(
            seam="promotion.approved",
            details={
                **proposal_to_dict(updated),
                "committed_entry": new_entry,
            },
            caller_session_id=f"promotion:phrasebook:{proposal_id}",
            source="reasoning",
        )
    except Exception as exc:
        logger.warning(
            "[kora.promote] promotion.approved emit raised %r — "
            "approval persisted; audit row missing",
            exc,
        )

    return {
        "proposal_id": proposal_id,
        "status": "approved",
        "committed_entry": new_entry,
        "entry_count_after": len(proposed_entries),
        "backup_filename": (
            backup_path.name if backup_path is not None else None
        ),
    }


@app.post("/api/promotions/phrasebook/{proposal_id}/reject")
async def reject_phrasebook_proposal(
    proposal_id: str, payload: Optional[Dict[str, Any]] = None
) -> Any:
    """Reject a pending proposal. Payload may carry
    ``{review_notes: str}`` — operator rationale recorded verbatim
    in the audit row (operator-decision-relevant per the #182
    precedent).
    """
    from kora_cli.audit import emit_audit
    from kora_cli.promote.phrasebook.proposer import proposal_to_dict
    from kora_cli.promote.phrasebook.store import (
        ProposalNotFound,
        load,
        transition,
    )

    review_notes = ""
    if isinstance(payload, dict):
        notes_raw = payload.get("review_notes")
        if isinstance(notes_raw, str):
            review_notes = notes_raw

    try:
        existing = load(proposal_id)
    except ProposalNotFound:
        return JSONResponse(
            status_code=404,
            content={"error": "proposal_not_found", "proposal_id": proposal_id},
        )
    if existing.status != "pending":
        return JSONResponse(
            status_code=409,
            content={
                "error": "proposal_not_pending",
                "proposal_id": proposal_id,
                "current_status": existing.status,
            },
        )

    updated = transition(
        proposal_id, new_status="rejected", review_notes=review_notes
    )

    try:
        emit_audit(
            seam="promotion.rejected",
            details=proposal_to_dict(updated),
            caller_session_id=f"promotion:phrasebook:{proposal_id}",
            source="reasoning",
        )
    except Exception as exc:
        logger.warning(
            "[kora.promote] promotion.rejected emit raised %r — "
            "rejection persisted; audit row missing",
            exc,
        )

    return {
        "proposal_id": proposal_id,
        "status": "rejected",
        "review_notes": review_notes,
    }


# ---------------------------------------------------------------------------
# Generic promotion-loop endpoints — KR-PROMOTE-LOOPS-COMPLETION-MEGABUCKET
# ---------------------------------------------------------------------------
#
# Three additional loops landed in this bucket (router-tuning,
# tool-trimming, probe-fix-envelopes). Each follows the phrasebook
# (#186) pattern but without the loop-specific "approve also writes
# to live config" step — these are propose-only at v1. The approve
# endpoint transitions the proposal status + emits ``promotion.approved``;
# reject does likewise with ``promotion.rejected``. Operator
# scaffolds the actual config change manually (router prompts, tool
# manifest, fix_envelopes.py) using the persisted proposal payload
# as the spec.
#
# DRY via :func:`_promotion_loop_pending` etc. — the per-loop GET +
# POST handlers are 3-line wrappers around the generic helpers.
#
# Drift-guard: ``_PROMOTION_STATUS_VALUES`` (defined above for the
# phrasebook endpoints) is shared.


def _promotion_loop_pending(loop_name: str) -> Dict[str, Any]:
    from kora_cli.promote._shared.proposal_store import list_by_status

    proposals = list_by_status(loop_name=loop_name, status="pending")
    # Highest-confidence first when payloads carry that field;
    # falls back to filesystem-name order otherwise.
    proposals.sort(
        key=lambda p: (
            -float(p.get("confidence") or 0.0),
            -int(p.get("cluster_size") or 0),
        )
    )
    return {
        "proposals": proposals,
        "status_values": list(_PROMOTION_STATUS_VALUES),
        "loop_name": loop_name,
    }


def _promotion_loop_transition(
    *,
    loop_name: str,
    proposal_id: str,
    new_status: str,
    audit_seam: str,
    payload: Optional[Dict[str, Any]],
) -> Any:
    """Shared transition helper for the 3 new loops. Validates
    pending state, mutates payload review_notes if present, emits
    audit row, returns the canonical response shape."""
    from kora_cli.audit import emit_audit
    from kora_cli.promote._shared.proposal_store import (
        ProposalNotFound,
        load,
        transition,
    )

    review_notes = ""
    if isinstance(payload, dict):
        notes_raw = payload.get("review_notes")
        if isinstance(notes_raw, str):
            review_notes = notes_raw

    try:
        current_status, _ = load(loop_name=loop_name, proposal_id=proposal_id)
    except ProposalNotFound:
        return JSONResponse(
            status_code=404,
            content={"error": "proposal_not_found", "proposal_id": proposal_id},
        )
    if current_status != "pending":
        return JSONResponse(
            status_code=409,
            content={
                "error": "proposal_not_pending",
                "proposal_id": proposal_id,
                "current_status": current_status,
            },
        )

    def _mutate(p: Dict[str, Any]) -> None:
        p["status"] = new_status
        if review_notes:
            p["review_notes"] = review_notes

    _, updated = transition(
        loop_name=loop_name,
        proposal_id=proposal_id,
        new_status=new_status,
        payload_mutator=_mutate,
    )

    try:
        emit_audit(
            seam=audit_seam,
            details=updated,
            caller_session_id=(
                f"promotion:{loop_name}:{proposal_id}"
            ),
            source="reasoning",
        )
    except Exception as exc:
        logger.warning(
            "[kora.promote] %s emit raised %r — transition persisted; "
            "audit row missing",
            audit_seam,
            exc,
        )

    return {
        "proposal_id": proposal_id,
        "status": new_status,
        "review_notes": review_notes,
    }


# --- Router-tuning ---------------------------------------------------------


@app.get("/api/promotions/router-tuning/pending")
async def list_pending_router_tuning_proposals() -> Dict[str, Any]:
    """Return pending router-tuning proposals.

    Payload shape per ``kora_cli.promote.router_tuning.proposer``:
    proposal_id / route / calls_count / escalation_count /
    escalation_rate / recommendation_kind / rationale / confidence /
    created_at / status.
    """
    return _promotion_loop_pending("router_tuning")


@app.post("/api/promotions/router-tuning/{proposal_id}/approve")
async def approve_router_tuning_proposal(
    proposal_id: str, payload: Optional[Dict[str, Any]] = None
) -> Any:
    """Approve a router-tuning proposal. Transitions the proposal
    + emits ``promotion.approved``; does NOT mutate router config —
    operator scaffolds the trigger-pattern change manually from the
    proposal rationale."""
    return _promotion_loop_transition(
        loop_name="router_tuning",
        proposal_id=proposal_id,
        new_status="approved",
        audit_seam="promotion.approved",
        payload=payload,
    )


@app.post("/api/promotions/router-tuning/{proposal_id}/reject")
async def reject_router_tuning_proposal(
    proposal_id: str, payload: Optional[Dict[str, Any]] = None
) -> Any:
    return _promotion_loop_transition(
        loop_name="router_tuning",
        proposal_id=proposal_id,
        new_status="rejected",
        audit_seam="promotion.rejected",
        payload=payload,
    )


# --- Tool-trimming ---------------------------------------------------------


@app.get("/api/promotions/tool-trimming/pending")
async def list_pending_tool_trimming_proposals() -> Dict[str, Any]:
    """Return pending tool-trimming proposals.

    Payload per ``kora_cli.promote.tool_trimming.proposer``:
    proposal_id / route / unused_tools / total_calls_for_route /
    observation_window_days / confidence / created_at / status.
    """
    return _promotion_loop_pending("tool_trimming")


@app.post("/api/promotions/tool-trimming/{proposal_id}/approve")
async def approve_tool_trimming_proposal(
    proposal_id: str, payload: Optional[Dict[str, Any]] = None
) -> Any:
    """Approve a tool-trim proposal. v1 transitions status + emits
    audit only — actual drop-list enforcement lands in the future
    KR-PLUGIN-TOOL-DESC-TRIM bucket which reads the approved
    proposals."""
    return _promotion_loop_transition(
        loop_name="tool_trimming",
        proposal_id=proposal_id,
        new_status="approved",
        audit_seam="promotion.approved",
        payload=payload,
    )


@app.post("/api/promotions/tool-trimming/{proposal_id}/reject")
async def reject_tool_trimming_proposal(
    proposal_id: str, payload: Optional[Dict[str, Any]] = None
) -> Any:
    return _promotion_loop_transition(
        loop_name="tool_trimming",
        proposal_id=proposal_id,
        new_status="rejected",
        audit_seam="promotion.rejected",
        payload=payload,
    )


# --- Probe-fix-envelopes ---------------------------------------------------


@app.get("/api/promotions/probe-envelopes/pending")
async def list_pending_probe_envelope_proposals() -> Dict[str, Any]:
    """Return pending probe-fix-envelope proposals.

    Payload per ``kora_cli.promote.probe_fix_envelopes.proposer``:
    proposal_id / probe / issue_category / fix_name_suggestion /
    cluster_size / recurring_recommendation_text /
    blast_radius_summary / confidence / created_at / status.

    HIGH-RISK loop — see module docstring. Operator manually
    scaffolds approved envelopes into ``probes/fix_envelopes.py``
    using the persisted payload as the spec.
    """
    return _promotion_loop_pending("probe_fix_envelopes")


@app.post("/api/promotions/probe-envelopes/{proposal_id}/approve")
async def approve_probe_envelope_proposal(
    proposal_id: str, payload: Optional[Dict[str, Any]] = None
) -> Any:
    """Approve a probe-fix-envelope proposal. v1 transitions status
    + emits audit only — Kora's ``fix_envelopes.py`` MUST be edited
    by hand. The approved/ proposal file is the audit trail for
    when the manual scaffold lands."""
    return _promotion_loop_transition(
        loop_name="probe_fix_envelopes",
        proposal_id=proposal_id,
        new_status="approved",
        audit_seam="promotion.approved",
        payload=payload,
    )


@app.post("/api/promotions/probe-envelopes/{proposal_id}/reject")
async def reject_probe_envelope_proposal(
    proposal_id: str, payload: Optional[Dict[str, Any]] = None
) -> Any:
    return _promotion_loop_transition(
        loop_name="probe_fix_envelopes",
        proposal_id=proposal_id,
        new_status="rejected",
        audit_seam="promotion.rejected",
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Email-intent audit lens (KR-FE-EMAIL-INTENT-LOG-PANEL)
# ---------------------------------------------------------------------------
#
# Operator-facing view of email-to-Sea_Ticket intent evaluations
# (PR #176 KR-INTENT-EMAIL-TO-SEA-TICKET). Reads the audit JSONL
# filtered to seam=intent.email_to_sea_ticket, projects per-row
# to the FE EmailIntentEvent shape, and surfaces by-action counts
# in the response so the panel can render its summary band
# without re-aggregating client-side.
#
# Discipline (carried forward from KR-AUDIT-PANEL-ENDPOINTS PR #155):
# one endpoint per audit seam — the alternative of a single generic
# /api/audit-events?seam=X was considered but rejected (security
# projection lives per-seam; a generic endpoint would either over-
# expose details or require seam-routing logic to do the projection).
#
# SECURITY:
#   * details.error is repr(exc) on the failed branch — could
#     leak stack-traceish content. Truncated to first 200 chars
#     so a runaway repr doesn't dump diagnostic state to a panel
#     consumer.
#   * details.proposed_title (dry_run branch) is whatever the
#     intent's proposed_sea_ticket title is — operator-author
#     content, safe to render.
#   * subject is the inbound email subject — same operator-author
#     content; truncated to 200 chars defensively.


_EMAIL_INTENT_ACTION_VALUES = (
    "created",
    "logged_only",
    "dry_run",
    "cap_exceeded",
    "failed",
)


def _project_email_intent_audit(entry: "AuditEntry", lineno: int) -> Dict[str, Any]:
    """Project an intent.email_to_sea_ticket audit row to the
    FE's EmailIntentEvent shape. NEVER includes the raw audit
    details dict — only the per-branch fields the panel renders,
    truncated to bounded lengths."""
    d = entry.details
    action_raw = str(d.get("action", "unknown"))
    action = action_raw if action_raw in _EMAIL_INTENT_ACTION_VALUES else "unknown"

    out: Dict[str, Any] = {
        "id": f"intent-{lineno}",
        "emitted_at": entry.emitted_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "action": action,
        "pattern_matched": str(d.get("pattern_matched", "")),
        "confidence": str(d.get("confidence", "")),
        "subject": str(d.get("subject", ""))[:200],
        # caller_session_id = "email:<message-id>" per the
        # _derive_caller_session_id email branch; surfaced so
        # operator can grep their inbox for the original.
        "caller_session_id": entry.caller_session_id or "",
    }

    # Per-branch optional fields. Each is only present when the
    # writer emitted it (see kora_cli/intent/email_to_sea_ticket.py
    # branches 1-5 — _safe_audit call sites).
    if action == "created":
        ticket_id = d.get("ticket_id")
        if ticket_id is not None:
            out["ticket_id"] = str(ticket_id)
        tags = d.get("tags")
        if isinstance(tags, list):
            out["tags"] = [str(t) for t in tags][:20]
    elif action == "logged_only":
        reason = d.get("reason")
        if reason is not None:
            out["reason"] = str(reason)[:120]
    elif action == "dry_run":
        proposed_title = d.get("proposed_title")
        if proposed_title is not None:
            out["proposed_title"] = str(proposed_title)[:200]
    elif action == "cap_exceeded":
        hourly_cap = d.get("hourly_cap")
        if hourly_cap is not None:
            out["hourly_cap"] = int(hourly_cap) if isinstance(hourly_cap, (int, float)) else None
    elif action == "failed":
        # repr(exc) truncated — defensive against runaway repr
        err = d.get("error")
        if err is not None:
            out["error"] = str(err)[:200]

    return out


@app.get("/api/email-intent/recent")
async def list_recent_email_intent(limit: int = 100) -> Dict[str, Any]:
    """Return recent email-intent audit events for the operator lens.

    Reads ``${KORA_HOME}/kora_audit_log.jsonl`` filtered to seam=
    intent.email_to_sea_ticket (written by KR-INTENT-EMAIL-TO-SEA-TICKET
    PR #176's emitter), projects each row, returns newest-first
    with 24h-window count aggregation by action.

    Query params:
      limit — number of newest entries to return; default 100,
              capped at 500. Higher default than other panels
              since the panel renders all visible at once (no
              pagination in v1) + intent events are sparse
              compared to e.g. mcp.tool_called.
    """
    from datetime import datetime, timedelta, timezone
    from kora_cli.audit.jsonl_reader import read_audit_entries

    capped_limit = max(1, min(int(limit or 100), 500))
    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)

    all_rows = read_audit_entries(seam="intent.email_to_sea_ticket")

    in_24h = [e for e in all_rows if e.emitted_at >= cutoff_24h]
    by_action_24h: Dict[str, int] = {a: 0 for a in _EMAIL_INTENT_ACTION_VALUES}
    by_action_24h["unknown"] = 0
    for e in in_24h:
        a = str(e.details.get("action", "unknown"))
        if a not in by_action_24h:
            a = "unknown"
        by_action_24h[a] = by_action_24h.get(a, 0) + 1

    # Daily-created counts over the last 14 days for the sparkline.
    # Buckets keyed by YYYY-MM-DD (UTC); operator gets a 2-week
    # rolling sense of "is Kora creating Sea_Tickets from email or
    # has the intent been quiet."
    # Bucket window: [today - 13d, today] inclusive = 14 buckets
    # ending TODAY. Event filter uses 14d back from `now` so
    # boundary events (emitted just after midnight UTC of day
    # `today - 13`) are still included.
    daily_created: Dict[str, int] = {}
    for d_offset in range(14):
        bucket = (now - timedelta(days=13 - d_offset)).strftime("%Y-%m-%d")
        daily_created[bucket] = 0
    cutoff_14d = now - timedelta(days=14)
    for e in all_rows:
        if e.emitted_at < cutoff_14d:
            continue
        if str(e.details.get("action", "")) != "created":
            continue
        key = e.emitted_at.strftime("%Y-%m-%d")
        if key in daily_created:
            daily_created[key] += 1

    projected = [
        _project_email_intent_audit(e, lineno=i + 1)
        for i, e in enumerate(all_rows[:capped_limit])
    ]

    return {
        "events": projected,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_recent_24h": len(in_24h),
        "by_action_24h": by_action_24h,
        # Ordered list of {date, count} so the FE can render the
        # sparkline in chronological order without re-sorting.
        "daily_created_14d": [
            {"date": k, "count": v}
            for k, v in sorted(daily_created.items())
        ],
        "action_values": list(_EMAIL_INTENT_ACTION_VALUES),
    }


# ---------------------------------------------------------------------------
# Outbound email audit lens (KR-FE-OUTBOUND-EMAIL-LOG-PANEL)
# ---------------------------------------------------------------------------
#
# Symmetric to /api/email-intent/recent (above). Surfaces the
# tool.email_to_operator_sent audit seam (PR #179 — the
# kora__send_email_to_operator reasoning-loop tool that lets Kora
# email the operator). Completes the cockpit's email-surface
# story: inbound (PR #176 + #180 viewer) + outbound (PR #179 +
# this viewer) both visible.
#
# PRIVACY discipline carried forward from PR #179:
#   * Body text NEVER appears in the audit row — only ``body_chars``
#     (the length). Same for subject: only ``subject_chars`` is
#     recorded, not the subject string. (Spec assumed subject was
#     surfaced; reality is stricter — neither leaves the daemon
#     process. This panel therefore shows sizes + status + a
#     stable smtp_message_id or rejection_reason for triage.)
#   * Recipient is PINNED to KORA_EMAIL_JOSHUA_ADDRESS at the tool
#     level (caller can't override); never audited because there's
#     no variation to record.
#
# SECURITY:
#   * ``rejection_detail`` is a dict carrying per-reason
#     diagnostic data (e.g. {hourly_cap: N} or {subject_chars: N}).
#     Truncated via JSON-serialize-then-substring to 200 chars so
#     a future writer adding a chatty detail field can't dump
#     diagnostic state to a panel consumer.
#   * ``error`` (smtp_failure branch) is the exception type name
#     only — short by construction; bounded defensively at 200.
#   * Unknown status values coerced to "unknown" (defensive
#     against future writers; pinned by drift-guard test).


_OUTBOUND_EMAIL_STATUS_VALUES = (
    "sent",
    "rejected",
    "smtp_failure",
)


def _project_outbound_email_audit(entry: "AuditEntry", lineno: int) -> Dict[str, Any]:
    """Project a tool.email_to_operator_sent audit row to the FE's
    OutboundEmailEvent shape. Per-status field whitelist — NEVER
    propagates the raw details dict so future-writer leaks
    (operator PII / SMTP headers / etc) are contained.

    Privacy: subject + body text are NOT in the audit row to begin
    with (per PR #179) — only ``subject_chars`` + ``body_chars``
    (numeric sizes). This projection surfaces those sizes
    verbatim; never reconstructs / displays string content."""
    d = entry.details
    status_raw = str(d.get("status", "unknown"))
    status = status_raw if status_raw in _OUTBOUND_EMAIL_STATUS_VALUES else "unknown"

    out: Dict[str, Any] = {
        "id": f"outbound-{lineno}",
        "emitted_at": entry.emitted_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": status,
        # Privacy-preserved size indicators (always present in the
        # audit row — kora_cli/tools/email_to_operator.py:365-369).
        "subject_chars": int(d.get("subject_chars", 0) or 0),
        "body_chars": int(d.get("body_chars", 0) or 0),
        "attachment_count": int(d.get("attachment_count", 0) or 0),
        # attachment_total_bytes is only populated post-attachment-
        # read (line 431). Rejected-pre-attachment-read rows omit
        # it. Surface as 0 when absent so the FE renders a stable
        # shape.
        "attachment_total_bytes": int(d.get("attachment_total_bytes", 0) or 0),
        # caller_session_id is the reasoning-engine session that
        # invoked the tool — operator can correlate with the
        # reasoning panel.
        "caller_session_id": entry.caller_session_id or "",
    }

    if status == "sent":
        # smtp_message_id is the SMTP server's stable identifier
        # for the message — opaque to the operator but useful for
        # triage ("did THIS one actually reach my inbox?").
        smtp_id = d.get("smtp_message_id")
        if smtp_id is not None:
            out["smtp_message_id"] = str(smtp_id)[:200]
        sent_at = d.get("sent_at")
        if sent_at is not None:
            out["sent_at"] = str(sent_at)[:60]
    elif status == "rejected":
        reason = d.get("rejection_reason")
        if reason is not None:
            out["rejection_reason"] = str(reason)[:120]
        # rejection_detail is the per-reason diagnostic dict. JSON-
        # serialize-truncate at 200 chars defensively.
        detail = d.get("rejection_detail")
        if detail is not None:
            try:
                detail_serialized = json.dumps(detail, default=str)
            except Exception:
                detail_serialized = str(detail)
            out["rejection_detail"] = detail_serialized[:200]
    elif status == "smtp_failure":
        err = d.get("error")
        if err is not None:
            out["error"] = str(err)[:200]
        smtp_status = d.get("smtp_status")
        if smtp_status is not None:
            out["smtp_status"] = str(smtp_status)[:120]

    return out


@app.get("/api/outbound-email/recent")
async def list_recent_outbound_email(limit: int = 100) -> Dict[str, Any]:
    """Return recent tool.email_to_operator_sent audit events.

    Reads ``${KORA_HOME}/kora_audit_log.jsonl`` filtered to seam=
    tool.email_to_operator_sent (written by PR #179's tool
    emitter), projects each row, returns newest-first with
    24h-window count aggregation by status + 14-day daily-sent
    sparkline points.

    Query params:
      limit — number of newest entries to return; default 100,
              capped at 500 (mirrors /api/email-intent/recent).
    """
    from datetime import datetime, timedelta, timezone
    from kora_cli.audit.jsonl_reader import read_audit_entries

    capped_limit = max(1, min(int(limit or 100), 500))
    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)

    all_rows = read_audit_entries(seam="tool.email_to_operator_sent")

    in_24h = [e for e in all_rows if e.emitted_at >= cutoff_24h]
    by_status_24h: Dict[str, int] = {
        s: 0 for s in _OUTBOUND_EMAIL_STATUS_VALUES
    }
    by_status_24h["unknown"] = 0
    for e in in_24h:
        s = str(e.details.get("status", "unknown"))
        if s not in by_status_24h:
            s = "unknown"
        by_status_24h[s] = by_status_24h.get(s, 0) + 1

    # Daily-sent counts over the last 14 days. Same shape as
    # email-intent's daily_created_14d so the FE sparkline
    # component can be reused. Window: [today - 13d, today].
    daily_sent: Dict[str, int] = {}
    for d_offset in range(14):
        bucket = (now - timedelta(days=13 - d_offset)).strftime("%Y-%m-%d")
        daily_sent[bucket] = 0
    cutoff_14d = now - timedelta(days=14)
    for e in all_rows:
        if e.emitted_at < cutoff_14d:
            continue
        if str(e.details.get("status", "")) != "sent":
            continue
        key = e.emitted_at.strftime("%Y-%m-%d")
        if key in daily_sent:
            daily_sent[key] += 1

    projected = [
        _project_outbound_email_audit(e, lineno=i + 1)
        for i, e in enumerate(all_rows[:capped_limit])
    ]

    return {
        "events": projected,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_recent_24h": len(in_24h),
        "by_status_24h": by_status_24h,
        "daily_sent_14d": [
            {"date": k, "count": v}
            for k, v in sorted(daily_sent.items())
        ],
        "status_values": list(_OUTBOUND_EMAIL_STATUS_VALUES),
    }


# ---------------------------------------------------------------------------
# Probe-autofix audit lens (KR-FE-AUTOFIX-LOG-PANEL)
# ---------------------------------------------------------------------------
#
# Surfaces the tool.probe_autofix_attempted audit seam (PR #182 —
# the kora__attempt_probe_autofix reasoning-loop tool that lets
# Kora restart Fly machines / similar bounded fixes after a probe
# detects unhealthy state). Operator sees what Kora attempted,
# why she attempted it, and the before→after state transition.
#
# SECURITY discipline:
#   * before_state / after_state are dicts projected from the Fly
#     machine API; we expose ONLY the `state` string field
#     (e.g. "started" → "stopped") to the panel. The full dict
#     (region, instance_id, etc) stays in the audit JSONL but
#     doesn't leak through this projection — same anti-leak
#     posture as PR #180 / #183.
#   * reason_from_reasoning is recorded VERBATIM in the audit per
#     PR #182 discipline — surfaced truncated to 300 chars
#     (operator-decision-relevant; longer reasons get summarized
#     in their first sentence).
#   * rejection_detail is JSON-serialized + truncated to 200.
#   * error (execution_failed branch) is the exception type name
#     — bounded by construction; truncated to 200 anyway.
#   * Unknown status values coerced to "unknown" defensively.


_PROBE_AUTOFIX_STATUS_VALUES = (
    "attempted",
    "rejected",
    "execution_failed",
)


def _project_probe_autofix_audit(
    entry: "AuditEntry", lineno: int
) -> Dict[str, Any]:
    """Project a tool.probe_autofix_attempted audit row to the
    FE's ProbeAutofixEvent shape. Per-status field whitelist.

    SECURITY: before_state/after_state dicts are projected to just
    their `state` string — the full machine dict (with region /
    instance_id / etc) is NOT exposed."""
    d = entry.details
    status_raw = str(d.get("status", "unknown"))
    status = (
        status_raw if status_raw in _PROBE_AUTOFIX_STATUS_VALUES else "unknown"
    )

    out: Dict[str, Any] = {
        "id": f"autofix-{lineno}",
        "emitted_at": entry.emitted_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": status,
        "probe": str(d.get("probe", ""))[:64],
        "action": str(d.get("action", ""))[:64],
        "target_id": str(d.get("target_id", ""))[:128],
        # reason_from_reasoning is the operator-decision-relevant
        # narrative. Truncate at 300 — longer than other
        # truncations because the panel uses this for "why did
        # Kora do this?" triage.
        "reason_from_reasoning": str(
            d.get("reason_from_reasoning", "")
        )[:300],
        "caller_session_id": entry.caller_session_id or "",
    }

    action_canonical = d.get("action_canonical")
    if action_canonical is not None:
        out["action_canonical"] = str(action_canonical)[:64]

    if status == "attempted":
        executor_ms = d.get("executor_duration_ms")
        if isinstance(executor_ms, (int, float)):
            out["executor_duration_ms"] = int(executor_ms)
        before_state = d.get("before_state")
        if isinstance(before_state, dict):
            bs = before_state.get("state")
            if bs is not None:
                out["before_state_label"] = str(bs)[:48]
        after_state = d.get("after_state")
        if isinstance(after_state, dict):
            as_ = after_state.get("state")
            if as_ is not None:
                out["after_state_label"] = str(as_)[:48]
        action_taken = d.get("action_taken")
        if action_taken is not None:
            out["action_taken"] = str(action_taken)[:64]
    elif status == "rejected":
        reason = d.get("rejection_reason")
        if reason is not None:
            out["rejection_reason"] = str(reason)[:120]
        detail = d.get("rejection_detail")
        if detail is not None:
            try:
                serialized = json.dumps(detail, default=str)
            except Exception:
                serialized = str(detail)
            out["rejection_detail"] = serialized[:200]
    elif status == "execution_failed":
        err = d.get("error")
        if err is not None:
            out["error"] = str(err)[:200]
        executor_ms = d.get("executor_duration_ms")
        if isinstance(executor_ms, (int, float)):
            out["executor_duration_ms"] = int(executor_ms)
        # Even on execution_failed we get a before_state usually
        # (the executor recorded state before the failed API call).
        before_state = d.get("before_state")
        if isinstance(before_state, dict):
            bs = before_state.get("state")
            if bs is not None:
                out["before_state_label"] = str(bs)[:48]

    return out


@app.get("/api/probe-autofix/recent")
async def list_recent_probe_autofix(limit: int = 100) -> Dict[str, Any]:
    """Return recent tool.probe_autofix_attempted audit events.

    Pattern mirror of /api/email-intent/recent (PR #180) +
    /api/outbound-email/recent (PR #183).

    Query params:
      limit — newest entries to return; default 100, capped 500.
    """
    from datetime import datetime, timedelta, timezone
    from kora_cli.audit.jsonl_reader import read_audit_entries

    capped_limit = max(1, min(int(limit or 100), 500))
    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)

    all_rows = read_audit_entries(seam="tool.probe_autofix_attempted")

    in_24h = [e for e in all_rows if e.emitted_at >= cutoff_24h]
    by_status_24h: Dict[str, int] = {
        s: 0 for s in _PROBE_AUTOFIX_STATUS_VALUES
    }
    by_status_24h["unknown"] = 0
    for e in in_24h:
        s = str(e.details.get("status", "unknown"))
        if s not in by_status_24h:
            s = "unknown"
        by_status_24h[s] = by_status_24h.get(s, 0) + 1

    # Daily-attempted counts over 14 days for the sparkline.
    # Same windowing math as email-intent / outbound-email.
    daily_attempted: Dict[str, int] = {}
    for d_offset in range(14):
        bucket = (now - timedelta(days=13 - d_offset)).strftime("%Y-%m-%d")
        daily_attempted[bucket] = 0
    cutoff_14d = now - timedelta(days=14)
    for e in all_rows:
        if e.emitted_at < cutoff_14d:
            continue
        if str(e.details.get("status", "")) != "attempted":
            continue
        key = e.emitted_at.strftime("%Y-%m-%d")
        if key in daily_attempted:
            daily_attempted[key] += 1

    projected = [
        _project_probe_autofix_audit(e, lineno=i + 1)
        for i, e in enumerate(all_rows[:capped_limit])
    ]

    return {
        "events": projected,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_recent_24h": len(in_24h),
        "by_status_24h": by_status_24h,
        "daily_attempted_14d": [
            {"date": k, "count": v}
            for k, v in sorted(daily_attempted.items())
        ],
        "status_values": list(_PROBE_AUTOFIX_STATUS_VALUES),
    }


# ---------------------------------------------------------------------------
# Kora-actions aggregated panel (KR-FE-KORA-ACTIONS-AGGREGATED-PANEL)
# ---------------------------------------------------------------------------
#
# Apex operator-trust surface: joins ALL mutating-action audit
# seams into one chronological timeline. "What did Kora do
# today?" in one view. Reads existing per-seam streams; no new
# audit/JSONL plumbing — orchestration + summary composition only.
#
# Joined seams (with per-seam filters):
#
#   * email_sent              ← tool.email_to_operator_sent (#179)
#   * sea_ticket_created      ← intent.email_to_sea_ticket (#176),
#                               filtered to details.action == "created"
#                               (logged_only / dry_run / failed go to
#                               the per-seam panel, not the
#                               kora-DID-something timeline)
#   * autofix_attempted       ← tool.probe_autofix_attempted (#182),
#                               filtered to details.status == "attempted"
#                               (rejected / execution_failed go to the
#                               per-seam panel)
#   * investigation_completed ← probe.investigation_completed (pending
#                               #406); reads the seam if available so
#                               this PR is forward-compatible; until
#                               #406 lands the loop produces 0 rows
#   * phrasebook_proposal_approved ← phrasebook.updated (#177),
#                               filtered to details.actor != "operator".
#                               In v1 ALL phrasebook updates have
#                               actor="operator" (no kora-proposal
#                               loop yet); future KR-PROMOTE-PHRASEBOOK
#                               bucket emits with actor="kora_proposal_
#                               approved" and this category populates.
#
# SECURITY: each per-category summary composer uses the same
# anti-leak whitelist discipline as the per-seam panels. No raw
# audit-details dicts pass through to the response.


_KORA_ACTION_CATEGORIES = (
    "email_sent",
    "sea_ticket_created",
    "autofix_attempted",
    "investigation_completed",
    "phrasebook_proposal_approved",
    "other",  # forward-compat catch-all for future seams
)


def _kora_action_summary_email_sent(d: Dict[str, Any]) -> Dict[str, Any]:
    """Compose a one-liner + status badge for an outbound-email row.
    Privacy-preserved per PR #179 — only sizes."""
    status_raw = str(d.get("status", "unknown"))
    parts = []
    sc = d.get("subject_chars")
    bc = d.get("body_chars")
    ac = d.get("attachment_count")
    if isinstance(sc, (int, float)):
        parts.append(f"{int(sc)}-char subject")
    if isinstance(bc, (int, float)):
        parts.append(f"{int(bc)}-char body")
    if isinstance(ac, (int, float)) and ac > 0:
        parts.append(f"{int(ac)} attachment{'s' if ac != 1 else ''}")
    summary = "Sent email to operator"
    if parts:
        summary += " · " + " · ".join(parts)
    return {
        "summary": summary,
        "status": status_raw,
        "deep_link": "/outbound-email-log",
    }


def _kora_action_summary_sea_ticket_created(d: Dict[str, Any]) -> Dict[str, Any]:
    ticket_id = d.get("ticket_id")
    pattern = str(d.get("pattern_matched", ""))[:64]
    summary = "Saved Sea_Ticket"
    if ticket_id:
        summary += f" #{ticket_id}"
    if pattern:
        summary += f" from email · {pattern}"
    out: Dict[str, Any] = {
        "summary": summary,
        "status": "created",
    }
    if ticket_id:
        out["deep_link"] = (
            f"/sea-tickets?focus={ticket_id}"
        )
    else:
        out["deep_link"] = "/email-intent-log"
    return out


def _kora_action_summary_autofix_attempted(
    d: Dict[str, Any],
) -> Dict[str, Any]:
    probe = str(d.get("probe", ""))[:48]
    action = str(
        d.get("action_canonical") or d.get("action_taken") or d.get("action", "")
    )[:64]
    target = str(d.get("target_id", ""))[:48]
    bs = ""
    as_ = ""
    if isinstance(d.get("before_state"), dict):
        bs = str(d["before_state"].get("state", ""))[:32]
    if isinstance(d.get("after_state"), dict):
        as_ = str(d["after_state"].get("state", ""))[:32]
    summary = f"{action} on {probe}/{target}"
    if bs and as_:
        summary += f" · {bs}→{as_}"
    elif bs:
        summary += f" · before: {bs}"
    return {
        "summary": summary,
        "status": "attempted",
        "deep_link": "/probe-autofix-log",
    }


def _kora_action_summary_phrasebook_proposal_approved(
    d: Dict[str, Any],
) -> Dict[str, Any]:
    before = d.get("entry_count_before")
    after = d.get("entry_count_after")
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        summary = (
            f"Phrasebook updated · {int(before)}→{int(after)} entries"
        )
    else:
        summary = "Phrasebook updated"
    return {
        "summary": summary,
        "status": "approved",
        "deep_link": "/phrasebook",
    }


def _kora_action_summary_investigation_completed(
    d: Dict[str, Any],
) -> Dict[str, Any]:
    probe = str(d.get("probe", ""))[:48]
    summary = "Probe investigation completed"
    if probe:
        summary += f" · {probe}"
    return {
        "summary": summary,
        "status": "completed",
        "deep_link": "/probe-investigations",
    }


@app.get("/api/kora-actions/recent")
async def list_recent_kora_actions(
    limit: int = 100,
) -> Dict[str, Any]:
    """Apex "what did Kora do" timeline. Joins all mutating-action
    audit seams into one chronological list.

    Query params:
      limit — total newest events returned; default 100, capped
              500. Cross-seam merge is in-memory + bounded by the
              per-seam list lengths (each per-seam read_audit_entries
              call is already bounded by the JSONL file size).

    See module-level comment for the per-seam filter rules.
    """
    from datetime import datetime, timedelta, timezone
    from kora_cli.audit.jsonl_reader import read_audit_entries

    capped_limit = max(1, min(int(limit or 100), 500))
    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)

    # Read each contributing seam. Order doesn't matter; we sort
    # all results by emitted_at desc at the end.
    email_rows = read_audit_entries(seam="tool.email_to_operator_sent")
    intent_rows = read_audit_entries(seam="intent.email_to_sea_ticket")
    autofix_rows = read_audit_entries(seam="tool.probe_autofix_attempted")
    phrasebook_rows = read_audit_entries(seam="phrasebook.updated")
    # Forward-compat: probe.investigation_completed isn't in the
    # SeamName Literal yet (lands with PR #406). read_audit_entries
    # silently returns [] when no entries match — safe to call.
    try:
        investigation_rows = read_audit_entries(
            seam="probe.investigation_completed"
        )
    except Exception:
        investigation_rows = []

    items: List[Dict[str, Any]] = []
    lineno = 0

    for e in email_rows:
        lineno += 1
        s = _kora_action_summary_email_sent(e.details)
        items.append(
            {
                "id": f"action-email-{lineno}",
                "emitted_at": e.emitted_at,
                "action_category": "email_sent",
                "caller_session_id": e.caller_session_id or "",
                **s,
            }
        )

    for e in intent_rows:
        lineno += 1
        if str(e.details.get("action", "")) != "created":
            continue
        s = _kora_action_summary_sea_ticket_created(e.details)
        items.append(
            {
                "id": f"action-intent-{lineno}",
                "emitted_at": e.emitted_at,
                "action_category": "sea_ticket_created",
                "caller_session_id": e.caller_session_id or "",
                **s,
            }
        )

    for e in autofix_rows:
        lineno += 1
        if str(e.details.get("status", "")) != "attempted":
            continue
        s = _kora_action_summary_autofix_attempted(e.details)
        items.append(
            {
                "id": f"action-autofix-{lineno}",
                "emitted_at": e.emitted_at,
                "action_category": "autofix_attempted",
                "caller_session_id": e.caller_session_id or "",
                **s,
            }
        )

    for e in phrasebook_rows:
        lineno += 1
        # v1: ALL phrasebook.updated rows have actor="operator"
        # (no kora-proposal loop yet). The actor != "operator"
        # filter therefore yields zero in v1 — that's correct.
        # Future KR-PROMOTE-PHRASEBOOK emits actor="kora_
        # proposal_approved" and this category begins to populate.
        if str(e.details.get("actor", "")) == "operator":
            continue
        s = _kora_action_summary_phrasebook_proposal_approved(e.details)
        items.append(
            {
                "id": f"action-phrasebook-{lineno}",
                "emitted_at": e.emitted_at,
                "action_category": "phrasebook_proposal_approved",
                "caller_session_id": e.caller_session_id or "",
                **s,
            }
        )

    for e in investigation_rows:
        lineno += 1
        s = _kora_action_summary_investigation_completed(e.details)
        items.append(
            {
                "id": f"action-investigation-{lineno}",
                "emitted_at": e.emitted_at,
                "action_category": "investigation_completed",
                "caller_session_id": e.caller_session_id or "",
                **s,
            }
        )

    # Sort newest-first by emitted_at + serialize timestamps.
    items.sort(key=lambda it: it["emitted_at"], reverse=True)
    items_serialized = [
        {**it, "emitted_at": it["emitted_at"].strftime("%Y-%m-%dT%H:%M:%SZ")}
        for it in items[:capped_limit]
    ]

    # 24h-window by_category aggregation. Iterate the raw items
    # (pre-serialization, so emitted_at comparisons work).
    by_category_24h: Dict[str, int] = {c: 0 for c in _KORA_ACTION_CATEGORIES}
    in_24h = [it for it in items if it["emitted_at"] >= cutoff_24h]
    for it in in_24h:
        cat = it["action_category"]
        if cat not in by_category_24h:
            cat = "other"
        by_category_24h[cat] = by_category_24h.get(cat, 0) + 1

    # 14-day daily-actions sparkline (across all categories).
    daily_actions: Dict[str, int] = {}
    for d_offset in range(14):
        bucket = (now - timedelta(days=13 - d_offset)).strftime("%Y-%m-%d")
        daily_actions[bucket] = 0
    cutoff_14d = now - timedelta(days=14)
    for it in items:
        if it["emitted_at"] < cutoff_14d:
            continue
        key = it["emitted_at"].strftime("%Y-%m-%d")
        if key in daily_actions:
            daily_actions[key] += 1

    return {
        "items": items_serialized,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_recent_24h": len(in_24h),
        "by_category_24h": by_category_24h,
        "daily_actions_14d": [
            {"date": k, "count": v}
            for k, v in sorted(daily_actions.items())
        ],
        "action_categories": list(_KORA_ACTION_CATEGORIES),
    }


# ---------------------------------------------------------------------------
# Probe investigations xref — KR-FE-PROBE-INVESTIGATION-VIEWER
# ---------------------------------------------------------------------------
#
# Reads three sources + joins them per wake event:
#
#   1. probe.wake_requested audit rows (PR #163 emitter)
#   2. reasoning.tool_called audit rows where caller_session_id ==
#      "probe:{probe}:{category}" (PR #166 wired this caller_session
#      shape via _derive_caller_session_id, source="probe_investigation")
#   3. snapshot.service_health[probe] (current probe health → drives
#      resolution_status)
#
# v1 SCOPE NOTES (documented in PR description; flagged for follow-on):
#
#   * Probe DMs are NOT written to slack_dm_log.jsonl (wake_consumer
#     calls client.post_dm directly without the
#     SlackDMHandler._append_outbound_log_entry path). The response
#     does NOT include dm_sent confirmation; KR-PROBE-DM-JSONL-WIRE
#     follow-on can flip this to "DM sent at <ts>".
#
#   * Per-call cost_usd / model_used are not durably recorded per
#     reasoning call — only aggregated per-route in CostTelemetry.
#     For per-investigation cost, operator reads /cost-telemetry
#     route=probe_investigation. KR-PROBE-INVESTIGATION-COST-XREF
#     follow-on could add per-call records.
#
#   * Resolution semantics simplified to "snapshot.service_health
#     right now" rather than "probe healthy AND post-dating wake."
#     A per-probe observation timeline doesn't exist in v1 substrate
#     (only current state). Stale (>24h, no recent obs) reduces to
#     "current health=unknown" naturally. Sufficient for "is this
#     one still firing?" — the operator's actual question.

import re as _probe_xref_re
from datetime import datetime as _probe_xref_datetime
from datetime import timedelta as _probe_xref_timedelta
from datetime import timezone as _probe_xref_timezone

_PROBE_INVESTIGATION_VIEWER_WINDOWS = {
    "24h": _probe_xref_timedelta(hours=24),
    "7d": _probe_xref_timedelta(days=7),
    "all": None,
}

_PROBE_CALLER_SESSION_RE = _probe_xref_re.compile(
    r"^probe:([a-zA-Z0-9_-]+):([a-zA-Z0-9_-]+)$"
)


def _probe_caller_session_id(probe: str, category: str) -> str:
    """Mirror anthropic_engine._derive_caller_session_id's
    probe_investigation shape so the join is keyed on a literal
    that both sides agree on. Drift-guarded by
    test_caller_session_id_matches_reasoning_engine."""
    return f"probe:{probe}:{category}"


def _project_reasoning_call(entry: "AuditEntry") -> Dict[str, Any]:
    """Project a reasoning.tool_called audit row to FE shape.

    Only the names + numeric/status fields the writer actually emits
    (anthropic_engine._emit_tool_called_audit). NEVER includes tool
    input/output bodies — those aren't in the audit details and must
    never leak through this endpoint even if a future writer adds
    them."""
    d = entry.details
    out: Dict[str, Any] = {
        "tool_name": str(d.get("tool_name", "")),
        "triggered_by": str(d.get("triggered_by", "")),
        "tool_duration_ms": int(d.get("tool_duration_ms", 0) or 0),
        "tool_status": str(d.get("tool_status", "")),
        "emitted_at": entry.emitted_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    exc_type = d.get("exc_type")
    if exc_type:
        out["exc_type"] = str(exc_type)
    return out


def _resolution_status_from_health(current_health: str) -> str:
    """Map current snapshot health to a resolution-status tag.

    Simplified-v1 semantics: "currently healthy" → resolved (the
    issue is no longer being observed); "currently unhealthy" or
    "degraded" → active; "unknown" → unknown.
    """
    if current_health == "healthy":
        return "resolved"
    if current_health in ("unhealthy", "degraded"):
        return "active"
    return "unknown"


@app.get("/api/probe-investigations")
async def get_probe_investigations(
    window: str = "24h",
    limit: int = 50,
) -> Dict[str, Any]:
    """Probe wake → investigation xref panel feed.

    Joins three sources per wake event:
      * probe.wake_requested audit rows
      * reasoning.tool_called audit rows (caller_session_id ==
        "probe:{probe}:{category}" — pinned by drift-guard test)
      * snapshot.service_health[probe] (current health → resolution)

    Args:
      window: ``24h`` | ``7d`` | ``all``. Bounds wake events read.
      limit: cap on items returned (defensive against unbounded
        growth). 1-200; default 50.

    Returns: summary counts + per-event items ordered newest-first.

    v1 deferred: per-call cost / model_used / DM-sent confirmation
    aren't durably recorded in current substrate (see PR
    description). The response omits these fields rather than
    fabricating zeros.
    """
    from kora_cli.audit.jsonl_reader import read_audit_entries
    from kora_cli.snapshot import read_snapshot

    if window not in _PROBE_INVESTIGATION_VIEWER_WINDOWS:
        window = "24h"
    delta = _PROBE_INVESTIGATION_VIEWER_WINDOWS[window]

    capped_limit = max(1, min(int(limit or 50), 200))
    now = _probe_xref_datetime.now(_probe_xref_timezone.utc)
    since = now - delta if delta is not None else None

    wake_rows = read_audit_entries(
        seam="probe.wake_requested", since=since
    )
    # All reasoning.tool_called rows in the SAME window — we index
    # by caller_session_id below to associate each wake with its
    # investigation tool-calls. Reading without a per-wake filter
    # would scale O(wakes * file-size); a single read + dict
    # bucketing is O(file-size) total.
    reasoning_rows = read_audit_entries(
        seam="reasoning.tool_called", since=since
    )
    snap = read_snapshot() or {}
    service_health = (
        (snap.get("service_health") or {})
        if isinstance(snap, dict)
        else {}
    )

    tool_calls_by_session: Dict[str, list] = {}
    for entry in reasoning_rows:
        sid = entry.caller_session_id or ""
        if not _PROBE_CALLER_SESSION_RE.match(sid):
            continue
        tool_calls_by_session.setdefault(sid, []).append(entry)

    items: list = []
    for entry in wake_rows[:capped_limit]:
        d = entry.details
        probe = str(d.get("probe") or "unknown")
        category = str(d.get("category") or "unknown")
        severity = str(d.get("severity") or "warning")
        session_id = _probe_caller_session_id(probe, category)
        wake_iso = entry.emitted_at.strftime("%Y-%m-%dT%H:%M:%SZ")

        related_calls = tool_calls_by_session.get(session_id, [])
        # Tool-calls audit rows are SAVED newest-first by the
        # reader; chronologise within the investigation so the FE
        # can show "tool_a → tool_b → tool_c" left-to-right.
        related_calls_chrono = sorted(
            related_calls, key=lambda e: e.emitted_at
        )
        # Only count calls AFTER the wake event (defensive against
        # session-id reuse if a later investigation reuses the same
        # probe:category key — the wake event marks the start).
        post_wake_calls = [
            c for c in related_calls_chrono
            if c.emitted_at >= entry.emitted_at
        ]
        if post_wake_calls:
            total_duration_ms = sum(
                int(c.details.get("tool_duration_ms", 0) or 0)
                for c in post_wake_calls
            )
            any_errored = any(
                str(c.details.get("tool_status", "")).lower()
                not in ("ok", "success", "")
                or bool(c.details.get("exc_type"))
                for c in post_wake_calls
            )
            investigation: Optional[Dict[str, Any]] = {
                "tool_calls": [
                    _project_reasoning_call(c) for c in post_wake_calls
                ],
                "total_duration_ms": total_duration_ms,
                "any_errored": any_errored,
                "call_count": len(post_wake_calls),
            }
        else:
            investigation = None

        current_health = str(service_health.get(probe, "unknown"))
        items.append({
            "wake_event_id": f"{wake_iso}:{probe}:{category}",
            "wake_timestamp": wake_iso,
            "probe_name": probe,
            "issue_category": category,
            "severity": severity,
            "title": str(d.get("title") or ""),
            "detail": str(d.get("detail") or ""),
            "envelope_enabled": bool(d.get("envelope_enabled", False)),
            "envelope_fix_name": str(d.get("envelope_fix_name") or "(none)"),
            "caller_session_id": session_id,
            "investigation": investigation,
            "current_probe_health": current_health,
            "resolution_status": _resolution_status_from_health(current_health),
        })

    total_count = len(items)
    active_count = sum(1 for it in items if it["resolution_status"] == "active")
    resolved_count = sum(
        1 for it in items if it["resolution_status"] == "resolved"
    )
    unknown_count = total_count - active_count - resolved_count

    return {
        "window": window,
        "since": since.strftime("%Y-%m-%dT%H:%M:%SZ") if since else None,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_count": total_count,
        "active_count": active_count,
        "resolved_count": resolved_count,
        "unknown_count": unknown_count,
        "current_probe_health": {
            name: str(service_health.get(name, "unknown"))
            for name in ("vercel", "sentry", "doppler", "supabase", "fly")
        },
        "items": items,
        "v1_notes": {
            "per_call_cost_usd": (
                "not durably recorded; see /api/cost_telemetry "
                "route=probe_investigation for aggregate"
            ),
            "dm_sent_confirmation": (
                "probe DMs bypass slack_dm_log.jsonl in v1; "
                "follow-on KR-PROBE-DM-JSONL-WIRE"
            ),
        },
    }


# ---------------------------------------------------------------------------
# Profile management endpoints (minimal — list/create/rename/delete + SOUL.md)
# ---------------------------------------------------------------------------


class ProfileCreate(BaseModel):
    name: str
    clone_from_default: bool = False
    no_skills: bool = False


class ProfileRename(BaseModel):
    new_name: str


class ProfileSoulUpdate(BaseModel):
    content: str


def _profile_attr(info, name: str, default: Any = None) -> Any:
    try:
        return getattr(info, name)
    except Exception:
        return default


def _profile_to_dict(info) -> Dict[str, Any]:
    return {
        "name": _profile_attr(info, "name", ""),
        "path": str(_profile_attr(info, "path", "")),
        "is_default": bool(_profile_attr(info, "is_default", False)),
        "model": _profile_attr(info, "model"),
        "provider": _profile_attr(info, "provider"),
        "has_env": bool(_profile_attr(info, "has_env", False)),
        "skill_count": int(_profile_attr(info, "skill_count", 0) or 0),
    }


def _fallback_profile_dicts(profiles_mod) -> List[Dict[str, Any]]:
    def _safe(callable_, default):
        try:
            return callable_()
        except Exception:
            return default

    profiles: List[Dict[str, Any]] = []
    default_home = profiles_mod._get_default_hermes_home()
    if default_home.is_dir():
        model, provider = _safe(lambda: profiles_mod._read_config_model(default_home), (None, None))
        profiles.append({
            "name": "default",
            "path": str(default_home),
            "is_default": True,
            "model": model,
            "provider": provider,
            "has_env": (default_home / ".env").exists(),
            "skill_count": _safe(lambda: profiles_mod._count_skills(default_home), 0),
        })

    profiles_root = profiles_mod._get_profiles_root()
    if profiles_root.is_dir():
        for entry in sorted(profiles_root.iterdir()):
            if not entry.is_dir() or not profiles_mod._PROFILE_ID_RE.match(entry.name):
                continue
            model, provider = _safe(lambda entry=entry: profiles_mod._read_config_model(entry), (None, None))
            profiles.append({
                "name": entry.name,
                "path": str(entry),
                "is_default": False,
                "model": model,
                "provider": provider,
                "has_env": (entry / ".env").exists(),
                "skill_count": _safe(lambda entry=entry: profiles_mod._count_skills(entry), 0),
            })

    return profiles


def _resolve_profile_dir(name: str) -> Path:
    """Validate ``name`` and resolve to its directory or raise an HTTPException."""
    from kora_cli import profiles as profiles_mod
    try:
        profiles_mod.validate_profile_name(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not profiles_mod.profile_exists(name):
        raise HTTPException(status_code=404, detail=f"Profile '{name}' does not exist.")
    return profiles_mod.get_profile_dir(name)


def _profile_setup_command(name: str) -> str:
    """Return the shell command used to configure a profile in the CLI."""
    _resolve_profile_dir(name)
    return "hermes setup" if name == "default" else f"{name} setup"


@app.get("/api/profiles")
async def list_profiles_endpoint():
    from kora_cli import profiles as profiles_mod
    try:
        return {"profiles": [_profile_to_dict(p) for p in profiles_mod.list_profiles()]}
    except Exception:
        _log.exception("GET /api/profiles failed; falling back to profile directory scan")
        return {"profiles": _fallback_profile_dicts(profiles_mod)}


@app.post("/api/profiles")
async def create_profile_endpoint(body: ProfileCreate):
    from kora_cli import profiles as profiles_mod
    try:
        path = profiles_mod.create_profile(
            name=body.name,
            clone_from="default" if body.clone_from_default else None,
            clone_config=body.clone_from_default,
            no_skills=body.no_skills,
        )
        # Match the CLI's profile-create flow: fresh named profiles get the
        # bundled skills installed. When cloning from default, create_profile()
        # has already copied the source profile's skills, including any
        # user-installed skills. When no_skills=True, create_profile() wrote
        # the opt-out marker and seed_profile_skills() will no-op.
        if not body.clone_from_default:
            profiles_mod.seed_profile_skills(path, quiet=True)

        # Match the CLI's profile-create flow: named profiles should get a
        # wrapper in ~/.local/bin when the alias is safe to create.
        collision = profiles_mod.check_alias_collision(body.name)
        if not collision:
            profiles_mod.create_wrapper_script(body.name)
    except (ValueError, FileExistsError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _log.exception("POST /api/profiles failed")
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "name": body.name, "path": str(path)}


@app.get("/api/profiles/{name}/setup-command")
async def get_profile_setup_command(name: str):
    return {"command": _profile_setup_command(name)}


@app.post("/api/profiles/{name}/open-terminal")
async def open_profile_terminal_endpoint(name: str):
    try:
        command = _profile_setup_command(name)

        if sys.platform.startswith("win"):
            subprocess.Popen(["cmd.exe", "/c", "start", "", command])
        elif sys.platform == "darwin":
            escaped = command.replace("\\", "\\\\").replace('"', '\\"')
            applescript = (
                'tell application "Terminal"\n'
                "activate\n"
                f'do script "{escaped}"\n'
                "end tell"
            )
            subprocess.Popen(["osascript", "-e", applescript])
        else:
            terminal_commands = [
                ("x-terminal-emulator", ["x-terminal-emulator", "-e", "sh", "-lc", command]),
                ("gnome-terminal", ["gnome-terminal", "--", "sh", "-lc", command]),
                ("konsole", ["konsole", "-e", "sh", "-lc", command]),
                ("xfce4-terminal", ["xfce4-terminal", "-e", f"sh -lc '{command}'"]),
                ("mate-terminal", ["mate-terminal", "-e", f"sh -lc '{command}'"]),
                ("lxterminal", ["lxterminal", "-e", f"sh -lc '{command}'"]),
                ("tilix", ["tilix", "-e", "sh", "-lc", command]),
                ("alacritty", ["alacritty", "-e", "sh", "-lc", command]),
                ("kitty", ["kitty", "sh", "-lc", command]),
                ("xterm", ["xterm", "-e", "sh", "-lc", command]),
            ]
            for executable, popen_args in terminal_commands:
                if subprocess.call(
                    ["which", executable],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ) == 0:
                    subprocess.Popen(popen_args)
                    break
            else:
                raise HTTPException(
                    status_code=400,
                    detail="No supported terminal emulator found",
                )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        _log.exception("POST /api/profiles/%s/open-terminal failed", name)
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "command": command}


@app.patch("/api/profiles/{name}")
async def rename_profile_endpoint(name: str, body: ProfileRename):
    from kora_cli import profiles as profiles_mod
    try:
        path = profiles_mod.rename_profile(name, body.new_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (ValueError, FileExistsError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _log.exception("PATCH /api/profiles/%s failed", name)
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "name": body.new_name, "path": str(path)}


@app.delete("/api/profiles/{name}")
async def delete_profile_endpoint(name: str):
    """Delete a profile. The dashboard collects the user's confirmation in
    its own dialog before this request, so we always pass ``yes=True`` to
    skip the CLI's interactive prompt."""
    from kora_cli import profiles as profiles_mod
    try:
        path = profiles_mod.delete_profile(name, yes=True)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _log.exception("DELETE /api/profiles/%s failed", name)
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "path": str(path)}


@app.get("/api/profiles/{name}/soul")
async def get_profile_soul(name: str):
    soul_path = _resolve_profile_dir(name) / "SOUL.md"
    if soul_path.exists():
        try:
            return {"content": soul_path.read_text(encoding="utf-8"), "exists": True}
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"Could not read SOUL.md: {e}")
    return {"content": "", "exists": False}


@app.put("/api/profiles/{name}/soul")
async def update_profile_soul(name: str, body: ProfileSoulUpdate):
    soul_path = _resolve_profile_dir(name) / "SOUL.md"
    try:
        soul_path.write_text(body.content, encoding="utf-8")
    except OSError as e:
        _log.exception("PUT /api/profiles/%s/soul failed", name)
        raise HTTPException(status_code=500, detail=f"Could not write SOUL.md: {e}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Skills & Tools endpoints
# ---------------------------------------------------------------------------


class SkillToggle(BaseModel):
    name: str
    enabled: bool


@app.get("/api/skills")
async def get_skills():
    from tools.skills_tool import _find_all_skills
    from kora_cli.skills_config import get_disabled_skills
    config = load_config()
    disabled = get_disabled_skills(config)
    skills = _find_all_skills(skip_disabled=True)
    for s in skills:
        s["enabled"] = s["name"] not in disabled
    return skills


@app.put("/api/skills/toggle")
async def toggle_skill(body: SkillToggle):
    from kora_cli.skills_config import get_disabled_skills, save_disabled_skills
    config = load_config()
    disabled = get_disabled_skills(config)
    if body.enabled:
        disabled.discard(body.name)
    else:
        disabled.add(body.name)
    save_disabled_skills(config, disabled)
    return {"ok": True, "name": body.name, "enabled": body.enabled}


@app.get("/api/tools/toolsets")
async def get_toolsets():
    from kora_cli.tools_config import (
        _get_effective_configurable_toolsets,
        _get_platform_tools,
        _toolset_has_keys,
    )
    from toolsets import resolve_toolset

    config = load_config()
    enabled_toolsets = _get_platform_tools(
        config,
        "cli",
        include_default_mcp_servers=False,
    )
    result = []
    for name, label, desc in _get_effective_configurable_toolsets():
        try:
            tools = sorted(set(resolve_toolset(name)))
        except Exception:
            tools = []
        is_enabled = name in enabled_toolsets
        result.append({
            "name": name, "label": label, "description": desc,
            "enabled": is_enabled,
            "available": is_enabled,
            "configured": _toolset_has_keys(name, config),
            "tools": tools,
        })
    return result


# ---------------------------------------------------------------------------
# Raw YAML config endpoint
# ---------------------------------------------------------------------------


class RawConfigUpdate(BaseModel):
    yaml_text: str


@app.get("/api/config/raw")
async def get_config_raw():
    path = get_config_path()
    if not path.exists():
        return {"yaml": ""}
    return {"yaml": path.read_text(encoding="utf-8")}


@app.put("/api/config/raw")
async def update_config_raw(body: RawConfigUpdate):
    try:
        parsed = yaml.safe_load(body.yaml_text)
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=400, detail="YAML must be a mapping")
        save_config(parsed)
        return {"ok": True}
    except yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}")


# ---------------------------------------------------------------------------
# Token / cost analytics endpoint
# ---------------------------------------------------------------------------


@app.get("/api/analytics/usage")
async def get_usage_analytics(days: int = 30):
    from kora_state import SessionDB
    from agent.insights import InsightsEngine

    db = SessionDB()
    try:
        cutoff = time.time() - (days * 86400)
        cur = db._conn.execute("""
            SELECT date(started_at, 'unixepoch') as day,
                   SUM(input_tokens) as input_tokens,
                   SUM(output_tokens) as output_tokens,
                   SUM(cache_read_tokens) as cache_read_tokens,
                   SUM(reasoning_tokens) as reasoning_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) as actual_cost,
                   COUNT(*) as sessions,
                   SUM(COALESCE(api_call_count, 0)) as api_calls
            FROM sessions WHERE started_at > ?
            GROUP BY day ORDER BY day
        """, (cutoff,))
        daily = [dict(r) for r in cur.fetchall()]

        cur2 = db._conn.execute("""
            SELECT model,
                   SUM(input_tokens) as input_tokens,
                   SUM(output_tokens) as output_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
                   COUNT(*) as sessions,
                   SUM(COALESCE(api_call_count, 0)) as api_calls
            FROM sessions WHERE started_at > ? AND model IS NOT NULL
            GROUP BY model ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC
        """, (cutoff,))
        by_model = [dict(r) for r in cur2.fetchall()]

        cur3 = db._conn.execute("""
            SELECT SUM(input_tokens) as total_input,
                   SUM(output_tokens) as total_output,
                   SUM(cache_read_tokens) as total_cache_read,
                   SUM(reasoning_tokens) as total_reasoning,
                   COALESCE(SUM(estimated_cost_usd), 0) as total_estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) as total_actual_cost,
                   COUNT(*) as total_sessions,
                   SUM(COALESCE(api_call_count, 0)) as total_api_calls
            FROM sessions WHERE started_at > ?
        """, (cutoff,))
        totals = dict(cur3.fetchone())
        insights_report = InsightsEngine(db).generate(days=days)
        skills = insights_report.get("skills", {
            "summary": {
                "total_skill_loads": 0,
                "total_skill_edits": 0,
                "total_skill_actions": 0,
                "distinct_skills_used": 0,
            },
            "top_skills": [],
        })

        return {
            "daily": daily,
            "by_model": by_model,
            "totals": totals,
            "period_days": days,
            "skills": skills,
        }
    finally:
        db.close()


@app.get("/api/analytics/models")
async def get_models_analytics(days: int = 30):
    """Rich per-model analytics for the Models dashboard page.

    Returns token/cost/session breakdown per model plus capability metadata
    from models.dev (context window, vision, tools, reasoning, etc.).
    """
    from kora_state import SessionDB

    db = SessionDB()
    try:
        cutoff = time.time() - (days * 86400)

        cur = db._conn.execute("""
            SELECT model,
                   billing_provider,
                   SUM(input_tokens) as input_tokens,
                   SUM(output_tokens) as output_tokens,
                   SUM(cache_read_tokens) as cache_read_tokens,
                   SUM(reasoning_tokens) as reasoning_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) as actual_cost,
                   COUNT(*) as sessions,
                   SUM(COALESCE(api_call_count, 0)) as api_calls,
                   SUM(tool_call_count) as tool_calls,
                   MAX(started_at) as last_used_at,
                   AVG(input_tokens + output_tokens) as avg_tokens_per_session
            FROM sessions WHERE started_at > ? AND model IS NOT NULL AND model != ''
            GROUP BY model, billing_provider
            ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC
        """, (cutoff,))
        rows = [dict(r) for r in cur.fetchall()]

        models = []
        for row in rows:
            provider = row.get("billing_provider") or ""
            model_name = row["model"]
            caps = {}
            try:
                from agent.models_dev import get_model_capabilities
                mc = get_model_capabilities(provider=provider, model=model_name)
                if mc is not None:
                    caps = {
                        "supports_tools": mc.supports_tools,
                        "supports_vision": mc.supports_vision,
                        "supports_reasoning": mc.supports_reasoning,
                        "context_window": mc.context_window,
                        "max_output_tokens": mc.max_output_tokens,
                        "model_family": mc.model_family,
                    }
            except Exception:
                pass

            models.append({
                "model": model_name,
                "provider": provider,
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "cache_read_tokens": row["cache_read_tokens"],
                "reasoning_tokens": row["reasoning_tokens"],
                "estimated_cost": row["estimated_cost"],
                "actual_cost": row["actual_cost"],
                "sessions": row["sessions"],
                "api_calls": row["api_calls"],
                "tool_calls": row["tool_calls"],
                "last_used_at": row["last_used_at"],
                "avg_tokens_per_session": row["avg_tokens_per_session"],
                "capabilities": caps,
            })

        totals_cur = db._conn.execute("""
            SELECT COUNT(DISTINCT model) as distinct_models,
                   SUM(input_tokens) as total_input,
                   SUM(output_tokens) as total_output,
                   SUM(cache_read_tokens) as total_cache_read,
                   SUM(reasoning_tokens) as total_reasoning,
                   COALESCE(SUM(estimated_cost_usd), 0) as total_estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) as total_actual_cost,
                   COUNT(*) as total_sessions,
                   SUM(COALESCE(api_call_count, 0)) as total_api_calls
            FROM sessions WHERE started_at > ? AND model IS NOT NULL AND model != ''
        """, (cutoff,))
        totals = dict(totals_cur.fetchone())

        return {
            "models": models,
            "totals": totals,
            "period_days": days,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# /api/pty — PTY-over-WebSocket bridge for the dashboard "Chat" tab.
#
# The endpoint spawns the same ``hermes --tui`` binary the CLI uses, behind
# a POSIX pseudo-terminal, and forwards bytes + resize escapes across a
# WebSocket.  The browser renders the ANSI through xterm.js (see
# web/src/pages/ChatPage.tsx).
#
# Auth: ``?token=<session_token>`` query param (browsers can't set
# Authorization on the WS upgrade).  Same ephemeral ``_SESSION_TOKEN`` as
# REST.  Localhost-only — we defensively reject non-loopback clients even
# though uvicorn binds to 127.0.0.1.
# ---------------------------------------------------------------------------

import re
import asyncio

# PTY bridge is POSIX-only (depends on fcntl/termios/ptyprocess).  On native
# Windows the import raises; catch and leave PtyBridge=None so the rest of
# the dashboard (sessions, jobs, metrics, config editor) still loads and the
# /api/pty endpoint cleanly refuses with a WSL-suggested message.
try:
    from kora_cli.pty_bridge import PtyBridge, PtyUnavailableError
    _PTY_BRIDGE_AVAILABLE = True
except ImportError as _pty_import_err:  # pragma: no cover - Windows-only path
    PtyBridge = None  # type: ignore[assignment]
    _PTY_BRIDGE_AVAILABLE = False

    class PtyUnavailableError(RuntimeError):  # type: ignore[no-redef]
        """Stub on platforms where pty_bridge can't be imported."""
        pass

_RESIZE_RE = re.compile(rb"\x1b\[RESIZE:(\d+);(\d+)\]")
_PTY_READ_CHUNK_TIMEOUT = 0.2
_VALID_CHANNEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
# Starlette's TestClient reports the peer as "testclient"; treat it as
# loopback so tests don't need to rewrite request scope.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def _is_public_bind() -> bool:
    """True when bound to all-interfaces (operator used --insecure)."""
    return getattr(app.state, "bound_host", "") in {"0.0.0.0", "::"}


def _ws_client_is_allowed(ws: "WebSocket") -> bool:
    """Check if the WebSocket client IP is acceptable.

    Allows loopback always; allows any IP when bound to all-interfaces
    (--insecure mode, guarded by session token auth).
    """
    if _is_public_bind():
        return True
    client_host = ws.client.host if ws.client else ""
    if not client_host:
        return True
    return client_host in _LOOPBACK_HOSTS

# Per-channel subscriber registry used by /api/pub (PTY-side gateway → dashboard)
# and /api/events (dashboard → browser sidebar).  Keyed by an opaque channel id
# the chat tab generates on mount; entries auto-evict when the last subscriber
# drops AND the publisher has disconnected.
_event_channels: dict[str, set] = {}
_event_lock = asyncio.Lock()


def _resolve_chat_argv(
    resume: Optional[str] = None,
    sidecar_url: Optional[str] = None,
) -> tuple[list[str], Optional[str], Optional[dict]]:
    """Resolve the argv + cwd + env for the chat PTY.

    Default: whatever ``hermes --tui`` would run.  Tests monkeypatch this
    function to inject a tiny fake command (``cat``, ``sh -c 'printf …'``)
    so nothing has to build Node or the TUI bundle.

    Session resume is propagated via the ``HERMES_TUI_RESUME`` env var —
    matching what ``kora_cli.main._launch_tui`` does for the CLI path.
    Appending ``--resume <id>`` to argv doesn't work because ``ui-tui`` does
    not parse its argv.

    `sidecar_url` (when set) is forwarded as ``HERMES_TUI_SIDECAR_URL`` so
    the spawned ``tui_gateway.entry`` can mirror dispatcher emits to the
    dashboard's ``/api/pub`` endpoint (see :func:`pub_ws`).
    """
    from kora_cli.main import PROJECT_ROOT, _make_tui_argv

    argv, cwd = _make_tui_argv(PROJECT_ROOT / "ui-tui", tui_dev=False)
    env = os.environ.copy()
    env.setdefault("NODE_ENV", "production")
    # Browser-embedded chat should prefer stable wheel-based scrollback over
    # native terminal mouse tracking. When mouse tracking is enabled, wheel
    # events are consumed by the TUI and forwarded as terminal input, which
    # makes browser-side transcript scrolling feel broken. Keep the terminal
    # build unchanged for native CLI usage; only disable mouse tracking for
    # the dashboard PTY path.
    env.setdefault("HERMES_TUI_DISABLE_MOUSE", "1")
    env.setdefault("HERMES_TUI_INLINE", "1")

    if resume:
        latest_resume, _latest_path = _session_latest_descendant(resume)
        if latest_resume:
            resume = latest_resume
        env["HERMES_TUI_RESUME"] = resume

    if sidecar_url:
        env["HERMES_TUI_SIDECAR_URL"] = sidecar_url

    return list(argv), str(cwd) if cwd else None, env


def _build_sidecar_url(channel: str) -> Optional[str]:
    """ws:// URL the PTY child should publish events to, or None when unbound."""
    host = getattr(app.state, "bound_host", None)
    port = getattr(app.state, "bound_port", None)

    if not host or not port:
        return None

    netloc = f"[{host}]:{port}" if ":" in host and not host.startswith("[") else f"{host}:{port}"
    qs = urllib.parse.urlencode({"token": _SESSION_TOKEN, "channel": channel})

    return f"ws://{netloc}/api/pub?{qs}"


async def _broadcast_event(channel: str, payload: str) -> None:
    """Fan out one publisher frame to every subscriber on `channel`."""
    async with _event_lock:
        subs = list(_event_channels.get(channel, ()))

    for sub in subs:
        try:
            await sub.send_text(payload)
        except Exception:
            # Subscriber went away mid-send; the /api/events finally clause
            # will remove it from the registry on its next iteration.
            pass


def _channel_or_close_code(ws: WebSocket) -> Optional[str]:
    """Return the channel id from the query string or None if invalid."""
    channel = ws.query_params.get("channel", "")

    return channel if _VALID_CHANNEL_RE.match(channel) else None


@app.websocket("/api/pty")
async def pty_ws(ws: WebSocket) -> None:
    if not _DASHBOARD_EMBEDDED_CHAT_ENABLED:
        await ws.close(code=4403)
        return

    # --- auth + loopback check (before accept so we can close cleanly) ---
    token = ws.query_params.get("token", "")
    expected = _SESSION_TOKEN
    if not hmac.compare_digest(token.encode(), expected.encode()):
        await ws.close(code=4401)
        return

    if not _ws_client_is_allowed(ws):
        await ws.close(code=4403)
        return

    await ws.accept()

    # On native Windows, the POSIX PTY bridge can't be imported.  Tell the
    # client and close cleanly rather than pretending the feature works.
    if not _PTY_BRIDGE_AVAILABLE:
        await ws.send_text(
            "\r\n\x1b[31mChat unavailable: the embedded terminal requires a "
            "POSIX PTY, which native Windows Python doesn't provide.\x1b[0m\r\n"
            "\x1b[33mInstall Hermes inside WSL2 to use the dashboard's /chat "
            "tab — the rest of the dashboard works here.\x1b[0m\r\n"
        )
        await ws.close(code=1011)
        return

    # --- spawn PTY ------------------------------------------------------
    resume = ws.query_params.get("resume") or None
    channel = _channel_or_close_code(ws)
    sidecar_url = _build_sidecar_url(channel) if channel else None

    try:
        argv, cwd, env = _resolve_chat_argv(resume=resume, sidecar_url=sidecar_url)
    except SystemExit as exc:
        # _make_tui_argv calls sys.exit(1) when node/npm is missing.
        await ws.send_text(f"\r\n\x1b[31mChat unavailable: {exc}\x1b[0m\r\n")
        await ws.close(code=1011)
        return


    try:
        bridge = PtyBridge.spawn(argv, cwd=cwd, env=env)
    except PtyUnavailableError as exc:
        await ws.send_text(f"\r\n\x1b[31mChat unavailable: {exc}\x1b[0m\r\n")
        await ws.close(code=1011)
        return
    except (FileNotFoundError, OSError) as exc:
        await ws.send_text(f"\r\n\x1b[31mChat failed to start: {exc}\x1b[0m\r\n")
        await ws.close(code=1011)
        return

    loop = asyncio.get_running_loop()

    # --- reader task: PTY master → WebSocket ----------------------------
    async def pump_pty_to_ws() -> None:
        while True:
            chunk = await loop.run_in_executor(
                None, bridge.read, _PTY_READ_CHUNK_TIMEOUT
            )
            if chunk is None:  # EOF
                return
            if not chunk:  # no data this tick; yield control and retry
                await asyncio.sleep(0)
                continue
            try:
                await ws.send_bytes(chunk)
            except Exception:
                return

    reader_task = asyncio.create_task(pump_pty_to_ws())

    # --- writer loop: WebSocket → PTY master ----------------------------
    try:
        while True:
            msg = await ws.receive()
            msg_type = msg.get("type")
            if msg_type == "websocket.disconnect":
                break
            raw = msg.get("bytes")
            if raw is None:
                text = msg.get("text")
                raw = text.encode("utf-8") if isinstance(text, str) else b""
            if not raw:
                continue

            # Resize escape is consumed locally, never written to the PTY.
            match = _RESIZE_RE.match(raw)
            if match and match.end() == len(raw):
                cols = int(match.group(1))
                rows = int(match.group(2))
                bridge.resize(cols=cols, rows=rows)
                continue

            bridge.write(raw)
    except WebSocketDisconnect:
        pass
    finally:
        reader_task.cancel()
        try:
            await reader_task
        except (asyncio.CancelledError, Exception):
            pass
        bridge.close()


# ---------------------------------------------------------------------------
# /api/ws — JSON-RPC WebSocket sidecar for the dashboard "Chat" tab.
#
# Drives the same `tui_gateway.dispatch` surface Ink uses over stdio, so the
# dashboard can render structured metadata (model badge, tool-call sidebar,
# slash launcher, session info) alongside the xterm.js terminal that PTY
# already paints. Both transports bind to the same session id when one is
# active, so a tool.start emitted by the agent fans out to both sinks.
# ---------------------------------------------------------------------------


@app.websocket("/api/ws")
async def gateway_ws(ws: WebSocket) -> None:
    if not _DASHBOARD_EMBEDDED_CHAT_ENABLED:
        await ws.close(code=4403)
        return

    token = ws.query_params.get("token", "")
    if not hmac.compare_digest(token.encode(), _SESSION_TOKEN.encode()):
        await ws.close(code=4401)
        return

    if not _ws_client_is_allowed(ws):
        await ws.close(code=4403)
        return

    from tui_gateway.ws import handle_ws

    await handle_ws(ws)


# ---------------------------------------------------------------------------
# /api/pub + /api/events — chat-tab event broadcast.
#
# The PTY-side ``tui_gateway.entry`` opens /api/pub at startup (driven by
# HERMES_TUI_SIDECAR_URL set in /api/pty's PTY env) and writes every
# dispatcher emit through it.  The dashboard fans those frames out to any
# subscriber that opened /api/events on the same channel id.  This is what
# gives the React sidebar its tool-call feed without breaking the PTY
# child's stdio handshake with Ink.
# ---------------------------------------------------------------------------


@app.websocket("/api/pub")
async def pub_ws(ws: WebSocket) -> None:
    if not _DASHBOARD_EMBEDDED_CHAT_ENABLED:
        await ws.close(code=4403)
        return

    token = ws.query_params.get("token", "")
    if not hmac.compare_digest(token.encode(), _SESSION_TOKEN.encode()):
        await ws.close(code=4401)
        return

    if not _ws_client_is_allowed(ws):
        await ws.close(code=4403)
        return

    channel = _channel_or_close_code(ws)
    if not channel:
        await ws.close(code=4400)
        return

    await ws.accept()

    try:
        while True:
            await _broadcast_event(channel, await ws.receive_text())
    except WebSocketDisconnect:
        pass


@app.websocket("/api/events")
async def events_ws(ws: WebSocket) -> None:
    if not _DASHBOARD_EMBEDDED_CHAT_ENABLED:
        await ws.close(code=4403)
        return

    token = ws.query_params.get("token", "")
    if not hmac.compare_digest(token.encode(), _SESSION_TOKEN.encode()):
        await ws.close(code=4401)
        return

    if not _ws_client_is_allowed(ws):
        await ws.close(code=4403)
        return

    channel = _channel_or_close_code(ws)
    if not channel:
        await ws.close(code=4400)
        return

    await ws.accept()

    async with _event_lock:
        _event_channels.setdefault(channel, set()).add(ws)

    try:
        while True:
            # Subscribers don't speak — the receive() just blocks until
            # disconnect so the connection stays open as long as the
            # browser holds it.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        async with _event_lock:
            subs = _event_channels.get(channel)

            if subs is not None:
                subs.discard(ws)

                if not subs:
                    _event_channels.pop(channel, None)


def _normalise_prefix(raw: Optional[str]) -> str:
    """Normalise an X-Forwarded-Prefix header value.

    Returns a string like ``"/hermes"`` (no trailing slash) or ``""`` when
    no prefix is set / the header is malformed. We deliberately reject
    anything containing ``..`` or non-printable bytes so a hostile proxy
    can't inject HTML via the prefix.
    """
    if not raw:
        return ""
    p = raw.strip()
    if not p:
        return ""
    if not p.startswith("/"):
        p = "/" + p
    p = p.rstrip("/")
    if "//" in p or ".." in p or any(c in p for c in ('"', "'", "<", ">", " ", "\n", "\r", "\t")):
        return ""
    if len(p) > 64:
        return ""
    return p


def mount_spa(application: FastAPI):
    """Mount the built SPA. Falls back to index.html for client-side routing.

    The session token is injected into index.html via a ``<script>`` tag so
    the SPA can authenticate against protected API endpoints without a
    separate (unauthenticated) token-dispensing endpoint.

    When served behind a path-prefix reverse proxy (e.g.
    ``mission-control.tilos.com/hermes/*`` -> local Caddy -> :9119), the
    proxy injects ``X-Forwarded-Prefix: /hermes`` on every request. We
    rewrite the served ``index.html`` so absolute asset URLs (``/assets/...``)
    and the SPA's runtime ``__HERMES_BASE_PATH__`` honour that prefix
    without rebuilding the bundle.
    """
    if not WEB_DIST.exists():
        @application.get("/{full_path:path}")
        async def no_frontend(full_path: str):
            return JSONResponse(
                {"error": "Frontend not built. Run: cd web && npm run build"},
                status_code=404,
            )
        return

    _index_path = WEB_DIST / "index.html"

    def _serve_index(prefix: str = ""):
        """Return index.html with the session token + base-path injected.

        ``prefix`` is the normalised ``X-Forwarded-Prefix`` (e.g. ``/hermes``)
        or empty string when served at root.
        """
        html = _index_path.read_text()
        chat_js = "true" if _DASHBOARD_EMBEDDED_CHAT_ENABLED else "false"
        token_script = (
            f'<script>window.__HERMES_SESSION_TOKEN__="{_SESSION_TOKEN}";'
            f"window.__HERMES_DASHBOARD_EMBEDDED_CHAT__={chat_js};"
            f'window.__HERMES_BASE_PATH__="{prefix}";</script>'
        )
        if prefix:
            # Rewrite absolute asset URLs baked into the Vite build so the
            # browser fetches them through the same proxy prefix.
            html = html.replace('href="/assets/', f'href="{prefix}/assets/')
            html = html.replace('src="/assets/', f'src="{prefix}/assets/')
            html = html.replace('href="/favicon.ico"', f'href="{prefix}/favicon.ico"')
            html = html.replace('href="/fonts/', f'href="{prefix}/fonts/')
            html = html.replace('href="/ds-assets/', f'href="{prefix}/ds-assets/')
            html = html.replace('src="/ds-assets/', f'src="{prefix}/ds-assets/')
        html = html.replace("</head>", f"{token_script}</head>", 1)
        return HTMLResponse(
            html,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    # When served behind a path-prefix proxy, the built CSS contains
    # absolute ``url(/fonts/...)`` and ``url(/ds-assets/...)`` references.
    # Browsers resolve those against the document origin, which means
    # under ``/hermes`` they'd hit ``mission-control.tilos.com/fonts/...``
    # (the MC Pages app), not the Hermes backend. Intercept CSS asset
    # requests BEFORE the StaticFiles mount and rewrite the absolute paths
    # when a prefix is in play.
    @application.get("/assets/{filename}.css")
    async def serve_css(filename: str, request: Request):
        css_path = WEB_DIST / "assets" / f"{filename}.css"
        if not css_path.is_file() or not css_path.resolve().is_relative_to(
            WEB_DIST.resolve()
        ):
            return JSONResponse({"error": "not found"}, status_code=404)
        prefix = _normalise_prefix(request.headers.get("x-forwarded-prefix"))
        css = css_path.read_text()
        if prefix:
            for asset_dir in ("/fonts/", "/fonts-terminal/", "/ds-assets/", "/assets/"):
                css = css.replace(f"url({asset_dir}", f"url({prefix}{asset_dir}")
                css = css.replace(f"url(\"{asset_dir}", f"url(\"{prefix}{asset_dir}")
                css = css.replace(f"url('{asset_dir}", f"url('{prefix}{asset_dir}")
        return Response(content=css, media_type="text/css")

    application.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @application.get("/{full_path:path}")
    async def serve_spa(full_path: str, request: Request):
        prefix = _normalise_prefix(request.headers.get("x-forwarded-prefix"))
        file_path = WEB_DIST / full_path
        # Prevent path traversal via url-encoded sequences (%2e%2e/)
        if (
            full_path
            and file_path.resolve().is_relative_to(WEB_DIST.resolve())
            and file_path.exists()
            and file_path.is_file()
        ):
            return FileResponse(file_path)
        return _serve_index(prefix)


# ---------------------------------------------------------------------------
# Dashboard theme endpoints
# ---------------------------------------------------------------------------

# Built-in dashboard themes — label + description only.  The actual color
# definitions live in the frontend (web/src/themes/presets.ts).
_BUILTIN_DASHBOARD_THEMES = [
    {"name": "default",       "label": "Hermes Teal",         "description": "Classic dark teal — the canonical Hermes look"},
    {"name": "default-large", "label": "Hermes Teal (Large)", "description": "Hermes Teal with bigger fonts and roomier spacing"},
    {"name": "midnight",      "label": "Midnight",            "description": "Deep blue-violet with cool accents"},
    {"name": "ember",     "label": "Ember",          "description": "Warm crimson and bronze — forge vibes"},
    {"name": "mono",      "label": "Mono",           "description": "Clean grayscale — minimal and focused"},
    {"name": "cyberpunk", "label": "Cyberpunk",      "description": "Neon green on black — matrix terminal"},
    {"name": "rose",      "label": "Rosé",           "description": "Soft pink and warm ivory — easy on the eyes"},
]


def _parse_theme_layer(value: Any, default_hex: str, default_alpha: float = 1.0) -> Optional[Dict[str, Any]]:
    """Normalise a theme layer spec from YAML into `{hex, alpha}` form.

    Accepts shorthand (a bare hex string) or full dict form.  Returns
    ``None`` on garbage input so the caller can fall back to a built-in
    default rather than blowing up.
    """
    if value is None:
        return {"hex": default_hex, "alpha": default_alpha}
    if isinstance(value, str):
        return {"hex": value, "alpha": default_alpha}
    if isinstance(value, dict):
        hex_val = value.get("hex", default_hex)
        alpha_val = value.get("alpha", default_alpha)
        if not isinstance(hex_val, str):
            return None
        try:
            alpha_f = float(alpha_val)
        except (TypeError, ValueError):
            alpha_f = default_alpha
        return {"hex": hex_val, "alpha": max(0.0, min(1.0, alpha_f))}
    return None


_THEME_DEFAULT_TYPOGRAPHY: Dict[str, str] = {
    "fontSans": 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
    "fontMono": 'ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace',
    "baseSize": "15px",
    "lineHeight": "1.55",
    "letterSpacing": "0",
}

_THEME_DEFAULT_LAYOUT: Dict[str, str] = {
    "radius": "0.5rem",
    "density": "comfortable",
}

_THEME_OVERRIDE_KEYS = {
    "card", "cardForeground", "popover", "popoverForeground",
    "primary", "primaryForeground", "secondary", "secondaryForeground",
    "muted", "mutedForeground", "accent", "accentForeground",
    "destructive", "destructiveForeground", "success", "warning",
    "border", "input", "ring",
}

# Well-known named asset slots themes can populate.  Any other keys under
# ``assets.custom`` are exposed as ``--theme-asset-custom-<key>`` CSS vars
# for plugin/shell use.
_THEME_NAMED_ASSET_KEYS = {"bg", "hero", "logo", "crest", "sidebar", "header"}

# Component-style buckets themes can override.  The value under each bucket
# is a mapping from camelCase property name to CSS string; each pair emits
# ``--component-<bucket>-<kebab-property>`` on :root.  The frontend's shell
# components (Card, App header, Backdrop, etc.) consume these vars so themes
# can restyle chrome (clip-path, border-image, segmented progress, etc.)
# without shipping their own CSS.
_THEME_COMPONENT_BUCKETS = {
    "card", "header", "footer", "sidebar", "tab",
    "progress", "badge", "backdrop", "page",
}

_THEME_LAYOUT_VARIANTS = {"standard", "cockpit", "tiled"}

# Cap on customCSS length so a malformed/oversized theme YAML can't blow up
# the response payload or the <style> tag.  32 KiB is plenty for every
# practical reskin (the Strike Freedom demo is ~2 KiB).
_THEME_CUSTOM_CSS_MAX = 32 * 1024


def _normalise_theme_definition(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalise a user theme YAML into the wire format `ThemeProvider`
    expects.  Returns ``None`` if the theme is unusable.

    Accepts both the full schema (palette/typography/layout) and a loose
    form with bare hex strings, so hand-written YAMLs stay friendly.
    """
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return None

    # Palette
    palette_src = data.get("palette", {}) if isinstance(data.get("palette"), dict) else {}
    # Allow top-level `colors.background` as a shorthand too.
    colors_src = data.get("colors", {}) if isinstance(data.get("colors"), dict) else {}

    def _layer(key: str, default_hex: str, default_alpha: float = 1.0) -> Dict[str, Any]:
        spec = palette_src.get(key, colors_src.get(key))
        parsed = _parse_theme_layer(spec, default_hex, default_alpha)
        return parsed if parsed is not None else {"hex": default_hex, "alpha": default_alpha}

    palette = {
        "background": _layer("background", "#041c1c", 1.0),
        "midground": _layer("midground", "#ffe6cb", 1.0),
        "foreground": _layer("foreground", "#ffffff", 0.0),
        "warmGlow": palette_src.get("warmGlow") or data.get("warmGlow") or "rgba(255, 189, 56, 0.35)",
        "noiseOpacity": 1.0,
    }
    raw_noise = palette_src.get("noiseOpacity", data.get("noiseOpacity"))
    try:
        palette["noiseOpacity"] = float(raw_noise) if raw_noise is not None else 1.0
    except (TypeError, ValueError):
        palette["noiseOpacity"] = 1.0

    # Typography
    typo_src = data.get("typography", {}) if isinstance(data.get("typography"), dict) else {}
    typography = dict(_THEME_DEFAULT_TYPOGRAPHY)
    for key in ("fontSans", "fontMono", "fontDisplay", "fontUrl", "baseSize", "lineHeight", "letterSpacing"):
        val = typo_src.get(key)
        if isinstance(val, str) and val.strip():
            typography[key] = val

    # Layout
    layout_src = data.get("layout", {}) if isinstance(data.get("layout"), dict) else {}
    layout = dict(_THEME_DEFAULT_LAYOUT)
    radius = layout_src.get("radius")
    if isinstance(radius, str) and radius.strip():
        layout["radius"] = radius
    density = layout_src.get("density")
    if isinstance(density, str) and density in {"compact", "comfortable", "spacious"}:
        layout["density"] = density

    # Color overrides — keep only valid keys with string values.
    overrides_src = data.get("colorOverrides", {})
    color_overrides: Dict[str, str] = {}
    if isinstance(overrides_src, dict):
        for key, val in overrides_src.items():
            if key in _THEME_OVERRIDE_KEYS and isinstance(val, str) and val.strip():
                color_overrides[key] = val

    # Assets — named slots + arbitrary user-defined keys.  Values must be
    # strings (URLs or CSS ``url(...)``/``linear-gradient(...)`` expressions).
    # We don't fetch remote assets here; the frontend just injects them as
    # CSS vars.  Empty values are dropped so a theme can explicitly clear a
    # slot by setting ``hero: ""``.
    assets_out: Dict[str, Any] = {}
    assets_src = data.get("assets", {}) if isinstance(data.get("assets"), dict) else {}
    for key in _THEME_NAMED_ASSET_KEYS:
        val = assets_src.get(key)
        if isinstance(val, str) and val.strip():
            assets_out[key] = val
    custom_assets_src = assets_src.get("custom")
    if isinstance(custom_assets_src, dict):
        custom_assets: Dict[str, str] = {}
        for key, val in custom_assets_src.items():
            if (
                isinstance(key, str)
                and key.replace("-", "").replace("_", "").isalnum()
                and isinstance(val, str)
                and val.strip()
            ):
                custom_assets[key] = val
        if custom_assets:
            assets_out["custom"] = custom_assets

    # Custom CSS — raw CSS text the frontend injects as a scoped <style>
    # tag on theme apply.  Clipped to _THEME_CUSTOM_CSS_MAX to keep the
    # payload bounded.  We intentionally do NOT parse/sanitise the CSS
    # here — the dashboard is localhost-only and themes are user-authored
    # YAML in ~/.kora/, same trust level as the config file itself.
    custom_css_val = data.get("customCSS")
    custom_css: Optional[str] = None
    if isinstance(custom_css_val, str) and custom_css_val.strip():
        custom_css = custom_css_val[:_THEME_CUSTOM_CSS_MAX]

    # Component style overrides — per-bucket dicts of camelCase CSS
    # property -> CSS string.  The frontend converts these into CSS vars
    # that shell components (Card, App header, Backdrop) consume.
    component_styles_src = data.get("componentStyles", {})
    component_styles: Dict[str, Dict[str, str]] = {}
    if isinstance(component_styles_src, dict):
        for bucket, props in component_styles_src.items():
            if bucket not in _THEME_COMPONENT_BUCKETS or not isinstance(props, dict):
                continue
            clean: Dict[str, str] = {}
            for prop, value in props.items():
                if (
                    isinstance(prop, str)
                    and prop.replace("-", "").replace("_", "").isalnum()
                    and isinstance(value, (str, int, float))
                    and str(value).strip()
                ):
                    clean[prop] = str(value)
            if clean:
                component_styles[bucket] = clean

    layout_variant_src = data.get("layoutVariant")
    layout_variant = (
        layout_variant_src
        if isinstance(layout_variant_src, str) and layout_variant_src in _THEME_LAYOUT_VARIANTS
        else "standard"
    )

    result: Dict[str, Any] = {
        "name": name,
        "label": data.get("label") or name,
        "description": data.get("description", ""),
        "palette": palette,
        "typography": typography,
        "layout": layout,
        "layoutVariant": layout_variant,
    }
    if color_overrides:
        result["colorOverrides"] = color_overrides
    if assets_out:
        result["assets"] = assets_out
    if custom_css is not None:
        result["customCSS"] = custom_css
    if component_styles:
        result["componentStyles"] = component_styles
    return result


def _discover_user_themes() -> list:
    """Scan ~/.kora/dashboard-themes/*.yaml for user-created themes.

    Returns a list of fully-normalised theme definitions ready to ship
    to the frontend, so the client can apply them without a secondary
    round-trip or a built-in stub.
    """
    themes_dir = get_kora_home() / "dashboard-themes"
    if not themes_dir.is_dir():
        return []
    result = []
    for f in sorted(themes_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        normalised = _normalise_theme_definition(data)
        if normalised is not None:
            result.append(normalised)
    return result


@app.get("/api/dashboard/themes")
async def get_dashboard_themes():
    """Return available themes and the currently active one.

    Built-in entries ship name/label/description only (the frontend owns
    their full definitions in `web/src/themes/presets.ts`).  User themes
    from `~/.kora/dashboard-themes/*.yaml` ship with their full
    normalised definition under `definition`, so the client can apply
    them without a stub.
    """
    config = load_config()
    active = cfg_get(config, "dashboard", "theme", default="default")
    user_themes = _discover_user_themes()
    seen = set()
    themes = []
    for t in _BUILTIN_DASHBOARD_THEMES:
        seen.add(t["name"])
        themes.append(t)
    for t in user_themes:
        if t["name"] in seen:
            continue
        themes.append({
            "name": t["name"],
            "label": t["label"],
            "description": t["description"],
            "definition": t,
        })
        seen.add(t["name"])
    return {"themes": themes, "active": active}


class ThemeSetBody(BaseModel):
    name: str


@app.put("/api/dashboard/theme")
async def set_dashboard_theme(body: ThemeSetBody):
    """Set the active dashboard theme (persists to config.yaml)."""
    config = load_config()
    if "dashboard" not in config:
        config["dashboard"] = {}
    config["dashboard"]["theme"] = body.name
    save_config(config)
    return {"ok": True, "theme": body.name}


# ---------------------------------------------------------------------------
# Dashboard plugin system
# ---------------------------------------------------------------------------

def _discover_dashboard_plugins() -> list:
    """Scan plugins/*/dashboard/manifest.json for dashboard extensions.

    Checks three plugin sources (same as kora_cli.plugins):
    1. User plugins:    ~/.kora/plugins/<name>/dashboard/manifest.json
    2. Bundled plugins: <repo>/plugins/<name>/dashboard/manifest.json  (memory/, etc.)
    3. Project plugins: ./.kora/plugins/  (only if HERMES_ENABLE_PROJECT_PLUGINS)
    """
    plugins = []
    seen_names: set = set()

    from kora_cli.plugins import get_bundled_plugins_dir
    bundled_root = get_bundled_plugins_dir()
    search_dirs = [
        (get_kora_home() / "plugins", "user"),
        (bundled_root / "memory", "bundled"),
        (bundled_root, "bundled"),
    ]
    if os.environ.get("HERMES_ENABLE_PROJECT_PLUGINS"):
        search_dirs.append((Path.cwd() / ".kora" / "plugins", "project"))

    for plugins_root, source in search_dirs:
        if not plugins_root.is_dir():
            continue
        for child in sorted(plugins_root.iterdir()):
            if not child.is_dir():
                continue
            manifest_file = child / "dashboard" / "manifest.json"
            if not manifest_file.exists():
                continue
            try:
                data = json.loads(manifest_file.read_text(encoding="utf-8"))
                name = data.get("name", child.name)
                if name in seen_names:
                    continue
                seen_names.add(name)
                # Tab options: ``path`` + ``position`` for a new tab, optional
                # ``override`` to replace a built-in route, and ``hidden`` to
                # register the plugin component/slots without adding a tab
                # (useful for slot-only plugins like a header-crest injector).
                raw_tab = data.get("tab", {}) if isinstance(data.get("tab"), dict) else {}
                tab_info = {
                    "path": raw_tab.get("path", f"/{name}"),
                    "position": raw_tab.get("position", "end"),
                }
                override_path = raw_tab.get("override")
                if isinstance(override_path, str) and override_path.startswith("/"):
                    tab_info["override"] = override_path
                if bool(raw_tab.get("hidden")):
                    tab_info["hidden"] = True
                # Slots: list of named slot locations this plugin populates.
                # The frontend exposes ``registerSlot(pluginName, slotName, Component)``
                # on window; plugins with non-empty slots call it from their JS bundle.
                slots_src = data.get("slots")
                slots: List[str] = []
                if isinstance(slots_src, list):
                    slots = [s for s in slots_src if isinstance(s, str) and s]
                plugins.append({
                    "name": name,
                    "label": data.get("label", name),
                    "description": data.get("description", ""),
                    "icon": data.get("icon", "Puzzle"),
                    "version": data.get("version", "0.0.0"),
                    "tab": tab_info,
                    "slots": slots,
                    "entry": data.get("entry", "dist/index.js"),
                    "css": data.get("css"),
                    "has_api": bool(data.get("api")),
                    "source": source,
                    "_dir": str(child / "dashboard"),
                    "_api_file": data.get("api"),
                })
            except Exception as exc:
                _log.warning("Bad dashboard plugin manifest %s: %s", manifest_file, exc)
                continue
    return plugins


# Cache discovered plugins per-process (refresh on explicit re-scan).
_dashboard_plugins_cache: Optional[list] = None


def _get_dashboard_plugins(force_rescan: bool = False) -> list:
    global _dashboard_plugins_cache
    if _dashboard_plugins_cache is None or force_rescan:
        _dashboard_plugins_cache = _discover_dashboard_plugins()
    elif _dashboard_plugins_cache:
        if any(not Path(p["_dir"]).is_dir() for p in _dashboard_plugins_cache):
            _dashboard_plugins_cache = _discover_dashboard_plugins()
    return _dashboard_plugins_cache


@app.get("/api/dashboard/plugins")
async def get_dashboard_plugins():
    """Return discovered dashboard plugins (excludes user-hidden ones)."""
    plugins = _get_dashboard_plugins()
    # Read user's hidden plugins list from config.
    config = load_config()
    hidden: list = cfg_get(config, "dashboard", "hidden_plugins", default=[]) or []
    # Strip internal fields before sending to frontend and filter out hidden.
    return [
        {k: v for k, v in p.items() if not k.startswith("_")}
        for p in plugins
        if p["name"] not in hidden
    ]


@app.get("/api/dashboard/plugins/rescan")
async def rescan_dashboard_plugins():
    """Force re-scan of dashboard plugins."""
    plugins = _get_dashboard_plugins(force_rescan=True)
    return {"ok": True, "count": len(plugins)}


class _AgentPluginInstallBody(BaseModel):
    identifier: str
    force: bool = False
    enable: bool = True


def _strip_dashboard_manifest(p: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in p.items() if not k.startswith("_")}


def _merged_plugins_hub() -> Dict[str, Any]:
    """Agent discovery + dashboard manifests + optional provider picker metadata."""
    from kora_cli.plugins_cmd import (
        _discover_all_plugins,
        _get_current_context_engine,
        _get_current_memory_provider,
        _discover_context_engines,
        _discover_memory_providers,
        _get_disabled_set,
        _get_enabled_set,
        _read_manifest as _read_plugin_manifest_at,
    )

    dashboard_list = _get_dashboard_plugins()
    dash_by_name = {str(p["name"]): p for p in dashboard_list}

    disabled_set = _get_disabled_set()
    enabled_set = _get_enabled_set()

    # Read user-hidden plugins from config for the user_hidden field.
    config = load_config()
    hidden_plugins: list = cfg_get(config, "dashboard", "hidden_plugins", default=[]) or []

    plugins_root_resolved = (get_kora_home() / "plugins").resolve()
    rows: List[Dict[str, Any]] = []

    for name, version, description, source, dir_str in _discover_all_plugins():
        if name in disabled_set:
            runtime_status = "disabled"
        elif name in enabled_set:
            runtime_status = "enabled"
        else:
            runtime_status = "inactive"

        dir_path = Path(dir_str)
        dm = dash_by_name.get(name)
        has_dash_manifest = dm is not None or (dir_path / "dashboard" / "manifest.json").exists()

        under_user_tree = False
        try:
            dir_path.resolve().relative_to(plugins_root_resolved)
            under_user_tree = True
        except ValueError:
            pass

        can_remove_update = (
            source in {"user", "git"} and under_user_tree and Path(dir_str).is_dir()
        )

        # Check if this plugin provides tools that require auth
        auth_required = False
        auth_command = ""
        manifest_data = _read_plugin_manifest_at(dir_path)
        provides_tools = manifest_data.get("provides_tools") or []
        if provides_tools:
            try:
                from tools.registry import registry
                for tname in provides_tools:
                    entry = registry.get_entry(tname)
                    if entry and entry.check_fn and not entry.check_fn():
                        auth_required = True
                        auth_command = f"hermes auth {name}"
                        break
            except Exception:
                pass

        rows.append({
            "name": name,
            "version": version or "",
            "description": description or "",
            "source": source,
            "runtime_status": runtime_status,
            "has_dashboard_manifest": has_dash_manifest,
            "dashboard_manifest": _strip_dashboard_manifest(dm) if dm else None,
            "path": dir_str,
            "can_remove": can_remove_update,
            "can_update_git": can_remove_update and (Path(dir_str) / ".git").exists(),
            "auth_required": auth_required,
            "auth_command": auth_command,
            "user_hidden": name in hidden_plugins,
        })

    agent_names = {r["name"] for r in rows}
    orphan_dashboard = [
        _strip_dashboard_manifest(p)
        for p in dashboard_list
        if str(p["name"]) not in agent_names
    ]

    memory_providers: List[Dict[str, str]] = []
    try:
        for n, desc in _discover_memory_providers():
            memory_providers.append({"name": n, "description": desc})
    except Exception:
        memory_providers = []

    context_engines: List[Dict[str, str]] = []
    try:
        for n, desc in _discover_context_engines():
            context_engines.append({"name": n, "description": desc})
    except Exception:
        context_engines = []

    return {
        "plugins": rows,
        "orphan_dashboard_plugins": orphan_dashboard,
        "providers": {
            "memory_provider": _get_current_memory_provider() or "",
            "memory_options": memory_providers,
            "context_engine": _get_current_context_engine(),
            "context_options": context_engines,
        },
    }


@app.get("/api/dashboard/plugins/hub")
async def get_plugins_hub(request: Request):
    """Unified agent plugins + dashboard extension metadata (session protected)."""
    _require_token(request)
    try:
        return _merged_plugins_hub()
    except Exception as exc:
        _log.warning("plugins/hub failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to build plugins hub.") from exc


@app.post("/api/dashboard/agent-plugins/install")
async def post_agent_plugin_install(request: Request, body: _AgentPluginInstallBody):
    _require_token(request)
    from kora_cli.plugins_cmd import dashboard_install_plugin

    result = dashboard_install_plugin(
        body.identifier.strip(),
        force=body.force,
        enable=body.enable,
    )
    if not result.get("ok"):
        raise HTTPException(
            status_code=400,
            detail=result.get("error") or "Install failed.",
        )
    _get_dashboard_plugins(force_rescan=True)
    # Strip internal paths from the response
    result.pop("after_install_path", None)
    return result


def _validate_plugin_name(name: str) -> str:
    """Reject path-traversal attempts in plugin name URL parameters."""
    if not name or "/" in name or "\\" in name or ".." in name:
        raise HTTPException(status_code=400, detail="Invalid plugin name.")
    return name


@app.post("/api/dashboard/agent-plugins/{name}/enable")
async def post_agent_plugin_enable(request: Request, name: str):
    _require_token(request)
    name = _validate_plugin_name(name)
    from kora_cli.plugins_cmd import dashboard_set_agent_plugin_enabled

    result = dashboard_set_agent_plugin_enabled(name, enabled=True)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Enable failed.")
    return result


@app.post("/api/dashboard/agent-plugins/{name}/disable")
async def post_agent_plugin_disable(request: Request, name: str):
    _require_token(request)
    name = _validate_plugin_name(name)
    from kora_cli.plugins_cmd import dashboard_set_agent_plugin_enabled

    result = dashboard_set_agent_plugin_enabled(name, enabled=False)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Disable failed.")
    return result


@app.post("/api/dashboard/agent-plugins/{name}/update")
async def post_agent_plugin_update(request: Request, name: str):
    _require_token(request)
    name = _validate_plugin_name(name)
    from kora_cli.plugins_cmd import dashboard_update_user_plugin

    result = dashboard_update_user_plugin(name)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Update failed.")
    _get_dashboard_plugins(force_rescan=True)
    return result


@app.delete("/api/dashboard/agent-plugins/{name}")
async def delete_agent_plugin(request: Request, name: str):
    _require_token(request)
    name = _validate_plugin_name(name)
    from kora_cli.plugins_cmd import dashboard_remove_user_plugin

    result = dashboard_remove_user_plugin(name)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Remove failed.")
    _get_dashboard_plugins(force_rescan=True)
    return result


class _PluginProvidersPutBody(BaseModel):
    memory_provider: Optional[str] = None
    context_engine: Optional[str] = None


@app.put("/api/dashboard/plugin-providers")
async def put_plugin_providers(request: Request, body: _PluginProvidersPutBody):
    """Persist memory provider / context engine selection (writes config.yaml)."""
    _require_token(request)
    from kora_cli.plugins_cmd import (
        _save_context_engine,
        _save_memory_provider,
    )

    if body.memory_provider is not None:
        _save_memory_provider(body.memory_provider)
    if body.context_engine is not None:
        _save_context_engine(body.context_engine)
    return {"ok": True}


class _PluginVisibilityBody(BaseModel):
    hidden: bool


@app.post("/api/dashboard/plugins/{name}/visibility")
async def post_plugin_visibility(request: Request, name: str, body: _PluginVisibilityBody):
    """Toggle a plugin's sidebar visibility (persists to config.yaml dashboard.hidden_plugins)."""
    _require_token(request)
    name = _validate_plugin_name(name)

    config = load_config()
    if "dashboard" not in config or not isinstance(config.get("dashboard"), dict):
        config["dashboard"] = {}
    hidden_list: list = config["dashboard"].get("hidden_plugins") or []
    if not isinstance(hidden_list, list):
        hidden_list = []

    if body.hidden and name not in hidden_list:
        hidden_list.append(name)
    elif not body.hidden and name in hidden_list:
        hidden_list.remove(name)

    config["dashboard"]["hidden_plugins"] = hidden_list
    save_config(config)
    return {"ok": True, "name": name, "hidden": body.hidden}


@app.get("/dashboard-plugins/{plugin_name}/{file_path:path}")
async def serve_plugin_asset(plugin_name: str, file_path: str):
    """Serve static assets from a dashboard plugin directory.

    Only serves files from the plugin's ``dashboard/`` subdirectory.
    Path traversal is blocked by checking ``resolve().is_relative_to()``.
    """
    plugins = _get_dashboard_plugins()
    plugin = next((p for p in plugins if p["name"] == plugin_name), None)
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")

    base = Path(plugin["_dir"])
    target = (base / file_path).resolve()

    if not target.is_relative_to(base.resolve()):
        raise HTTPException(status_code=403, detail="Path traversal blocked")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    # Guess content type
    suffix = target.suffix.lower()
    content_types = {
        ".js": "application/javascript",
        ".mjs": "application/javascript",
        ".css": "text/css",
        ".json": "application/json",
        ".html": "text/html",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".woff2": "font/woff2",
        ".woff": "font/woff",
    }
    media_type = content_types.get(suffix, "application/octet-stream")
    return FileResponse(
        target,
        media_type=media_type,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


def _mount_plugin_api_routes():
    """Import and mount backend API routes from plugins that declare them.

    Each plugin's ``api`` field points to a Python file that must expose
    a ``router`` (FastAPI APIRouter).  Routes are mounted under
    ``/api/plugins/<name>/``.
    """
    for plugin in _get_dashboard_plugins():
        api_file_name = plugin.get("_api_file")
        if not api_file_name:
            continue
        api_path = Path(plugin["_dir"]) / api_file_name
        if not api_path.exists():
            _log.warning("Plugin %s declares api=%s but file not found", plugin["name"], api_file_name)
            continue
        try:
            module_name = f"hermes_dashboard_plugin_{plugin['name']}"
            spec = importlib.util.spec_from_file_location(module_name, api_path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            # Register in sys.modules BEFORE exec_module so pydantic/FastAPI
            # can resolve forward references (e.g. models defined in a file
            # that uses `from __future__ import annotations`). Without this,
            # TypeAdapter lazy-build fails at first request with
            # "is not fully defined" because the module namespace isn't
            # reachable by name for string-annotation resolution.
            sys.modules[module_name] = mod
            try:
                spec.loader.exec_module(mod)
            except Exception:
                sys.modules.pop(module_name, None)
                raise
            router = getattr(mod, "router", None)
            if router is None:
                _log.warning("Plugin %s api file has no 'router' attribute", plugin["name"])
                continue
            app.include_router(router, prefix=f"/api/plugins/{plugin['name']}")
            _log.info("Mounted plugin API routes: /api/plugins/%s/", plugin["name"])
        except Exception as exc:
            _log.warning("Failed to load plugin %s API routes: %s", plugin["name"], exc)


# Mount plugin API routes before the SPA catch-all.
_mount_plugin_api_routes()

mount_spa(app)


def start_server(
    host: str = "127.0.0.1",
    port: int = 9119,
    open_browser: bool = True,
    allow_public: bool = False,
    *,
    embedded_chat: bool = False,
):
    """Start the web UI server."""
    import uvicorn

    global _DASHBOARD_EMBEDDED_CHAT_ENABLED
    _DASHBOARD_EMBEDDED_CHAT_ENABLED = embedded_chat

    _LOCALHOST = ("127.0.0.1", "localhost", "::1")
    if host not in _LOCALHOST and not allow_public:
        raise SystemExit(
            f"Refusing to bind to {host} — the dashboard exposes API keys "
            f"and config without robust authentication.\n"
            f"Use --insecure to override (NOT recommended on untrusted networks)."
        )
    if host not in _LOCALHOST:
        _log.warning(
            "Binding to %s with --insecure — the dashboard has no robust "
            "authentication. Only use on trusted networks.", host,
        )

    # Record the bound host so host_header_middleware can validate incoming
    # Host headers against it. Defends against DNS rebinding (GHSA-ppp5-vxwm-4cf7).
    # bound_port is also stashed so /api/pty can build the back-WS URL the
    # PTY child uses to publish events to the dashboard sidebar.
    app.state.bound_host = host
    app.state.bound_port = port

    if open_browser:
        import webbrowser

        # On headless Linux (no DISPLAY or WAYLAND_DISPLAY) some registered
        # browsers are TUI programs (links, lynx, www-browser) that try to
        # take over the terminal.  That can send SIGHUP to the server process
        # and cause an immediate exit even though uvicorn bound successfully.
        # Skip the auto-open attempt on headless systems and let the user
        # open the URL manually.  macOS and Windows are always considered
        # display-capable.
        _has_display = (
            sys.platform != "linux"
            or bool(os.environ.get("DISPLAY"))
            or bool(os.environ.get("WAYLAND_DISPLAY"))
        )

        if _has_display:
            def _open():
                try:
                    time.sleep(1.0)
                    webbrowser.open(f"http://{host}:{port}")
                except Exception:
                    pass

            threading.Thread(target=_open, daemon=True).start()
        else:
            _log.debug(
                "Skipping browser-open: no DISPLAY or WAYLAND_DISPLAY detected "
                "(headless Linux). Pass --no-open to suppress this detection."
            )

    print(f"  Hermes Web UI → http://{host}:{port}")
    # proxy_headers=False so _ws_client_is_allowed sees the real connection peer
    # rather than X-Forwarded-For's rewritten value (which would defeat the
    # loopback gate when behind a reverse proxy).
    uvicorn.run(app, host=host, port=port, log_level="warning", proxy_headers=False)
