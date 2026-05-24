"""Tests for kora_cli.promote.snapshot_expand.proposer."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kora_cli.promote.snapshot_expand.observer import ToolCallObservation
from kora_cli.promote.snapshot_expand.proposer import (
    DEFAULT_MIN_CLUSTER_SIZE,
    cluster_by_tool_name,
    generate_proposals,
)


def _obs(
    tool_name: str,
    *,
    csid: str = "D1JOSH:1",
    arguments_summary: str = "",
) -> ToolCallObservation:
    return ToolCallObservation(
        tool_name=tool_name,
        caller_session_id=csid,
        timestamp=datetime.now(timezone.utc),
        arguments_summary=arguments_summary,
    )


def test_cluster_by_tool_name_groups_exact_match():
    obs = [
        _obs("kora__a", csid="s1"),
        _obs("kora__a", csid="s2"),
        _obs("kora__b", csid="s3"),
    ]
    clusters = cluster_by_tool_name(obs)
    assert set(clusters.keys()) == {"kora__a", "kora__b"}
    assert len(clusters["kora__a"]) == 2
    assert len(clusters["kora__b"]) == 1


def test_generate_proposals_skips_under_min_cluster():
    obs = [
        _obs("kora__rare", csid=f"s{i}") for i in range(DEFAULT_MIN_CLUSTER_SIZE - 1)
    ]
    out = generate_proposals(obs, now=datetime.now(timezone.utc))
    assert out == []


def test_generate_proposals_emits_one_per_cluster():
    obs = (
        [_obs("kora__open_tickets", csid=f"s{i}") for i in range(6)]
        + [_obs("kora__alerts_summary", csid=f"a{i}") for i in range(6)]
        + [_obs("kora__rare", csid="r1")]
    )
    out = generate_proposals(obs, now=datetime.now(timezone.utc))
    names = sorted(p.source_tool_name for p in out)
    assert names == ["kora__alerts_summary", "kora__open_tickets"]


def test_proposed_field_path_strips_kora_prefix():
    obs = [_obs("kora__open_tickets", csid=f"s{i}") for i in range(6)]
    out = generate_proposals(obs, now=datetime.now(timezone.utc))
    assert out[0].proposed_field_path == "open_tickets"


def test_confidence_caps_at_one():
    obs = [_obs("kora__busy", csid=f"s{i}") for i in range(50)]
    out = generate_proposals(obs, now=datetime.now(timezone.utc))
    assert out[0].confidence == 1.0


def test_sample_caller_ids_dedup_and_cap():
    obs = [
        _obs("kora__open_tickets", csid="s1"),
        _obs("kora__open_tickets", csid="s1"),
        _obs("kora__open_tickets", csid="s2"),
        _obs("kora__open_tickets", csid="s3"),
        _obs("kora__open_tickets", csid="s4"),
        _obs("kora__open_tickets", csid="s5"),
    ]
    out = generate_proposals(obs, now=datetime.now(timezone.utc))
    assert len(out) == 1
    assert len(out[0].sample_caller_session_ids) <= 3
    assert len(set(out[0].sample_caller_session_ids)) == len(
        out[0].sample_caller_session_ids
    )


def test_collector_summary_surfaces_dominant_arg_shape():
    obs = [
        _obs(
            "kora__filter_tickets",
            csid=f"s{i}",
            arguments_summary="status=open",
        )
        for i in range(6)
    ]
    out = generate_proposals(obs, now=datetime.now(timezone.utc))
    assert "status=open" in out[0].proposed_collector_summary


def test_proposals_sorted_by_confidence_desc():
    obs = (
        [_obs("kora__low", csid=f"l{i}") for i in range(5)]
        + [_obs("kora__high", csid=f"h{i}") for i in range(10)]
    )
    out = generate_proposals(obs, now=datetime.now(timezone.utc))
    assert out[0].source_tool_name == "kora__high"
    assert out[1].source_tool_name == "kora__low"
