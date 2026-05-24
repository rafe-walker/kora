"""Heartbeat-scheduled snapshot-expand promotion cycle — KR-PROMOTE-SNAPSHOT-EXPAND.

Registers :func:`run_snapshot_expand_cycle` as a periodic task
against the heartbeat scheduler. Cadence operator-tunable via
``KORA_PROMOTE_SNAPSHOT_EXPAND_INTERVAL_SEC`` (default 86400s = once
daily). Master kill-switch
``KORA_PROMOTE_SNAPSHOT_EXPAND_ENABLED=false`` checked inside the
cycle so flipping the env at runtime takes effect on the next tick.

# Why a periodic interval, not a cron string

Same rationale as the phrasebook listener — the heartbeat scheduler
is interval-based; the bucket spec's ``"0 7 * * *"`` cron suggestion
is documented but not honored verbatim. Daily-interval is sufficient
for a batch promotion loop (proposals go into the audit JSONL for
operator review; not a real-time surface).

# Fail-soft startup

Cycle exceptions are swallowed by the heartbeat scheduler's
``_loop``; per-proposal failures are caught inside the cycle so
one bad proposal doesn't poison the batch.
"""

from __future__ import annotations

import logging

from kora_cli.listeners.heartbeat import register_periodic_task
from kora_cli.promote.snapshot_expand.cycle import (
    get_interval_seconds,
    run_snapshot_expand_cycle,
)

logger = logging.getLogger(__name__)


async def _periodic_task() -> None:
    """Thin async wrapper so the heartbeat scheduler's signature is
    satisfied. Cycle's summary dict logged at DEBUG so operator can
    grep this specific task name when triaging."""
    try:
        summary = await run_snapshot_expand_cycle()
        logger.debug(
            "[kora.promote.snapshot_expand.listener] tick complete: "
            "proposals_applied=%d auto_apply_mode=%s duration_ms=%d",
            summary.get("proposals_applied", 0),
            summary.get("auto_apply_mode", False),
            summary.get("duration_ms", 0),
        )
    except Exception as exc:
        # Belt-and-suspenders — cycle is fail-soft, but the wrapper
        # catches anything that escapes (e.g., asyncio cancellation
        # during shutdown).
        logger.warning(
            "[kora.promote.snapshot_expand.listener] tick raised %r — "
            "next scheduled run will retry",
            exc,
        )


register_periodic_task(
    "promote_snapshot_expand_cycle",
    interval_seconds=float(get_interval_seconds()),
    callable=_periodic_task,
)
