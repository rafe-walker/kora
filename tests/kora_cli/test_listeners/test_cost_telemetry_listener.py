"""Tests for the KR-CHEAP-COST-TELEMETRY daemon listener.

Scenarios:
  1. Listener registered in LISTENER_REGISTRY at import time
  2. All 3 periodic tasks (persist, rolling_24h_reset, monthly_reset)
     registered with the heartbeat scheduler
  3. Default cadences honored; env overrides respected
  4. Listener factory tuple shape correct
  5. Persist task writes atomically to disk
  6. Persist task fail-soft on disk error
  7. Rolling-24h reset fires when UTC date crosses
  8. Rolling-24h reset NO-OP when UTC date stable
  9. Monthly reset fires when UTC month crosses
 10. Monthly reset NO-OP when UTC month stable
 11. read_telemetry_snapshot returns None when file missing
 12. read_telemetry_snapshot returns parsed dict when present
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from kora_cli import daemon as daemon_mod
from kora_cli.listeners.heartbeat import PERIODIC_TASK_REGISTRY
from kora_cli.listeners import cost_telemetry_listener
from kora_cli.listeners.cost_telemetry_listener import (
    COST_TELEMETRY_PATH_ENV,
    DEFAULT_PERSIST_INTERVAL_SEC,
    DEFAULT_RESET_TICK_INTERVAL_SEC,
    PERSIST_INTERVAL_ENV,
    RESET_TICK_INTERVAL_ENV,
    CostTelemetryListener,
    _factory,
    _read_persist_interval,
    _read_reset_tick_interval,
    _reset_window_tracking_for_tests,
    cost_telemetry_path,
    read_telemetry_snapshot,
    run_monthly_reset_check,
    run_persist_cycle,
    run_rolling_24h_reset_check,
    write_telemetry_snapshot,
)
from kora_cli.telemetry import (
    ROUTE_SLACK_DM,
    WINDOW_MONTHLY,
    WINDOW_ROLLING_24H,
    get_telemetry,
)
from kora_cli.telemetry.cost_telemetry import _reset_singleton_for_tests


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Isolated KORA_HOME + fresh telemetry singleton + cleared
    window-reset module state."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setattr("kora_constants.get_kora_home", lambda: tmp_path)
    monkeypatch.delenv(COST_TELEMETRY_PATH_ENV, raising=False)
    monkeypatch.delenv(PERSIST_INTERVAL_ENV, raising=False)
    monkeypatch.delenv(RESET_TICK_INTERVAL_ENV, raising=False)
    _reset_singleton_for_tests()
    _reset_window_tracking_for_tests()
    yield tmp_path
    _reset_singleton_for_tests()
    _reset_window_tracking_for_tests()


# ===========================================================================
# Registration
# ===========================================================================


def test_listener_registered_in_daemon_registry():
    names = {name for name, _f in daemon_mod.LISTENER_REGISTRY}
    assert "cost_telemetry" in names


def test_all_three_periodic_tasks_registered():
    names = {t.name for t in PERIODIC_TASK_REGISTRY}
    assert "cost_telemetry.persist" in names
    assert "cost_telemetry.rolling_24h_reset" in names
    assert "cost_telemetry.monthly_reset" in names


def test_factory_tuple_shape():
    startup, shutdown, timeout = _factory()
    assert callable(startup)
    assert callable(shutdown)
    assert isinstance(timeout, (int, float))


# ===========================================================================
# Cadence
# ===========================================================================


def test_persist_interval_default(monkeypatch):
    monkeypatch.delenv(PERSIST_INTERVAL_ENV, raising=False)
    assert _read_persist_interval() == DEFAULT_PERSIST_INTERVAL_SEC == 300.0


def test_persist_interval_env_override(monkeypatch):
    monkeypatch.setenv(PERSIST_INTERVAL_ENV, "60")
    assert _read_persist_interval() == 60.0


def test_reset_tick_interval_default(monkeypatch):
    monkeypatch.delenv(RESET_TICK_INTERVAL_ENV, raising=False)
    assert _read_reset_tick_interval() == DEFAULT_RESET_TICK_INTERVAL_SEC == 3600.0


def test_persist_interval_invalid_falls_back(monkeypatch, caplog):
    monkeypatch.setenv(PERSIST_INTERVAL_ENV, "not-numeric")
    with caplog.at_level("WARNING"):
        assert _read_persist_interval() == DEFAULT_PERSIST_INTERVAL_SEC
    assert any("is not numeric" in r.message for r in caplog.records)


# ===========================================================================
# Path resolution
# ===========================================================================


def test_cost_telemetry_path_default(_isolate, tmp_path):
    assert cost_telemetry_path() == tmp_path / "cache" / "cost_telemetry.json"


def test_cost_telemetry_path_env_override(_isolate, tmp_path, monkeypatch):
    override = tmp_path / "alt_cost.json"
    monkeypatch.setenv(COST_TELEMETRY_PATH_ENV, str(override))
    assert cost_telemetry_path() == override


# ===========================================================================
# Persistence
# ===========================================================================


def test_write_telemetry_snapshot_creates_file_with_payload(_isolate):
    # Pre-populate a counter so the write has interesting content.
    fake_usage = type("U", (), {"input_tokens": 10, "output_tokens": 5})()
    get_telemetry().record_call(
        route=ROUTE_SLACK_DM,
        model="claude-opus-4-7",
        canonical_usage=fake_usage,
        cost_estimate_usd=0.001,
    )
    write_telemetry_snapshot()
    target = cost_telemetry_path()
    assert target.exists()
    payload = json.loads(target.read_text())
    assert payload["schema_version"] == 1
    assert "written_at" in payload
    assert "windows" in payload
    # All three windows present.
    assert set(payload["windows"].keys()) == {
        "process_lifetime",
        "rolling_24h",
        "monthly",
    }


@pytest.mark.asyncio
async def test_run_persist_cycle_writes_file_end_to_end(_isolate):
    await run_persist_cycle()
    assert cost_telemetry_path().exists()


@pytest.mark.asyncio
async def test_run_persist_cycle_fail_soft_on_write_error(
    _isolate, monkeypatch, caplog
):
    def boom():
        raise OSError("disk full")

    monkeypatch.setattr(
        "kora_cli.listeners.cost_telemetry_listener.write_telemetry_snapshot",
        boom,
    )
    with caplog.at_level("WARNING"):
        await run_persist_cycle()  # must not raise
    assert any("persist cycle raised" in r.message for r in caplog.records)


# ===========================================================================
# Window-reset checks
# ===========================================================================


@pytest.mark.asyncio
async def test_rolling_24h_first_call_stamps_no_reset(_isolate):
    """First call after boot stamps the date but does NOT fire a
    reset (counters at zero anyway)."""
    get_telemetry().record_call(
        route=ROUTE_SLACK_DM,
        model="claude-opus-4-7",
        canonical_usage=type("U", (), {"input_tokens": 10})(),
        cost_estimate_usd=0.001,
    )
    await run_rolling_24h_reset_check()
    # Counter still populated — no reset fired on first call.
    snap = get_telemetry().snapshot()
    assert snap[WINDOW_ROLLING_24H][ROUTE_SLACK_DM]["calls_count"] == 1


@pytest.mark.asyncio
async def test_rolling_24h_reset_fires_when_utc_date_crosses(_isolate):
    """Force the tracked "last reset" date back to yesterday so the
    next check fires a reset."""
    get_telemetry().record_call(
        route=ROUTE_SLACK_DM,
        model="claude-opus-4-7",
        canonical_usage=type("U", (), {"input_tokens": 10})(),
        cost_estimate_usd=0.001,
    )
    # First tick — stamps today.
    await run_rolling_24h_reset_check()
    # Manually backdate the tracked date so the next tick crosses.
    yesterday = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=1)
    cost_telemetry_listener._last_24h_reset_date = yesterday
    await run_rolling_24h_reset_check()
    snap = get_telemetry().snapshot()
    # rolling_24h zeroed.
    assert snap[WINDOW_ROLLING_24H][ROUTE_SLACK_DM]["calls_count"] == 0
    # process_lifetime + monthly untouched.
    assert snap["process_lifetime"][ROUTE_SLACK_DM]["calls_count"] == 1
    assert snap[WINDOW_MONTHLY][ROUTE_SLACK_DM]["calls_count"] == 1


@pytest.mark.asyncio
async def test_rolling_24h_reset_noop_when_date_stable(_isolate):
    """Two ticks in the same UTC day → second one is a no-op."""
    get_telemetry().record_call(
        route=ROUTE_SLACK_DM,
        model="claude-opus-4-7",
        canonical_usage=type("U", (), {"input_tokens": 10})(),
        cost_estimate_usd=0.001,
    )
    await run_rolling_24h_reset_check()
    await run_rolling_24h_reset_check()
    snap = get_telemetry().snapshot()
    assert snap[WINDOW_ROLLING_24H][ROUTE_SLACK_DM]["calls_count"] == 1


@pytest.mark.asyncio
async def test_monthly_reset_fires_when_month_crosses(_isolate):
    get_telemetry().record_call(
        route=ROUTE_SLACK_DM,
        model="claude-opus-4-7",
        canonical_usage=type("U", (), {"input_tokens": 10})(),
        cost_estimate_usd=0.001,
    )
    await run_monthly_reset_check()
    # Backdate to last month.
    now = datetime.now(timezone.utc)
    prev_month = (now.year - 1, 12) if now.month == 1 else (
        now.year, now.month - 1
    )
    cost_telemetry_listener._last_monthly_reset_month = prev_month
    await run_monthly_reset_check()
    snap = get_telemetry().snapshot()
    assert snap[WINDOW_MONTHLY][ROUTE_SLACK_DM]["calls_count"] == 0
    # Other windows untouched.
    assert snap[WINDOW_ROLLING_24H][ROUTE_SLACK_DM]["calls_count"] == 1


@pytest.mark.asyncio
async def test_monthly_reset_noop_when_month_stable(_isolate):
    get_telemetry().record_call(
        route=ROUTE_SLACK_DM,
        model="claude-opus-4-7",
        canonical_usage=type("U", (), {"input_tokens": 10})(),
        cost_estimate_usd=0.001,
    )
    await run_monthly_reset_check()
    await run_monthly_reset_check()
    snap = get_telemetry().snapshot()
    assert snap[WINDOW_MONTHLY][ROUTE_SLACK_DM]["calls_count"] == 1


# ===========================================================================
# read_telemetry_snapshot
# ===========================================================================


def test_read_returns_none_when_missing(_isolate):
    assert read_telemetry_snapshot() is None


def test_read_returns_parsed_when_present(_isolate):
    write_telemetry_snapshot()
    snap = read_telemetry_snapshot()
    assert snap is not None
    assert snap["schema_version"] == 1
    assert "windows" in snap


def test_read_returns_none_on_malformed_json(_isolate):
    target = cost_telemetry_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{ not json")
    assert read_telemetry_snapshot() is None


# ===========================================================================
# Lifecycle log lines
# ===========================================================================


@pytest.mark.asyncio
async def test_listener_lifecycle_emits_info_lines(_isolate, caplog):
    listener = CostTelemetryListener()
    with caplog.at_level("INFO"):
        await listener.startup()
        await listener.shutdown()
    msgs = " ".join(r.message for r in caplog.records)
    assert "periodic tasks registered" in msgs
    assert "shutdown" in msgs
