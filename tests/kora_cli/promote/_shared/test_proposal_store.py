"""Tests for kora_cli.promote._shared.proposal_store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kora_cli.promote._shared.proposal_store import (
    ProposalNotFound,
    expire_older_than,
    list_by_status,
    load,
    save_pending,
    transition,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setenv("KORA_PROMOTIONS_DIR", str(tmp_path / "promotions"))
    return tmp_path


def _payload(proposal_id: str = "prop-1", **extra) -> dict:
    base = {
        "proposal_id": proposal_id,
        "status": "pending",
        "created_at": "2026-05-23T12:00:00Z",
    }
    base.update(extra)
    return base


def test_save_pending_writes_atomically(tmp_path):
    save_pending(loop_name="foo", proposal_id="p1", payload=_payload("p1"))
    target = tmp_path / "promotions" / "foo" / "pending" / "p1.json"
    assert target.is_file()
    written = json.loads(target.read_text())
    assert written["proposal_id"] == "p1"


def test_list_by_status_returns_dicts():
    save_pending(loop_name="foo", proposal_id="p1", payload=_payload("p1"))
    save_pending(loop_name="foo", proposal_id="p2", payload=_payload("p2"))
    items = list_by_status(loop_name="foo", status="pending")
    assert sorted(p["proposal_id"] for p in items) == ["p1", "p2"]


def test_load_returns_status_and_payload():
    save_pending(loop_name="foo", proposal_id="p1", payload=_payload("p1"))
    status, payload = load(loop_name="foo", proposal_id="p1")
    assert status == "pending"
    assert payload["proposal_id"] == "p1"


def test_load_raises_when_missing():
    with pytest.raises(ProposalNotFound):
        load(loop_name="foo", proposal_id="ghost")


def test_transition_moves_file_between_dirs(tmp_path):
    save_pending(loop_name="foo", proposal_id="p1", payload=_payload("p1"))
    old_status, payload = transition(
        loop_name="foo",
        proposal_id="p1",
        new_status="approved",
        payload_mutator=lambda p: p.update({"review_notes": "shipping it"}),
    )
    assert old_status == "pending"
    assert payload["review_notes"] == "shipping it"
    # Old location gone, new location populated.
    assert not (tmp_path / "promotions" / "foo" / "pending" / "p1.json").is_file()
    assert (tmp_path / "promotions" / "foo" / "approved" / "p1.json").is_file()


def test_transition_rejects_unknown_status():
    save_pending(loop_name="foo", proposal_id="p1", payload=_payload("p1"))
    with pytest.raises(ValueError):
        transition(
            loop_name="foo", proposal_id="p1", new_status="archived"
        )


def test_expire_older_than_moves_old_pending():
    save_pending(
        loop_name="foo",
        proposal_id="old",
        payload=_payload("old", created_at="2020-01-01T00:00:00Z"),
    )
    save_pending(loop_name="foo", proposal_id="new", payload=_payload("new"))
    moved = expire_older_than(loop_name="foo", days=7)
    assert moved == 1
    # old now in expired/, new stays in pending/
    expired = list_by_status(loop_name="foo", status="expired")
    pending = list_by_status(loop_name="foo", status="pending")
    assert [p["proposal_id"] for p in expired] == ["old"]
    assert [p["proposal_id"] for p in pending] == ["new"]


def test_per_loop_dirs_dont_collide():
    save_pending(loop_name="alpha", proposal_id="x", payload=_payload("x"))
    save_pending(loop_name="beta", proposal_id="x", payload=_payload("x"))
    assert len(list_by_status(loop_name="alpha", status="pending")) == 1
    assert len(list_by_status(loop_name="beta", status="pending")) == 1
