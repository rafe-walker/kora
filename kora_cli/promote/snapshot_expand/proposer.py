"""Snapshot-field proposal generator — KR-PROMOTE-SNAPSHOT-EXPAND.

Input: a list of :class:`ToolCallObservation` from the observer.
Output: zero or more :class:`SnapshotFieldProposal` records, each
suggesting a new snapshot field whose collector would cover the
cluster.

# Pipeline

  1. Cluster observations by ``tool_name`` (exact match; the
     proxy for "same question shape" — observations covered by
     :data:`observer.KNOWN_BACKED_TOOLS` are already filtered).
  2. For each cluster ≥ ``min_cluster_size`` (default 5):
     a. Derive a proposed snapshot field path from the tool name
        (lowercase, strip ``kora__`` prefix, dot-separate).
     b. Build a short collector summary string describing what
        the new field would project.
     c. Confidence = ``min(1.0, cluster_size / 10.0)`` — capped at
        1.0 so a 10-call cluster is "max confidence" but
        20-call clusters don't get more weight than they need.

# Why exact tool_name clustering (vs argument-aware)

The argument-aware variant would split "open tickets" from
"open critical tickets" into separate proposals. That precision is
useful but adds a clustering step (token similarity on the
argument_summary strings) and the proposer's output is operator-
reviewed anyway — coarser clusters are fine for v1. A follow-on
bucket can refine if operator finds the proposals too broad.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Tuple

from .observer import ToolCallObservation

logger = logging.getLogger(__name__)


SnapshotProposalStatus = Literal["proposed", "auto_applied", "rejected"]


# Default min-cluster-size — kept moderate so the operator isn't
# flooded with marginal proposals on day one. Operator tunes via
# the cycle's env vars.
DEFAULT_MIN_CLUSTER_SIZE = 5
# Cap sample-call IDs surfaced in each proposal so the audit row
# stays bounded even on large clusters.
SAMPLE_CALL_IDS_CAP = 3


@dataclass(frozen=True, slots=True)
class SnapshotFieldProposal:
    """Wire-stable proposal shape. Emitted in the
    ``promotion.snapshot_field_added`` audit row payload."""

    proposal_id: str
    cluster_size: int
    proposed_field_path: str  # e.g. "tickets.open_count"
    proposed_collector_summary: str
    source_tool_name: str
    sample_caller_session_ids: List[str]
    confidence: float
    created_at: datetime
    status: SnapshotProposalStatus


def _format_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def proposal_to_dict(p: SnapshotFieldProposal) -> Dict[str, Any]:
    """Serialize a proposal for audit-row emission."""
    out = asdict(p)
    out["created_at"] = _format_iso(p.created_at)
    return out


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def _derive_field_path(tool_name: str) -> str:
    """Map a tool name to a proposed snapshot field path.

    ``kora__open_tickets`` → ``open_tickets``. The dot-separated
    shape stays flat (no synthetic grouping) — the operator can
    rename / regroup on approval.
    """
    name = tool_name.strip()
    if name.startswith("kora__"):
        name = name[len("kora__") :]
    return name


def _derive_collector_summary(
    tool_name: str, observations: List[ToolCallObservation]
) -> str:
    """Build a short human-readable string describing the proposed
    collector. Surfaces enough context for operator review without
    re-fetching the raw observations.
    """
    arg_shapes: Counter = Counter()
    for obs in observations:
        if obs.arguments_summary:
            arg_shapes[obs.arguments_summary] += 1
    if arg_shapes:
        top_shape, top_count = arg_shapes.most_common(1)[0]
        return (
            f"Collector would project the result of {tool_name} "
            f"called with args [{top_shape}] ({top_count}/"
            f"{len(observations)} observations matched this shape)"
        )
    return (
        f"Collector would project the result of {tool_name} "
        f"(no recurring argument shape; called {len(observations)} "
        f"times in window)"
    )


def cluster_by_tool_name(
    observations: List[ToolCallObservation],
) -> Dict[str, List[ToolCallObservation]]:
    """Group observations by exact ``tool_name`` match."""
    clusters: Dict[str, List[ToolCallObservation]] = defaultdict(list)
    for obs in observations:
        clusters[obs.tool_name].append(obs)
    return dict(clusters)


def generate_proposals(
    observations: List[ToolCallObservation],
    *,
    min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE,
    now: datetime,
) -> List[SnapshotFieldProposal]:
    """Cluster + filter + propose. Returns proposals sorted by
    confidence descending (highest-confidence first matches the
    phrasebook loop's convention so operator triage order is
    consistent across promotion loops)."""
    clusters = cluster_by_tool_name(observations)
    out: List[SnapshotFieldProposal] = []
    for tool_name, members in clusters.items():
        if len(members) < min_cluster_size:
            continue
        sample_ids = []
        seen_ids = set()
        for obs in members:
            if not obs.caller_session_id:
                continue
            if obs.caller_session_id in seen_ids:
                continue
            seen_ids.add(obs.caller_session_id)
            sample_ids.append(obs.caller_session_id)
            if len(sample_ids) >= SAMPLE_CALL_IDS_CAP:
                break
        confidence = min(1.0, len(members) / 10.0)
        out.append(
            SnapshotFieldProposal(
                proposal_id=str(uuid.uuid4()),
                cluster_size=len(members),
                proposed_field_path=_derive_field_path(tool_name),
                proposed_collector_summary=_derive_collector_summary(
                    tool_name, members
                ),
                source_tool_name=tool_name,
                sample_caller_session_ids=sample_ids,
                confidence=confidence,
                created_at=now,
                status="proposed",
            )
        )
    out.sort(key=lambda p: (-p.confidence, -p.cluster_size))
    return out
