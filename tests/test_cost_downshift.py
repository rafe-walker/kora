"""Unit tests for ``agent/cost_downshift.py`` (KR-P2-K ST3).

Covers ``select_effective_model_tier`` across all 4 rungs x 3
criticality values (downshift_eligible / frontier_only / NULL) plus
the model-tier classifier helper.
"""

from __future__ import annotations

import dataclasses

import pytest

from agent.cost_downshift import (
    CRITICALITY_DOWNSHIFT_ELIGIBLE,
    CRITICALITY_FRONTIER_ONLY,
    DownshiftDecision,
    ModelTier,
    classify_model_tier,
    select_effective_model_tier,
)
from agent.cost_state_holder import CostRung


# ---------------------------------------------------------------------------
# NORMAL rung — no downshift regardless of criticality
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "criticality",
    [CRITICALITY_DOWNSHIFT_ELIGIBLE, CRITICALITY_FRONTIER_ONLY, None],
)
@pytest.mark.parametrize(
    "configured_tier", [ModelTier.OPUS, ModelTier.SONNET, ModelTier.HAIKU]
)
def test_normal_rung_runs_at_configured_tier(criticality, configured_tier):
    decision = select_effective_model_tier(
        criticality, CostRung.NORMAL, configured_tier
    )
    assert decision.defer is False
    assert decision.effective_tier is configured_tier
    assert "NORMAL" in decision.reason


# ---------------------------------------------------------------------------
# WARN_75 rung — downshift_eligible steps one tier; others defer
# ---------------------------------------------------------------------------


def test_warn_75_eligible_opus_steps_to_sonnet():
    decision = select_effective_model_tier(
        CRITICALITY_DOWNSHIFT_ELIGIBLE, CostRung.WARN_75, ModelTier.OPUS
    )
    assert decision.defer is False
    assert decision.effective_tier is ModelTier.SONNET
    assert "WARN_75" in decision.reason
    assert "opus" in decision.reason and "sonnet" in decision.reason


def test_warn_75_eligible_sonnet_steps_to_haiku():
    decision = select_effective_model_tier(
        CRITICALITY_DOWNSHIFT_ELIGIBLE, CostRung.WARN_75, ModelTier.SONNET
    )
    assert decision.defer is False
    assert decision.effective_tier is ModelTier.HAIKU


def test_warn_75_eligible_haiku_floors_at_haiku():
    """Haiku is already the cheapest tier — step-down floors here."""
    decision = select_effective_model_tier(
        CRITICALITY_DOWNSHIFT_ELIGIBLE, CostRung.WARN_75, ModelTier.HAIKU
    )
    assert decision.defer is False
    assert decision.effective_tier is ModelTier.HAIKU


def test_warn_75_frontier_only_defers():
    decision = select_effective_model_tier(
        CRITICALITY_FRONTIER_ONLY, CostRung.WARN_75, ModelTier.OPUS
    )
    assert decision.defer is True
    assert decision.effective_tier is None
    assert "deferred_cost_limit" in decision.reason
    assert "frontier_only" in decision.reason


def test_warn_75_null_criticality_defers_fail_closed():
    """NULL criticality (non-sea ticket) is treated as frontier_only.
    Fail-CLOSED per PM ruling A1: don't downshift unknown policies."""
    decision = select_effective_model_tier(
        None, CostRung.WARN_75, ModelTier.OPUS
    )
    assert decision.defer is True
    assert decision.effective_tier is None
    assert "NULL" in decision.reason


def test_warn_75_unknown_string_criticality_defers():
    """An unexpected string value (e.g. legacy bucket-spec's 'normal')
    is not equal to ``downshift_eligible`` and therefore defers."""
    decision = select_effective_model_tier(
        "normal", CostRung.WARN_75, ModelTier.OPUS
    )
    assert decision.defer is True
    assert decision.effective_tier is None


# ---------------------------------------------------------------------------
# DOWNSHIFT_90 rung — eligible drops straight to Haiku; others defer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "configured_tier", [ModelTier.OPUS, ModelTier.SONNET, ModelTier.HAIKU]
)
def test_downshift_90_eligible_always_drops_to_haiku(configured_tier):
    decision = select_effective_model_tier(
        CRITICALITY_DOWNSHIFT_ELIGIBLE, CostRung.DOWNSHIFT_90, configured_tier
    )
    assert decision.defer is False
    assert decision.effective_tier is ModelTier.HAIKU
    assert "DOWNSHIFT_90" in decision.reason


def test_downshift_90_frontier_only_defers():
    decision = select_effective_model_tier(
        CRITICALITY_FRONTIER_ONLY, CostRung.DOWNSHIFT_90, ModelTier.OPUS
    )
    assert decision.defer is True
    assert decision.effective_tier is None
    assert "deferred_cost_limit" in decision.reason


def test_downshift_90_null_criticality_defers():
    decision = select_effective_model_tier(
        None, CostRung.DOWNSHIFT_90, ModelTier.OPUS
    )
    assert decision.defer is True
    assert decision.effective_tier is None


# ---------------------------------------------------------------------------
# HARD_STOP_100 rung — defer everything
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "criticality",
    [CRITICALITY_DOWNSHIFT_ELIGIBLE, CRITICALITY_FRONTIER_ONLY, None],
)
@pytest.mark.parametrize(
    "configured_tier", [ModelTier.OPUS, ModelTier.SONNET, ModelTier.HAIKU]
)
def test_hard_stop_100_defers_all(criticality, configured_tier):
    decision = select_effective_model_tier(
        criticality, CostRung.HARD_STOP_100, configured_tier
    )
    assert decision.defer is True
    assert decision.effective_tier is None
    assert "HARD_STOP_100" in decision.reason


# ---------------------------------------------------------------------------
# DownshiftDecision invariants
# ---------------------------------------------------------------------------


def test_downshift_decision_is_frozen():
    decision = select_effective_model_tier(
        CRITICALITY_DOWNSHIFT_ELIGIBLE, CostRung.NORMAL, ModelTier.OPUS
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.defer = True  # type: ignore[misc]


@pytest.mark.parametrize(
    "criticality",
    [CRITICALITY_DOWNSHIFT_ELIGIBLE, CRITICALITY_FRONTIER_ONLY, None],
)
@pytest.mark.parametrize(
    "rung",
    [
        CostRung.NORMAL,
        CostRung.WARN_75,
        CostRung.DOWNSHIFT_90,
        CostRung.HARD_STOP_100,
    ],
)
def test_decision_reason_is_always_non_empty(criticality, rung):
    decision = select_effective_model_tier(criticality, rung, ModelTier.OPUS)
    assert decision.reason
    assert len(decision.reason) > 0


@pytest.mark.parametrize(
    "criticality",
    [CRITICALITY_DOWNSHIFT_ELIGIBLE, CRITICALITY_FRONTIER_ONLY, None],
)
@pytest.mark.parametrize(
    "rung",
    [
        CostRung.NORMAL,
        CostRung.WARN_75,
        CostRung.DOWNSHIFT_90,
        CostRung.HARD_STOP_100,
    ],
)
def test_defer_implies_no_effective_tier(criticality, rung):
    """Invariant: ``defer=True`` ⇒ ``effective_tier is None`` and
    vice versa."""
    decision = select_effective_model_tier(criticality, rung, ModelTier.OPUS)
    if decision.defer:
        assert decision.effective_tier is None
    else:
        assert decision.effective_tier is not None


# ---------------------------------------------------------------------------
# classify_model_tier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("claude-opus-4-7", ModelTier.OPUS),
        ("anthropic/claude-opus-4.6", ModelTier.OPUS),
        ("claude-opus-4-6-20250414", ModelTier.OPUS),
        ("Claude-Opus-4-5", ModelTier.OPUS),  # case-insensitive
        ("claude-sonnet-4-6", ModelTier.SONNET),
        ("anthropic/claude-sonnet-4-5", ModelTier.SONNET),
        ("claude-haiku-4-5", ModelTier.HAIKU),
        ("anthropic.claude-haiku-4-5", ModelTier.HAIKU),
    ],
)
def test_classify_model_tier_known_names(name, expected):
    assert classify_model_tier(name) is expected


@pytest.mark.parametrize("name", [None, "", "gpt-4", "llama-3-70b", "unknown-model"])
def test_classify_model_tier_unknown_returns_none(name):
    assert classify_model_tier(name) is None


# ---------------------------------------------------------------------------
# ModelTier enum sanity
# ---------------------------------------------------------------------------


def test_model_tier_string_values():
    assert ModelTier.OPUS.value == "opus"
    assert ModelTier.SONNET.value == "sonnet"
    assert ModelTier.HAIKU.value == "haiku"


def test_downshift_decision_signature_matches_bucket_sketch():
    """Returns a DownshiftDecision with defer / effective_tier /
    reason fields — superset of bucket sketch's
    ``tuple[ModelTier, Optional[str]]`` for explicit defer signal."""
    decision = select_effective_model_tier(
        CRITICALITY_DOWNSHIFT_ELIGIBLE, CostRung.WARN_75, ModelTier.OPUS
    )
    assert isinstance(decision, DownshiftDecision)
    assert hasattr(decision, "defer")
    assert hasattr(decision, "effective_tier")
    assert hasattr(decision, "reason")
