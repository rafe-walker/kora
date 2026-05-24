"""Router-tuning proposal generator — KR-PROMOTE-ROUTER-TUNING.

Input: per-route :class:`RouteEscalationRollup` from the observer.
Output: zero or more :class:`RouterTuningProposal` records — one
per route whose escalation pattern crosses an operator-attention
threshold.

# Thresholds (operator-tunable via env)

  * ``KORA_PROMOTE_ROUTER_TUNING_MIN_CALLS`` (default 20) —
    minimum calls in window before a route gets considered.
    Below this, sample size is too noisy.
  * ``KORA_PROMOTE_ROUTER_TUNING_TIGHTEN_THRESHOLD`` (default 0.40)
    — escalation_rate ≥ this on an eligible route → tighten_review
    proposal. (Default 40% — well above the natural escalation
    baseline of <15% from healthy decision-language patterns.)

# Why no loosen_review in v1

The signal for ``loosen_review`` is "operator overrode Haiku to
Opus via /opus N times" — that observation doesn't have its own
audit row yet (it lives in the routing decision logs, not the
JSONL audit). Future bucket can emit a ``router.operator_override``
seam; the proposer here would then surface routes with high
override-rate as loosen candidates. Documented in
``__init__.py`` v1 scope.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Tuple

from .observer import RouteEscalationRollup

logger = logging.getLogger(__name__)


MIN_CALLS_ENV = "KORA_PROMOTE_ROUTER_TUNING_MIN_CALLS"
TIGHTEN_THRESHOLD_ENV = "KORA_PROMOTE_ROUTER_TUNING_TIGHTEN_THRESHOLD"

DEFAULT_MIN_CALLS = 20
DEFAULT_TIGHTEN_THRESHOLD = 0.40


ProposalStatus = Literal["pending", "approved", "rejected", "expired"]
RecommendationKind = Literal["tighten_review", "loosen_review"]


@dataclass(frozen=True, slots=True)
class RouterTuningProposal:
    """Wire-stable proposal shape. Mirrors the snapshot_expand /
    phrasebook proposal shape conventions (proposal_id /
    cluster_size / confidence / created_at / status)."""

    proposal_id: str
    route: str
    calls_count: int
    escalation_count: int
    escalation_rate: float  # 0.0..1.0
    cost_estimate_usd_total: float
    recommendation_kind: RecommendationKind
    rationale: str
    confidence: float  # derived from sample size; 0.0..1.0
    created_at: datetime
    status: ProposalStatus = "pending"
    review_notes: str = ""


def _format_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def proposal_to_dict(p: RouterTuningProposal) -> Dict[str, Any]:
    out = asdict(p)
    out["created_at"] = _format_iso(p.created_at)
    return out


def _float_env(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "[kora.promote.router_tuning.proposer] %s=%r not numeric — "
            "using default %f",
            name,
            raw,
            default,
        )
        return default
    if value < minimum:
        return default
    return value


def _int_env(name: str, default: int, *, minimum: int = 0) -> int:
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


def _confidence_from_calls(calls: int, *, threshold_calls: int) -> float:
    """Map the call count to a 0..1 confidence band.

    At threshold_calls we land at 0.5 (just enough); 4× threshold
    earns near-1.0; below threshold isn't proposed anyway.
    """
    if calls <= 0:
        return 0.0
    return min(1.0, (calls / (2 * threshold_calls)))


def generate_proposals(
    rollups: List[RouteEscalationRollup],
    *,
    now: datetime,
) -> List[RouterTuningProposal]:
    """Cluster + filter + propose. Returns proposals sorted by
    confidence descending (matches the phrasebook convention so the
    cockpit can use a single ordering rule across all loops)."""
    min_calls = _int_env(MIN_CALLS_ENV, DEFAULT_MIN_CALLS, minimum=2)
    tighten_threshold = _float_env(
        TIGHTEN_THRESHOLD_ENV,
        DEFAULT_TIGHTEN_THRESHOLD,
        minimum=0.0,
    )
    if tighten_threshold > 1.0:
        tighten_threshold = 1.0

    out: List[RouterTuningProposal] = []
    for r in rollups:
        if r.calls_count < min_calls:
            continue
        if r.escalation_rate < tighten_threshold:
            continue
        confidence = _confidence_from_calls(
            r.calls_count, threshold_calls=min_calls
        )
        rationale = (
            f"Route {r.route!r} escalated to Opus on "
            f"{r.escalation_count}/{r.calls_count} calls "
            f"({r.escalation_rate * 100:.1f}%) in the rolling 24h "
            f"window. Per-call escalations cost a full Opus turn on "
            f"top of the original Haiku turn. Operator review of the "
            f"escalation trigger pattern for this route is "
            f"recommended; spend so far: ${r.cost_estimate_usd_total:.4f}."
        )
        out.append(
            RouterTuningProposal(
                proposal_id=str(uuid.uuid4()),
                route=r.route,
                calls_count=r.calls_count,
                escalation_count=r.escalation_count,
                escalation_rate=round(r.escalation_rate, 4),
                cost_estimate_usd_total=r.cost_estimate_usd_total,
                recommendation_kind="tighten_review",
                rationale=rationale,
                confidence=round(confidence, 4),
                created_at=now,
                status="pending",
            )
        )
    out.sort(key=lambda p: (-p.confidence, -p.escalation_rate))
    return out
