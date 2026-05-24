"""Heartbeat-scheduled router-tuning promotion cycle — KR-PROMOTE-ROUTER-TUNING.

Registers :func:`run_router_tuning_cycle` as a periodic task.
Cadence operator-tunable via
``KORA_PROMOTE_ROUTER_TUNING_INTERVAL_SEC`` (default 86400s = 24h).
Master kill-switch ``KORA_PROMOTE_ROUTER_TUNING_ENABLED=false``
checked inside the cycle so flipping the env at runtime takes
effect on the next tick.

# Why a periodic interval, not a cron string

Same rationale as the phrasebook + snapshot-expand listeners —
the heartbeat scheduler is interval-based; the bucket spec's
``"0 8 * * *"`` cron suggestion is documented but not honored
verbatim. Daily-interval is sufficient for a batch promotion
loop.

# Fail-soft

Cycle exceptions swallowed by the heartbeat scheduler's
``_loop``; per-proposal exceptions caught inside the cycle so one
bad proposal doesn't poison the batch.
"""

from __future__ import annotations

import logging

from agent.background_daemon_registry import (
    BackgroundDaemonEntry,
    PeriodicTaskSpec,
    background_daemon_registry,
)
from kora_cli.daemon import DEFAULT_SHUTDOWN_TIMEOUT
from kora_cli.listeners.heartbeat import register_periodic_task
from kora_cli.promote.router_tuning.plugin import (
    get_interval_seconds,
    run_router_tuning_cycle,
)

logger = logging.getLogger(__name__)


# KR-DAEMON-LISTENERS-VIA-GATEWAY Phase 2.5 — no-op lifecycle wrappers
# (same pattern as promote_phrasebook + promote_snapshot_expand from #199).


async def _startup_noop(coordinator=None) -> None:
    logger.debug(
        "[kora.promote.router_tuning.listener] startup (no-op; "
        "periodic task drives the work)"
    )


async def _shutdown_noop() -> None:
    logger.debug(
        "[kora.promote.router_tuning.listener] shutdown (no-op)"
    )


async def _periodic_task() -> None:
    try:
        summary = await run_router_tuning_cycle()
        logger.debug(
            "[kora.promote.router_tuning.listener] tick complete: "
            "proposals_persisted=%d expired_count=%d duration_ms=%d",
            summary.get("proposals_persisted", 0),
            summary.get("expired_count", 0),
            summary.get("duration_ms", 0),
        )
    except Exception as exc:
        logger.warning(
            "[kora.promote.router_tuning.listener] tick raised %r — "
            "next scheduled run will retry",
            exc,
        )


register_periodic_task(
    "promote_router_tuning_cycle",
    interval_seconds=float(get_interval_seconds()),
    callable=_periodic_task,
)


# ---------------------------------------------------------------------------
# Hermes-side registration (Phase 2.5; same Path B thin-shim semantics)
# ---------------------------------------------------------------------------

_hermes_entry = BackgroundDaemonEntry(
    name="promote_router_tuning",
    startup=_startup_noop,
    shutdown=_shutdown_noop,
    periodic_task=PeriodicTaskSpec(
        interval_seconds=float(get_interval_seconds()),
        callback=_periodic_task,
        name="promote_router_tuning_cycle",
    ),
    shutdown_timeout=DEFAULT_SHUTDOWN_TIMEOUT,
    plugin_name="kora",
)

try:
    background_daemon_registry().register(_hermes_entry)
except ValueError as _exc:
    logger.debug(
        "[kora.promote.router_tuning.listener] hermes registry already "
        "had 'promote_router_tuning' entry: %s — skipping duplicate "
        "registration",
        _exc,
    )
