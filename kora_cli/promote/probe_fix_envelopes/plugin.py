"""Probe-fix-envelope cycle orchestrator — KR-PROMOTE-PROBE-FIX-ENVELOPES.

Called by the periodic-task heartbeat (registered by
:mod:`kora_cli.listeners.promote_probe_fix_envelopes_listener`).

# Env

  * ``KORA_PROMOTE_PROBE_FIX_ENABLED`` (default ``true``)
  * ``KORA_PROMOTE_PROBE_FIX_INTERVAL_SEC`` (default 86400 = 24h)
  * ``KORA_PROMOTE_PROBE_FIX_EXPIRY_DAYS`` (default 14)
  * ``KORA_PROMOTE_PROBE_FIX_MIN_CLUSTER`` (default 3)

# Auto-apply

HARDCODED FALSE (no env). See module ``__init__`` for the
safety rationale.
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

from .observer import collect_recent_investigations
from .proposer import (
    ProbeEnvelopeProposal,
    generate_proposals,
    proposal_to_dict,
)

logger = logging.getLogger(__name__)


LOOP_NAME = "probe_fix_envelopes"

ENABLED_ENV = "KORA_PROMOTE_PROBE_FIX_ENABLED"
INTERVAL_SEC_ENV = "KORA_PROMOTE_PROBE_FIX_INTERVAL_SEC"
EXPIRY_DAYS_ENV = "KORA_PROMOTE_PROBE_FIX_EXPIRY_DAYS"
OBSERVATION_WINDOW_DAYS_ENV = "KORA_PROMOTE_PROBE_FIX_WINDOW_DAYS"

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


def _emit_audit(proposal: ProbeEnvelopeProposal) -> None:
    try:
        from kora_cli.audit.jsonl_sink import emit_audit
    except Exception as exc:
        logger.warning(
            "[kora.promote.probe_fix_envelopes] audit import failed: %r",
            exc,
        )
        return
    try:
        emit_audit(
            "promotion.probe_envelope_action_proposed",
            proposal_to_dict(proposal),
            caller_session_id=(
                f"promotion:probe_fix_envelopes:{proposal.proposal_id}"
            ),
            source="reasoning",
        )
    except Exception as exc:
        logger.warning(
            "[kora.promote.probe_fix_envelopes] emit_audit raised %r — "
            "proposal persisted; audit row missing",
            exc,
        )


async def run_probe_fix_envelopes_cycle(
    *, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """One cycle of the probe-fix-envelope promotion loop."""
    started_dt = now or datetime.now(timezone.utc)
    started_monotonic = time.monotonic()

    summary: Dict[str, Any] = {
        "enabled": True,
        "observations_read": 0,
        "proposals_generated": 0,
        "proposals_persisted": 0,
        "expired_count": 0,
        "started_at": started_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_ms": 0,
        # HARDCODED FALSE auto-apply for this loop — surface in
        # summary so cycle log makes the discipline explicit.
        "auto_apply_mode": False,
    }

    if not _is_enabled():
        summary["enabled"] = False
        logger.info(
            "[kora.promote.probe_fix_envelopes] disabled (%s=false) — "
            "skipping",
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
        observations = await collect_recent_investigations(
            since=started_dt - timedelta(days=window_days),
        )
        summary["observations_read"] = len(observations)
    except Exception as exc:
        logger.warning(
            "[kora.promote.probe_fix_envelopes] observer failed: %r", exc
        )
        summary["duration_ms"] = int(
            (time.monotonic() - started_monotonic) * 1000
        )
        return summary

    try:
        proposals = generate_proposals(observations, now=started_dt)
        summary["proposals_generated"] = len(proposals)
    except Exception as exc:
        logger.warning(
            "[kora.promote.probe_fix_envelopes] proposer failed: %r",
            exc,
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
                "[kora.promote.probe_fix_envelopes] persist failed for "
                "%s: %r",
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
            "[kora.promote.probe_fix_envelopes] expire_older_than "
            "raised %r",
            exc,
        )

    # KR-CC1-POLISH — auto-approve sweep AFTER fresh proposals
    # land. Order matters: a freshly-proposed low-risk proposal
    # spends its full wait window in pending before the NEXT
    # cycle's sweep picks it up. Sweep is a no-op when the
    # operator env opt-in is falsy (default).
    try:
        from .auto_approve import run_auto_approve_sweep

        sweep = run_auto_approve_sweep(now=started_dt)
        summary["auto_approved_low_risk_count"] = sweep.approved_count
        summary["auto_approve_candidates_considered"] = (
            sweep.candidates_considered
        )
        summary["auto_approve_under_wait_window"] = (
            sweep.candidates_under_wait_window
        )
    except Exception as exc:
        logger.warning(
            "[kora.promote.probe_fix_envelopes] auto_approve sweep "
            "raised %r — cycle continues",
            exc,
        )
        summary["auto_approved_low_risk_count"] = 0

    summary["duration_ms"] = int(
        (time.monotonic() - started_monotonic) * 1000
    )
    logger.info(
        "[kora.promote.probe_fix_envelopes] cycle complete: %s", summary
    )
    return summary
