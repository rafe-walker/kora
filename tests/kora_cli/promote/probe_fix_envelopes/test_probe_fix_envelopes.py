"""Tests for kora_cli.promote.probe_fix_envelopes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kora_cli.audit.jsonl_sink import BATCH_SIZE_ENV, _reset_batching_for_tests
from kora_cli.promote.probe_fix_envelopes.observer import (
    InvestigationObservation,
    collect_recent_investigations,
)
from kora_cli.promote.probe_fix_envelopes.plugin import (
    ENABLED_ENV,
    run_probe_fix_envelopes_cycle,
)
from kora_cli.promote.probe_fix_envelopes.proposer import (
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


def _investigation_entry(
    *,
    probe: str = "fly",
    issue_category: str = "machine_down",
    summary: str = "Restart the fly machine to recover.",
    autofix_attempted: bool = False,
    caller_session_id: str = "probe:fly:machine_down",
    emitted_at: datetime | None = None,
) -> dict:
    if emitted_at is None:
        emitted_at = datetime.now(timezone.utc) - timedelta(hours=1)
    return {
        "emitted_at": emitted_at.isoformat(),
        "seam": "probe.investigation_completed",
        "details": {
            "probe": probe,
            "issue_category": issue_category,
            "severity": "warning",
            "investigation_summary_text": summary,
            "autofix_attempted": autofix_attempted,
        },
        "caller_session_id": caller_session_id,
        "source": "reasoning",
    }


# ---------------------------------------------------------------------------
# Observer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observer_skips_autofix_attempted(tmp_path):
    _write_audit(
        tmp_path,
        [
            _investigation_entry(autofix_attempted=False),
            _investigation_entry(autofix_attempted=True),
        ],
    )
    out = await collect_recent_investigations(
        since=datetime.now(timezone.utc) - timedelta(days=1)
    )
    assert len(out) == 1


@pytest.mark.asyncio
async def test_observer_skips_empty_summaries(tmp_path):
    _write_audit(
        tmp_path,
        [
            _investigation_entry(summary=""),
            _investigation_entry(summary="real text"),
        ],
    )
    out = await collect_recent_investigations(
        since=datetime.now(timezone.utc) - timedelta(days=1)
    )
    assert len(out) == 1


# ---------------------------------------------------------------------------
# Proposer
# ---------------------------------------------------------------------------


def _obs(
    probe: str = "fly",
    cat: str = "machine_down",
    summary: str = "Restart the fly machine.",
    csid: str = "probe:fly:machine_down:1",
) -> InvestigationObservation:
    return InvestigationObservation(
        probe=probe,
        issue_category=cat,
        severity="warning",
        investigation_summary_text=summary,
        caller_session_id=csid,
        timestamp=datetime.now(timezone.utc),
    )


def test_proposer_clusters_by_probe_and_category(monkeypatch):
    monkeypatch.setenv(MIN_CLUSTER_SIZE_ENV, "2")
    obs = [
        _obs(probe="fly", cat="machine_down", csid=f"c{i}") for i in range(3)
    ] + [
        _obs(probe="vercel", cat="deploy_fail", csid=f"v{i}") for i in range(3)
    ]
    out = generate_proposals(obs, now=datetime.now(timezone.utc))
    by_probe = {p.probe: p for p in out}
    assert set(by_probe.keys()) == {"fly", "vercel"}
    assert by_probe["fly"].fix_name_suggestion == "proposed_fly_machine_down"
    assert by_probe["fly"].cluster_size == 3


def test_proposer_skips_under_min_cluster(monkeypatch):
    monkeypatch.setenv(MIN_CLUSTER_SIZE_ENV, "3")
    obs = [_obs(probe="fly", cat="machine_down", csid="c1")]
    out = generate_proposals(obs, now=datetime.now(timezone.utc))
    assert out == []


def test_proposer_default_blast_radius_is_conservative(monkeypatch):
    monkeypatch.setenv(MIN_CLUSTER_SIZE_ENV, "2")
    obs = [_obs(csid=f"c{i}") for i in range(2)]
    out = generate_proposals(obs, now=datetime.now(timezone.utc))
    assert "operator must review" in out[0].blast_radius_summary


def test_proposer_surfaces_recurring_text(monkeypatch):
    monkeypatch.setenv(MIN_CLUSTER_SIZE_ENV, "2")
    obs = [
        _obs(summary="Restart the fly machine.", csid=f"c{i}") for i in range(3)
    ] + [_obs(summary="something else entirely", csid="cother")]
    out = generate_proposals(obs, now=datetime.now(timezone.utc))
    assert out[0].recurring_recommendation_text.startswith("Restart")


# ---------------------------------------------------------------------------
# Cycle
# ---------------------------------------------------------------------------


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
async def test_cycle_emits_audit_with_correct_seam(tmp_path, monkeypatch):
    monkeypatch.setenv(MIN_CLUSTER_SIZE_ENV, "2")
    _write_audit(
        tmp_path,
        [
            _investigation_entry(
                summary="Restart the fly machine.",
                caller_session_id=f"probe:fly:machine_down:{i}",
            )
            for i in range(3)
        ],
    )
    summary = await run_probe_fix_envelopes_cycle()
    assert summary["proposals_generated"] == 1
    # HARDCODED auto_apply_mode = False per safety posture.
    assert summary["auto_apply_mode"] is False

    rows = _read_audit(tmp_path)
    promo = [
        r
        for r in rows
        if r["seam"] == "promotion.probe_envelope_action_proposed"
    ]
    assert len(promo) == 1
    assert promo[0]["details"]["probe"] == "fly"
    assert promo[0]["details"]["fix_name_suggestion"] == "proposed_fly_machine_down"


@pytest.mark.asyncio
async def test_cycle_disabled_short_circuits(tmp_path, monkeypatch):
    monkeypatch.setenv(ENABLED_ENV, "false")
    summary = await run_probe_fix_envelopes_cycle()
    assert summary["enabled"] is False
