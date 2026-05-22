"""MCP-consumption daemon listener (KR-MCP-CONSUMPTION ST1).

Bridges KR-MCP-1's :class:`kora_mcp.pool.MCPClientPool` to the
KR-D-DAEMON lifecycle harness. After this listener is registered:

  - Daemon boot instantiates the pool from the effective catalog
    (``kora_mcp.catalog.load_effective_catalog`` → defaults
    github + cloudflare + operator overrides from
    ``~/.kora/config.yaml``).
  - The pool is exposed via the module-level :func:`current_pool`
    accessor — mirrors the :func:`kora_cli.daemon.current_coordinator`
    pattern shipped in KR-D-DAEMON ST1 (PR #100/101).
  - Daemon shutdown calls :meth:`MCPClientPool.close_all` under a
    10-second timeout (matches the coordinator's per-listener
    default).

# Lazy startup, not eager (per §4 Q2 default)

Startup constructs the pool but does NOT open any transports. The
pool is lazy by design: connections open on first ``call_tool`` /
``list_tools_all``. Trade-off:

  - **Lazy (chosen)**: daemon boot is fast + doesn't block on
    slow remote MCPs. Connection failures don't fire until a
    caller actually uses an endpoint.
  - **Eager (rejected)**: daemon boot would block on every
    endpoint's transport open. A single slow / down MCP would
    fail the whole daemon startup.

ST2 (active health-check via heartbeat scheduler) opens the
connections periodically + caches the result so the operator
panel surfaces live state without paying connection latency on
the page render.

# Pool singleton vs per-daemon-restart (per §4 Q3 default)

Each daemon start rebuilds the pool from a fresh
``load_effective_catalog()`` read. Hot-reload of catalog config
would require an explicit pool-rebuild API; out of scope for now.
Operator can edit ``~/.kora/config.yaml`` + ``kora daemon
restart`` for a refresh.
"""

from __future__ import annotations

import logging
from typing import Optional

from kora_cli.daemon import DEFAULT_SHUTDOWN_TIMEOUT, register_daemon_listener
from kora_mcp.catalog import load_effective_catalog
from kora_mcp.pool import MCPClientPool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Listener
# ---------------------------------------------------------------------------


class MCPConsumptionListener:
    """Holds the live :class:`MCPClientPool` for the daemon's lifetime.

    The coordinator calls ``startup()`` at boot + ``shutdown()`` at
    shutdown (LIFO with other listeners). The held pool instance
    is exposed via :func:`current_pool`; the singleton in the
    module below is set by :meth:`startup` so accessors return
    ``None`` cleanly when no listener is running.
    """

    def __init__(self) -> None:
        self._pool: Optional[MCPClientPool] = None

    @property
    def pool(self) -> Optional[MCPClientPool]:
        return self._pool

    async def startup(self) -> None:
        """Build the pool from the effective catalog. Lazy — no
        transport opens at this point. Any pool construction error
        propagates so the coordinator can abort the daemon."""
        registry = load_effective_catalog()
        self._pool = MCPClientPool(registry)
        _set_singleton(self._pool)
        logger.info(
            "[kora.mcp_consumption] pool constructed with %d endpoint(s) "
            "(lazy — no connections opened): %s",
            len(registry.endpoints),
            ", ".join(e.name for e in registry.endpoints) or "(empty catalog)",
        )

    async def shutdown(self) -> None:
        """Close every cached pool session under the coordinator's
        per-listener shutdown timeout. Fail-soft on close errors —
        the pool's close_all logs WARN + continues."""
        if self._pool is None:
            return
        try:
            await self._pool.close_all()
        finally:
            _clear_singleton()
            self._pool = None
        logger.info("[kora.mcp_consumption] pool closed")


# ---------------------------------------------------------------------------
# Module-level singleton + accessor (mirrors current_coordinator pattern)
# ---------------------------------------------------------------------------


_pool_singleton: Optional[MCPClientPool] = None


def _set_singleton(pool: MCPClientPool) -> None:
    global _pool_singleton
    _pool_singleton = pool


def _clear_singleton() -> None:
    global _pool_singleton
    _pool_singleton = None


def current_pool() -> Optional[MCPClientPool]:
    """Return the live :class:`MCPClientPool`, or ``None``.

    ``None`` cases:
      - Daemon not running (no listener registered or running)
      - Listener registered but not yet started
      - Listener stopped (post-shutdown)

    Mirrors :func:`kora_cli.daemon.current_coordinator` — the
    cross-cutting accessor that surfaces from anywhere in the
    process without import-time coupling to the daemon module.
    """
    return _pool_singleton


# ---------------------------------------------------------------------------
# Factory + registration (import-time side effect)
# ---------------------------------------------------------------------------


def _factory():
    listener = MCPConsumptionListener()
    return (listener.startup, listener.shutdown, DEFAULT_SHUTDOWN_TIMEOUT)


register_daemon_listener("mcp_consumption", _factory)
