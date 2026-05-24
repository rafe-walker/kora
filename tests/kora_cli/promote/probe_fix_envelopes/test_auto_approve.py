"""Tests for kora_cli.promote.probe_fix_envelopes.auto_approve — KR-CC1-POLISH."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kora_cli.audit.jsonl_sink import (
    BATCH_SIZE_ENV,
    _reset_batching_for_tests,
)
from kora_cli.promote._shared.proposal_store import save_pending
from kora_cli.promote.probe_fix_envelopes.auto_approve import (
    AUTO_APPROVE_ENABLED_ENV,
    AUTO_APPROVE_WAIT_HOURS_ENV,
    is_auto_approve_enabled,
    run_auto_approve_sweep,
)
from kora_cli.promote.probe_fix_envelopes.proposer import (
    ProbeEnvelopeProposal,
    _derive_blast_radius_level,
    generate_proposals,
    proposal_from_dict,
    proposal_to_dict,
)
from kora_cli.promote.probe_fix_envelopes.observer import (
    InvestigationObservation,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setenv("KORA_PROMOTIONS_DIR", str(tmp_path / "promotions"))
    monkeypatch.setenv(
        "KORA_AUDIT_LOG_PATH", str(tmp_path / "kora_audit_log.jsonl")
    )
    monkeypatch.setenv(BATCH_SIZE_ENV, "0")
    monkeypatch.delenv(AUTO_APPROVE_ENABLED_ENV, raising=False)
    monkeypatch.delenv(AUTO_APPROVE_WAIT_HOURS_ENV, raising=False)
    _reset_batching_for_tests()
    yield
    _reset_batching_for_tests()


# ---------------------------------------------------------------------------
# Heuristic — _derive_blast_radius_level
# ---------------------------------------------------------------------------


def test_derive_blast_radius_level_known_low_risk():
    assert _derive_blast_radius_level("fly", "machine_down") == "low"
    assert (
        _derive_blast_radius_level("fly", "single_machine_not_started")
        == "low"
    )


def test_derive_blast_radius_level_default_high():
    assert _derive_blast_radius_level("supabase", "down") == "high"
    assert (
        _derive_blast_radius_level("fly", "deploy_failure_cascade")
        == "high"
    )
    assert _derive_blast_radius_level("vercel", "404_storm") == "high"


def test_proposer_stamps_low_for_known_pattern(monkeypatch):
    monkeypatch.setenv("KORA_PROMOTE_PROBE_FIX_MIN_CLUSTER", "2")
    obs = [
        InvestigationObservation(
            probe="fly",
            issue_category="machine_down",
            severity="warning",
            investigation_summary_text=f"Restart machine #{i}",
            caller_session_id=f"probe:fly:machine_down:{i}",
            timestamp=datetime.now(timezone.utc),
        )
        for i in range(3)
    ]
    out = generate_proposals(obs, now=datetime.now(timezone.utc))
    assert len(out) == 1
    assert out[0].blast_radius_level == "low"


def test_proposer_stamps_high_for_unknown_pattern(monkeypatch):
    monkeypatch.setenv("KORA_PROMOTE_PROBE_FIX_MIN_CLUSTER", "2")
    obs = [
        InvestigationObservation(
            probe="supabase",
            issue_category="connection_pool_exhausted",
            severity="warning",
            investigation_summary_text=f"increase pool {i}",
            caller_session_id=f"probe:supabase:pool:{i}",
            timestamp=datetime.now(timezone.utc),
        )
        for i in range(3)
    ]
    out = generate_proposals(obs, now=datetime.now(timezone.utc))
    assert len(out) == 1
    assert out[0].blast_radius_level == "high"


# ---------------------------------------------------------------------------
# proposal_from_dict — backwards-compat with legacy payloads
# ---------------------------------------------------------------------------


def test_proposal_from_dict_legacy_payload_defaults_to_high():
    """Pre-KR-CC1-POLISH payloads on disk don't carry
    ``blast_radius_level``. Rehydrate defaults to ``"high"`` so
    legacy proposals stay operator-gated."""
    legacy = {
        "proposal_id": "old-p",
        "probe": "fly",
        "issue_category": "machine_down",
        "fix_name_suggestion": "proposed_fly_machine_down",
        "cluster_size": 5,
        "sample_caller_session_ids": ["a", "b"],
        "recurring_recommendation_text": "Restart",
        "blast_radius_summary": "operator must review",
        "confidence": 0.7,
        "created_at": "2026-05-20T12:00:00Z",
        "status": "pending",
        "review_notes": "",
    }
    p = proposal_from_dict(legacy)
    assert p.blast_radius_level == "high"


# ---------------------------------------------------------------------------
# is_auto_approve_enabled — env gate
# ---------------------------------------------------------------------------


def test_auto_approve_disabled_by_default():
    assert is_auto_approve_enabled() is False


def test_auto_approve_enabled_when_truthy(monkeypatch):
    monkeypatch.setenv(AUTO_APPROVE_ENABLED_ENV, "true")
    assert is_auto_approve_enabled() is True


# ---------------------------------------------------------------------------
# Sweep — main behavior
# ---------------------------------------------------------------------------


def _persist_low_risk_proposal(
    tmp_path,
    *,
    proposal_id: str,
    created_at: datetime,
    blast_radius_level: str = "low",
) -> None:
    proposal = ProbeEnvelopeProposal(
        proposal_id=proposal_id,
        probe="fly",
        issue_category="machine_down",
        fix_name_suggestion="proposed_fly_machine_down",
        cluster_size=3,
        sample_caller_session_ids=["a", "b", "c"],
        recurring_recommendation_text="Restart the machine.",
        blast_radius_summary="single-target restart",
        confidence=0.6,
        created_at=created_at,
        status="pending",
        blast_radius_level=blast_radius_level,  # type: ignore[arg-type]
    )
    save_pending(
        loop_name="probe_fix_envelopes",
        proposal_id=proposal_id,
        payload=proposal_to_dict(proposal),
    )


def _read_audit(tmp_path) -> list:
    path = tmp_path / "kora_audit_log.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_sweep_disabled_is_noop(tmp_path):
    """Without the operator opt-in env, sweep returns a no-op
    result + doesn't modify any on-disk state."""
    long_ago = datetime.now(timezone.utc) - timedelta(hours=24)
    _persist_low_risk_proposal(
        tmp_path, proposal_id="p1", created_at=long_ago
    )
    result = run_auto_approve_sweep()
    assert result.approved_count == 0
    assert result.candidates_considered == 0
    # Still in pending.
    pending_dir = tmp_path / "promotions" / "probe_fix_envelopes" / "pending"
    assert (pending_dir / "p1.json").is_file()


def test_sweep_auto_approves_low_risk_past_wait_window(
    tmp_path, monkeypatch
):
    """Eligible low-risk proposal past the wait window → moves to
    approved/ + audit fires."""
    monkeypatch.setenv(AUTO_APPROVE_ENABLED_ENV, "true")
    monkeypatch.setenv(AUTO_APPROVE_WAIT_HOURS_ENV, "1")
    long_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    _persist_low_risk_proposal(
        tmp_path, proposal_id="p1", created_at=long_ago
    )
    result = run_auto_approve_sweep()
    assert result.candidates_considered == 1
    assert result.candidates_under_wait_window == 0
    assert result.approved_count == 1
    # File moved.
    base = tmp_path / "promotions" / "probe_fix_envelopes"
    assert not (base / "pending" / "p1.json").is_file()
    assert (base / "approved" / "p1.json").is_file()
    # Audit row emitted.
    rows = _read_audit(tmp_path)
    auto = [
        r
        for r in rows
        if r["seam"] == "promotion.probe_envelope_action_auto_approved"
    ]
    assert len(auto) == 1
    details = auto[0]["details"]
    assert details["status"] == "approved"
    assert details["auto_approve_wait_hours"] == 1.0
    assert details["proposal_id"] == "p1"


def test_sweep_holds_low_risk_during_wait_window(tmp_path, monkeypatch):
    """Low-risk proposal under the wait window → buffered, not
    approved; counted in candidates_under_wait_window."""
    monkeypatch.setenv(AUTO_APPROVE_ENABLED_ENV, "true")
    monkeypatch.setenv(AUTO_APPROVE_WAIT_HOURS_ENV, "1")
    recent = datetime.now(timezone.utc) - timedelta(minutes=10)
    _persist_low_risk_proposal(
        tmp_path, proposal_id="p1", created_at=recent
    )
    result = run_auto_approve_sweep()
    assert result.candidates_considered == 1
    assert result.candidates_under_wait_window == 1
    assert result.approved_count == 0
    # Still pending.
    base = tmp_path / "promotions" / "probe_fix_envelopes"
    assert (base / "pending" / "p1.json").is_file()


def test_sweep_skips_high_risk_proposals(tmp_path, monkeypatch):
    """High-risk proposals never auto-approve regardless of age."""
    monkeypatch.setenv(AUTO_APPROVE_ENABLED_ENV, "true")
    monkeypatch.setenv(AUTO_APPROVE_WAIT_HOURS_ENV, "1")
    long_ago = datetime.now(timezone.utc) - timedelta(days=5)
    _persist_low_risk_proposal(
        tmp_path,
        proposal_id="p-high",
        created_at=long_ago,
        blast_radius_level="high",
    )
    result = run_auto_approve_sweep()
    assert result.candidates_considered == 0
    assert result.approved_count == 0
    base = tmp_path / "promotions" / "probe_fix_envelopes"
    assert (base / "pending" / "p-high.json").is_file()


def test_sweep_audit_payload_carries_auto_approved_at(
    tmp_path, monkeypatch
):
    """The auto_approved_at timestamp should be present + ISO-8601."""
    monkeypatch.setenv(AUTO_APPROVE_ENABLED_ENV, "true")
    monkeypatch.setenv(AUTO_APPROVE_WAIT_HOURS_ENV, "0.5")
    long_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    _persist_low_risk_proposal(
        tmp_path, proposal_id="p1", created_at=long_ago
    )
    run_auto_approve_sweep()
    rows = _read_audit(tmp_path)
    auto = [
        r
        for r in rows
        if r["seam"] == "promotion.probe_envelope_action_auto_approved"
    ]
    assert len(auto) == 1
    ts = auto[0]["details"]["auto_approved_at"]
    # Round-trip parses cleanly.
    datetime.fromisoformat(ts.replace("Z", "+00:00"))
