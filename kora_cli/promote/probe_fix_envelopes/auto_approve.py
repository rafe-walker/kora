"""Auto-approve sweep for low-risk envelope proposals — KR-CC1-POLISH.

Optional opt-in sweep that runs after the main probe-fix-envelope
cycle. Auto-approves proposals whose ``blast_radius_level == "low"``
after a configurable wait window (default 1h), giving the operator
a chance to manually reject before the auto-approve fires.

# Two-tier gating (CRITICAL)

This sweep auto-approves the *proposal* — i.e. the proposed
envelope action moves from ``pending/`` to ``approved/`` + an
audit row fires. It does NOT auto-EXECUTE the envelope action.
The envelope still requires the operator to flip the per-probe
enable env (``KORA_PROBE_AUTOFIX_<NAME>_ENABLED=true``, see
``kora_cli/probes/fix_envelopes.py``) before Kora's reasoning
loop will actually invoke the fix at runtime.

The two tiers are:
  1. Auto-approve → "this is in our envelope vocabulary"
  2. Per-probe ENABLED env → "Kora is permitted to invoke it"

# Env

  * ``KORA_PROMOTE_PROBE_FIX_AUTO_APPROVE_LOW_RISK`` (default
    ``false``) — master opt-in. False = sweep is a no-op.
  * ``KORA_PROMOTE_PROBE_FIX_AUTO_APPROVE_WAIT_HOURS`` (default
    ``1.0``) — minimum time a low-risk proposal must sit in
    ``pending/`` before auto-approval. Operator's review window.

# When to run

The sweep is invoked at the end of each probe-fix-envelope cycle
in ``plugin.py`` AFTER fresh proposals have been persisted. That
way a freshly-proposed low-risk proposal still spends its full
wait window in pending before the next sweep picks it up.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List

from kora_cli.promote._shared.proposal_store import (
    list_by_status,
    transition,
)

from .proposer import (
    ProbeEnvelopeProposal,
    proposal_from_dict,
    proposal_to_dict,
)

logger = logging.getLogger(__name__)


LOOP_NAME = "probe_fix_envelopes"

AUTO_APPROVE_ENABLED_ENV = "KORA_PROMOTE_PROBE_FIX_AUTO_APPROVE_LOW_RISK"
AUTO_APPROVE_WAIT_HOURS_ENV = (
    "KORA_PROMOTE_PROBE_FIX_AUTO_APPROVE_WAIT_HOURS"
)
DEFAULT_AUTO_APPROVE_WAIT_HOURS = 1.0


@dataclass(frozen=True, slots=True)
class AutoApproveSweepResult:
    """Per-sweep telemetry. Cycle aggregator stores ``approved_count``
    in its summary so operator-grep can find sweep activity."""

    candidates_considered: int  # all low-risk pending
    candidates_under_wait_window: int  # low-risk but < wait_hours old
    approved_count: int


def is_auto_approve_enabled() -> bool:
    raw = os.environ.get(AUTO_APPROVE_ENABLED_ENV, "false").strip().lower()
    return raw in {"true", "1", "yes", "on"}


def _read_wait_hours() -> float:
    raw = os.environ.get(AUTO_APPROVE_WAIT_HOURS_ENV, "").strip()
    if not raw:
        return DEFAULT_AUTO_APPROVE_WAIT_HOURS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "[kora.promote.probe_fix_envelopes.auto_approve] %s=%r not "
            "numeric; using default %sh",
            AUTO_APPROVE_WAIT_HOURS_ENV,
            raw,
            DEFAULT_AUTO_APPROVE_WAIT_HOURS,
        )
        return DEFAULT_AUTO_APPROVE_WAIT_HOURS
    if value < 0:
        return DEFAULT_AUTO_APPROVE_WAIT_HOURS
    return value


def _emit_auto_approved_audit(
    proposal: ProbeEnvelopeProposal, *, wait_hours: float, approved_at: datetime
) -> None:
    """Emit ``promotion.probe_envelope_action_auto_approved`` per
    auto-approved proposal. Best-effort: any audit-write failure
    logs + is swallowed (the transition already succeeded)."""
    try:
        from kora_cli.audit.jsonl_sink import emit_audit
    except Exception as exc:
        logger.warning(
            "[kora.promote.probe_fix_envelopes.auto_approve] audit "
            "import failed: %r — auto_approved row skipped",
            exc,
        )
        return
    payload = proposal_to_dict(proposal)
    payload["status"] = "approved"
    payload["auto_approve_wait_hours"] = round(wait_hours, 4)
    payload["auto_approved_at"] = approved_at.strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    try:
        emit_audit(
            "promotion.probe_envelope_action_auto_approved",
            payload,
            caller_session_id=(
                f"promotion:probe_fix_envelopes:{proposal.proposal_id}"
            ),
            source="reasoning",
        )
    except Exception as exc:
        logger.warning(
            "[kora.promote.probe_fix_envelopes.auto_approve] emit_audit "
            "raised %r — proposal already transitioned",
            exc,
        )


def run_auto_approve_sweep(
    *, now: datetime | None = None
) -> AutoApproveSweepResult:
    """Walk pending proposals + auto-approve low-risk ones whose age
    crossed the wait window.

    No-op when ``AUTO_APPROVE_ENABLED_ENV`` is falsy. Returns a
    structured result so the caller (cycle aggregator) can surface
    sweep activity in its summary log.
    """
    if not is_auto_approve_enabled():
        return AutoApproveSweepResult(
            candidates_considered=0,
            candidates_under_wait_window=0,
            approved_count=0,
        )

    wait_hours = _read_wait_hours()
    wait_seconds = wait_hours * 3600.0
    now_dt = now or datetime.now(timezone.utc)

    pending_payloads = list_by_status(
        loop_name=LOOP_NAME, status="pending"
    )
    candidates: List[ProbeEnvelopeProposal] = []
    for payload in pending_payloads:
        try:
            proposal = proposal_from_dict(payload)
        except Exception as exc:
            logger.warning(
                "[kora.promote.probe_fix_envelopes.auto_approve] "
                "proposal_from_dict raised %r — skipping",
                exc,
            )
            continue
        if proposal.blast_radius_level != "low":
            continue
        candidates.append(proposal)

    eligible: List[ProbeEnvelopeProposal] = []
    under_window = 0
    for proposal in candidates:
        age_seconds = (now_dt - proposal.created_at).total_seconds()
        if age_seconds < wait_seconds:
            under_window += 1
            continue
        eligible.append(proposal)

    approved = 0
    for proposal in eligible:
        try:
            transition(
                loop_name=LOOP_NAME,
                proposal_id=proposal.proposal_id,
                new_status="approved",
                payload_mutator=lambda p: p.update(
                    {
                        "status": "approved",
                        "review_notes": (
                            f"auto-approved (low-risk; "
                            f"{wait_hours:.2f}h wait window)"
                        ),
                    }
                ),
            )
        except Exception as exc:
            logger.warning(
                "[kora.promote.probe_fix_envelopes.auto_approve] "
                "transition raised %r for %s — skipping",
                exc,
                proposal.proposal_id,
            )
            continue
        _emit_auto_approved_audit(
            proposal, wait_hours=wait_hours, approved_at=now_dt
        )
        approved += 1
    return AutoApproveSweepResult(
        candidates_considered=len(candidates),
        candidates_under_wait_window=under_window,
        approved_count=approved,
    )
