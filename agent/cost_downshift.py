"""Cost-ladder downshift selector (KR-P2-K ST3, R4.1 §9.6).

Pure-function library that decides, given a ticket's criticality and the
active cost-ladder rung, what model tier the runner should use — or
whether the ticket should be deferred entirely.

The selector is rung-aware and criticality-aware; it does NOT mutate
any state. The caller (the future Sea_Ticket poller in KR-P2-E, or any
other dispatch site) acts on the returned
:class:`DownshiftDecision`:

  - ``defer=False`` + ``effective_tier=tier`` → run the ticket at
    ``tier`` (which may equal the configured tier or be downshifted).
  - ``defer=True`` + ``effective_tier=None`` → do not run; transition
    the Sea_Ticket to ``deferred_cost_limit``.

# Criticality semantics (PM ruling A1, substrate migration 0098)

The substrate ``tickets.criticality`` column is a 2-value CHECK enum:

  - ``"downshift_eligible"`` — the ticket may run on a cheaper model
    when cost-pressured.
  - ``"frontier_only"`` — the ticket must run on the configured
    frontier model OR not at all.

A ``NULL`` value (non-sea tickets, or sea tickets without an explicit
hint) is treated as ``"frontier_only"`` per fail-CLOSED policy:
when we don't know the ticket's downshift policy, we don't downshift.

# Per-rung behavior

  - ``CostRung.NORMAL`` — no cost pressure. All tickets run at
    ``configured_tier``.
  - ``CostRung.WARN_75`` — first cost-pressure threshold.
    Downshift-eligible tickets step down one tier (Opus→Sonnet,
    Sonnet→Haiku). Frontier-only / NULL tickets defer.
  - ``CostRung.DOWNSHIFT_90`` — aggressive cost-pressure threshold.
    Downshift-eligible tickets drop straight to Haiku (the cheapest
    tier). Frontier-only / NULL tickets defer.
  - ``CostRung.HARD_STOP_100`` — budget exhausted. All tickets defer.
    The poller should never claim at this rung once ST4 wires the
    PAUSED{COST} transition; the selector returns defer defensively.

# Why frontier-only defers at WARN_75 (not just at DOWNSHIFT_90)

The PM ruling text is explicit: when any cost-pressure rung is
active, only downshift-eligible tickets continue to run. Frontier-only
tickets are deferred until the budget recovers (either via monthly
refresh or via reconciliation pulling spend backwards — though the
reconciler only moves spend FORWARD per ST1).

This is more conservative than the original bucket sketch (which
proposed running frontier-only at configured tier through WARN_75).
The conservative choice is the right one: at WARN_75 we are
projected to overrun the budget, and continuing to spend on
non-downshiftable work compounds the projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from agent.cost_state_holder import CostRung


class ModelTier(str, Enum):
    """Anthropic model tier — Opus > Sonnet > Haiku by price/capability.

    String values are short identifiers used in log lines and decision
    reasons; full model names like ``"claude-opus-4-7"`` map to these
    tiers via :func:`classify_model_tier`.
    """

    OPUS = "opus"
    SONNET = "sonnet"
    HAIKU = "haiku"


_TIER_STEP_DOWN = {
    ModelTier.OPUS: ModelTier.SONNET,
    ModelTier.SONNET: ModelTier.HAIKU,
    ModelTier.HAIKU: ModelTier.HAIKU,
}


CRITICALITY_DOWNSHIFT_ELIGIBLE = "downshift_eligible"
CRITICALITY_FRONTIER_ONLY = "frontier_only"


@dataclass(frozen=True, slots=True)
class DownshiftDecision:
    """Result of :func:`select_effective_model_tier`.

    Attributes:
        defer: ``True`` if the caller should not run this ticket and
            should instead transition it to ``deferred_cost_limit``.
        effective_tier: The model tier to use when ``defer=False``;
            ``None`` when ``defer=True``.
        reason: Operator-readable explanation of the decision. Always
            non-empty.
    """

    defer: bool
    effective_tier: Optional[ModelTier]
    reason: str


def classify_model_tier(model_name: Optional[str]) -> Optional[ModelTier]:
    """Map a full model identifier to its :class:`ModelTier`.

    The match is substring-based on the lowercased name, so prefixed
    forms like ``"anthropic/claude-opus-4.6"`` and bare forms like
    ``"claude-haiku-4-5"`` both classify correctly.

    Returns ``None`` for unrecognized names — the caller should treat
    an unclassifiable model as having no downshift path (run as
    configured; never step).
    """
    if not model_name:
        return None
    lowered = model_name.lower()
    if "opus" in lowered:
        return ModelTier.OPUS
    if "sonnet" in lowered:
        return ModelTier.SONNET
    if "haiku" in lowered:
        return ModelTier.HAIKU
    return None


def select_effective_model_tier(
    ticket_criticality: Optional[str],
    active_rung: CostRung,
    configured_tier: ModelTier,
) -> DownshiftDecision:
    """Decide how a ticket should run given the active cost-ladder rung.

    See the module docstring for full semantics.

    Args:
        ticket_criticality: Substrate ``tickets.criticality`` value —
            one of ``"downshift_eligible"``, ``"frontier_only"``, or
            ``None``. Any other string value is treated as
            ``"frontier_only"`` (fail-CLOSED).
        active_rung: The current rung from
            :meth:`agent.cost_state_holder.CostStateHolder.active_rung`.
        configured_tier: The model tier the ticket would run at if
            there were no cost pressure (typically the agent's
            configured frontier model classified via
            :func:`classify_model_tier`).
    """
    if active_rung is CostRung.NORMAL:
        return DownshiftDecision(
            defer=False,
            effective_tier=configured_tier,
            reason="rung=NORMAL: no cost pressure; run at configured tier",
        )

    if active_rung is CostRung.HARD_STOP_100:
        return DownshiftDecision(
            defer=True,
            effective_tier=None,
            reason="rung=HARD_STOP_100: budget exhausted; defer all tickets",
        )

    is_eligible = ticket_criticality == CRITICALITY_DOWNSHIFT_ELIGIBLE

    if not is_eligible:
        return DownshiftDecision(
            defer=True,
            effective_tier=None,
            reason=(
                f"rung={active_rung.value}: ticket criticality="
                f"{ticket_criticality or 'NULL'} is not downshift-eligible; "
                "defer (transition Sea_Ticket to deferred_cost_limit)"
            ),
        )

    if active_rung is CostRung.WARN_75:
        stepped = _TIER_STEP_DOWN[configured_tier]
        return DownshiftDecision(
            defer=False,
            effective_tier=stepped,
            reason=(
                f"rung=WARN_75: downshift-eligible; "
                f"{configured_tier.value}->{stepped.value} (one tier step)"
            ),
        )

    if active_rung is CostRung.DOWNSHIFT_90:
        return DownshiftDecision(
            defer=False,
            effective_tier=ModelTier.HAIKU,
            reason=(
                f"rung=DOWNSHIFT_90: downshift-eligible; "
                f"{configured_tier.value}->{ModelTier.HAIKU.value} "
                "(drop to cheapest tier)"
            ),
        )

    return DownshiftDecision(
        defer=False,
        effective_tier=configured_tier,
        reason=f"rung={active_rung.value}: unhandled rung; pass through",
    )
