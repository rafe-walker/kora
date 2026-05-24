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
BlastRadiusLevel = Literal["low", "medium", "high"]


_DEFAULT_BLAST_RADIUS = (
    "operator must review — proposed envelope action has not been "
    "classified for production-mutation risk; treat as broad-impact "
    "by default until operator narrows the scope"
)


# KR-CC1-POLISH — low-risk pattern table for the auto-approve loop.
# Each entry is a (probe, issue_category_keyword) tuple that the
# heuristic in :func:`_derive_blast_radius_level` recognizes as a
# single-target / narrow-scope action operator has already
# authorized via the per-probe ENABLE env. New entries should ONLY
# be added when:
#   * The mapped FixEnvelope is narrow-scope (touches at most one
#     resource at a time — e.g. one fly machine, not the whole app)
#   * Operator visibility + retry semantics are documented in
#     ``kora_cli/probes/fix_envelopes.py``
# Anything more invasive stays "high" by default — operator review
# is the safety net.
_KNOWN_LOW_RISK_PATTERNS = (
    # restart_unhealthy_machine envelope (probes/fix_envelopes.py)
    # — single-target, idempotent, already enable-env-gated.
    ("fly", "machine_down"),
    ("fly", "machine_not_started"),
    ("fly", "single_machine_not_started"),
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
    # KR-CC1-POLISH — auto-approve loop's gating field. Default
    # "high" preserves the pre-classification posture (operator
    # must review). Backwards-compat: existing payloads without
    # this field load as "high" via :func:`proposal_from_dict`.
    blast_radius_level: BlastRadiusLevel = "high"


def _format_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def proposal_to_dict(p: ProbeEnvelopeProposal) -> Dict[str, Any]:
    out = asdict(p)
    out["created_at"] = _format_iso(p.created_at)
    out["sample_caller_session_ids"] = list(p.sample_caller_session_ids)
    return out


def proposal_from_dict(payload: Dict[str, Any]) -> ProbeEnvelopeProposal:
    """Rehydrate from on-disk JSON. Tolerant of the pre-KR-CC1-POLISH
    payload shape (no ``blast_radius_level`` field) — defaults to
    ``"high"`` so legacy proposals stay operator-gated."""
    raw_ts = payload.get("created_at")
    if isinstance(raw_ts, str) and raw_ts.endswith("Z"):
        raw_ts = raw_ts[:-1] + "+00:00"
    created_at = (
        datetime.fromisoformat(raw_ts)
        if isinstance(raw_ts, str) and raw_ts
        else datetime.now(timezone.utc)
    )
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    level_raw = payload.get("blast_radius_level") or "high"
    blast_radius_level: BlastRadiusLevel = (
        level_raw if level_raw in ("low", "medium", "high") else "high"
    )
    return ProbeEnvelopeProposal(
        proposal_id=str(payload["proposal_id"]),
        probe=str(payload.get("probe") or "unknown"),
        issue_category=str(payload.get("issue_category") or "unknown"),
        fix_name_suggestion=str(payload.get("fix_name_suggestion") or ""),
        cluster_size=int(payload.get("cluster_size") or 0),
        sample_caller_session_ids=list(
            payload.get("sample_caller_session_ids") or []
        ),
        recurring_recommendation_text=str(
            payload.get("recurring_recommendation_text") or ""
        ),
        blast_radius_summary=str(
            payload.get("blast_radius_summary") or _DEFAULT_BLAST_RADIUS
        ),
        confidence=float(payload.get("confidence") or 0.0),
        created_at=created_at,
        status=str(payload.get("status") or "pending"),  # type: ignore[arg-type]
        review_notes=str(payload.get("review_notes") or ""),
        blast_radius_level=blast_radius_level,
    )


def _derive_blast_radius_level(
    probe: str, issue_category: str
) -> BlastRadiusLevel:
    """Heuristic: map (probe, issue_category) → ``"low"`` only when
    it matches a known-narrow envelope action in
    :data:`_KNOWN_LOW_RISK_PATTERNS`. Everything else stays
    ``"high"`` — defaults to operator-must-review.

    The heuristic intentionally undershoots: false-low classifications
    would let proposals through the auto-approve loop's 1h wait
    window and onto the operator's envelope without explicit review.
    Better to leave a low-risk proposal in the pending queue than
    to slip a medium-risk one through.
    """
    probe_lower = (probe or "").lower()
    cat_lower = (issue_category or "").lower()
    for known_probe, cat_keyword in _KNOWN_LOW_RISK_PATTERNS:
        if probe_lower == known_probe and cat_keyword in cat_lower:
            return "low"
    return "high"


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
        blast_radius_level = _derive_blast_radius_level(probe, category)
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
                blast_radius_level=blast_radius_level,
            )
        )
    out.sort(key=lambda p: (-p.confidence, -p.cluster_size, p.probe))
    return out
