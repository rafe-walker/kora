"""Per-route escalation observer — KR-PROMOTE-ROUTER-TUNING.

Reads the live :func:`kora_cli.telemetry.get_telemetry` singleton's
:meth:`snapshot` and projects per-route rollups for the proposer.
Also reads the ``opus_override.applied`` audit seam (added in
KR-PROMOTE-ROUTER-LOOSEN-AUDIT-ROW) to surface routes whose
operator-overrides exceed the loosen-path threshold.

# Two data sources, two proposal kinds

  1. ``cost_telemetry.snapshot()`` per-route counters →
     ``tighten_review`` proposals (route escalates often; review
     whether the trigger is paying off).
  2. ``opus_override.applied`` audit rows (this PR) →
     ``loosen_review`` proposals (operator manually overrode
     Haiku to Opus N+ times on a route; trigger pattern should
     probably auto-escalate that case).

# Why this design earns the loosen path

#193 left the loosen path dormant because no override audit row
existed. With the seam added, the observer reads it directly +
the proposer wires the second proposal kind. Per the bucket
spec §2 deliverable C: "the loosen-path code that #193 left
dormant becomes active."
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RouteEscalationRollup:
    """One route's projection from cost_telemetry's rolling_24h window."""

    route: str
    calls_count: int
    escalation_count: int
    escalation_rate: float  # 0.0..1.0; 0.0 when calls_count == 0
    cost_estimate_usd_total: float


@dataclass(frozen=True, slots=True)
class RouteOverrideRollup:
    """One route's projection from ``opus_override.applied`` audit
    rows over the observation window. Feeds the proposer's
    loosen_review path (KR-PROMOTE-ROUTER-LOOSEN-AUDIT-ROW)."""

    route: str
    override_count: int
    sample_message_texts: List[str] = field(default_factory=list)
    # Per-source breakdown ("operator_prefix" / "force_env") so the
    # proposer can distinguish per-call /opus moves from a global
    # KORA_FORCE_OPUS env flip (which is much weaker signal).
    by_source: Dict[str, int] = field(default_factory=dict)


def _safe_rate(escalations: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return min(1.0, escalations / total)


def collect_route_rollups() -> List[RouteEscalationRollup]:
    """Return per-route rollups from the live cost_telemetry singleton.

    Fail-soft: telemetry singleton unavailable / snapshot raises →
    empty list. Proposer treats that as "no data this cycle, no
    proposals" — same fail-soft contract as the other promotion
    loops.

    Routes are sorted alphabetically for stable test assertions;
    the proposer reorders by score before emitting.
    """
    try:
        from kora_cli.telemetry import (
            WINDOW_ROLLING_24H,
            get_telemetry,
        )
    except Exception as exc:
        logger.debug(
            "[kora.promote.router_tuning.observer] telemetry import "
            "failed: %r — no rollups",
            exc,
        )
        return []

    try:
        all_windows = get_telemetry().snapshot()
    except Exception as exc:
        logger.warning(
            "[kora.promote.router_tuning.observer] telemetry.snapshot() "
            "raised %r — no rollups",
            exc,
        )
        return []

    window_data = all_windows.get(WINDOW_ROLLING_24H, {})
    if not isinstance(window_data, dict):
        return []

    out: List[RouteEscalationRollup] = []
    for route, counters in window_data.items():
        if not isinstance(counters, dict):
            continue
        calls = int(counters.get("calls_count") or 0)
        escs = int(counters.get("escalation_count") or 0)
        cost = float(counters.get("cost_estimate_usd_total") or 0.0)
        out.append(
            RouteEscalationRollup(
                route=str(route),
                calls_count=calls,
                escalation_count=escs,
                escalation_rate=_safe_rate(escs, calls),
                cost_estimate_usd_total=round(cost, 6),
            )
        )
    out.sort(key=lambda r: r.route)
    return out


# ---------------------------------------------------------------------------
# Loosen-path observer (KR-PROMOTE-ROUTER-LOOSEN-AUDIT-ROW)
# ---------------------------------------------------------------------------


# Cap on sample message texts surfaced per route — keeps the
# proposer payload bounded even when a route accumulates many
# override events.
_SAMPLE_TEXT_CAP = 3


def collect_route_overrides(
    *, since: Optional[datetime] = None
) -> List[RouteOverrideRollup]:
    """Read ``opus_override.applied`` audit rows + group by route.

    Args:
      since: Lower bound (aware datetime). Defaults to 24h before
        now — same window as the tighten-path's rolling_24h
        cost-telemetry source so the two proposal kinds emit on
        comparable observation windows.

    Returns rollups sorted alphabetically by route (stable test
    ordering; the proposer reorders by score before emitting).
    Fail-soft per the other observer methods: audit reader failure
    returns ``[]``.
    """
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(days=1)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    try:
        from kora_cli.audit.jsonl_reader import read_audit_entries
    except Exception as exc:
        logger.debug(
            "[kora.promote.router_tuning.observer] audit reader import "
            "failed: %r — no override rollups",
            exc,
        )
        return []

    try:
        entries = read_audit_entries(
            seam="opus_override.applied", since=since
        )
    except Exception as exc:
        logger.warning(
            "[kora.promote.router_tuning.observer] read_audit_entries "
            "raised %r — no override rollups",
            exc,
        )
        return []

    counts: Dict[str, int] = defaultdict(int)
    samples: Dict[str, List[str]] = defaultdict(list)
    by_source: Dict[str, Dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    for entry in entries:
        details = entry.details or {}
        route = details.get("route")
        if not isinstance(route, str) or not route:
            route = "unknown"
        counts[route] += 1
        source = details.get("override_source")
        if isinstance(source, str) and source:
            by_source[route][source] += 1
        text = details.get("original_message_text")
        if (
            isinstance(text, str)
            and text.strip()
            and len(samples[route]) < _SAMPLE_TEXT_CAP
        ):
            samples[route].append(text.strip())

    out: List[RouteOverrideRollup] = []
    for route in sorted(counts.keys()):
        out.append(
            RouteOverrideRollup(
                route=route,
                override_count=counts[route],
                sample_message_texts=list(samples[route]),
                by_source=dict(by_source[route]),
            )
        )
    return out
