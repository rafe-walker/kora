"""Web admin UI listener (KR-D-DAEMON ST2 — listener "web").

Wraps the existing ``kora_cli.web_server.app`` FastAPI instance + binds
it via uvicorn programmatically (rather than ``uvicorn.run`` which
blocks). The daemon-coordinator pattern needs to await startup() +
shutdown() as coroutines; uvicorn's ``Server.serve()`` /
``Server.should_exit`` API supports this directly.

# Port + bind host

Bound to ``127.0.0.1:9119`` per R2 §5 — INTERNAL ONLY. Operator
reaches it via ``flyctl proxy 9119:9119 -a kora-runtime``. The
PUBLIC webhook listener on port 9118 is a separate FastAPI app
managed by a different listener (KR-D-DAEMON ST3).

# Env override

``KORA_WEB_HOST`` / ``KORA_WEB_PORT`` allow overriding for local
dev + tests. Defaults stay ``127.0.0.1:9119`` so production behavior
matches the bucket spec without env-var tuning.

# Shutdown shape

uvicorn's ``Server.should_exit = True`` causes ``serve()`` to return
on the next event-loop tick. Our shutdown() sets the flag + awaits
the serve-task; the per-listener timeout in the coordinator caps
how long that wait can run.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from kora_cli.daemon import DEFAULT_SHUTDOWN_TIMEOUT, register_daemon_listener

logger = logging.getLogger(__name__)


DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 9119


class WebListener:
    """Owns the uvicorn server task for the admin FastAPI app."""

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        # Late-bound: created in startup() so we don't import uvicorn
        # at module-import time (keeps `kora --help` fast).
        self._server = None  # uvicorn.Server | None
        self._serve_task: Optional[asyncio.Task] = None

    async def startup(self) -> None:
        import uvicorn

        from kora_cli.web_server import app

        config = uvicorn.Config(
            app,
            host=self._host,
            port=self._port,
            log_level="warning",
            # Match start_server's setting — see kora_cli/web_server.py:6484.
            # Disabling proxy_headers preserves the real connection peer for
            # loopback-only checks downstream.
            proxy_headers=False,
        )
        self._server = uvicorn.Server(config)
        self._serve_task = asyncio.create_task(
            self._server.serve(), name="web-listener:serve"
        )
        # Wait for the server to enter its serving state. uvicorn
        # exposes ``started`` flag after the bind succeeds.
        # We poll briefly rather than depending on internal events
        # (which differ across uvicorn versions).
        for _ in range(50):  # up to 1s
            if getattr(self._server, "started", False):
                break
            await asyncio.sleep(0.02)
        if not getattr(self._server, "started", False):
            # Either the bind is hanging or it failed — surface as a
            # startup failure so the coordinator unwinds. Don't await
            # the serve task here; the coordinator will do per-listener
            # shutdown next.
            raise RuntimeError(
                f"web listener: uvicorn did not enter started state on "
                f"{self._host}:{self._port} within 1s"
            )
        logger.info(
            "[kora.web] uvicorn bound on %s:%d", self._host, self._port
        )

    async def shutdown(self) -> None:
        if self._server is None or self._serve_task is None:
            return
        self._server.should_exit = True
        # Awaiting the serve task is the canonical "wait for uvicorn
        # to exit" pattern. The per-listener timeout in the coordinator
        # caps the wait.
        try:
            await self._serve_task
        except asyncio.CancelledError:
            pass
        logger.info("[kora.web] uvicorn stopped")


def _factory():
    """Mint a fresh WebListener using env overrides if present."""
    host = os.environ.get("KORA_WEB_HOST", DEFAULT_WEB_HOST).strip() or DEFAULT_WEB_HOST
    port_raw = os.environ.get("KORA_WEB_PORT", "").strip()
    try:
        port = int(port_raw) if port_raw else DEFAULT_WEB_PORT
    except ValueError as exc:
        raise SystemExit(
            f"KORA_WEB_PORT must be an int; got {port_raw!r}: {exc}"
        )
    listener = WebListener(host=host, port=port)
    return (listener.startup, listener.shutdown, DEFAULT_SHUTDOWN_TIMEOUT)


register_daemon_listener("web", _factory)
