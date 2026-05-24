"""Heartbeat-scheduled phrasebook promotion cycle — KR-PROMOTE-PHRASEBOOK-FOUNDATION
(Deliverable F registration side).

Registers ``run_phrasebook_promotion_cycle`` as a periodic task
against the heartbeat scheduler. Cadence is operator-tunable via
``KORA_PROMOTE_PHRASEBOOK_INTERVAL_SEC`` (default 86400s = once
daily). The kill-switch
``KORA_PROMOTE_PHRASEBOOK_ENABLED=false`` is checked inside the
cycle function so flipping the env at runtime takes effect on the
next tick without re-registering.

# Why a periodic-task interval, not a cron string

The bucket spec suggested ``"0 6 * * *"`` (6am UTC daily). The
existing heartbeat scheduler in ``listeners/heartbeat.py`` is
interval-based — it sleeps the specified seconds between fires.
There's no cron-string scheduler today. Wall-clock-anchored
scheduling (e.g., "fire at 6am UTC") would need a separate
"watch + act" pattern modeled on
``listeners/cost_telemetry_listener.py``'s daily-reset task
(check whether the boundary has crossed since the last fire,
fire if yes). That's reasonable for v2 if the operator wants
deterministic UTC anchor — but for v1 the simpler "fire once
every 24h" interval is sufficient: the proposer's outputs are
batch artifacts that go into a review queue, not real-time
events, so the exact wall-clock anchor doesn't matter
operationally.

Documented this decision verbatim in the bucket PR body so the
spec → impl divergence is explicit.

# Fail-soft startup

If ``run_phrasebook_promotion_cycle`` itself raises on a
particular tick, the heartbeat scheduler's ``_loop`` swallows
the exception + logs a warning + continues. No retry queue;
next tick (default 24h later) is the natural retry cadence —
this is a batch promotion loop, not an interactive surface.
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
from kora_cli.promote.phrasebook.cycle import (
    get_interval_seconds,
    run_phrasebook_promotion_cycle,
)

logger = logging.getLogger(__name__)


# KR-DAEMON-LISTENERS-VIA-GATEWAY Phase 2 — no-op lifecycle wrappers
# for the BackgroundDaemonRegistry entry. This listener is pure
# periodic-task (no daemon-state to hold); the Hermes-side
# registration carries the periodic task only — no Kora-side
# ``register_daemon_listener`` call has ever existed for this
# listener so there is no LISTENER_REGISTRY entry to bridge.


async def _startup_noop(coordinator=None) -> None:
    logger.debug(
        "[kora.promote.phrasebook.listener] startup (no-op; periodic "
        "task drives the work)"
    )


async def _shutdown_noop() -> None:
    logger.debug("[kora.promote.phrasebook.listener] shutdown (no-op)")


async def _periodic_task() -> None:
    """Thin async wrapper so the heartbeat scheduler's signature
    (``Callable[[], Awaitable[None]]``) is satisfied. Cycle's
    return value (the summary dict) is consumed locally + logged
    at INFO; the scheduler doesn't need to see it."""
    try:
        summary = await run_phrasebook_promotion_cycle()
        # Cycle already logs the summary at INFO; we don't
        # duplicate. Re-log here at DEBUG so operator can grep
        # this specific task name when triaging "did the cron
        # task even run today" without sifting cycle-internal
        # info lines.
        logger.debug(
            "[kora.promote.phrasebook.listener] tick complete: "
            "proposals_persisted=%d expired_count=%d duration_ms=%d",
            summary.get("proposals_persisted", 0),
            summary.get("expired_count", 0),
            summary.get("duration_ms", 0),
        )
    except Exception as exc:
        # Belt-and-suspenders — cycle is fail-soft, but the wrapper
        # catches anything that escapes (e.g., asyncio cancellation
        # during shutdown).
        logger.warning(
            "[kora.promote.phrasebook.listener] tick raised %r — "
            "next scheduled run will retry",
            exc,
        )


register_periodic_task(
    "promote_phrasebook_cycle",
    interval_seconds=float(get_interval_seconds()),
    callable=_periodic_task,
)


# ---------------------------------------------------------------------------
# Hermes-side registration (Phase 2; same Path B thin-shim semantics)
# ---------------------------------------------------------------------------

_hermes_entry = BackgroundDaemonEntry(
    name="promote_phrasebook",
    startup=_startup_noop,
    shutdown=_shutdown_noop,
    periodic_task=PeriodicTaskSpec(
        interval_seconds=float(get_interval_seconds()),
        callback=_periodic_task,
        name="promote_phrasebook_cycle",
    ),
    shutdown_timeout=DEFAULT_SHUTDOWN_TIMEOUT,
    plugin_name="kora",
)

try:
    background_daemon_registry().register(_hermes_entry)
except ValueError as _exc:
    logger.debug(
        "[kora.promote.phrasebook.listener] hermes registry already had "
        "'promote_phrasebook' entry: %s — skipping duplicate registration",
        _exc,
    )
