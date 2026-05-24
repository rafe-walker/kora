"""Router-tuning proposal generator — KR-PROMOTE-ROUTER-TUNING.

Inputs:
  * Per-route :class:`RouteEscalationRollup` from cost_telemetry
    (tighten-path).
  * Per-route :class:`RouteOverrideRollup` from
    ``opus_override.applied`` audit (loosen-path; activated by
    KR-PROMOTE-ROUTER-LOOSEN-AUDIT-ROW).

Output: zero or more :class:`RouterTuningProposal` records — one
per route whose pattern crosses the relevant operator-attention
threshold.

# Tighten path

``KORA_PROMOTE_ROUTER_TUNING_TIGHTEN_THRESHOLD`` (default 0.40) —
escalation_rate ≥ this on an eligible route → ``tighten_review``.

# Loosen path

``KORA_PROMOTE_ROUTER_TUNING_LOOSEN_OVERRIDE_THRESHOLD`` (default
3) — operator forced Opus ≥ this many times in the window on a
single route → ``loosen_review``. The proposer's rationale points
the operator at the sample message texts so they can identify the
trigger pattern that should auto-escalate.

# Sample size minimum

``KORA_PROMOTE_ROUTER_TUNING_MIN_CALLS`` (default 20) — applies
to the tighten path only; the loosen path has its own threshold
since override events are intrinsically rarer than total calls.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Tuple

from .observer import RouteEscalationRollup, RouteOverrideRollup

logger = logging.getLogger(__name__)


MIN_CALLS_ENV = "KORA_PROMOTE_ROUTER_TUNING_MIN_CALLS"
TIGHTEN_THRESHOLD_ENV = "KORA_PROMOTE_ROUTER_TUNING_TIGHTEN_THRESHOLD"
LOOSEN_OVERRIDE_THRESHOLD_ENV = (
    "KORA_PROMOTE_ROUTER_TUNING_LOOSEN_OVERRIDE_THRESHOLD"
)

DEFAULT_MIN_CALLS = 20
DEFAULT_TIGHTEN_THRESHOLD = 0.40
DEFAULT_LOOSEN_OVERRIDE_THRESHOLD = 3


ProposalStatus = Literal["pending", "approved", "rejected", "expired"]
RecommendationKind = Literal["tighten_review", "loosen_review"]


@dataclass(frozen=True, slots=True)
class RouterTuningProposal:
    """Wire-stable proposal shape. Mirrors the snapshot_expand /
    phrasebook proposal shape conventions (proposal_id /
    cluster_size / confidence / created_at / status).

    Both ``tighten_review`` and ``loosen_review`` proposals use this
    single shape; field semantics vary by ``recommendation_kind``:

      * tighten_review: ``calls_count`` / ``escalation_count`` /
        ``escalation_rate`` / ``cost_estimate_usd_total`` are the
        tighten-path numbers; ``override_count`` / ``sample_message_texts``
        are 0 / empty.
      * loosen_review: ``override_count`` + ``sample_message_texts`` +
        ``by_override_source`` are the loosen-path numbers;
        ``escalation_count`` / ``escalation_rate`` /
        ``cost_estimate_usd_total`` may be 0 (the override path
        doesn't depend on those).
    """

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
    # KR-PROMOTE-ROUTER-LOOSEN-AUDIT-ROW additions. Default to
    # 0 / empty so the existing tighten-path callers keep working.
    override_count: int = 0
    sample_message_texts: List[str] = field(default_factory=list)
    by_override_source: Dict[str, int] = field(default_factory=dict)


def _format_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def proposal_to_dict(p: RouterTuningProposal) -> Dict[str, Any]:
    out = asdict(p)
    out["created_at"] = _format_iso(p.created_at)
    # asdict mutates list/dict default_factory fields into new
    # containers — defensive copy keeps caller mutation safe.
    out["sample_message_texts"] = list(p.sample_message_texts)
    out["by_override_source"] = dict(p.by_override_source)
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


# ---------------------------------------------------------------------------
# Loosen-path generator (KR-PROMOTE-ROUTER-LOOSEN-AUDIT-ROW)
# ---------------------------------------------------------------------------


def _loosen_confidence(
    override_count: int, *, threshold: int
) -> float:
    """0..1 confidence from override count + threshold. Hits 1.0
    at 3× threshold so 9 overrides on default config is full
    confidence."""
    if override_count <= 0:
        return 0.0
    return min(1.0, override_count / (3 * threshold))


def generate_loosen_proposals(
    override_rollups: List[RouteOverrideRollup],
    *,
    now: datetime,
) -> List[RouterTuningProposal]:
    """For each route whose override_count crosses the loosen
    threshold, emit one ``loosen_review`` proposal. Returns
    proposals sorted by confidence descending."""
    threshold = _int_env(
        LOOSEN_OVERRIDE_THRESHOLD_ENV,
        DEFAULT_LOOSEN_OVERRIDE_THRESHOLD,
        minimum=1,
    )
    out: List[RouterTuningProposal] = []
    for rollup in override_rollups:
        if rollup.override_count < threshold:
            continue
        # Compose a rationale that surfaces the sample texts the
        # operator typed when they manually escalated. That's the
        # operator-decision-relevant context: "this is what I
        # wanted Opus for; the trigger pattern should cover it."
        per_source_str = (
            ", ".join(
                f"{src}={count}"
                for src, count in sorted(rollup.by_source.items())
            )
            if rollup.by_source
            else "(no per-source data)"
        )
        sample_block = (
            "; ".join(f'"{t[:80]}"' for t in rollup.sample_message_texts)
            if rollup.sample_message_texts
            else "(no sample texts captured)"
        )
        rationale = (
            f"Route {rollup.route!r} saw {rollup.override_count} "
            f"operator-driven Opus override(s) in the rolling 24h "
            f"window ({per_source_str}). The Haiku-router would "
            f"otherwise have left these on Haiku — the trigger "
            f"pattern likely needs loosening to auto-escalate "
            f"similar messages. Sample message text(s) operator "
            f"escalated: {sample_block}."
        )
        confidence = _loosen_confidence(
            rollup.override_count, threshold=threshold
        )
        out.append(
            RouterTuningProposal(
                proposal_id=str(uuid.uuid4()),
                route=rollup.route,
                calls_count=0,
                escalation_count=0,
                escalation_rate=0.0,
                cost_estimate_usd_total=0.0,
                recommendation_kind="loosen_review",
                rationale=rationale,
                confidence=round(confidence, 4),
                created_at=now,
                status="pending",
                override_count=rollup.override_count,
                sample_message_texts=list(rollup.sample_message_texts),
                by_override_source=dict(rollup.by_source),
            )
        )
    out.sort(key=lambda p: (-p.confidence, -p.override_count))
    return out
