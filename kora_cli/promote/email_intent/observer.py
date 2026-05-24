"""Email-intent observation collector — KR-PROMOTE-EMAIL-INTENT.

Reads ``intent.email_to_sea_ticket`` audit rows where
``action="logged_only"`` and projects them into the shape the
proposer clusters on.

# Filtering

  * ``action != "logged_only"`` rows are skipped — only the
    unmatched/below-floor emails carry the "Kora should learn
    this" signal.
  * Rows where ``subject`` is missing / empty are skipped —
    nothing to cluster on.
  * ``since`` defaults to 14 days back; long enough to surface
    recurring patterns without dragging in stale subjects.

# PII discipline

The body text is NOT logged in the source audit (see
``kora_cli/intent/email_to_sea_ticket.py`` security posture).
This observer projects only the subject + the audit row's
``pattern_matched`` / ``reason`` / ``confidence`` for proposer
context. Future bucket may extend audit to carry a hashed body
fingerprint if subject-only clustering proves too noisy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EmailIntentObservation:
    """One logged-only email projected for clustering."""

    subject: str
    pattern_matched: Optional[str]  # None when no pattern matched
    confidence: str  # "unrecognized" / "low" / "medium" / "high"
    reason: str  # "no_pattern_matched" / "below_floor_<floor>"
    caller_session_id: str
    timestamp: datetime


async def collect_recent_logged_only(
    *, since: Optional[datetime] = None
) -> List[EmailIntentObservation]:
    """Read recent ``intent.email_to_sea_ticket`` audit rows + filter
    to ``action="logged_only"``.

    Args:
      since: Lower bound (aware datetime). Defaults to 14 days
        before now.

    Returns observations sorted by ``timestamp`` ascending. Empty
    on reader failure (fail-soft per the other promotion-loop
    observers).
    """
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(days=14)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    try:
        from kora_cli.audit.jsonl_reader import read_audit_entries
    except Exception as exc:
        logger.warning(
            "[kora.promote.email_intent.observer] audit reader import "
            "failed: %r — no observations",
            exc,
        )
        return []

    try:
        entries = read_audit_entries(
            seam="intent.email_to_sea_ticket", since=since
        )
    except Exception as exc:
        logger.warning(
            "[kora.promote.email_intent.observer] read_audit_entries "
            "raised %r — no observations",
            exc,
        )
        return []

    out: List[EmailIntentObservation] = []
    for entry in entries:
        details = entry.details or {}
        if details.get("action") != "logged_only":
            continue
        subject = details.get("subject")
        if not isinstance(subject, str) or not subject.strip():
            continue
        pattern_raw = details.get("pattern_matched")
        pattern_matched = (
            pattern_raw if isinstance(pattern_raw, str) and pattern_raw else None
        )
        out.append(
            EmailIntentObservation(
                subject=subject.strip(),
                pattern_matched=pattern_matched,
                confidence=str(details.get("confidence") or "unrecognized"),
                reason=str(details.get("reason") or ""),
                caller_session_id=str(entry.caller_session_id or ""),
                timestamp=entry.emitted_at,
            )
        )

    out.sort(key=lambda o: o.timestamp)
    return out
