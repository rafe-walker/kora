"""Tests for the 3 phrasebook-promotion endpoints in web_server."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from kora_cli.promote.phrasebook.proposer import PromotionProposal
from kora_cli.promote.phrasebook.store import (
    PROMOTIONS_ROOT_ENV,
    save_pending,
)
from kora_cli.web_server import app


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
    # Phrasebook editor reads ${KORA_HOME}/phrasebook/slack_dm.yml
    # via get_kora_home() — the KORA_HOME monkeypatch above is
    # sufficient. No separate env override needed.
    return tmp_path


def _save_proposal(proposal_id: str = "p1", confidence: float = 0.85):
    p = PromotionProposal(
        proposal_id=proposal_id,
        cluster_size=5,
        sample_questions=["q1", "q2"],
        proposed_pattern="(?i)(burn|cost|budget)",
        proposed_reply_template="Burn is $42 today.",
        proposed_category="cost_query",
        confidence=confidence,
        created_at=datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc),
        status="pending",
        review_notes="",
    )
    return save_pending(p)


def _client():
    """Authenticated TestClient — the cockpit endpoints sit behind
    the dashboard session-header auth middleware, same as every
    other /api/* surface."""
    from kora_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return client


def test_list_pending_returns_proposals_highest_confidence_first(tmp_path):
    _save_proposal(proposal_id="low", confidence=0.4)
    _save_proposal(proposal_id="high", confidence=0.9)
    _save_proposal(proposal_id="mid", confidence=0.6)

    resp = _client().get("/api/promotions/phrasebook/pending")
    assert resp.status_code == 200
    body = resp.json()
    assert [p["proposal_id"] for p in body["proposals"]] == [
        "high",
        "mid",
        "low",
    ]
    assert body["status_values"] == [
        "pending",
        "approved",
        "rejected",
        "expired",
    ]


def test_list_pending_empty_returns_empty_list():
    resp = _client().get("/api/promotions/phrasebook/pending")
    assert resp.status_code == 200
    assert resp.json()["proposals"] == []


def test_approve_404_when_proposal_missing():
    resp = _client().post(
        "/api/promotions/phrasebook/never-existed/approve",
        json={},
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "proposal_not_found"


def test_approve_409_when_proposal_not_pending(tmp_path):
    _save_proposal()
    from kora_cli.promote.phrasebook.store import transition

    transition("p1", new_status="rejected", review_notes="no")
    resp = _client().post(
        "/api/promotions/phrasebook/p1/approve", json={}
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "proposal_not_pending"


def test_approve_happy_path_persists_phrasebook_and_emits_audit(tmp_path):
    _save_proposal()
    resp = _client().post(
        "/api/promotions/phrasebook/p1/approve",
        json={"review_notes": "good catch"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["committed_entry"]["pattern"] == "(?i)(burn|cost|budget)"

    # Audit emitted: promotion.approved + phrasebook.updated.
    rows = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text().splitlines()
        if line
    ]
    seams = [r["seam"] for r in rows]
    assert "promotion.approved" in seams
    assert "phrasebook.updated" in seams
    # phrasebook.updated uses the forward-compat actor literal.
    pb_rows = [r for r in rows if r["seam"] == "phrasebook.updated"]
    assert pb_rows[0]["details"]["actor"] == "kora_proposal_approved"
    assert pb_rows[0]["details"]["proposal_id"] == "p1"


def test_approve_with_overrides_applies_them(tmp_path):
    _save_proposal()
    resp = _client().post(
        "/api/promotions/phrasebook/p1/approve",
        json={
            "pattern_override": "(?i)\\bspend\\b",
            "reply_template_override": "Spend is $42.",
            "category_override": "spending",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["committed_entry"]["pattern"] == "(?i)\\bspend\\b"
    assert body["committed_entry"]["reply_template"] == "Spend is $42."
    assert body["committed_entry"]["category"] == "spending"


def test_approve_validation_failure_returns_422(tmp_path):
    """Malformed regex from override → editor rejects → 422."""
    _save_proposal()
    resp = _client().post(
        "/api/promotions/phrasebook/p1/approve",
        json={"pattern_override": "[unterminated"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "validation_failed"


def test_reject_404_when_proposal_missing():
    resp = _client().post(
        "/api/promotions/phrasebook/x/reject",
        json={"review_notes": "no"},
    )
    assert resp.status_code == 404


def test_reject_409_when_proposal_not_pending(tmp_path):
    _save_proposal()
    from kora_cli.promote.phrasebook.store import transition

    transition("p1", new_status="approved", review_notes="")
    resp = _client().post(
        "/api/promotions/phrasebook/p1/reject",
        json={"review_notes": "actually no"},
    )
    assert resp.status_code == 409


def test_reject_happy_path_moves_proposal_and_emits_audit(tmp_path):
    _save_proposal()
    resp = _client().post(
        "/api/promotions/phrasebook/p1/reject",
        json={"review_notes": "category is wrong"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"

    rows = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text().splitlines()
        if line
    ]
    rejected_rows = [r for r in rows if r["seam"] == "promotion.rejected"]
    assert len(rejected_rows) == 1
    assert rejected_rows[0]["details"]["review_notes"] == "category is wrong"
