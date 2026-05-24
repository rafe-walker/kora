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

# KR-DAEMON-LISTENERS-VIA-GATEWAY Phase 1 — dual-registry migration

Per the audit at ``kora_docs/14_research/daemon_listeners_via_
gateway_2026-05-24/REPORT.md`` §5, this listener is the proof-
of-pattern migration target: pure periodic-task daemon with no
cross-cutting accessor + no startup-failure-FATAL semantic.

After this migration, the listener is registered against BOTH:

  1. **Kora's** ``LISTENER_REGISTRY`` (the existing
     ``register_daemon_listener("snapshot", _factory)`` call).
     Kept as a backward-compat shim — Kora's
     ``DaemonCoordinator`` still walks this registry today.
     Removed in Phase 6 (the dissolution phase).

  2. **Hermes's** ``BackgroundDaemonRegistry`` (the new
     ``background_daemon_registry().register(entry)`` call
     added by this migration). Lets future gateway consumers
     (KR-REASONING-ROUTE-THROUGH-GATEWAY follow-on) drive the
     listener lifecycle through the Hermes-side surface added
     in #172.

Both registrations point at the SAME ``SnapshotListener``
instance methods. The ``startup`` method now accepts an
optional ``coordinator`` kwarg so both consumer shapes work:
Kora's coordinator calls ``startup()`` (no arg, ignored kwarg
default applies); Hermes's consumer calls ``startup(coordinator)``
matching ``BackgroundDaemonEntry.startup: Callable[[Any], Any]``.

# Fail-soft

The snapshot listener does NOT carry any mutable state (no
in-memory cache, no client). Construction is a no-op. The cycle
function is fail-soft per its own contract — exceptions inside
log + skip, scheduler keeps ticking.
"""

from __future__ import annotations

import logging
import os

from agent.background_daemon_registry import (
    BackgroundDaemonEntry,
    PeriodicTaskSpec,
    background_daemon_registry,
)
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

    ``startup`` accepts an optional ``coordinator`` kwarg so both
    consumer shapes work: Kora's ``DaemonCoordinator`` calls it
    with no arg (the kwarg default applies); Hermes's
    ``BackgroundDaemonRegistry`` consumer calls it with the
    coordinator object per the ``StartupCallable`` type hint.
    Either way the listener is stateless w.r.t. the coordinator
    — it ignores the arg today and stays forward-compatible for
    any future use.
    """

    async def startup(self, coordinator=None) -> None:
        logger.info(
            "[kora.snapshot_listener] snapshot periodic task registered; "
            "cadence=%ss",
            _read_interval(),
        )

    async def shutdown(self) -> None:
        logger.info("[kora.snapshot_listener] shutdown")


# Process-wide singleton instance — both registry registrations
# below point at the same listener so behavior stays identical
# whether Kora's coordinator or the Hermes registry consumer
# drives the lifecycle. Phase 6 (dissolution) deletes the Kora-
# side registration; this singleton stays.
_listener_singleton = SnapshotListener()


# ---------------------------------------------------------------------------
# Factory + Kora-side registration (import-time side effect)
# ---------------------------------------------------------------------------


def _factory():
    """Kora ``LISTENER_REGISTRY`` factory — returns the lifecycle
    tuple Kora's ``DaemonCoordinator`` expects. Returns the
    singleton's bound methods so both registrations point at the
    same lifecycle hooks."""
    return (
        _listener_singleton.startup,
        _listener_singleton.shutdown,
        DEFAULT_SHUTDOWN_TIMEOUT,
    )


register_daemon_listener("snapshot", _factory)


# Periodic-task registration — same shape as the other listeners
# that use the heartbeat scheduler for cheap in-process recurring
# compute. Stays on the heartbeat scheduler today; Phase 4
# (scheduler dissolution) will migrate this to the
# ``BackgroundDaemonEntry.periodic_task`` consumer-driver wired
# into the gateway main loop.
register_periodic_task(
    "snapshot.compute",
    interval_seconds=_read_interval(),
    callable=run_snapshot_cycle,
)


# ---------------------------------------------------------------------------
# Hermes-side registration (KR-DAEMON-LISTENERS-VIA-GATEWAY Phase 1)
# ---------------------------------------------------------------------------
#
# Register against the Hermes ``BackgroundDaemonRegistry`` so any
# future consumer that walks ``background_daemon_registry().
# list_entries()`` (e.g. the gateway main loop landing in a
# follow-on bucket) gets the snapshot daemon's lifecycle hooks
# AND its periodic-task spec from a single source. The Kora-
# side ``register_periodic_task`` call above stays as-is —
# Path B (thin shim) keeps both wirings live until Phase 6
# dissolves the Kora-side LISTENER_REGISTRY.
#
# The Hermes entry's ``periodic_task`` carries the same callback
# the heartbeat scheduler runs today. Consumers that drive
# periodic_task themselves don't need to also register against
# Kora's heartbeat scheduler — they iterate the entry directly.

_hermes_entry = BackgroundDaemonEntry(
    name="snapshot",
    startup=_listener_singleton.startup,
    shutdown=_listener_singleton.shutdown,
    periodic_task=PeriodicTaskSpec(
        interval_seconds=_read_interval(),
        callback=run_snapshot_cycle,
        name="snapshot.compute",
    ),
    shutdown_timeout=DEFAULT_SHUTDOWN_TIMEOUT,
    plugin_name="kora",
)

try:
    background_daemon_registry().register(_hermes_entry)
except ValueError as _exc:
    # Defensive: re-import path (rare; happens in test fixtures
    # that re-import this module after a non-reset registry).
    # Production code path imports once; this branch is for
    # ``importlib.reload`` callers + xdist test workers that
    # share a process.
    logger.debug(
        "[kora.snapshot_listener] hermes registry already had "
        "'snapshot' entry: %s — skipping duplicate registration",
        _exc,
    )
