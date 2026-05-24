"""Email-intent cycle orchestrator — KR-PROMOTE-EMAIL-INTENT.

Called by the periodic-task heartbeat (registered by
:mod:`kora_cli.listeners.promote_email_intent_listener`).

# Env

  * ``KORA_PROMOTE_EMAIL_INTENT_ENABLED`` (default ``true``)
  * ``KORA_PROMOTE_EMAIL_INTENT_INTERVAL_SEC`` (default 86400 = 24h)
  * ``KORA_PROMOTE_EMAIL_INTENT_EXPIRY_DAYS`` (default 14)
  * ``KORA_PROMOTE_EMAIL_INTENT_WINDOW_DAYS`` (default 14)
  * Proposer-side: ``KORA_PROMOTE_EMAIL_INTENT_MIN_CLUSTER``,
    ``KORA_PROMOTE_EMAIL_INTENT_COHESION``

# Auto-apply

Default OFF per the email-intent risk profile (a bad regex could
silently auto-Sea_Ticket emails the operator didn't intend).
Operator scaffolds approved patterns manually into
``kora_cli/intent/email_to_sea_ticket.py``.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from kora_cli.promote._shared.proposal_store import (
    expire_older_than,
    save_pending,
)

from .observer import collect_recent_logged_only
from .proposer import (
    EmailIntentProposal,
    generate_proposals,
    proposal_to_dict,
)

logger = logging.getLogger(__name__)


LOOP_NAME = "email_intent"

ENABLED_ENV = "KORA_PROMOTE_EMAIL_INTENT_ENABLED"
INTERVAL_SEC_ENV = "KORA_PROMOTE_EMAIL_INTENT_INTERVAL_SEC"
EXPIRY_DAYS_ENV = "KORA_PROMOTE_EMAIL_INTENT_EXPIRY_DAYS"
OBSERVATION_WINDOW_DAYS_ENV = "KORA_PROMOTE_EMAIL_INTENT_WINDOW_DAYS"

DEFAULT_INTERVAL_SEC = 86400  # once daily
DEFAULT_EXPIRY_DAYS = 14
DEFAULT_OBSERVATION_WINDOW_DAYS = 14


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


def _emit_audit(proposal: EmailIntentProposal) -> None:
    try:
        from kora_cli.audit.jsonl_sink import emit_audit
    except Exception as exc:
        logger.warning(
            "[kora.promote.email_intent] audit import failed: %r — "
            "promotion.email_intent_pattern_proposed skipped",
            exc,
        )
        return
    payload = proposal_to_dict(proposal)
    payload["action"] = "proposed"  # v1 — auto-apply OFF
    try:
        emit_audit(
            "promotion.email_intent_pattern_proposed",
            payload,
            caller_session_id=(
                f"promotion:email_intent:{proposal.proposal_id}"
            ),
            source="email",
        )
    except Exception as exc:
        logger.warning(
            "[kora.promote.email_intent] emit_audit raised %r — "
            "proposal persisted; audit row missing",
            exc,
        )


async def run_email_intent_cycle(
    *, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """One cycle of the email-intent promotion loop."""
    started_dt = now or datetime.now(timezone.utc)
    started_monotonic = time.monotonic()

    summary: Dict[str, Any] = {
        "enabled": True,
        "observations_read": 0,
        "proposals_generated": 0,
        "proposals_persisted": 0,
        "expired_count": 0,
        "auto_apply_mode": False,  # v1 hardcoded; future env can flip
        "started_at": started_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_ms": 0,
    }

    if not _is_enabled():
        summary["enabled"] = False
        logger.info(
            "[kora.promote.email_intent] disabled (%s=false) — skipping",
            ENABLED_ENV,
        )
        summary["duration_ms"] = int(
            (time.monotonic() - started_monotonic) * 1000
        )
        return summary

    window_days = _int_env(
        OBSERVATION_WINDOW_DAYS_ENV,
        DEFAULT_OBSERVATION_WINDOW_DAYS,
        minimum=1,
    )

    try:
        observations = await collect_recent_logged_only(
            since=started_dt - timedelta(days=window_days),
        )
        summary["observations_read"] = len(observations)
    except Exception as exc:
        logger.warning(
            "[kora.promote.email_intent] observer failed: %r — "
            "no proposals generated",
            exc,
        )
        summary["duration_ms"] = int(
            (time.monotonic() - started_monotonic) * 1000
        )
        return summary

    try:
        proposals = await generate_proposals(observations, now=started_dt)
        summary["proposals_generated"] = len(proposals)
    except Exception as exc:
        logger.warning(
            "[kora.promote.email_intent] proposer failed: %r", exc
        )
        summary["duration_ms"] = int(
            (time.monotonic() - started_monotonic) * 1000
        )
        return summary

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
                "[kora.promote.email_intent] persist failed for "
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
            "[kora.promote.email_intent] expire_older_than raised %r",
            exc,
        )

    summary["duration_ms"] = int(
        (time.monotonic() - started_monotonic) * 1000
    )
    logger.info(
        "[kora.promote.email_intent] cycle complete: %s", summary
    )
    return summary
