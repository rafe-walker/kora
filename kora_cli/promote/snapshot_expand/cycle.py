"""Snapshot-expand cycle orchestrator — KR-PROMOTE-SNAPSHOT-EXPAND.

Called by the periodic-task heartbeat (registered by
:mod:`kora_cli.listeners.promote_snapshot_expand_listener`).

# Cadence

Once daily at the heartbeat-scheduler's interval. The bucket spec
suggested ``"0 7 * * *"`` (7am UTC = 1h after the phrasebook
loop's 6am suggestion). The heartbeat scheduler is interval-based,
not cron-string, so we use ``KORA_PROMOTE_SNAPSHOT_EXPAND_INTERVAL_SEC``
(default 86400s = once daily) matching the phrasebook precedent.

# Env

  * ``KORA_PROMOTE_SNAPSHOT_EXPAND_ENABLED`` (default ``true``) —
    master kill-switch. False = cycle returns 0 proposals without
    reading observations.
  * ``KORA_PROMOTE_SNAPSHOT_EXPAND_AUTO_APPLY`` (default ``false``) —
    read by the applier; see its module docstring for safety
    posture.
  * ``KORA_PROMOTE_SNAPSHOT_EXPAND_INTERVAL_SEC`` (default ``86400``).
  * ``KORA_PROMOTE_SNAPSHOT_EXPAND_OBSERVATION_WINDOW_DAYS`` (default ``7``).
  * ``KORA_PROMOTE_SNAPSHOT_EXPAND_MIN_CLUSTER_SIZE`` (default ``5``).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from .applier import apply_proposal
from .observer import collect_recent_tool_calls
from .proposer import DEFAULT_MIN_CLUSTER_SIZE, generate_proposals

logger = logging.getLogger(__name__)


ENABLED_ENV = "KORA_PROMOTE_SNAPSHOT_EXPAND_ENABLED"
INTERVAL_SEC_ENV = "KORA_PROMOTE_SNAPSHOT_EXPAND_INTERVAL_SEC"
OBSERVATION_WINDOW_DAYS_ENV = (
    "KORA_PROMOTE_SNAPSHOT_EXPAND_OBSERVATION_WINDOW_DAYS"
)
MIN_CLUSTER_SIZE_ENV = "KORA_PROMOTE_SNAPSHOT_EXPAND_MIN_CLUSTER_SIZE"

DEFAULT_INTERVAL_SEC = 86400  # once daily
DEFAULT_OBSERVATION_WINDOW_DAYS = 7


def _is_enabled() -> bool:
    raw = os.environ.get(ENABLED_ENV, "true").strip().lower()
    return raw in {"true", "1", "yes", "on", ""}


def _int_env(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "[kora.promote.snapshot_expand.cycle] %s=%r is not an int — "
            "using default %d",
            name,
            raw,
            default,
        )
        return default
    if value < minimum:
        logger.warning(
            "[kora.promote.snapshot_expand.cycle] %s=%d below minimum %d "
            "— using default %d",
            name,
            value,
            minimum,
            default,
        )
        return default
    return value


def get_interval_seconds() -> int:
    return _int_env(INTERVAL_SEC_ENV, DEFAULT_INTERVAL_SEC, minimum=60)


async def run_snapshot_expand_cycle(
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """One cycle of the snapshot-expand promotion loop.

    Returns a summary dict the heartbeat scheduler logs at INFO:

      {
        "enabled": bool,
        "observations_read": int,
        "proposals_generated": int,
        "proposals_applied": int,
        "auto_apply_mode": bool,
        "started_at": ISO 8601 str,
        "duration_ms": int,
      }

    Caller is the heartbeat scheduler's per-task loop; failures
    are swallowed there. Inside this function, individual stages
    fail-soft so one stage's failure doesn't blank the summary.
    """
    started_dt = now or datetime.now(timezone.utc)
    started_monotonic = _monotonic_now()

    summary: Dict[str, Any] = {
        "enabled": True,
        "observations_read": 0,
        "proposals_generated": 0,
        "proposals_applied": 0,
        "auto_apply_mode": _read_auto_apply_for_summary(),
        "started_at": started_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_ms": 0,
    }

    if not _is_enabled():
        summary["enabled"] = False
        logger.info(
            "[kora.promote.snapshot_expand.cycle] disabled (%s=false) — "
            "skipping",
            ENABLED_ENV,
        )
        summary["duration_ms"] = int(
            (_monotonic_now() - started_monotonic) * 1000
        )
        return summary

    window_days = _int_env(
        OBSERVATION_WINDOW_DAYS_ENV,
        DEFAULT_OBSERVATION_WINDOW_DAYS,
        minimum=1,
    )
    min_cluster_size = _int_env(
        MIN_CLUSTER_SIZE_ENV, DEFAULT_MIN_CLUSTER_SIZE, minimum=2
    )

    try:
        observations = await collect_recent_tool_calls(
            since=started_dt - timedelta(days=window_days),
        )
        summary["observations_read"] = len(observations)
    except Exception as exc:
        logger.warning(
            "[kora.promote.snapshot_expand.cycle] observer failed: %r "
            "— no proposals generated",
            exc,
        )
        summary["duration_ms"] = int(
            (_monotonic_now() - started_monotonic) * 1000
        )
        return summary

    try:
        proposals = generate_proposals(
            observations,
            min_cluster_size=min_cluster_size,
            now=started_dt,
        )
        summary["proposals_generated"] = len(proposals)
    except Exception as exc:
        logger.warning(
            "[kora.promote.snapshot_expand.cycle] proposer failed: %r "
            "— no proposals applied",
            exc,
        )
        summary["duration_ms"] = int(
            (_monotonic_now() - started_monotonic) * 1000
        )
        return summary

    for proposal in proposals:
        try:
            apply_proposal(proposal)
            summary["proposals_applied"] += 1
        except Exception as exc:
            logger.warning(
                "[kora.promote.snapshot_expand.cycle] applier failed for "
                "%s: %r — proposal not recorded",
                proposal.proposal_id,
                exc,
            )

    summary["duration_ms"] = int(
        (_monotonic_now() - started_monotonic) * 1000
    )
    logger.info(
        "[kora.promote.snapshot_expand.cycle] cycle complete: %s",
        summary,
    )
    return summary


def _read_auto_apply_for_summary() -> bool:
    """Local helper so the cycle summary can surface the mode without
    importing the applier module's private helper. (Importing the
    applier's :func:`_is_auto_apply_enabled` would be cleaner but
    private-by-convention; this duplicates the 1-line check.)"""
    raw = os.environ.get(
        "KORA_PROMOTE_SNAPSHOT_EXPAND_AUTO_APPLY", "false"
    ).strip().lower()
    return raw in {"true", "1", "yes", "on"}


def _monotonic_now() -> float:
    import time as _time

    return _time.monotonic()
