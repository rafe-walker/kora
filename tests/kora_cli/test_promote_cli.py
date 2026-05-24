"""Tests for kora_cli.promote_cli — KR-CC1-POLISH (#198).

Covers the four ``kora promote`` subcommands' Python handlers
(without invoking the argparse layer — each handler is called
directly with a ``SimpleNamespace`` args object).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from kora_cli.audit.jsonl_sink import (
    BATCH_SIZE_ENV,
    _reset_batching_for_tests,
)
from kora_cli.promote._shared.proposal_store import save_pending
from kora_cli.promote_cli import LOOP_NAMES, cmd_promote


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setenv(
        "KORA_PROMOTIONS_DIR", str(tmp_path / "promotions")
    )
    monkeypatch.setenv(
        "KORA_AUDIT_LOG_PATH", str(tmp_path / "kora_audit_log.jsonl")
    )
    monkeypatch.setenv(BATCH_SIZE_ENV, "0")
    _reset_batching_for_tests()
    yield
    _reset_batching_for_tests()


def _stdout_json(capsys) -> dict:
    captured = capsys.readouterr()
    return json.loads(captured.out)


# ---------------------------------------------------------------------------
# Loop registry coverage — must include all 6
# ---------------------------------------------------------------------------


def test_loop_names_covers_all_six_loops():
    assert set(LOOP_NAMES) == {
        "phrasebook",
        "snapshot_expand",
        "router_tuning",
        "tool_trimming",
        "probe_fix_envelopes",
        "email_intent",
    }


# ---------------------------------------------------------------------------
# status — empty + populated
# ---------------------------------------------------------------------------


def test_status_with_no_proposals_returns_zero_counts(capsys):
    code = cmd_promote(SimpleNamespace(promote_command="status"))
    assert code == 0
    out = _stdout_json(capsys)
    loops = {row["loop"]: row for row in out["loops"]}
    assert set(loops.keys()) == set(LOOP_NAMES)
    # Phrasebook (standard layout) → 0/0/0/0.
    p = loops["phrasebook"]
    assert p["store_layout"] == "standard"
    assert p["counts"] == {
        "pending": 0,
        "approved": 0,
        "rejected": 0,
        "expired": 0,
    }
    # snapshot_expand is the special-case applied-only layout.
    se = loops["snapshot_expand"]
    assert se["store_layout"] == "applied_only"
    assert se["applied_count"] == 0


def test_status_reflects_persisted_proposals(capsys, tmp_path):
    save_pending(
        loop_name="router_tuning",
        proposal_id="p1",
        payload={
            "proposal_id": "p1",
            "status": "pending",
            "created_at": "2026-05-24T00:00:00Z",
        },
    )
    save_pending(
        loop_name="router_tuning",
        proposal_id="p2",
        payload={
            "proposal_id": "p2",
            "status": "pending",
            "created_at": "2026-05-24T01:00:00Z",
        },
    )
    code = cmd_promote(SimpleNamespace(promote_command="status"))
    assert code == 0
    out = _stdout_json(capsys)
    rt = next(r for r in out["loops"] if r["loop"] == "router_tuning")
    assert rt["counts"]["pending"] == 2
    assert rt["last_activity_at"] is not None


# ---------------------------------------------------------------------------
# pending — happy + snapshot_expand special-case
# ---------------------------------------------------------------------------


def test_pending_returns_payloads_sorted_by_confidence(capsys):
    save_pending(
        loop_name="phrasebook",
        proposal_id="low",
        payload={
            "proposal_id": "low",
            "status": "pending",
            "confidence": 0.3,
        },
    )
    save_pending(
        loop_name="phrasebook",
        proposal_id="high",
        payload={
            "proposal_id": "high",
            "status": "pending",
            "confidence": 0.9,
        },
    )
    code = cmd_promote(
        SimpleNamespace(promote_command="pending", loop="phrasebook")
    )
    assert code == 0
    out = _stdout_json(capsys)
    assert [p["proposal_id"] for p in out["pending"]] == ["high", "low"]


def test_pending_snapshot_expand_returns_structured_error(capsys):
    """snapshot_expand has no pending/ — should error cleanly."""
    code = cmd_promote(
        SimpleNamespace(promote_command="pending", loop="snapshot_expand")
    )
    assert code == 1
    out = _stdout_json(capsys)
    assert "audit-only" in out["error"]


def test_pending_unknown_loop_returns_error(capsys):
    code = cmd_promote(
        SimpleNamespace(promote_command="pending", loop="bogus")
    )
    assert code == 1
    out = _stdout_json(capsys)
    assert "must be one of" in out["error"]


# ---------------------------------------------------------------------------
# history — audit JSONL projection
# ---------------------------------------------------------------------------


def _write_audit(tmp_path: Path, entries: list) -> None:
    path = tmp_path / "kora_audit_log.jsonl"
    path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )


def test_history_returns_recent_audit_rows(capsys, tmp_path):
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    _write_audit(
        tmp_path,
        [
            {
                "emitted_at": now.isoformat(),
                "seam": "promotion.router_trigger_proposed",
                "details": {"route": "slack_dm"},
                "caller_session_id": "promotion:router_tuning:p1",
                "source": "reasoning",
            }
        ],
    )
    code = cmd_promote(
        SimpleNamespace(
            promote_command="history",
            loop="router_tuning",
            days=30,
        )
    )
    assert code == 0
    out = _stdout_json(capsys)
    assert out["loop"] == "router_tuning"
    assert out["days"] == 30
    assert len(out["rows"]) == 1
    assert out["rows"][0]["seam"] == "promotion.router_trigger_proposed"


def test_history_filters_cross_loop_shared_seams_by_csid(
    capsys, tmp_path
):
    """The shared ``promotion.approved`` seam is used by all loops;
    the history call must scope rows to the requested loop via
    the ``promotion:<loop>:`` caller_session_id prefix."""
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    _write_audit(
        tmp_path,
        [
            {
                "emitted_at": now.isoformat(),
                "seam": "promotion.approved",
                "details": {},
                "caller_session_id": "promotion:router_tuning:p1",
                "source": "reasoning",
            },
            {
                "emitted_at": now.isoformat(),
                "seam": "promotion.approved",
                "details": {},
                "caller_session_id": "promotion:tool_trimming:p2",
                "source": "reasoning",
            },
        ],
    )
    code = cmd_promote(
        SimpleNamespace(
            promote_command="history",
            loop="router_tuning",
            days=30,
        )
    )
    assert code == 0
    out = _stdout_json(capsys)
    csids = [r["caller_session_id"] for r in out["rows"]]
    assert csids == ["promotion:router_tuning:p1"]


# ---------------------------------------------------------------------------
# run-once — dispatches to the loop's cycle function
# ---------------------------------------------------------------------------


def test_run_once_dispatches_to_loop_cycle(capsys, monkeypatch):
    """Patch the per-loop cycle import + verify run-once calls it
    + emits the summary it returns."""
    fake_summary = {"enabled": True, "proposals_generated": 3}

    async def _fake_cycle():
        return fake_summary

    monkeypatch.setattr(
        "kora_cli.promote.email_intent.plugin.run_email_intent_cycle",
        _fake_cycle,
    )
    code = cmd_promote(
        SimpleNamespace(
            promote_command="run-once", loop="email_intent"
        )
    )
    assert code == 0
    out = _stdout_json(capsys)
    assert out["loop"] == "email_intent"
    assert out["summary"] == fake_summary


def test_run_once_unknown_loop_returns_error(capsys):
    code = cmd_promote(
        SimpleNamespace(promote_command="run-once", loop="bogus")
    )
    assert code == 1
    out = _stdout_json(capsys)
    assert "must be one of" in out["error"]


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------


def test_missing_subcommand_lists_subcommands(capsys):
    code = cmd_promote(SimpleNamespace())
    assert code == 1
    out = _stdout_json(capsys)
    assert out["error"] == "missing subcommand"
    assert set(out["subcommands"]) == {
        "status",
        "run-once",
        "history",
        "pending",
    }
    assert set(out["loops"]) == set(LOOP_NAMES)
