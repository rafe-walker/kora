"""Probe-fix observation collector — KR-PROMOTE-PROBE-FIX-ENVELOPES.

Reads ``probe.investigation_completed`` (#184) audit rows and
projects them into the shape the proposer clusters on.

# Why investigation_completed (not probe.wake_requested)

The wake_requested rows fire BEFORE Kora investigates. The
investigation_completed rows carry Kora's actual recommendation
text — the operator-decision-relevant signal the loop wants to
cluster. wake_requested rows are still relevant for "frequency of
this issue category" but the recommendation text only lives in
investigation_completed.

# autofix_attempted cross-reference

Investigations that already triggered an autofix attempt
(``autofix_attempted=True``) are SKIPPED — the loop's purpose is
to propose NEW envelope actions, not reinforce existing ones.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class InvestigationObservation:
    """One investigation projected for clustering."""

    probe: str
    issue_category: str
    severity: str
    investigation_summary_text: str
    caller_session_id: str
    timestamp: datetime


async def collect_recent_investigations(
    *, since: Optional[datetime] = None
) -> List[InvestigationObservation]:
    """Read recent ``probe.investigation_completed`` audit rows.

    Args:
      since: Lower bound (aware datetime). Defaults to 14 days
        before now — broad enough to surface recurring issues
        while small enough that the audit JSONL read stays cheap.

    Returns observations sorted by ``timestamp`` ascending.
    Investigations that already triggered an autofix attempt
    (``autofix_attempted=True``) are skipped — see module
    docstring.
    """
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(days=14)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    try:
        from kora_cli.audit.jsonl_reader import read_audit_entries
    except Exception as exc:
        logger.warning(
            "[kora.promote.probe_fix_envelopes.observer] audit reader "
            "import failed: %r — no observations",
            exc,
        )
        return []

    try:
        entries = read_audit_entries(
            seam="probe.investigation_completed", since=since
        )
    except Exception as exc:
        logger.warning(
            "[kora.promote.probe_fix_envelopes.observer] "
            "read_audit_entries raised %r — no observations",
            exc,
        )
        return []

    out: List[InvestigationObservation] = []
    for entry in entries:
        details = entry.details or {}
        if bool(details.get("autofix_attempted")):
            continue
        probe = details.get("probe")
        category = details.get("issue_category")
        if not isinstance(probe, str) or not probe:
            continue
        if not isinstance(category, str) or not category:
            continue
        summary = details.get("investigation_summary_text") or ""
        if not isinstance(summary, str) or not summary.strip():
            continue
        out.append(
            InvestigationObservation(
                probe=str(probe),
                issue_category=str(category),
                severity=str(details.get("severity") or "warning"),
                investigation_summary_text=summary,
                caller_session_id=str(entry.caller_session_id or ""),
                timestamp=entry.emitted_at,
            )
        )

    out.sort(key=lambda o: o.timestamp)
    return out
