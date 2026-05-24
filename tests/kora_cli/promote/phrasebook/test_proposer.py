"""Tests for kora_cli.promote.phrasebook.proposer."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kora_cli.promote.phrasebook.observer import ReasoningObservation
from kora_cli.promote.phrasebook.proposer import (
    DEFAULT_MIN_CLUSTER_SIZE,
    PromotionProposal,
    generate_proposals,
    proposal_from_dict,
    proposal_to_dict,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setattr(
        "kora_constants.get_kora_home", lambda: tmp_path, raising=False
    )
    return tmp_path


def _obs(text: str, idx: int) -> ReasoningObservation:
    return ReasoningObservation(
        operator_question="",
        kora_response=text,
        timestamp=datetime(2026, 5, 24, 0, 0, idx, tzinfo=timezone.utc),
        caller_session_id=f"D1JOSH:170000000{idx}.1",
        cost_usd=0.001,
        model_used="claude-haiku-4-5-20251001",
        route="slack_dm",
    )


# ===========================================================================
# Synthetic clusters → proposal
# ===========================================================================


@pytest.mark.asyncio
async def test_consistent_cluster_yields_proposal():
    """5 near-identical answers about burn → one proposal."""
    observations = [
        _obs("Burn is $42 today; 75% of budget used.", i)
        for i in range(5)
    ]
    proposals, cost = await generate_proposals(observations)
    assert len(proposals) == 1
    assert cost == 0.0  # no Haiku synthesis by default
    p = proposals[0]
    assert p.cluster_size == 5
    assert p.proposed_pattern.startswith("(?i)(")
    # Pattern is an alternation over the top tokens — verify it
    # compiles + matches the source text (without pinning which
    # specific tokens ranked top-3, since identical-document
    # df-ties are insertion-order dependent).
    import re as _re

    compiled = _re.compile(p.proposed_pattern)
    assert compiled.search("Burn is $42 today; 75% of budget used.")
    assert p.proposed_reply_template == "Burn is $42 today; 75% of budget used."
    assert p.haiku_synthesized is False
    assert p.status == "pending"


@pytest.mark.asyncio
async def test_small_cluster_not_proposed():
    """Below min_cluster_size → no proposal."""
    observations = [_obs("Burn is $42", i) for i in range(3)]
    proposals, _ = await generate_proposals(observations)
    assert proposals == []


@pytest.mark.asyncio
async def test_divergent_answers_cluster_rejected():
    """5 messages about burn but each gives a wildly different
    answer → answer-consistency check rejects the cluster."""
    observations = [
        _obs("Burn is $42 today; 75% of budget used.", 1),
        _obs("Burn rate fluctuates wildly across the month.", 2),
        _obs("Burn? not sure, check the cost panel directly.", 3),
        _obs("Burn is high; reduce Opus usage if you can.", 4),
        _obs("Burn metric is fine; nothing to worry about.", 5),
    ]
    proposals, _ = await generate_proposals(
        observations, cohesion_threshold=0.2
    )
    # With a loose cohesion threshold the cluster forms, but
    # answer-consistency check below 0.75 should reject. With a
    # tighter cohesion threshold the cluster doesn't form at
    # all. Either way: no proposal.
    assert proposals == []


@pytest.mark.asyncio
async def test_two_distinct_clusters_yield_two_proposals():
    burn = [
        _obs("Burn is $42 today; 75% of budget used.", i) for i in range(5)
    ]
    paused = [
        # State-query phrasing — avoids "cost"/"burn" tokens so
        # the categorizer reaches the state keywords first.
        _obs("Yes, you're paused; primary_state is PAUSED.", 10 + i)
        for i in range(5)
    ]
    proposals, _ = await generate_proposals(burn + paused)
    assert len(proposals) == 2
    cats = sorted(p.proposed_category for p in proposals)
    # Burn and state-query keywords drive the categories.
    assert "cost_query" in cats
    assert "state_query" in cats


@pytest.mark.asyncio
async def test_empty_observations_yields_empty():
    proposals, cost = await generate_proposals([])
    assert proposals == []
    assert cost == 0.0


@pytest.mark.asyncio
async def test_proposals_sorted_highest_confidence_first():
    """5 perfect-cohesion observations + 6 looser ones should
    rank perfect-cohesion higher."""
    perfect = [_obs("Burn is $42.", i) for i in range(5)]
    looser = [
        _obs("Yes, primary_state PAUSED at 09:00 UTC.", i + 10)
        for i in range(6)
    ]
    proposals, _ = await generate_proposals(
        perfect + looser, cohesion_threshold=0.5
    )
    assert len(proposals) == 2
    # Confidences non-decreasing; first should be the perfect
    # cluster.
    assert proposals[0].confidence >= proposals[1].confidence


# ===========================================================================
# Haiku synthesis (mocked)
# ===========================================================================


@pytest.mark.asyncio
async def test_synthesizer_invoked_one_cost_recorded():
    observations = [_obs("Burn is $42 today.", i) for i in range(5)]
    calls = []

    async def fake_synth(obs_list):
        calls.append(len(obs_list))
        return ("Synthesized template body", 0.0012)

    proposals, total_cost = await generate_proposals(
        observations, synthesize_reply_template=fake_synth
    )
    assert len(proposals) == 1
    assert proposals[0].haiku_synthesized is True
    assert proposals[0].proposed_reply_template == (
        "Synthesized template body"
    )
    assert calls == [5]
    assert total_cost == pytest.approx(0.0012)


# ===========================================================================
# Serialization
# ===========================================================================


def test_proposal_round_trip_to_dict_and_back():
    p = PromotionProposal(
        proposal_id="abc-123",
        cluster_size=7,
        sample_questions=["q1", "q2", "q3"],
        proposed_pattern="(?i)(burn|cost)",
        proposed_reply_template="Burn is $42.",
        proposed_category="cost_query",
        confidence=0.92,
        created_at=datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc),
        status="pending",
        review_notes="",
        cluster_caller_session_ids=["D1:1", "D1:2"],
        haiku_synthesized=False,
    )
    out = proposal_from_dict(proposal_to_dict(p))
    assert out.proposal_id == p.proposal_id
    assert out.cluster_size == p.cluster_size
    assert out.proposed_pattern == p.proposed_pattern
    assert out.proposed_reply_template == p.proposed_reply_template
    assert out.proposed_category == p.proposed_category
    assert out.confidence == p.confidence
    assert out.status == p.status
    assert out.created_at == p.created_at
    assert out.cluster_caller_session_ids == p.cluster_caller_session_ids
    assert out.haiku_synthesized == p.haiku_synthesized


def test_proposal_from_dict_handles_z_suffix():
    p = proposal_from_dict(
        {
            "proposal_id": "x",
            "cluster_size": 5,
            "sample_questions": [],
            "proposed_pattern": "(?i)(burn)",
            "proposed_reply_template": "x",
            "proposed_category": "x",
            "confidence": 0.5,
            "created_at": "2026-05-24T12:00:00Z",
            "status": "pending",
        }
    )
    assert p.created_at.tzinfo is not None


def test_default_min_cluster_size_is_5():
    assert DEFAULT_MIN_CLUSTER_SIZE == 5
