"""Connection plumbing for the IsoKron memory provider.

Holds the Postgres asyncpg pool (read path) + an MCP client wrapper
(write path). Lazily initialized; the public surface is sync because
Hermes' MemoryProvider ABC is sync, while the underlying transports are
async. We run a dedicated asyncio event loop on a background daemon
thread and submit coroutines via ``run_coroutine_threadsafe`` — matches
the pattern in ``tools/mcp_tool.py`` (upstream Hermes' MCP client).

**KR-2 ST1 — skeleton.** Connection objects exist with correct shapes
and lifecycle hooks; calls into them raise ``NotImplementedError`` until
ST2 (reads) and ST3 (writes) land. The IO loop is real and starts during
``initialize()`` so smoke tests can exercise the lifecycle without
mocking the threading layer.

**Why a background loop instead of `asyncio.run` per call:**
asyncpg's connection pool benefits from being kept warm across calls;
re-creating it per call defeats the pool. The MCP stdio transport
likewise wants a persistent subprocess. The Hermes upstream chose
``threading.Thread + asyncio.new_event_loop()`` for the same reason in
``tools/mcp_tool.py``; we follow the same pattern.

Rule-6 honest label: KR-2 ST1's connection wrapper does NOT open any
real network connections. ``initialize()`` only starts the event loop.
ST2 actually opens the asyncpg pool; ST3 opens the MCP stdio /
HTTP transport.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Optional

from .config import IsoKronProviderConfig

logger = logging.getLogger(__name__)


class IsoKronConnectionError(RuntimeError):
    """Raised when the substrate connection is in an unusable state."""


class _DedicatedAsyncIOLoop:
    """Daemon-thread asyncio loop used by the IsoKron connection wrapper.

    Mirrors the ``_mcp_loop`` pattern in ``tools/mcp_tool.py`` upstream.
    Sync callers submit coroutines via ``submit()``; the loop runs them
    on its dedicated thread and returns concurrent.futures.Future for
    blocking waits.
    """

    def __init__(self, name: str = "kora-isokron-io"):
        self._name = name
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._stopped = threading.Event()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return  # idempotent
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=self._name,
            daemon=True,
        )
        self._thread.start()
        # Wait for the loop to enter run_forever — otherwise submit() races.
        if not self._ready.wait(timeout=5.0):
            raise IsoKronConnectionError(
                f"IsoKron IO loop {self._name} did not signal ready within 5s"
            )

    def _run_loop(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()
            self._stopped.set()

    def submit(self, coro):
        """Submit a coroutine to the loop; returns concurrent.futures.Future."""
        if self._loop is None:
            raise IsoKronConnectionError(
                "IsoKron IO loop not started — call start() first"
            )
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the loop + join the thread."""
        if self._loop is None or self._thread is None:
            return
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._stopped.wait(timeout=timeout)
        self._thread.join(timeout=timeout)
        self._loop = None
        self._thread = None
        self._ready.clear()
        self._stopped.clear()

    @property
    def is_running(self) -> bool:
        return (
            self._loop is not None
            and self._thread is not None
            and self._thread.is_alive()
            and self._loop.is_running()
        )


class IsoKronConnection:
    """Combined Postgres-pool + MCP-client wrapper.

    Lifecycle:
        __init__(config)    — store config, do not connect.
        start()             — start the dedicated IO loop. Does NOT open
                              the Postgres pool or MCP transport yet —
                              ST2 / ST3 add those.
        close()             — stop the loop, close the pool, terminate
                              the MCP transport.

    Read paths in ST2 will call ``self._submit_async(coro).result()``
    on this object's loop; write paths in ST3 likewise.
    """

    def __init__(self, config: IsoKronProviderConfig):
        self._config = config
        self._loop = _DedicatedAsyncIOLoop(name="kora-isokron-io")
        self._pg_pool: Any = None  # asyncpg.Pool — typed in ST2
        self._mcp_client: Any = None  # mcp.Client — typed in ST3
        self._started = False

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start the IO loop. Does NOT open real connections in ST1."""
        if self._started:
            return
        self._loop.start()
        self._started = True
        logger.info(
            "[isokron.connection] IO loop started (Rule-6: KR-2 ST1 "
            "skeleton — Postgres pool and MCP client not yet opened; "
            "ST2 + ST3 land those)."
        )

    def close(self) -> None:
        """Tear down loop + pool + MCP transport (idempotent)."""
        if not self._started:
            return
        # ST2 will close the pg pool; ST3 closes the MCP client.
        # For ST1 we just stop the loop.
        self._loop.stop()
        self._started = False
        logger.info("[isokron.connection] IO loop stopped")

    @property
    def is_started(self) -> bool:
        return self._started and self._loop.is_running

    # -- Async submission (sync wrapper around the dedicated loop) -----------

    def _submit_async(self, coro):
        """Submit a coroutine to the IO loop; return its future."""
        if not self.is_started:
            raise IsoKronConnectionError(
                "IsoKronConnection.start() must be called before any submit"
            )
        return self._loop.submit(coro)

    # -- Pool accessor (ST2 fills the implementation) ------------------------

    def pg_pool(self):
        """Return the asyncpg pool (lazy). KR-2 ST2 implements; ST1 raises."""
        if self._pg_pool is None:
            raise NotImplementedError(
                "[isokron.connection] Postgres pool not opened — "
                "KR-2 ST2 ships this. Rule-6: ST1 is a structural "
                "skeleton; no real reads happen yet."
            )
        return self._pg_pool

    # -- MCP client accessor (ST3 fills the implementation) ------------------

    def mcp_client(self):
        """Return the MCP client. KR-2 ST3 implements; ST1 raises."""
        if self._mcp_client is None:
            raise NotImplementedError(
                "[isokron.connection] MCP client not opened — "
                "KR-2 ST3 ships this. Rule-6: ST1 is a structural "
                "skeleton; no real writes happen yet."
            )
        return self._mcp_client
