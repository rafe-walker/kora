"""Email-intent proposal generator — KR-PROMOTE-EMAIL-INTENT.

Input: :class:`EmailIntentObservation` from the observer.
Output: :class:`EmailIntentProposal` records — one per cluster
≥ min_cluster_size.

# Pipeline

  1. Embed each observation's subject via
     :func:`kora_cli.clustering.text_similarity.embed_texts`.
  2. Cluster via greedy similarity at
     :data:`DEFAULT_COHESION_THRESHOLD`.
  3. For each cluster ≥ min_cluster_size:
     a. Derive a candidate regex from the cluster's common tokens
        (escape all regex metacharacters; emit case-insensitive
        alternation of top-3 tokens).
     b. Default ``proposed_action_kind = "save_note"`` (the most
        common existing action; operator can flip to
        ``"log_only"`` or ``"save_with_reply"`` at approve).
     c. Confidence derived from cluster size; capped at 1.0.

# Pattern derivation safety

Same escape + alternation pattern as the phrasebook proposer
(#186). All regex metacharacters in the cluster's tokens are
escaped before joining; pathological subjects (with literal
``(``/``*``/etc.) can't bomb the regex engine at compile-time.
Operator can refine on approval.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal

from kora_cli.clustering.text_similarity import (
    cluster_by_similarity,
    embed_texts,
)

from .observer import EmailIntentObservation

logger = logging.getLogger(__name__)


MIN_CLUSTER_SIZE_ENV = "KORA_PROMOTE_EMAIL_INTENT_MIN_CLUSTER"
COHESION_THRESHOLD_ENV = "KORA_PROMOTE_EMAIL_INTENT_COHESION"

DEFAULT_MIN_CLUSTER_SIZE = 3
DEFAULT_COHESION_THRESHOLD = 0.65
# Lower than the phrasebook threshold (0.85) because subjects are
# inherently shorter / sparser than full DM replies; tighter
# clustering would over-fragment.

SAMPLE_SUBJECTS_CAP = 3

ProposalStatus = Literal["pending", "approved", "rejected", "expired"]
ActionKind = Literal["save_note", "log_only", "save_with_reply"]


@dataclass(frozen=True, slots=True)
class EmailIntentProposal:
    """Wire-stable proposal shape."""

    proposal_id: str
    cluster_size: int
    sample_subjects: List[str]
    proposed_pattern: str
    proposed_action_kind: ActionKind
    confidence: float
    created_at: datetime
    status: ProposalStatus = "pending"
    review_notes: str = ""
    sample_caller_session_ids: List[str] = field(default_factory=list)


def _format_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def proposal_to_dict(p: EmailIntentProposal) -> Dict[str, Any]:
    out = asdict(p)
    out["created_at"] = _format_iso(p.created_at)
    out["sample_subjects"] = list(p.sample_subjects)
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


def _float_env(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value < minimum:
        return default
    return value


# Stopwords specific to email subjects — common prefix noise like
# "Re:" / "Fwd:" gets stripped so the proposer doesn't cluster
# on transport-level header chatter.
_STOPWORDS = frozenset(
    {
        "re",
        "fwd",
        "fw",
        "a",
        "an",
        "and",
        "the",
        "to",
        "of",
        "for",
        "in",
        "on",
        "at",
        "is",
        "are",
        "was",
        "be",
    }
)


_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _meaningful_tokens(text: str) -> List[str]:
    return [
        t
        for t in _TOKEN_RE.findall(text.lower())
        if t not in _STOPWORDS and len(t) >= 2
    ]


def _derive_pattern(subjects: List[str]) -> str:
    """Generate a conservative regex from the cluster's common
    subject tokens. Pattern: case-insensitive alternation of the
    top-3 cross-subject tokens. All metacharacters escaped.

    Fallback (no shared tokens): a permissive ``(?i).*`` placeholder
    that the operator MUST refine before approving — defaults to
    "match everything" so the proposal is obviously a placeholder
    rather than a silently-bad regex.
    """
    df: Counter = Counter()
    for subj in subjects:
        for tok in set(_meaningful_tokens(subj)):
            df[tok] += 1
    if not df:
        return "(?i).*"
    top = [tok for tok, _ in df.most_common(3)]
    escaped = [re.escape(t) for t in top]
    return "(?i)(" + "|".join(escaped) + ")"


async def generate_proposals(
    observations: List[EmailIntentObservation],
    *,
    now: datetime,
) -> List[EmailIntentProposal]:
    """Cluster + propose. Returns proposals sorted by confidence
    descending. Fail-soft on embedder error (returns empty list)."""
    if not observations:
        return []
    min_cluster_size = _int_env(
        MIN_CLUSTER_SIZE_ENV, DEFAULT_MIN_CLUSTER_SIZE, minimum=2
    )
    cohesion = _float_env(
        COHESION_THRESHOLD_ENV,
        DEFAULT_COHESION_THRESHOLD,
        minimum=0.0,
    )
    if cohesion > 1.0:
        cohesion = 1.0

    subjects = [o.subject for o in observations]
    try:
        embeddings = await embed_texts(subjects)
    except Exception as exc:
        logger.warning(
            "[kora.promote.email_intent.proposer] embed_texts raised "
            "%r — no proposals",
            exc,
        )
        return []

    try:
        clusters = cluster_by_similarity(embeddings, threshold=cohesion)
    except Exception as exc:
        logger.warning(
            "[kora.promote.email_intent.proposer] cluster_by_similarity "
            "raised %r — no proposals",
            exc,
        )
        return []

    # Map embeddings back to observations by index — embed_texts
    # preserves order, cluster_by_similarity preserves order, so
    # we can find each embedding's source observation via
    # ``subjects[i]`` equality (stable since same input list).
    text_to_obs: Dict[str, List[EmailIntentObservation]] = {}
    for o in observations:
        text_to_obs.setdefault(o.subject, []).append(o)

    out: List[EmailIntentProposal] = []
    for cluster in clusters:
        if len(cluster) < min_cluster_size:
            continue
        cluster_subjects = [emb.text for emb in cluster]
        # Dedup + cap sample subjects so the audit payload stays
        # bounded; operator wants representative examples, not the
        # full cluster.
        seen_subjects: List[str] = []
        for s in cluster_subjects:
            if s not in seen_subjects:
                seen_subjects.append(s)
            if len(seen_subjects) >= SAMPLE_SUBJECTS_CAP:
                break

        # Sample caller_session_ids drawn from the matching
        # observations.
        sample_ids: List[str] = []
        seen_ids = set()
        for emb in cluster:
            for obs in text_to_obs.get(emb.text, []):
                if not obs.caller_session_id or obs.caller_session_id in seen_ids:
                    continue
                seen_ids.add(obs.caller_session_id)
                sample_ids.append(obs.caller_session_id)
                if len(sample_ids) >= SAMPLE_SUBJECTS_CAP:
                    break
            if len(sample_ids) >= SAMPLE_SUBJECTS_CAP:
                break

        confidence = min(1.0, len(cluster) / (2 * min_cluster_size))
        out.append(
            EmailIntentProposal(
                proposal_id=str(uuid.uuid4()),
                cluster_size=len(cluster),
                sample_subjects=seen_subjects,
                proposed_pattern=_derive_pattern(cluster_subjects),
                # v1 default — operator can flip to log_only /
                # save_with_reply at approve-time. save_note matches
                # the most common existing pattern shape (subject_note_prefix
                # / subject_idea_prefix / subject_todo_prefix from
                # the current registry).
                proposed_action_kind="save_note",
                confidence=round(confidence, 4),
                created_at=now,
                status="pending",
                sample_caller_session_ids=sample_ids,
            )
        )
    out.sort(key=lambda p: (-p.confidence, -p.cluster_size))
    return out
