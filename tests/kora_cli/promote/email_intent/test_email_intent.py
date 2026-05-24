"""Tests for kora_cli.promote.email_intent — KR-PROMOTE-EMAIL-INTENT."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kora_cli.audit.jsonl_sink import BATCH_SIZE_ENV, _reset_batching_for_tests
from kora_cli.promote.email_intent.observer import (
    EmailIntentObservation,
    collect_recent_logged_only,
)
from kora_cli.promote.email_intent.plugin import (
    ENABLED_ENV,
    run_email_intent_cycle,
)
from kora_cli.promote.email_intent.proposer import (
    MIN_CLUSTER_SIZE_ENV,
    generate_proposals,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setenv("KORA_PROMOTIONS_DIR", str(tmp_path / "promotions"))
    monkeypatch.setenv(
        "KORA_AUDIT_LOG_PATH", str(tmp_path / "kora_audit_log.jsonl")
    )
    monkeypatch.setenv(BATCH_SIZE_ENV, "0")
    _reset_batching_for_tests()
    yield
    _reset_batching_for_tests()


def _write_audit(tmp_path: Path, entries: list) -> None:
    path = tmp_path / "kora_audit_log.jsonl"
    path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )


def _entry(
    *,
    subject: str = "Quick idea about probes",
    action: str = "logged_only",
    pattern: str | None = None,
    reason: str = "no_pattern_matched",
    confidence: str = "unrecognized",
    caller_session_id: str = "email:msg-1",
    emitted_at: datetime | None = None,
) -> dict:
    if emitted_at is None:
        emitted_at = datetime.now(timezone.utc) - timedelta(hours=1)
    return {
        "emitted_at": emitted_at.isoformat(),
        "seam": "intent.email_to_sea_ticket",
        "details": {
            "action": action,
            "subject": subject,
            "pattern_matched": pattern,
            "confidence": confidence,
            "reason": reason,
        },
        "caller_session_id": caller_session_id,
        "source": "email",
    }


# ===========================================================================
# Observer
# ===========================================================================


@pytest.mark.asyncio
async def test_observer_returns_logged_only_rows(tmp_path):
    _write_audit(
        tmp_path,
        [
            _entry(subject="Idea: ship the v2 panel"),
            _entry(subject="Burn warning"),
        ],
    )
    out = await collect_recent_logged_only(
        since=datetime.now(timezone.utc) - timedelta(days=1)
    )
    assert len(out) == 2
    assert all(o.confidence == "unrecognized" for o in out)


@pytest.mark.asyncio
async def test_observer_skips_other_actions(tmp_path):
    _write_audit(
        tmp_path,
        [
            _entry(subject="Logged only", action="logged_only"),
            _entry(subject="Got created", action="created"),
            _entry(subject="Dry run", action="dry_run"),
        ],
    )
    out = await collect_recent_logged_only(
        since=datetime.now(timezone.utc) - timedelta(days=1)
    )
    assert [o.subject for o in out] == ["Logged only"]


@pytest.mark.asyncio
async def test_observer_skips_empty_subjects(tmp_path):
    _write_audit(
        tmp_path,
        [
            _entry(subject=""),
            _entry(subject="   "),
            _entry(subject="real text"),
        ],
    )
    out = await collect_recent_logged_only(
        since=datetime.now(timezone.utc) - timedelta(days=1)
    )
    assert [o.subject for o in out] == ["real text"]


@pytest.mark.asyncio
async def test_observer_respects_since(tmp_path):
    old = datetime.now(timezone.utc) - timedelta(days=30)
    fresh = datetime.now(timezone.utc) - timedelta(hours=1)
    _write_audit(
        tmp_path,
        [
            _entry(subject="too old", emitted_at=old),
            _entry(subject="fresh enough", emitted_at=fresh),
        ],
    )
    out = await collect_recent_logged_only(
        since=datetime.now(timezone.utc) - timedelta(days=7)
    )
    assert [o.subject for o in out] == ["fresh enough"]


# ===========================================================================
# Proposer
# ===========================================================================


def _obs(subject: str, csid: str = "email:c1") -> EmailIntentObservation:
    return EmailIntentObservation(
        subject=subject,
        pattern_matched=None,
        confidence="unrecognized",
        reason="no_pattern_matched",
        caller_session_id=csid,
        timestamp=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_proposer_clusters_similar_subjects(monkeypatch):
    monkeypatch.setenv(MIN_CLUSTER_SIZE_ENV, "2")
    obs = [
        _obs("Question: burn rate for this week", csid=f"q{i}")
        for i in range(3)
    ] + [
        _obs("Unrelated thing entirely about birds", csid="other")
    ]
    out = await generate_proposals(obs, now=datetime.now(timezone.utc))
    # The 3 burn-rate subjects should land in one cluster (≥2 min);
    # the bird subject is alone and below threshold.
    assert len(out) == 1
    assert out[0].cluster_size == 3
    # Pattern is alternation of top-3 cross-subject tokens — exact
    # token choice depends on Counter tie-break (the burn-rate
    # subjects have multiple tokens tied at frequency 3). Assert
    # the pattern contains at least one of the cluster's meaningful
    # tokens rather than pinning a specific one.
    pat_lower = out[0].proposed_pattern.lower()
    assert any(
        tok in pat_lower
        for tok in ("question", "burn", "rate", "week")
    )


@pytest.mark.asyncio
async def test_proposer_skips_under_min_cluster(monkeypatch):
    monkeypatch.setenv(MIN_CLUSTER_SIZE_ENV, "3")
    obs = [_obs(f"Idea about X {i}", csid=f"c{i}") for i in range(2)]
    out = await generate_proposals(obs, now=datetime.now(timezone.utc))
    assert out == []


@pytest.mark.asyncio
async def test_proposer_default_action_kind_is_save_note(monkeypatch):
    monkeypatch.setenv(MIN_CLUSTER_SIZE_ENV, "2")
    obs = [_obs(f"Idea: cool feature ABC {i}", csid=f"c{i}") for i in range(3)]
    out = await generate_proposals(obs, now=datetime.now(timezone.utc))
    assert len(out) == 1
    assert out[0].proposed_action_kind == "save_note"


@pytest.mark.asyncio
async def test_proposer_pattern_is_safe_regex(monkeypatch):
    """Subjects with regex metacharacters don't bomb the engine —
    derive_pattern escapes them."""
    import re as _re

    monkeypatch.setenv(MIN_CLUSTER_SIZE_ENV, "2")
    obs = [
        _obs(f"Note (urgent) about $foo {i}", csid=f"c{i}") for i in range(3)
    ]
    out = await generate_proposals(obs, now=datetime.now(timezone.utc))
    assert len(out) == 1
    # The pattern must compile without error.
    _re.compile(out[0].proposed_pattern)


@pytest.mark.asyncio
async def test_proposer_sample_subjects_deduped_and_capped(monkeypatch):
    monkeypatch.setenv(MIN_CLUSTER_SIZE_ENV, "2")
    obs = [_obs("Same subject", csid=f"c{i}") for i in range(8)]
    out = await generate_proposals(obs, now=datetime.now(timezone.utc))
    assert len(out) == 1
    # Same subject dedups to 1 in the sample list.
    assert out[0].sample_subjects == ["Same subject"]
    # Sample callers list cap.
    assert len(out[0].sample_caller_session_ids) <= 3


# ===========================================================================
# Cycle
# ===========================================================================


def _read_audit(tmp_path) -> list:
    path = tmp_path / "kora_audit_log.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.mark.asyncio
async def test_cycle_emits_correct_seam(tmp_path, monkeypatch):
    monkeypatch.setenv(MIN_CLUSTER_SIZE_ENV, "2")
    _write_audit(
        tmp_path,
        [
            _entry(
                subject="Idea: new dashboard panel for cost burn",
                caller_session_id=f"email:msg-{i}",
            )
            for i in range(3)
        ],
    )
    summary = await run_email_intent_cycle()
    assert summary["enabled"] is True
    assert summary["observations_read"] == 3
    assert summary["proposals_generated"] == 1
    assert summary["proposals_persisted"] == 1
    assert summary["auto_apply_mode"] is False

    rows = _read_audit(tmp_path)
    promo = [
        r
        for r in rows
        if r["seam"] == "promotion.email_intent_pattern_proposed"
    ]
    assert len(promo) == 1
    assert promo[0]["details"]["action"] == "proposed"
    assert promo[0]["source"] == "email"


@pytest.mark.asyncio
async def test_cycle_disabled_short_circuits(monkeypatch):
    monkeypatch.setenv(ENABLED_ENV, "false")
    summary = await run_email_intent_cycle()
    assert summary["enabled"] is False
    assert summary["proposals_generated"] == 0
