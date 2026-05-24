"""Tests for kora_cli.promote.tool_trimming."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kora_cli.audit.jsonl_sink import BATCH_SIZE_ENV, _reset_batching_for_tests
from kora_cli.promote.tool_trimming.observer import (
    RouteToolUsage,
    collect_route_tool_usage,
)
from kora_cli.promote.tool_trimming.plugin import (
    ENABLED_ENV,
    run_tool_trimming_cycle,
)
from kora_cli.promote.tool_trimming.proposer import (
    MIN_CALLS_ENV,
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


def _audit_entry(
    *,
    tool_name: str = "kora__get_operational_state",
    route: str | None = "slack_dm",
    caller_session_id: str = "D1JOSH:1",
    emitted_at: datetime | None = None,
) -> dict:
    if emitted_at is None:
        emitted_at = datetime.now(timezone.utc) - timedelta(hours=1)
    details: dict = {"tool_name": tool_name}
    if route is not None:
        details["route"] = route
    return {
        "emitted_at": emitted_at.isoformat(),
        "seam": "reasoning.tool_called",
        "details": details,
        "caller_session_id": caller_session_id,
        "source": "reasoning",
    }


# ---------------------------------------------------------------------------
# Observer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observer_groups_by_route(tmp_path):
    _write_audit(
        tmp_path,
        [
            _audit_entry(tool_name="kora__a", route="slack_dm"),
            _audit_entry(tool_name="kora__a", route="slack_dm"),
            _audit_entry(tool_name="kora__b", route="email_inbound"),
        ],
    )
    out = await collect_route_tool_usage(
        since=datetime.now(timezone.utc) - timedelta(days=1)
    )
    routes = {r.route: r for r in out}
    assert routes["slack_dm"].total_calls == 2
    assert routes["slack_dm"].per_tool_calls == {"kora__a": 2}
    assert routes["email_inbound"].total_calls == 1


@pytest.mark.asyncio
async def test_observer_derives_route_from_csid_prefix(tmp_path):
    """When details.route is absent, the observer falls back to the
    caller_session_id prefix convention."""
    _write_audit(
        tmp_path,
        [
            _audit_entry(
                tool_name="kora__probe_check",
                route=None,
                caller_session_id="probe:fly:machine_down",
            ),
        ],
    )
    out = await collect_route_tool_usage(
        since=datetime.now(timezone.utc) - timedelta(days=1)
    )
    assert [r.route for r in out] == ["probe_investigation"]


# ---------------------------------------------------------------------------
# Proposer
# ---------------------------------------------------------------------------


def test_proposer_emits_unused_tools_per_route(monkeypatch):
    """slack_dm called tool_a; email called tool_b; available = both.
    Expect: slack_dm gets a proposal for tool_b; email for tool_a."""
    monkeypatch.setenv(MIN_CALLS_ENV, "2")
    rollups = [
        RouteToolUsage(
            route="slack_dm",
            total_calls=5,
            tools_called={"kora__a"},
            per_tool_calls={"kora__a": 5},
        ),
        RouteToolUsage(
            route="email_inbound",
            total_calls=5,
            tools_called={"kora__b"},
            per_tool_calls={"kora__b": 5},
        ),
    ]
    out = generate_proposals(rollups, now=datetime.now(timezone.utc))
    proposals = {p.route: p for p in out}
    assert proposals["slack_dm"].unused_tools == ["kora__b"]
    assert proposals["email_inbound"].unused_tools == ["kora__a"]


def test_proposer_skips_routes_with_no_unused_tools(monkeypatch):
    """Single-route case: tools_called == available → nothing to drop."""
    monkeypatch.setenv(MIN_CALLS_ENV, "2")
    rollups = [
        RouteToolUsage(
            route="slack_dm",
            total_calls=10,
            tools_called={"kora__a", "kora__b"},
            per_tool_calls={"kora__a": 5, "kora__b": 5},
        )
    ]
    out = generate_proposals(rollups, now=datetime.now(timezone.utc))
    assert out == []


def test_proposer_respects_min_calls(monkeypatch):
    monkeypatch.setenv(MIN_CALLS_ENV, "10")
    rollups = [
        RouteToolUsage(
            route="slack_dm",
            total_calls=3,
            tools_called={"kora__a"},
            per_tool_calls={"kora__a": 3},
        )
    ]
    out = generate_proposals(
        rollups,
        now=datetime.now(timezone.utc),
        available_tool_names={"kora__a", "kora__b"},
    )
    assert out == []


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
async def test_cycle_emits_audit_for_each_proposal(tmp_path, monkeypatch):
    monkeypatch.setenv(MIN_CALLS_ENV, "2")
    _write_audit(
        tmp_path,
        [
            _audit_entry(tool_name="kora__a", route="slack_dm"),
            _audit_entry(tool_name="kora__a", route="slack_dm"),
            _audit_entry(tool_name="kora__b", route="email_inbound"),
            _audit_entry(tool_name="kora__b", route="email_inbound"),
        ],
    )
    summary = await run_tool_trimming_cycle()
    assert summary["proposals_generated"] == 2
    assert summary["proposals_persisted"] == 2

    rows = _read_audit(tmp_path)
    promo = [r for r in rows if r["seam"] == "promotion.tool_trim_proposed"]
    assert len(promo) == 2
    routes = sorted(r["details"]["route"] for r in promo)
    assert routes == ["email_inbound", "slack_dm"]


@pytest.mark.asyncio
async def test_cycle_disabled_short_circuits(tmp_path, monkeypatch):
    monkeypatch.setenv(ENABLED_ENV, "false")
    summary = await run_tool_trimming_cycle()
    assert summary["enabled"] is False
