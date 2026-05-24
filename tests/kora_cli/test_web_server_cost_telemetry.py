"""Tests for /api/cost_telemetry + snapshot v2 integration.

Scenarios:
  1. /api/cost_telemetry returns the telemetry snapshot dict
  2. Endpoint surfaces all 3 windows
  3. Snapshot v2 schema_version bumped to 2
  4. Snapshot includes cost_telemetry section with rolling_24h + monthly
  5. Snapshot cost_telemetry survives telemetry import failure
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from kora_cli.telemetry import (
    ROUTE_SLACK_DM,
    WINDOW_MONTHLY,
    WINDOW_PROCESS_LIFETIME,
    WINDOW_ROLLING_24H,
    get_telemetry,
)
from kora_cli.telemetry.cost_telemetry import _reset_singleton_for_tests


@dataclass(frozen=True)
class _FakeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """3-namespace get_kora_home isolation + fresh telemetry."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setattr("kora_constants.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr("kora_cli.config.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr(
        "kora_cli.web_server.get_kora_home", lambda: tmp_path, raising=False
    )
    _reset_singleton_for_tests()
    yield tmp_path
    _reset_singleton_for_tests()


# ===========================================================================
# /api/cost_telemetry
# ===========================================================================


@pytest.mark.asyncio
async def test_endpoint_returns_telemetry_snapshot(_isolate):
    from kora_cli import web_server

    get_telemetry().record_call(
        route=ROUTE_SLACK_DM,
        model="claude-opus-4-7",
        canonical_usage=_FakeUsage(input_tokens=100, output_tokens=50),
        cost_estimate_usd=0.005,
    )
    result = await web_server.get_cost_telemetry()
    assert set(result.keys()) == {
        WINDOW_PROCESS_LIFETIME,
        WINDOW_ROLLING_24H,
        WINDOW_MONTHLY,
    }
    assert result[WINDOW_PROCESS_LIFETIME][ROUTE_SLACK_DM]["calls_count"] == 1
    assert result[WINDOW_ROLLING_24H][ROUTE_SLACK_DM]["calls_count"] == 1


@pytest.mark.asyncio
async def test_endpoint_returns_zero_counters_when_no_calls(_isolate):
    from kora_cli import web_server

    result = await web_server.get_cost_telemetry()
    # Stable shape even with no calls — every route at zero.
    for window in (
        WINDOW_PROCESS_LIFETIME,
        WINDOW_ROLLING_24H,
        WINDOW_MONTHLY,
    ):
        assert result[window][ROUTE_SLACK_DM]["calls_count"] == 0


# ===========================================================================
# Snapshot v2
# ===========================================================================


def test_snapshot_schema_version_bumped_to_v2(_isolate):
    from kora_cli.snapshot import compute_snapshot

    snap = compute_snapshot()
    assert snap["schema_version"] == 2


def test_snapshot_includes_cost_telemetry_section(_isolate):
    from kora_cli.snapshot import compute_snapshot

    get_telemetry().record_call(
        route=ROUTE_SLACK_DM,
        model="claude-opus-4-7",
        canonical_usage=_FakeUsage(input_tokens=10),
        cost_estimate_usd=0.001,
    )
    snap = compute_snapshot()
    assert "cost_telemetry" in snap
    # Exposes the two operator-facing windows (NOT process_lifetime
    # — see _collect_cost_telemetry docstring).
    assert set(snap["cost_telemetry"].keys()) == {"rolling_24h", "monthly"}
    assert (
        snap["cost_telemetry"]["rolling_24h"][ROUTE_SLACK_DM]["calls_count"]
        == 1
    )


def test_snapshot_cost_telemetry_degrades_when_singleton_unavailable(
    _isolate, monkeypatch
):
    """If the telemetry singleton's snapshot() raises, the section
    degrades to empty dicts rather than failing the whole snapshot."""
    from kora_cli.snapshot import compute_snapshot

    def boom():
        raise RuntimeError("telemetry dead")

    # Get the singleton built, then sabotage its snapshot method.
    t = get_telemetry()
    monkeypatch.setattr(t, "snapshot", boom)
    snap = compute_snapshot()
    assert snap["cost_telemetry"] == {"rolling_24h": {}, "monthly": {}}


@pytest.mark.asyncio
async def test_api_snapshot_endpoint_includes_cost_telemetry(_isolate):
    """End-to-end: /api/snapshot returns the v2 shape with the new
    cost_telemetry section."""
    from kora_cli import web_server
    from kora_cli.snapshot import compute_snapshot, write_snapshot

    get_telemetry().record_call(
        route=ROUTE_SLACK_DM,
        model="claude-opus-4-7",
        canonical_usage=_FakeUsage(input_tokens=50, output_tokens=25),
        cost_estimate_usd=0.002,
    )
    write_snapshot(compute_snapshot())
    result = await web_server.get_daemon_snapshot()
    assert result["schema_version"] == 2
    assert "cost_telemetry" in result
    assert (
        result["cost_telemetry"]["rolling_24h"][ROUTE_SLACK_DM]["calls_count"]
        == 1
    )
