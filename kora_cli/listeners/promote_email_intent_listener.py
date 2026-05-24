"""Heartbeat-scheduled email-intent promotion cycle — KR-PROMOTE-EMAIL-INTENT.

Same listener shape as the other promotion-loop listeners. Cadence
operator-tunable via ``KORA_PROMOTE_EMAIL_INTENT_INTERVAL_SEC``
(default 86400s = 24h). Master kill-switch
``KORA_PROMOTE_EMAIL_INTENT_ENABLED=false`` checked inside the
cycle.
"""

from __future__ import annotations

import logging

from kora_cli.listeners.heartbeat import register_periodic_task
from kora_cli.promote.email_intent.plugin import (
    get_interval_seconds,
    run_email_intent_cycle,
)

logger = logging.getLogger(__name__)


async def _periodic_task() -> None:
    try:
        summary = await run_email_intent_cycle()
        logger.debug(
            "[kora.promote.email_intent.listener] tick complete: "
            "proposals_persisted=%d expired_count=%d duration_ms=%d",
            summary.get("proposals_persisted", 0),
            summary.get("expired_count", 0),
            summary.get("duration_ms", 0),
        )
    except Exception as exc:
        logger.warning(
            "[kora.promote.email_intent.listener] tick raised %r — "
            "next scheduled run will retry",
            exc,
        )


register_periodic_task(
    "promote_email_intent_cycle",
    interval_seconds=float(get_interval_seconds()),
    callable=_periodic_task,
)
