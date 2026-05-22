"""Heartbeat-probes daemon listener (KR-FEAT-HEARTBEAT ST1).

Wires the 5 service probes into the daemon lifecycle:

  - Daemon boot registers (via the daemon-listener side) — there's
    no per-listener state to construct (probes are created per
    cycle by the runner); startup is a clean no-op + LOG line.
  - The heartbeat scheduler runs :func:`run_all_probes_scheduled`
    every :func:`_read_probe_interval` seconds (default 300s,
    operator override via ``KORA_HEARTBEAT_PROBE_INTERVAL_SEC``).
  - Daemon shutdown clears the snapshot cache so a stale
    pre-restart snapshot doesn't bleed into the post-restart
    panel view.

# Two distinct heartbeat tasks

KR-MCP-CONSUMPTION ST2 registered ``mcp.health_check`` at 5min;
this registers ``heartbeat.service_probes`` at 5min. Per §4 Q1
ruling: keep them separate so one slow probe doesn't backpressure
the other. Both share the cadence default but each can be tuned
via its own env var.
"""

from __future__ import annotations

import logging
import os

from kora_cli.daemon import DEFAULT_SHUTDOWN_TIMEOUT, register_daemon_listener
from kora_cli.heartbeat_probes.runner import (
    _clear_snapshot_cache,
    run_all_probes_scheduled,
)
from kora_cli.listeners.heartbeat import register_periodic_task

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cadence configuration
# ---------------------------------------------------------------------------


DEFAULT_PROBE_INTERVAL_SEC: float = 300.0  # 5 min per §4 Q1 ruling
PROBE_INTERVAL_ENV: str = "KORA_HEARTBEAT_PROBE_INTERVAL_SEC"


def _read_probe_interval() -> float:
    """Read the probe-cycle interval from env or fall back to default.

    Invalid (non-numeric, ≤0) values WARN-log + return the default.
    Matches the KR-MCP-CONSUMPTION ST2 env-validation pattern.
    """
    raw = os.environ.get(PROBE_INTERVAL_ENV, "").strip()
    if not raw:
        return DEFAULT_PROBE_INTERVAL_SEC
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "[kora.heartbeat_probes] %s=%r not numeric; using default %ss",
            PROBE_INTERVAL_ENV,
            raw,
            DEFAULT_PROBE_INTERVAL_SEC,
        )
        return DEFAULT_PROBE_INTERVAL_SEC
    if value <= 0:
        logger.warning(
            "[kora.heartbeat_probes] %s=%s must be > 0; using default %ss",
            PROBE_INTERVAL_ENV,
            value,
            DEFAULT_PROBE_INTERVAL_SEC,
        )
        return DEFAULT_PROBE_INTERVAL_SEC
    return value


# ---------------------------------------------------------------------------
# Listener
# ---------------------------------------------------------------------------


class HeartbeatProbesListener:
    """Lifecycle anchor for the probe scheduler.

    Probes themselves are constructed per cycle by the runner —
    listener has no per-instance state. Startup is a log line
    confirming the listener is registered; shutdown clears the
    snapshot cache so a stale snapshot doesn't survive daemon
    restart.
    """

    async def startup(self) -> None:
        interval = _read_probe_interval()
        logger.info(
            "[kora.heartbeat_probes] listener active; probe cycle every %ss "
            "(KORA_HEARTBEAT_PROBE_INTERVAL_SEC override)",
            interval,
        )

    async def shutdown(self) -> None:
        _clear_snapshot_cache()
        logger.info("[kora.heartbeat_probes] snapshot cache cleared")


# ---------------------------------------------------------------------------
# Factory + registration
# ---------------------------------------------------------------------------


def _factory():
    listener = HeartbeatProbesListener()
    return (listener.startup, listener.shutdown, DEFAULT_SHUTDOWN_TIMEOUT)


register_daemon_listener("heartbeat_probes", _factory)


# ---------------------------------------------------------------------------
# Periodic task registration (import-time side effect)
# ---------------------------------------------------------------------------

register_periodic_task(
    "heartbeat.service_probes",
    interval_seconds=_read_probe_interval(),
    callable=run_all_probes_scheduled,
)
