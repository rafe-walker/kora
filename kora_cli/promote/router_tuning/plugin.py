"""Router-tuning cycle orchestrator — KR-PROMOTE-ROUTER-TUNING.

Called by the periodic-task heartbeat (registered by
:mod:`kora_cli.listeners.promote_router_tuning_listener`). One
cycle:

  1. Read rolling-24h escalation rollups via
     :func:`observer.collect_route_rollups`.
  2. Generate proposals via :func:`proposer.generate_proposals`.
  3. Persist each proposal via the shared store + emit
     ``promotion.router_trigger_proposed`` audit row.
  4. Expire pending proposals older than ``EXPIRY_DAYS_ENV``
     (default 14) so the operator's review queue stays bounded.
  5. Log cycle summary.

# Env

  * ``KORA_PROMOTE_ROUTER_TUNING_ENABLED`` (default ``true``)
  * ``KORA_PROMOTE_ROUTER_TUNING_INTERVAL_SEC`` (default 86400 = 24h)
  * ``KORA_PROMOTE_ROUTER_TUNING_EXPIRY_DAYS`` (default 14)

# Fail-soft

Cycle exceptions log + are swallowed by the heartbeat scheduler.
Per-proposal exceptions caught so one bad proposal doesn't poison
the batch.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

_ONE_DAY = timedelta(days=1)

from kora_cli.promote._shared.proposal_store import (
    expire_older_than,
    save_pending,
)

from .observer import collect_route_overrides, collect_route_rollups
from .proposer import (
    RouterTuningProposal,
    generate_loosen_proposals,
    generate_proposals,
    proposal_to_dict,
)

logger = logging.getLogger(__name__)


LOOP_NAME = "router_tuning"

ENABLED_ENV = "KORA_PROMOTE_ROUTER_TUNING_ENABLED"
INTERVAL_SEC_ENV = "KORA_PROMOTE_ROUTER_TUNING_INTERVAL_SEC"
EXPIRY_DAYS_ENV = "KORA_PROMOTE_ROUTER_TUNING_EXPIRY_DAYS"

DEFAULT_INTERVAL_SEC = 86400  # once daily
DEFAULT_EXPIRY_DAYS = 14


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
        return default
    if value < minimum:
        return default
    return value


def get_interval_seconds() -> int:
    return _int_env(INTERVAL_SEC_ENV, DEFAULT_INTERVAL_SEC, minimum=60)


def _emit_audit(proposal: RouterTuningProposal) -> None:
    try:
        from kora_cli.audit.jsonl_sink import emit_audit
    except Exception as exc:
        logger.warning(
            "[kora.promote.router_tuning] audit import failed: %r — "
            "promotion.router_trigger_proposed skipped",
            exc,
        )
        return
    try:
        emit_audit(
            "promotion.router_trigger_proposed",
            proposal_to_dict(proposal),
            caller_session_id=(
                f"promotion:router_tuning:{proposal.proposal_id}"
            ),
            source="reasoning",
        )
    except Exception as exc:
        logger.warning(
            "[kora.promote.router_tuning] emit_audit raised %r — "
            "proposal persisted; audit row missing",
            exc,
        )


async def run_router_tuning_cycle(
    *, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """One cycle of the router-tuning promotion loop.

    Returns a summary dict the heartbeat scheduler logs at INFO.
    """
    started_dt = now or datetime.now(timezone.utc)
    started_monotonic = time.monotonic()

    summary: Dict[str, Any] = {
        "enabled": True,
        "rollups_observed": 0,
        # KR-PROMOTE-ROUTER-LOOSEN-AUDIT-ROW — separate counter so
        # operator can see tighten vs loosen volume in one cycle log.
        "overrides_observed": 0,
        "proposals_generated": 0,
        "proposals_persisted": 0,
        "expired_count": 0,
        "started_at": started_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_ms": 0,
    }

    if not _is_enabled():
        summary["enabled"] = False
        logger.info(
            "[kora.promote.router_tuning] disabled (%s=false) — skipping",
            ENABLED_ENV,
        )
        summary["duration_ms"] = int(
            (time.monotonic() - started_monotonic) * 1000
        )
        return summary

    try:
        rollups = collect_route_rollups()
        summary["rollups_observed"] = len(rollups)
    except Exception as exc:
        logger.warning(
            "[kora.promote.router_tuning] observer failed: %r — "
            "no proposals generated",
            exc,
        )
        summary["duration_ms"] = int(
            (time.monotonic() - started_monotonic) * 1000
        )
        return summary

    try:
        proposals = generate_proposals(rollups, now=started_dt)
    except Exception as exc:
        logger.warning(
            "[kora.promote.router_tuning] tighten proposer failed: %r",
            exc,
        )
        proposals = []

    # KR-PROMOTE-ROUTER-LOOSEN-AUDIT-ROW — loosen-path arm. Reads
    # opus_override.applied audit since the cycle's started_at - 24h
    # (mirrors the tighten-path's rolling_24h cost-telemetry window
    # so the two arms see comparable observation windows).
    try:
        override_rollups = collect_route_overrides(
            since=started_dt - _ONE_DAY,
        )
        summary["overrides_observed"] = len(override_rollups)
    except Exception as exc:
        logger.warning(
            "[kora.promote.router_tuning] loosen observer failed: %r",
            exc,
        )
        override_rollups = []

    try:
        loosen_proposals = generate_loosen_proposals(
            override_rollups, now=started_dt
        )
        proposals = proposals + loosen_proposals
    except Exception as exc:
        logger.warning(
            "[kora.promote.router_tuning] loosen proposer failed: %r",
            exc,
        )

    summary["proposals_generated"] = len(proposals)

    for proposal in proposals:
        try:
            save_pending(
                loop_name=LOOP_NAME,
                proposal_id=proposal.proposal_id,
                payload=proposal_to_dict(proposal),
            )
            summary["proposals_persisted"] += 1
        except Exception as exc:
            logger.warning(
                "[kora.promote.router_tuning] persist failed for "
                "%s: %r — proposal lost (audit row still emitted)",
                proposal.proposal_id,
                exc,
            )
        _emit_audit(proposal)

    try:
        summary["expired_count"] = expire_older_than(
            loop_name=LOOP_NAME,
            days=_int_env(EXPIRY_DAYS_ENV, DEFAULT_EXPIRY_DAYS, minimum=1),
        )
    except Exception as exc:
        logger.warning(
            "[kora.promote.router_tuning] expire_older_than raised %r",
            exc,
        )

    summary["duration_ms"] = int(
        (time.monotonic() - started_monotonic) * 1000
    )
    logger.info(
        "[kora.promote.router_tuning] cycle complete: %s", summary
    )
    return summary
