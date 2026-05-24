"""Heartbeat-scheduled probe-fix-envelope promotion cycle — KR-PROMOTE-PROBE-FIX-ENVELOPES.

Same listener shape as the other promotion-loop listeners. Cadence
operator-tunable via ``KORA_PROMOTE_PROBE_FIX_INTERVAL_SEC``.

Auto-apply HARDCODED FALSE — see
:mod:`kora_cli.promote.probe_fix_envelopes` module docstring for
the safety rationale.
"""

from __future__ import annotations

import logging

from kora_cli.listeners.heartbeat import register_periodic_task
from kora_cli.promote.probe_fix_envelopes.plugin import (
    get_interval_seconds,
    run_probe_fix_envelopes_cycle,
)

logger = logging.getLogger(__name__)


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
