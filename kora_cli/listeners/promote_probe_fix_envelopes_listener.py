"""Heartbeat-scheduled probe-fix-envelope promotion cycle — KR-PROMOTE-PROBE-FIX-ENVELOPES.

Same listener shape as the other promotion-loop listeners. Cadence
operator-tunable via ``KORA_PROMOTE_PROBE_FIX_INTERVAL_SEC``.

Auto-apply HARDCODED FALSE — see
:mod:`kora_cli.promote.probe_fix_envelopes` module docstring for
the safety rationale.
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
from kora_cli.promote.probe_fix_envelopes.plugin import (
    get_interval_seconds,
    run_probe_fix_envelopes_cycle,
)

logger = logging.getLogger(__name__)


# KR-DAEMON-LISTENERS-VIA-GATEWAY Phase 2.5 — no-op lifecycle wrappers
# for the BackgroundDaemonRegistry entry. Same pattern as
# promote_phrasebook + promote_snapshot_expand migrated in #199.


async def _startup_noop(coordinator=None) -> None:
    logger.debug(
        "[kora.promote.probe_fix_envelopes.listener] startup (no-op; "
        "periodic task drives the work)"
    )


async def _shutdown_noop() -> None:
    logger.debug(
        "[kora.promote.probe_fix_envelopes.listener] shutdown (no-op)"
    )


async def _periodic_task() -> None:
    try:
        summary = await run_probe_fix_envelopes_cycle()
        logger.debug(
            "[kora.promote.probe_fix_envelopes.listener] tick complete: "
            "proposals_persisted=%d expired_count=%d duration_ms=%d",
            summary.get("proposals_persisted", 0),
            summary.get("expired_count", 0),
            summary.get("duration_ms", 0),
        )
    except Exception as exc:
        logger.warning(
            "[kora.promote.probe_fix_envelopes.listener] tick raised "
            "%r — next scheduled run will retry",
            exc,
        )


register_periodic_task(
    "promote_probe_fix_envelopes_cycle",
    interval_seconds=float(get_interval_seconds()),
    callable=_periodic_task,
)


# ---------------------------------------------------------------------------
# Hermes-side registration (Phase 2.5; same Path B thin-shim semantics)
# ---------------------------------------------------------------------------

_hermes_entry = BackgroundDaemonEntry(
    name="promote_probe_fix_envelopes",
    startup=_startup_noop,
    shutdown=_shutdown_noop,
    periodic_task=PeriodicTaskSpec(
        interval_seconds=float(get_interval_seconds()),
        callback=_periodic_task,
        name="promote_probe_fix_envelopes_cycle",
    ),
    shutdown_timeout=DEFAULT_SHUTDOWN_TIMEOUT,
    plugin_name="kora",
)

try:
    background_daemon_registry().register(_hermes_entry)
except ValueError as _exc:
    logger.debug(
        "[kora.promote.probe_fix_envelopes.listener] hermes registry "
        "already had 'promote_probe_fix_envelopes' entry: %s — skipping "
        "duplicate registration",
        _exc,
    )
