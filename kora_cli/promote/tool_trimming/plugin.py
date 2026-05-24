"""Tool-trimming cycle orchestrator — KR-PROMOTE-TOOL-TRIMMING.

Called by the periodic-task heartbeat (registered by
:mod:`kora_cli.listeners.promote_tool_trimming_listener`).

# Env

  * ``KORA_PROMOTE_TOOL_TRIMMING_ENABLED`` (default ``true``)
  * ``KORA_PROMOTE_TOOL_TRIMMING_INTERVAL_SEC`` (default 86400 = 24h)
  * ``KORA_PROMOTE_TOOL_TRIMMING_EXPIRY_DAYS`` (default 14)
  * Proposer-side: ``KORA_PROMOTE_TOOL_TRIMMING_MIN_CALLS``,
    ``KORA_PROMOTE_TOOL_TRIMMING_WINDOW_DAYS``
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

from .observer import collect_route_tool_usage
from .proposer import (
    DEFAULT_OBSERVATION_WINDOW_DAYS,
    OBSERVATION_WINDOW_DAYS_ENV,
    ToolTrimProposal,
    generate_proposals,
    proposal_to_dict,
)

logger = logging.getLogger(__name__)


LOOP_NAME = "tool_trimming"

ENABLED_ENV = "KORA_PROMOTE_TOOL_TRIMMING_ENABLED"
INTERVAL_SEC_ENV = "KORA_PROMOTE_TOOL_TRIMMING_INTERVAL_SEC"
EXPIRY_DAYS_ENV = "KORA_PROMOTE_TOOL_TRIMMING_EXPIRY_DAYS"

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


def _emit_audit(proposal: ToolTrimProposal) -> None:
    try:
        from kora_cli.audit.jsonl_sink import emit_audit
    except Exception as exc:
        logger.warning(
            "[kora.promote.tool_trimming] audit import failed: %r", exc
        )
        return
    try:
        emit_audit(
            "promotion.tool_trim_proposed",
            proposal_to_dict(proposal),
            caller_session_id=(
                f"promotion:tool_trimming:{proposal.proposal_id}"
            ),
            source="reasoning",
        )
    except Exception as exc:
        logger.warning(
            "[kora.promote.tool_trimming] emit_audit raised %r — "
            "proposal persisted; audit row missing",
            exc,
        )


async def run_tool_trimming_cycle(
    *, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """One cycle of the tool-trimming promotion loop."""
    started_dt = now or datetime.now(timezone.utc)
    started_monotonic = time.monotonic()

    summary: Dict[str, Any] = {
        "enabled": True,
        "rollups_observed": 0,
        "proposals_generated": 0,
        "proposals_persisted": 0,
        "expired_count": 0,
        "started_at": started_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_ms": 0,
    }

    if not _is_enabled():
        summary["enabled"] = False
        logger.info(
            "[kora.promote.tool_trimming] disabled (%s=false) — skipping",
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
        rollups = await collect_route_tool_usage(
            since=started_dt - timedelta(days=window_days),
        )
        summary["rollups_observed"] = len(rollups)
    except Exception as exc:
        logger.warning(
            "[kora.promote.tool_trimming] observer failed: %r", exc
        )
        summary["duration_ms"] = int(
            (time.monotonic() - started_monotonic) * 1000
        )
        return summary

    try:
        proposals = generate_proposals(rollups, now=started_dt)
        summary["proposals_generated"] = len(proposals)
    except Exception as exc:
        logger.warning(
            "[kora.promote.tool_trimming] proposer failed: %r", exc
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
                "[kora.promote.tool_trimming] persist failed for "
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
            "[kora.promote.tool_trimming] expire_older_than raised %r",
            exc,
        )

    summary["duration_ms"] = int(
        (time.monotonic() - started_monotonic) * 1000
    )
    logger.info(
        "[kora.promote.tool_trimming] cycle complete: %s", summary
    )
    return summary
