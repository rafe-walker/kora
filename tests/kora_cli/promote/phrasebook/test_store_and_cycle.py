"""Tests for the promotion store + cycle orchestrator."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from kora_cli.promote.phrasebook.proposer import PromotionProposal
from kora_cli.promote.phrasebook.store import (
    PROMOTIONS_ROOT_ENV,
    ProposalNotFound,
    expire_older_than,
    list_pending,
    load,
    save_pending,
    transition,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setattr(
        "kora_constants.get_kora_home", lambda: tmp_path, raising=False
    )
    monkeypatch.setenv(PROMOTIONS_ROOT_ENV, str(tmp_path / "promotions"))
    monkeypatch.setenv(
        "KORA_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl")
    )
    monkeypatch.setenv(
        "KORA_SLACK_DM_LOG_PATH", str(tmp_path / "slack_dm_log.jsonl")
    )
    return tmp_path


def _mk(status="pending", proposal_id="p1", confidence=0.5) -> PromotionProposal:
    return PromotionProposal(
        proposal_id=proposal_id,
        cluster_size=5,
        sample_questions=["q1", "q2"],
        proposed_pattern="(?i)(burn)",
        proposed_reply_template="Burn is $42.",
        proposed_category="cost_query",
        confidence=confidence,
        created_at=datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc),
        status=status,
        review_notes="",
    )


def test_save_pending_writes_json_under_pending_dir(tmp_path):
    p = _mk()
    written = save_pending(p)
    assert written.is_file()
    assert written.parent.name == "pending"
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload["proposal_id"] == "p1"


def test_save_pending_refuses_non_pending_status():
    p = _mk(status="approved")
    with pytest.raises(ValueError):
        save_pending(p)


def test_list_pending_orders_highest_confidence_first():
    save_pending(_mk(proposal_id="p_low", confidence=0.2))
    save_pending(_mk(proposal_id="p_high", confidence=0.9))
    save_pending(_mk(proposal_id="p_mid", confidence=0.5))
    out = list_pending()
    assert [p.proposal_id for p in out] == ["p_high", "p_mid", "p_low"]


def test_transition_pending_to_approved_moves_file(tmp_path):
    save_pending(_mk())
    updated = transition(
        "p1", new_status="approved", review_notes="LGTM"
    )
    assert updated.status == "approved"
    assert updated.review_notes == "LGTM"
    assert not (tmp_path / "promotions" / "phrasebook" / "pending" / "p1.json").exists()
    assert (
        tmp_path / "promotions" / "phrasebook" / "approved" / "p1.json"
    ).is_file()


def test_transition_with_overrides_applies_them():
    save_pending(_mk())
    updated = transition(
        "p1",
        new_status="approved",
        review_notes="",
        overrides={
            "pattern": "(?i)(burn|spend|cost)",
            "reply_template": "Custom template",
            "category": "custom",
        },
    )
    assert updated.proposed_pattern == "(?i)(burn|spend|cost)"
    assert updated.proposed_reply_template == "Custom template"
    assert updated.proposed_category == "custom"


def test_load_returns_proposal_across_status_dirs():
    save_pending(_mk(proposal_id="p_findme"))
    out = load("p_findme")
    assert out.proposal_id == "p_findme"
    transition("p_findme", new_status="rejected", review_notes="no")
    out2 = load("p_findme")
    assert out2.status == "rejected"


def test_load_missing_raises():
    with pytest.raises(ProposalNotFound):
        load("never-existed")


def test_transition_missing_raises():
    with pytest.raises(ProposalNotFound):
        transition("never-existed", new_status="approved")


def test_expire_older_than_moves_old_pending(tmp_path):
    old = PromotionProposal(
        proposal_id="old_p",
        cluster_size=5,
        sample_questions=[],
        proposed_pattern="(?i)(x)",
        proposed_reply_template="x",
        proposed_category="x",
        confidence=0.5,
        created_at=datetime.now(timezone.utc) - timedelta(days=30),
        status="pending",
    )
    fresh = _mk(proposal_id="fresh_p")
    save_pending(old)
    save_pending(fresh)
    moved = expire_older_than(days=14)
    assert moved == 1
    pending_after = list_pending()
    assert [p.proposal_id for p in pending_after] == ["fresh_p"]


# ===========================================================================
# Cycle integration
# ===========================================================================


@pytest.mark.asyncio
async def test_cycle_with_no_observations_returns_summary(tmp_path):
    from kora_cli.promote.phrasebook.cycle import (
        run_phrasebook_promotion_cycle,
    )

    summary = await run_phrasebook_promotion_cycle()
    assert summary["enabled"] is True
    assert summary["observations_read"] == 0
    assert summary["proposals_generated"] == 0
    assert summary["proposals_persisted"] == 0


@pytest.mark.asyncio
async def test_cycle_disabled_skips_cleanly(tmp_path, monkeypatch):
    from kora_cli.promote.phrasebook.cycle import (
        ENABLED_ENV,
        run_phrasebook_promotion_cycle,
    )

    monkeypatch.setenv(ENABLED_ENV, "false")
    summary = await run_phrasebook_promotion_cycle()
    assert summary["enabled"] is False
    assert summary["observations_read"] == 0


@pytest.mark.asyncio
async def test_cycle_with_synthetic_observations_emits_audit(tmp_path):
    """End-to-end: synthetic slack_dm_log → cycle → proposals
    persisted + audit row per proposal."""
    log_path = tmp_path / "slack_dm_log.jsonl"
    entries = []
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    for i in range(6):
        entries.append(
            {
                "sent_at": (base + timedelta(minutes=i)).isoformat(),
                "channel_id": "D1JOSH",
                "thread_ts": None,
                "text": "Burn is $42 today; 75% of budget used.",
                "slack_message_ts": f"1.{i}",
                "send_status": "ok",
                "model_used": "claude-haiku-4-5-20251001",
                "input_tokens": 1000,
                "output_tokens": 30,
                "caller_session_id": f"D1JOSH:170000000{i}.1",
            }
        )
    log_path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n",
        encoding="utf-8",
    )

    from kora_cli.promote.phrasebook.cycle import (
        run_phrasebook_promotion_cycle,
    )

    summary = await run_phrasebook_promotion_cycle()
    assert summary["observations_read"] == 6
    assert summary["proposals_generated"] == 1
    assert summary["proposals_persisted"] == 1

    pending = list_pending()
    assert len(pending) == 1
    assert pending[0].cluster_size == 6

    # Audit row emitted.
    audit_path = tmp_path / "audit.jsonl"
    rows = [
        json.loads(line)
        for line in audit_path.read_text().splitlines()
        if line
    ]
    proposed_rows = [
        r for r in rows if r["seam"] == "promotion.proposed"
    ]
    assert len(proposed_rows) == 1
    assert proposed_rows[0]["details"]["cluster_size"] == 6
    assert "synth_cost_usd" in proposed_rows[0]["details"]
