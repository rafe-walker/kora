"""Pre-warmed snapshot daemon listener — KR-CHEAP-PRE-WARMED-SNAPSHOT.

Registers a periodic task with the heartbeat scheduler that runs
:func:`kora_cli.snapshot.run_snapshot_cycle` every
``KORA_SNAPSHOT_INTERVAL_SEC`` seconds (default 300 = 5 min, per
spec §2 cadence ``*/5 * * * *``).

# Design note: heartbeat-scheduler vs cron/jobs.py

The spec wording is "cron job" but ``cron/jobs.py`` is the
agent-driven cron (creates external worker processes per fire).
Using that for a cheap-substrate periodic compute would carry
heavyweight overhead for a job that's pure-Python in-process
state aggregation.

The existing pattern for in-daemon recurring compute is
``register_periodic_task(name, interval_seconds, callable)`` from
``kora_cli.listeners.heartbeat``. This is what MCP-CONSUMPTION
health-check, alert-notifier, email IMAP poll, and heartbeat
probes all use. The snapshot job slots in alongside them.

Same cadence ("every 5 min"); different mechanism. Spec §2(b)
explicitly allows "extend cron/jobs.py OR new kora_cli/snapshot/
__init__.py" — picking the latter for the simpler integration.

# Fail-soft

The snapshot listener does NOT carry any mutable state (no
in-memory cache, no client). Construction is a no-op. The cycle
function is fail-soft per its own contract — exceptions inside
log + skip, scheduler keeps ticking.
"""

from __future__ import annotations

import logging
import os

from kora_cli.daemon import DEFAULT_SHUTDOWN_TIMEOUT, register_daemon_listener
from kora_cli.listeners.heartbeat import register_periodic_task
from kora_cli.snapshot import run_snapshot_cycle

logger = logging.getLogger(__name__)


DEFAULT_INTERVAL_SEC: float = 300.0  # 5 min per spec §2(b)
INTERVAL_ENV: str = "KORA_SNAPSHOT_INTERVAL_SEC"


def _read_interval() -> float:
    """Resolve cadence from env with sane fallback.

    Mirrors :func:`kora_cli.listeners.alert_notifier_listener._read_interval`
    + email_inbound_imap_listener's _read_poll_interval — invalid
    values (non-numeric, <=0) WARN-log + fall back to default.
    """
    raw = os.environ.get(INTERVAL_ENV, "").strip()
    if not raw:
        return DEFAULT_INTERVAL_SEC
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "[kora.snapshot_listener] %s=%r is not numeric; using "
            "default %ss",
            INTERVAL_ENV,
            raw,
            DEFAULT_INTERVAL_SEC,
        )
        return DEFAULT_INTERVAL_SEC
    if value <= 0:
        logger.warning(
            "[kora.snapshot_listener] %s=%s must be > 0; using "
            "default %ss",
            INTERVAL_ENV,
            value,
            DEFAULT_INTERVAL_SEC,
        )
        return DEFAULT_INTERVAL_SEC
    return value


# ---------------------------------------------------------------------------
# Listener
# ---------------------------------------------------------------------------


class SnapshotListener:
    """Stateless listener — periodic task does all the work.

    The listener exists to bind the snapshot job into the daemon
    lifecycle (startup log line + shutdown log line) so operators
    can confirm via boot logs that the snapshot task is registered.
    """

    async def startup(self) -> None:
        logger.info(
            "[kora.snapshot_listener] snapshot periodic task registered; "
            "cadence=%ss",
            _read_interval(),
        )

    async def shutdown(self) -> None:
        logger.info("[kora.snapshot_listener] shutdown")


# ---------------------------------------------------------------------------
# Factory + registration (import-time side effect)
# ---------------------------------------------------------------------------


def _factory():
    listener = SnapshotListener()
    return (listener.startup, listener.shutdown, DEFAULT_SHUTDOWN_TIMEOUT)


register_daemon_listener("snapshot", _factory)


# Periodic-task registration — same shape as the other listeners
# that use the heartbeat scheduler for cheap in-process recurring
# compute.
register_periodic_task(
    "snapshot.compute",
    interval_seconds=_read_interval(),
    callable=run_snapshot_cycle,
)
