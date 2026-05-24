"""Tests for kora_cli.promote.router_tuning (observer + proposer + cycle)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from kora_cli.audit.jsonl_sink import BATCH_SIZE_ENV, _reset_batching_for_tests
from kora_cli.promote.router_tuning.observer import (
    RouteEscalationRollup,
    collect_route_rollups,
)
from kora_cli.promote.router_tuning.plugin import (
    ENABLED_ENV,
    run_router_tuning_cycle,
)
from kora_cli.promote.router_tuning.proposer import (
    DEFAULT_MIN_CALLS,
    DEFAULT_TIGHTEN_THRESHOLD,
    MIN_CALLS_ENV,
    TIGHTEN_THRESHOLD_ENV,
    generate_proposals,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setenv("KORA_PROMOTIONS_DIR", str(tmp_path / "promotions"))
    monkeypatch.setenv(
        "KORA_AUDIT_LOG_PATH", str(tmp_path / "kora_audit_log.jsonl")
    )
    # Force sync audit writes so tests can assert the row landed
    # without waiting on the background flusher.
    monkeypatch.setenv(BATCH_SIZE_ENV, "0")
    _reset_batching_for_tests()
    yield
    _reset_batching_for_tests()


def _rollup(
    route: str = "slack_dm",
    calls: int = 50,
    escs: int = 20,
    cost: float = 0.10,
) -> RouteEscalationRollup:
    return RouteEscalationRollup(
        route=route,
        calls_count=calls,
        escalation_count=escs,
        escalation_rate=escs / calls if calls else 0.0,
        cost_estimate_usd_total=cost,
    )


# ---------------------------------------------------------------------------
# Observer
# ---------------------------------------------------------------------------


def test_observer_reads_telemetry_singleton(monkeypatch):
    """Mock the telemetry singleton + verify the rolling_24h projection."""
    fake = MagicMock()
    fake.snapshot.return_value = {
        "rolling_24h": {
            "slack_dm": {
                "calls_count": 30,
                "escalation_count": 12,
                "cost_estimate_usd_total": 0.40,
            },
            "email_inbound": {
                "calls_count": 0,
                "escalation_count": 0,
                "cost_estimate_usd_total": 0.0,
            },
        },
        "monthly": {},
    }
    monkeypatch.setattr(
        "kora_cli.telemetry.cost_telemetry.get_telemetry",
        lambda: fake,
    )
    monkeypatch.setattr(
        "kora_cli.telemetry.get_telemetry", lambda: fake
    )

    out = collect_route_rollups()
    routes = {r.route: r for r in out}
    assert routes["slack_dm"].escalation_rate == pytest.approx(12 / 30)
    assert routes["email_inbound"].escalation_rate == 0.0
    assert routes["slack_dm"].cost_estimate_usd_total == 0.4


def test_observer_telemetry_failure_returns_empty(monkeypatch):
    fake = MagicMock()
    fake.snapshot.side_effect = RuntimeError("telemetry dead")
    monkeypatch.setattr(
        "kora_cli.telemetry.cost_telemetry.get_telemetry", lambda: fake
    )
    monkeypatch.setattr(
        "kora_cli.telemetry.get_telemetry", lambda: fake
    )
    out = collect_route_rollups()
    assert out == []


# ---------------------------------------------------------------------------
# Proposer
# ---------------------------------------------------------------------------


def test_proposer_skips_routes_below_min_calls(monkeypatch):
    """Sample-size cap: a tiny route can't produce a proposal."""
    monkeypatch.delenv(MIN_CALLS_ENV, raising=False)
    rollups = [_rollup(route="rare", calls=5, escs=5)]
    out = generate_proposals(rollups, now=datetime.now(timezone.utc))
    assert out == []


def test_proposer_skips_routes_below_threshold():
    rollups = [_rollup(route="slack_dm", calls=100, escs=10)]
    # 10% escalation < default 40% threshold → no proposal.
    out = generate_proposals(rollups, now=datetime.now(timezone.utc))
    assert out == []


def test_proposer_emits_tighten_review_when_threshold_crossed():
    rollups = [_rollup(route="slack_dm", calls=100, escs=50)]
    out = generate_proposals(rollups, now=datetime.now(timezone.utc))
    assert len(out) == 1
    assert out[0].recommendation_kind == "tighten_review"
    assert out[0].escalation_rate == pytest.approx(0.5)
    assert "slack_dm" in out[0].rationale


def test_proposer_sorted_by_confidence_desc():
    rollups = [
        _rollup(route="low", calls=20, escs=10),
        _rollup(route="high", calls=200, escs=100),
    ]
    out = generate_proposals(rollups, now=datetime.now(timezone.utc))
    assert [p.route for p in out] == ["high", "low"]


def test_proposer_env_overrides(monkeypatch):
    monkeypatch.setenv(MIN_CALLS_ENV, "5")
    monkeypatch.setenv(TIGHTEN_THRESHOLD_ENV, "0.10")
    rollups = [_rollup(route="slack_dm", calls=8, escs=2)]
    out = generate_proposals(rollups, now=datetime.now(timezone.utc))
    assert len(out) == 1


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
async def test_cycle_disabled_short_circuits(tmp_path, monkeypatch):
    monkeypatch.setenv(ENABLED_ENV, "false")
    summary = await run_router_tuning_cycle()
    assert summary["enabled"] is False
    assert summary["proposals_generated"] == 0


@pytest.mark.asyncio
async def test_cycle_generates_persists_and_audits(tmp_path, monkeypatch):
    """End-to-end: synthetic telemetry → 1 proposal persisted +
    1 audit row with the canonical seam name."""
    fake = MagicMock()
    fake.snapshot.return_value = {
        "rolling_24h": {
            "slack_dm": {
                "calls_count": 100,
                "escalation_count": 60,
                "cost_estimate_usd_total": 1.20,
            }
        },
        "monthly": {},
    }
    monkeypatch.setattr(
        "kora_cli.telemetry.cost_telemetry.get_telemetry", lambda: fake
    )
    monkeypatch.setattr(
        "kora_cli.telemetry.get_telemetry", lambda: fake
    )

    summary = await run_router_tuning_cycle()
    assert summary["enabled"] is True
    assert summary["proposals_generated"] == 1
    assert summary["proposals_persisted"] == 1

    rows = _read_audit(tmp_path)
    promo = [
        r for r in rows if r["seam"] == "promotion.router_trigger_proposed"
    ]
    assert len(promo) == 1
    assert promo[0]["details"]["route"] == "slack_dm"
    assert promo[0]["details"]["recommendation_kind"] == "tighten_review"

    # Pending proposal landed on disk.
    pending_dir = (
        tmp_path / "promotions" / "router_tuning" / "pending"
    )
    assert pending_dir.is_dir()
    files = list(pending_dir.iterdir())
    assert len(files) == 1
