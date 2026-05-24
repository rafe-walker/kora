"""Probe-fix envelope proposal generator — KR-PROMOTE-PROBE-FIX-ENVELOPES.

Input: :class:`InvestigationObservation` from the observer.
Output: :class:`ProbeEnvelopeProposal` records — one per
(probe, issue_category) cluster ≥ min_cluster_size.

# Cluster shape

Exact-match on ``(probe, issue_category)`` — coarse but operator-
reviewable. The proposer emits the most-common short snippet of
the cluster's investigation summaries as the
``recurring_recommendation_text`` so operator can see the
recurring suggestion at-a-glance.

# Blast-radius default

v1 always emits a conservative ``"operator must review — proposed
envelope action has not been classified"`` blast-radius summary.
The operator-reviewing-the-proposal step IS the blast-radius
review; the loop is propose-only and never auto-applies, so
defaulting to "review-required" is fine. Operator-edit-at-
approve-time can refine this in the persisted record.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal

from .observer import InvestigationObservation

logger = logging.getLogger(__name__)


MIN_CLUSTER_SIZE_ENV = "KORA_PROMOTE_PROBE_FIX_MIN_CLUSTER"
DEFAULT_MIN_CLUSTER_SIZE = 3  # lower than other loops — probe failures are
# rarer + recurring ones are higher-signal


ProposalStatus = Literal["pending", "approved", "rejected", "expired"]


_DEFAULT_BLAST_RADIUS = (
    "operator must review — proposed envelope action has not been "
    "classified for production-mutation risk; treat as broad-impact "
    "by default until operator narrows the scope"
)


@dataclass(frozen=True, slots=True)
class ProbeEnvelopeProposal:
    """Wire-stable proposal shape."""

    proposal_id: str
    probe: str
    issue_category: str
    fix_name_suggestion: str
    cluster_size: int
    sample_caller_session_ids: List[str]
    recurring_recommendation_text: str
    blast_radius_summary: str
    confidence: float
    created_at: datetime
    status: ProposalStatus = "pending"
    review_notes: str = ""


def _format_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def proposal_to_dict(p: ProbeEnvelopeProposal) -> Dict[str, Any]:
    out = asdict(p)
    out["created_at"] = _format_iso(p.created_at)
    out["sample_caller_session_ids"] = list(p.sample_caller_session_ids)
    return out


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


def _suggest_fix_name(probe: str, issue_category: str) -> str:
    """Derive a short stable id from (probe, issue_category).

    The id mirrors the existing ``FixEnvelope.fix_name`` convention
    (snake_case, narrow scope). Operator MUST rename on approval —
    this is a placeholder rather than a final identity.
    """
    safe_probe = re.sub(r"[^a-z0-9]+", "_", probe.lower()).strip("_")
    safe_cat = re.sub(r"[^a-z0-9]+", "_", issue_category.lower()).strip("_")
    return f"proposed_{safe_probe}_{safe_cat}"


_SUMMARY_FALLBACK_CHARS = 240


def _recurring_recommendation_text(
    observations: List[InvestigationObservation],
) -> str:
    """Pick the most-common short summary across the cluster.

    Falls back to the first observation's leading 240 chars if no
    shared substring emerges. Operator edits the result on
    approve.
    """
    if not observations:
        return ""
    # The summaries are usually short paragraphs; cluster by first-
    # 240-char projection so near-identical wording bundles.
    rolled: Counter = Counter()
    for o in observations:
        head = o.investigation_summary_text.strip()
        rolled[head[:_SUMMARY_FALLBACK_CHARS]] += 1
    most_common, _count = rolled.most_common(1)[0]
    return most_common


def generate_proposals(
    observations: List[InvestigationObservation],
    *,
    now: datetime,
) -> List[ProbeEnvelopeProposal]:
    """Cluster + filter + propose. Returns proposals sorted by
    confidence descending."""
    min_cluster_size = _int_env(
        MIN_CLUSTER_SIZE_ENV, DEFAULT_MIN_CLUSTER_SIZE, minimum=2
    )

    clusters: Dict[tuple, List[InvestigationObservation]] = defaultdict(list)
    for o in observations:
        clusters[(o.probe, o.issue_category)].append(o)

    out: List[ProbeEnvelopeProposal] = []
    for (probe, category), members in clusters.items():
        if len(members) < min_cluster_size:
            continue
        sample_ids: List[str] = []
        seen = set()
        for o in members:
            if not o.caller_session_id or o.caller_session_id in seen:
                continue
            seen.add(o.caller_session_id)
            sample_ids.append(o.caller_session_id)
            if len(sample_ids) >= 3:
                break
        confidence = min(1.0, len(members) / (2 * min_cluster_size))
        out.append(
            ProbeEnvelopeProposal(
                proposal_id=str(uuid.uuid4()),
                probe=probe,
                issue_category=category,
                fix_name_suggestion=_suggest_fix_name(probe, category),
                cluster_size=len(members),
                sample_caller_session_ids=sample_ids,
                recurring_recommendation_text=(
                    _recurring_recommendation_text(members)
                ),
                blast_radius_summary=_DEFAULT_BLAST_RADIUS,
                confidence=round(confidence, 4),
                created_at=now,
                status="pending",
            )
        )
    out.sort(key=lambda p: (-p.confidence, -p.cluster_size, p.probe))
    return out
