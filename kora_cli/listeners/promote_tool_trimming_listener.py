"""Heartbeat-scheduled tool-trimming promotion cycle — KR-PROMOTE-TOOL-TRIMMING.

Same listener shape as the other promotion-loop listeners. Cadence
operator-tunable via ``KORA_PROMOTE_TOOL_TRIMMING_INTERVAL_SEC``.
"""

from __future__ import annotations

import logging

from kora_cli.listeners.heartbeat import register_periodic_task
from kora_cli.promote.tool_trimming.plugin import (
    get_interval_seconds,
    run_tool_trimming_cycle,
)

logger = logging.getLogger(__name__)


async def _periodic_task() -> None:
    try:
        summary = await run_tool_trimming_cycle()
        logger.debug(
            "[kora.promote.tool_trimming.listener] tick complete: "
            "proposals_persisted=%d expired_count=%d duration_ms=%d",
            summary.get("proposals_persisted", 0),
            summary.get("expired_count", 0),
            summary.get("duration_ms", 0),
        )
    except Exception as exc:
        logger.warning(
            "[kora.promote.tool_trimming.listener] tick raised %r — "
            "next scheduled run will retry",
            exc,
        )


register_periodic_task(
    "promote_tool_trimming_cycle",
    interval_seconds=float(get_interval_seconds()),
    callable=_periodic_task,
)
