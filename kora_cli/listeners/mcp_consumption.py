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
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from agent.background_daemon_registry import (
    BackgroundDaemonEntry,
    PeriodicTaskSpec,
    background_daemon_registry,
)
from kora_cli.daemon import DEFAULT_SHUTDOWN_TIMEOUT, register_daemon_listener
from kora_cli.listeners.heartbeat import register_periodic_task
from kora_mcp.catalog import load_effective_catalog
from kora_mcp.pool import MCPCallFailed, MCPClientPool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ST2 — Health-check task config
# ---------------------------------------------------------------------------


DEFAULT_HEALTH_CHECK_INTERVAL_SEC: float = 300.0  # 5 min per §4 Q1 ruling
HEALTH_CHECK_INTERVAL_ENV: str = "KORA_MCP_HEALTH_CHECK_INTERVAL_SEC"


def _read_health_check_interval() -> float:
    """Read the per-cycle interval from env or fall back to default.

    Operator override via ``KORA_MCP_HEALTH_CHECK_INTERVAL_SEC``
    (Doppler-injectable). Invalid values (non-numeric, <=0) log
    WARN + fall back to the 300s default.
    """
    raw = os.environ.get(HEALTH_CHECK_INTERVAL_ENV, "").strip()
    if not raw:
        return DEFAULT_HEALTH_CHECK_INTERVAL_SEC
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "[kora.mcp_consumption] %s=%r is not numeric; using "
            "default %ss",
            HEALTH_CHECK_INTERVAL_ENV,
            raw,
            DEFAULT_HEALTH_CHECK_INTERVAL_SEC,
        )
        return DEFAULT_HEALTH_CHECK_INTERVAL_SEC
    if value <= 0:
        logger.warning(
            "[kora.mcp_consumption] %s=%s must be > 0; using "
            "default %ss",
            HEALTH_CHECK_INTERVAL_ENV,
            value,
            DEFAULT_HEALTH_CHECK_INTERVAL_SEC,
        )
        return DEFAULT_HEALTH_CHECK_INTERVAL_SEC
    return value


# ---------------------------------------------------------------------------
# ST2 — Health snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    """One per-endpoint health observation.

    Populated by :func:`run_health_check` per scheduled cycle.
    Cached in :data:`_health_cache` and surfaced through
    :func:`current_health_snapshots`.

    Attributes:
        connected: ``True`` if the endpoint's MCP session opened
            cleanly + ``list_tools()`` returned a result. ``False``
            on any transport / protocol failure.
        tools_count: Number of tools the endpoint exposed (when
            ``connected``). ``None`` when not connected.
        last_check_at: UTC time of the cycle that produced this
            snapshot. Used by the operator panel to detect stale
            data (snapshot older than the cadence → status visibly
            degraded).
        last_error: Operator-readable failure message (when not
            connected). ``None`` on success.
    """

    connected: bool
    tools_count: Optional[int]
    last_check_at: datetime
    last_error: Optional[str]


_health_cache: dict[str, HealthSnapshot] = {}


def current_health_snapshots() -> dict[str, HealthSnapshot]:
    """Return a copy of the per-endpoint snapshot cache.

    Read by the ``/api/mcp/clients/list`` endpoint to populate
    ``last_check_at`` / ``last_error`` / ``tools_count`` /
    ``status`` in the panel payload. Snapshot freshness is the
    panel's responsibility — compare ``last_check_at`` against the
    cadence to detect stale data.
    """
    return dict(_health_cache)


def _clear_health_cache() -> None:
    """Test hook + shutdown helper."""
    global _health_cache
    _health_cache = {}


async def run_health_check() -> None:
    """One pass: poll every endpoint in the current pool.

    Per-endpoint failure (timeout, transport, protocol) is captured
    on the snapshot — does NOT crash the heartbeat scheduler.
    Per-endpoint timeout is enforced by
    :meth:`MCPClientPool.list_tools` via the endpoint's configured
    ``timeout_seconds`` (default 30s — matches §4 Q1 ruling).

    Skips cleanly when no pool is running (daemon not yet started
    or already shut down). The scheduler keeps firing; cache stays
    empty until the listener is up.
    """
    pool = current_pool()
    if pool is None:
        logger.debug(
            "[kora.mcp_consumption] health-check skipped: no active pool"
        )
        return
    now = datetime.now(timezone.utc)
    for prefix in pool.endpoint_names():
        try:
            tools = await pool.list_tools(prefix)
        except MCPCallFailed as exc:
            _health_cache[prefix] = HealthSnapshot(
                connected=False,
                tools_count=None,
                last_check_at=now,
                last_error=str(exc),
            )
        except Exception as exc:
            # Defense in depth — unknown error type. Wrap so
            # operator sees the type prefix.
            _health_cache[prefix] = HealthSnapshot(
                connected=False,
                tools_count=None,
                last_check_at=now,
                last_error=f"{type(exc).__name__}: {exc}",
            )
        else:
            _health_cache[prefix] = HealthSnapshot(
                connected=True,
                tools_count=len(tools),
                last_check_at=now,
                last_error=None,
            )


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

    async def startup(self, coordinator=None) -> None:
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
        the pool's close_all logs WARN + continues.

        Also clears the ST2 health-snapshot cache so a subsequent
        daemon start sees a clean slate (avoids stale snapshots
        bleeding across restarts).
        """
        if self._pool is None:
            _clear_health_cache()
            return
        try:
            await self._pool.close_all()
        finally:
            _clear_singleton()
            _clear_health_cache()
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


# Process-wide singleton — KR-DAEMON-LISTENERS-VIA-GATEWAY Phase 2.
_listener_singleton = MCPConsumptionListener()


def _factory():
    return (
        _listener_singleton.startup,
        _listener_singleton.shutdown,
        DEFAULT_SHUTDOWN_TIMEOUT,
    )


register_daemon_listener("mcp_consumption", _factory)


# ---------------------------------------------------------------------------
# ST2 — Periodic health-check registration (import-time side effect)
# ---------------------------------------------------------------------------
#
# The heartbeat scheduler owns the asyncio.Task; we just register
# the callable + cadence. Cadence is read once at module-import
# time per the §4 Q1 ruling (5min default; KORA_MCP_HEALTH_CHECK_INTERVAL_SEC
# override). Restart-driven cadence-config refresh — matches the
# Q3 restart-driven contract for the pool itself.

register_periodic_task(
    "mcp.health_check",
    interval_seconds=_read_health_check_interval(),
    callable=run_health_check,
)


# ---------------------------------------------------------------------------
# Hermes-side registration (Phase 2; Path B thin-shim same as snapshot #196)
# ---------------------------------------------------------------------------

_hermes_entry = BackgroundDaemonEntry(
    name="mcp_consumption",
    startup=_listener_singleton.startup,
    shutdown=_listener_singleton.shutdown,
    periodic_task=PeriodicTaskSpec(
        interval_seconds=_read_health_check_interval(),
        callback=run_health_check,
        name="mcp.health_check",
    ),
    shutdown_timeout=DEFAULT_SHUTDOWN_TIMEOUT,
    plugin_name="kora",
)

try:
    background_daemon_registry().register(_hermes_entry)
except ValueError as _exc:
    logger.debug(
        "[kora.mcp_consumption] hermes registry already had "
        "'mcp_consumption' entry: %s — skipping duplicate registration",
        _exc,
    )
