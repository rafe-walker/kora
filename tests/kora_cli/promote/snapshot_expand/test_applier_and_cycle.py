"""Tests for kora_cli.promote.snapshot_expand.applier + .cycle."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kora_cli.promote.snapshot_expand.applier import (
    AUTO_APPLY_ENV,
    apply_proposal,
)
from kora_cli.promote.snapshot_expand.cycle import (
    ENABLED_ENV,
    run_snapshot_expand_cycle,
)
from kora_cli.promote.snapshot_expand.proposer import (
    SnapshotFieldProposal,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setenv(
        "KORA_AUDIT_LOG_PATH", str(tmp_path / "kora_audit_log.jsonl")
    )
    monkeypatch.setenv("KORA_PROMOTIONS_DIR", str(tmp_path / "promotions"))
    monkeypatch.delenv(AUTO_APPLY_ENV, raising=False)
    monkeypatch.delenv(ENABLED_ENV, raising=False)
    return tmp_path


def _proposal() -> SnapshotFieldProposal:
    return SnapshotFieldProposal(
        proposal_id="prop-123",
        cluster_size=8,
        proposed_field_path="open_tickets",
        proposed_collector_summary=(
            "Collector would project the result of kora__open_tickets"
        ),
        source_tool_name="kora__open_tickets",
        sample_caller_session_ids=["s1", "s2"],
        confidence=0.8,
        created_at=datetime.now(timezone.utc),
        status="proposed",
    )


def _read_audit(tmp_path: Path) -> list:
    path = tmp_path / "kora_audit_log.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_apply_proposal_default_emits_proposed_action(tmp_path):
    """Default (auto_apply OFF) — audit row emitted with
    ``action=proposed``; no persistence."""
    action = apply_proposal(_proposal())
    assert action == "proposed"
    rows = _read_audit(tmp_path)
    assert len(rows) == 1
    assert rows[0]["seam"] == "promotion.snapshot_field_added"
    assert rows[0]["details"]["action"] == "proposed"
    assert rows[0]["details"]["proposal_id"] == "prop-123"
    # No applied/ file should exist on the proposed path.
    applied_dir = tmp_path / "promotions" / "snapshot_expand" / "applied"
    assert not applied_dir.exists() or not list(applied_dir.iterdir())


def test_apply_proposal_auto_apply_persists_and_audits(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(AUTO_APPLY_ENV, "true")
    action = apply_proposal(_proposal())
    assert action == "auto_applied"
    rows = _read_audit(tmp_path)
    assert len(rows) == 1
    assert rows[0]["details"]["action"] == "auto_applied"
    applied = tmp_path / "promotions" / "snapshot_expand" / "applied" / "prop-123.json"
    assert applied.is_file()
    saved = json.loads(applied.read_text())
    assert saved["proposal_id"] == "prop-123"
    assert saved["proposed_field_path"] == "open_tickets"


@pytest.mark.asyncio
async def test_cycle_disabled_short_circuits(tmp_path, monkeypatch):
    monkeypatch.setenv(ENABLED_ENV, "false")
    summary = await run_snapshot_expand_cycle()
    assert summary["enabled"] is False
    assert summary["proposals_generated"] == 0
    assert _read_audit(tmp_path) == []


@pytest.mark.asyncio
async def test_cycle_generates_and_audits_proposals(
    tmp_path, monkeypatch
):
    """Synthetic observations → 1 proposal → 1 audit row with
    action=proposed (default AUTO_APPLY off)."""
    # Seed the audit log with enough tool_called rows to trigger one
    # cluster ≥ min_cluster_size (default 5).
    seeds = [
        {
            "emitted_at": (
                datetime.now(timezone.utc) - timedelta(hours=h)
            ).isoformat(),
            "seam": "reasoning.tool_called",
            "details": {"tool_name": "kora__open_tickets"},
            "caller_session_id": f"D1JOSH:170000{h}",
            "source": "reasoning",
        }
        for h in range(1, 7)
    ]
    audit_path = tmp_path / "kora_audit_log.jsonl"
    audit_path.write_text(
        "\n".join(json.dumps(e) for e in seeds) + "\n", encoding="utf-8"
    )

    summary = await run_snapshot_expand_cycle()
    assert summary["enabled"] is True
    assert summary["observations_read"] == 6
    assert summary["proposals_generated"] == 1
    assert summary["proposals_applied"] == 1
    assert summary["auto_apply_mode"] is False

    rows = _read_audit(tmp_path)
    promotion_rows = [
        r for r in rows if r["seam"] == "promotion.snapshot_field_added"
    ]
    assert len(promotion_rows) == 1
    assert promotion_rows[0]["details"]["action"] == "proposed"
    assert (
        promotion_rows[0]["details"]["source_tool_name"]
        == "kora__open_tickets"
    )


@pytest.mark.asyncio
async def test_cycle_auto_apply_mode_persists(tmp_path, monkeypatch):
    monkeypatch.setenv(AUTO_APPLY_ENV, "true")
    seeds = [
        {
            "emitted_at": (
                datetime.now(timezone.utc) - timedelta(hours=h)
            ).isoformat(),
            "seam": "reasoning.tool_called",
            "details": {"tool_name": "kora__busy_lookup"},
            "caller_session_id": f"D1JOSH:170000{h}",
            "source": "reasoning",
        }
        for h in range(1, 7)
    ]
    (tmp_path / "kora_audit_log.jsonl").write_text(
        "\n".join(json.dumps(e) for e in seeds) + "\n", encoding="utf-8"
    )
    summary = await run_snapshot_expand_cycle()
    assert summary["auto_apply_mode"] is True
    assert summary["proposals_applied"] == 1
    applied_dir = (
        tmp_path / "promotions" / "snapshot_expand" / "applied"
    )
    assert applied_dir.is_dir()
    files = list(applied_dir.iterdir())
    assert len(files) == 1
