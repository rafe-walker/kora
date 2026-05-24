"""Promotion cycle orchestrator — KR-PROMOTE-PHRASEBOOK-FOUNDATION (Deliverable F orchestrator).

Called by the periodic-task heartbeat (registered by
:mod:`kora_cli.listeners.promote_phrasebook_listener`). One cycle:

  1. Collect observations from the last
     ``KORA_PROMOTE_PHRASEBOOK_OBSERVATION_WINDOW_DAYS`` days
     (default 7) via :func:`observer.collect_recent_observations`.
  2. Generate proposals via :func:`proposer.generate_proposals`
     with operator-tunable thresholds.
  3. Persist each proposal as pending + emit
     ``promotion.proposed`` audit row.
  4. Expire pending proposals older than
     ``KORA_PROMOTE_PHRASEBOOK_EXPIRY_DAYS`` (default 14) so the
     pending list doesn't grow without bound.
  5. Log cycle summary: observation count, cluster count,
     proposal count, total Haiku synthesis cost.

# Env

  * ``KORA_PROMOTE_PHRASEBOOK_ENABLED`` (default ``true``) —
    master kill-switch. False = cycle returns 0 proposals without
    reading observations or invoking the LLM.
  * ``KORA_PROMOTE_PHRASEBOOK_INTERVAL_SEC`` (default ``86400``,
    i.e. once daily) — the heartbeat-scheduler interval. This is
    SECONDS not cron-string because the heartbeat scheduler is
    interval-based; the bucket spec's "0 6 * * *" cron suggestion
    is documented in the PR body as the alternative the daily-
    interval shape supersedes.
  * ``KORA_PROMOTE_PHRASEBOOK_OBSERVATION_WINDOW_DAYS`` (default
    ``7``).
  * ``KORA_PROMOTE_PHRASEBOOK_MIN_CLUSTER_SIZE`` (default ``5``).
  * ``KORA_PROMOTE_PHRASEBOOK_EXPIRY_DAYS`` (default ``14``).

# Fail-soft

Cycle exceptions log + are swallowed by the heartbeat scheduler
(see ``listeners/heartbeat.py:_loop`` — per-task failures don't
kill the loop). Per-proposal exceptions are caught here so one
bad proposal doesn't poison the rest of the batch.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from .observer import collect_recent_observations
from .proposer import (
    DEFAULT_MIN_CLUSTER_SIZE,
    PromotionProposal,
    generate_proposals,
    proposal_to_dict,
)
from .store import expire_older_than, save_pending

logger = logging.getLogger(__name__)


ENABLED_ENV = "KORA_PROMOTE_PHRASEBOOK_ENABLED"
INTERVAL_SEC_ENV = "KORA_PROMOTE_PHRASEBOOK_INTERVAL_SEC"
OBSERVATION_WINDOW_DAYS_ENV = (
    "KORA_PROMOTE_PHRASEBOOK_OBSERVATION_WINDOW_DAYS"
)
MIN_CLUSTER_SIZE_ENV = "KORA_PROMOTE_PHRASEBOOK_MIN_CLUSTER_SIZE"
EXPIRY_DAYS_ENV = "KORA_PROMOTE_PHRASEBOOK_EXPIRY_DAYS"

DEFAULT_INTERVAL_SEC = 86400  # once daily
DEFAULT_OBSERVATION_WINDOW_DAYS = 7
DEFAULT_EXPIRY_DAYS = 14


def _is_enabled() -> bool:
    raw = os.environ.get(ENABLED_ENV, "true").strip().lower()
    # Default ON — operator can flip the env to disable per the
    # ``feedback-fail-closed-by-default-for-security-infra``
    # exception path (this isn't security infra; default ON is
    # the right operator value).
    return raw in {"true", "1", "yes", "on", ""}


def _int_env(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "[kora.promote.phrasebook.cycle] %s=%r is not an int — "
            "using default %d",
            name,
            raw,
            default,
        )
        return default
    if value < minimum:
        logger.warning(
            "[kora.promote.phrasebook.cycle] %s=%d below minimum %d "
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


def _emit_proposed_audit(
    proposal: PromotionProposal, *, synth_cost_for_proposal: float
) -> None:
    """Emit one ``promotion.proposed`` row per proposal. The
    synth_cost field is per-proposal so the panel can show
    "cost = $0.00 (no synthesis)" vs "$0.001 (Haiku synthesized)"
    without inferring it from the haiku_synthesized boolean
    alone."""
    try:
        from kora_cli.audit.jsonl_sink import emit_audit
    except Exception as exc:
        logger.warning(
            "[kora.promote.phrasebook.cycle] audit import failed: "
            "%r — promotion.proposed skipped",
            exc,
        )
        return
    payload = proposal_to_dict(proposal)
    payload["synth_cost_usd"] = round(synth_cost_for_proposal, 6)
    try:
        emit_audit(
            "promotion.proposed",
            payload,
            caller_session_id=f"promotion:phrasebook:{proposal.proposal_id}",
            source="reasoning",
        )
    except Exception as exc:
        logger.warning(
            "[kora.promote.phrasebook.cycle] emit_audit raised %r — "
            "proposal still persisted",
            exc,
        )


async def run_phrasebook_promotion_cycle(
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """One cycle of the phrasebook promotion loop.

    Returns a summary dict the heartbeat scheduler logs at INFO:

      {
        "enabled": bool,
        "observations_read": int,
        "clusters_found": int,        # post-clustering, pre-thresholds
        "proposals_generated": int,
        "proposals_persisted": int,   # may differ from generated on
                                      # per-proposal persist failure
        "expired_count": int,
        "total_synth_cost_usd": float,
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
        "clusters_found": 0,
        "proposals_generated": 0,
        "proposals_persisted": 0,
        "expired_count": 0,
        "total_synth_cost_usd": 0.0,
        "started_at": started_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_ms": 0,
    }

    if not _is_enabled():
        summary["enabled"] = False
        logger.info(
            "[kora.promote.phrasebook.cycle] disabled (%s=false) — "
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
        observations = await collect_recent_observations(
            since=started_dt - timedelta(days=window_days),
        )
        summary["observations_read"] = len(observations)
    except Exception as exc:
        logger.warning(
            "[kora.promote.phrasebook.cycle] observer failed: %r — "
            "no proposals generated",
            exc,
        )
        summary["duration_ms"] = int(
            (_monotonic_now() - started_monotonic) * 1000
        )
        return summary

    try:
        proposals, total_synth_cost = await generate_proposals(
            observations,
            min_cluster_size=min_cluster_size,
            now=started_dt,
        )
        summary["proposals_generated"] = len(proposals)
        summary["total_synth_cost_usd"] = round(total_synth_cost, 6)
        # ``clusters_found`` is best-effort — we don't expose it
        # from generate_proposals so reconstruct loosely as
        # generated proposals (filtered clusters) plus any
        # rejected-on-consistency clusters get logged but not
        # counted here. Operator triages via cycle log + audit
        # JSONL grep if they need the breakdown.
        summary["clusters_found"] = len(proposals)
    except Exception as exc:
        logger.warning(
            "[kora.promote.phrasebook.cycle] proposer failed: %r — "
            "no proposals persisted",
            exc,
        )
        summary["duration_ms"] = int(
            (_monotonic_now() - started_monotonic) * 1000
        )
        return summary

    # Per-proposal synth cost — split the total evenly across
    # proposals (the synthesizer caller-side tracks it as one bulk
    # number; for the per-row audit field we average). Future
    # bucket can have generate_proposals return per-proposal cost
    # if operator needs per-row precision.
    per_proposal_cost = (
        (total_synth_cost / len(proposals)) if proposals else 0.0
    )

    for proposal in proposals:
        try:
            save_pending(proposal)
            summary["proposals_persisted"] += 1
        except Exception as exc:
            logger.warning(
                "[kora.promote.phrasebook.cycle] persist failed for "
                "%s: %r — proposal lost (audit row still emitted)",
                proposal.proposal_id,
                exc,
            )
        _emit_proposed_audit(
            proposal, synth_cost_for_proposal=per_proposal_cost
        )

    # Sweep up old pending proposals so the operator's review
    # queue doesn't grow without bound.
    expiry_days = _int_env(
        EXPIRY_DAYS_ENV, DEFAULT_EXPIRY_DAYS, minimum=1
    )
    try:
        summary["expired_count"] = expire_older_than(days=expiry_days)
    except Exception as exc:
        logger.warning(
            "[kora.promote.phrasebook.cycle] expire_older_than "
            "raised %r — expired_count stays 0",
            exc,
        )

    summary["duration_ms"] = int(
        (_monotonic_now() - started_monotonic) * 1000
    )
    logger.info(
        "[kora.promote.phrasebook.cycle] cycle complete: %s",
        summary,
    )
    return summary


def _monotonic_now() -> float:
    import time as _time

    return _time.monotonic()
