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


# ===========================================================================
# KR-PROMOTE-ROUTER-LOOSEN-AUDIT-ROW — loosen-path activation
# ===========================================================================


from datetime import timedelta

from kora_cli.promote.router_tuning.observer import (
    RouteOverrideRollup,
    collect_route_overrides,
)
from kora_cli.promote.router_tuning.proposer import (
    DEFAULT_LOOSEN_OVERRIDE_THRESHOLD,
    LOOSEN_OVERRIDE_THRESHOLD_ENV,
    generate_loosen_proposals,
)


def _write_audit_jsonl(tmp_path, entries):
    path = tmp_path / "kora_audit_log.jsonl"
    path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )


def _override_entry(
    *,
    route: str = "slack_dm",
    message_text: str = "please tell me the right call here",
    source: str = "operator_prefix",
    reason: str = "opus_prefix",
    emitted_at=None,
) -> dict:
    if emitted_at is None:
        emitted_at = datetime.now(timezone.utc) - timedelta(hours=1)
    return {
        "emitted_at": emitted_at.isoformat(),
        "seam": "opus_override.applied",
        "details": {
            "original_message_text": message_text,
            "pre_call_decision_reason": reason,
            "override_source": source,
            "route": route,
        },
        "caller_session_id": "slack_dm:D1:1.001",
        "source": "reasoning",
    }


def test_collect_route_overrides_groups_by_route(tmp_path):
    _write_audit_jsonl(
        tmp_path,
        [
            _override_entry(route="slack_dm"),
            _override_entry(route="slack_dm"),
            _override_entry(route="email_inbound"),
        ],
    )
    out = collect_route_overrides(
        since=datetime.now(timezone.utc) - timedelta(days=1)
    )
    by_route = {r.route: r for r in out}
    assert by_route["slack_dm"].override_count == 2
    assert by_route["email_inbound"].override_count == 1


def test_collect_route_overrides_per_source_breakdown(tmp_path):
    _write_audit_jsonl(
        tmp_path,
        [
            _override_entry(source="operator_prefix"),
            _override_entry(source="operator_prefix"),
            _override_entry(source="force_env"),
        ],
    )
    out = collect_route_overrides(
        since=datetime.now(timezone.utc) - timedelta(days=1)
    )
    assert len(out) == 1
    assert out[0].by_source == {"operator_prefix": 2, "force_env": 1}


def test_collect_route_overrides_captures_sample_texts(tmp_path):
    _write_audit_jsonl(
        tmp_path,
        [
            _override_entry(message_text="should I ship the migration"),
            _override_entry(message_text="what's the right call here"),
            _override_entry(message_text="explain the tradeoff"),
            _override_entry(message_text="another override"),
        ],
    )
    out = collect_route_overrides(
        since=datetime.now(timezone.utc) - timedelta(days=1)
    )
    # _SAMPLE_TEXT_CAP = 3 — only first 3 captured.
    assert len(out[0].sample_message_texts) == 3


def _override_rollup(
    route: str = "slack_dm",
    override_count: int = 5,
) -> RouteOverrideRollup:
    return RouteOverrideRollup(
        route=route,
        override_count=override_count,
        sample_message_texts=["should I ship migration X"],
        by_source={"operator_prefix": override_count},
    )


def test_loosen_proposer_skips_under_threshold(monkeypatch):
    monkeypatch.delenv(LOOSEN_OVERRIDE_THRESHOLD_ENV, raising=False)
    rollups = [_override_rollup(override_count=2)]
    out = generate_loosen_proposals(rollups, now=datetime.now(timezone.utc))
    assert out == []


def test_loosen_proposer_emits_when_threshold_crossed():
    rollups = [_override_rollup(override_count=5)]
    out = generate_loosen_proposals(rollups, now=datetime.now(timezone.utc))
    assert len(out) == 1
    assert out[0].recommendation_kind == "loosen_review"
    assert out[0].override_count == 5
    assert "should I ship migration X" in out[0].rationale


def test_loosen_proposer_env_override_threshold(monkeypatch):
    monkeypatch.setenv(LOOSEN_OVERRIDE_THRESHOLD_ENV, "1")
    rollups = [_override_rollup(override_count=1)]
    out = generate_loosen_proposals(rollups, now=datetime.now(timezone.utc))
    assert len(out) == 1


@pytest.mark.asyncio
async def test_cycle_loosen_path_end_to_end(tmp_path, monkeypatch):
    """Synthetic opus_override.applied audit → cycle reads
    overrides → loosen proposals emitted alongside the tighten
    path."""
    monkeypatch.setenv(LOOSEN_OVERRIDE_THRESHOLD_ENV, "2")
    _write_audit_jsonl(
        tmp_path,
        [
            _override_entry(route="slack_dm", message_text=f"override {i}")
            for i in range(3)
        ],
    )
    # No telemetry escalations — tighten path produces 0 proposals.
    from unittest.mock import MagicMock as _MM

    fake = _MM()
    fake.snapshot.return_value = {"rolling_24h": {}, "monthly": {}}
    monkeypatch.setattr(
        "kora_cli.telemetry.cost_telemetry.get_telemetry", lambda: fake
    )
    monkeypatch.setattr(
        "kora_cli.telemetry.get_telemetry", lambda: fake
    )

    summary = await run_router_tuning_cycle()
    assert summary["overrides_observed"] == 1
    assert summary["proposals_generated"] == 1
    assert summary["proposals_persisted"] == 1

    audit = _read_audit(tmp_path)
    promo = [
        r
        for r in audit
        if r["seam"] == "promotion.router_trigger_proposed"
    ]
    assert len(promo) == 1
    assert promo[0]["details"]["recommendation_kind"] == "loosen_review"
    assert promo[0]["details"]["override_count"] == 3
