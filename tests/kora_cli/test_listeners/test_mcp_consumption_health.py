"""Tests for KR-MCP-CONSUMPTION ST2 — active health-check.

Covers:
  - ``run_health_check`` skips cleanly when no pool is registered
  - One pass populates ``_health_cache`` with one snapshot per
    pool endpoint
  - Successful ``list_tools`` → snapshot.connected=True +
    tools_count=N + last_error=None
  - ``list_tools`` raises MCPCallFailed → snapshot.connected=False
    + tools_count=None + last_error preserved
  - Unknown exception type (defense in depth) → snapshot.connected=False
    + last_error includes ExceptionType prefix
  - Per-endpoint failure does NOT block siblings
  - ``current_health_snapshots`` returns a copy (mutations don't
    leak into cache)
  - Shutdown clears the cache
  - Listener startup-without-shutdown leaves cache empty (no
    auto-populate)
  - ``_read_health_check_interval``: default / env override /
    invalid value fallback / non-positive value fallback
  - Periodic task ``mcp.health_check`` registered in
    PERIODIC_TASK_REGISTRY at module-import time with correct
    cadence + callable
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from kora_cli.listeners import heartbeat as heartbeat_module
from kora_cli.listeners.mcp_consumption import (
    DEFAULT_HEALTH_CHECK_INTERVAL_SEC,
    HEALTH_CHECK_INTERVAL_ENV,
    HealthSnapshot,
    MCPConsumptionListener,
    _clear_health_cache,
    _clear_singleton,
    _read_health_check_interval,
    current_health_snapshots,
    run_health_check,
)
from kora_mcp.catalog import DEFAULT_CATALOG
from kora_mcp.pool import MCPCallFailed, ToolDescriptor
from kora_mcp.registry import MCPRegistryConfig


@pytest.fixture(autouse=True)
def _reset_state():
    _clear_singleton()
    _clear_health_cache()
    yield
    _clear_singleton()
    _clear_health_cache()


# ---------------------------------------------------------------------------
# run_health_check — no-pool branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_health_check_skips_when_no_pool(caplog):
    """No active pool (daemon not started / already stopped) →
    snapshot cache untouched + DEBUG log only. Scheduler keeps
    firing; doesn't crash."""
    import logging

    with caplog.at_level(logging.DEBUG, logger="kora_cli.listeners.mcp_consumption"):
        await run_health_check()
    assert current_health_snapshots() == {}
    assert any("no active pool" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# run_health_check — happy path + per-endpoint outcomes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_pass_populates_snapshot_per_endpoint():
    """Successful list_tools per endpoint → cache contains one
    HealthSnapshot per endpoint name; all marked connected with
    matching tools_count."""
    default_registry = MCPRegistryConfig(endpoints=list(DEFAULT_CATALOG))
    with patch(
        "kora_cli.listeners.mcp_consumption.load_effective_catalog",
        return_value=default_registry,
    ):
        listener = MCPConsumptionListener()
        await listener.startup()

        # Stub list_tools to return different counts per endpoint
        async def _fake_list_tools(prefix):
            if prefix == "github":
                return [
                    ToolDescriptor(name="create_issue", description=None, input_schema=None),
                    ToolDescriptor(name="list_repos", description=None, input_schema=None),
                ]
            return [ToolDescriptor(name="d1_query", description=None, input_schema=None)]

        with patch.object(listener.pool, "list_tools", new=_fake_list_tools):
            await run_health_check()

        snapshots = current_health_snapshots()
        assert set(snapshots.keys()) == {"github", "cloudflare"}
        assert snapshots["github"].connected is True
        assert snapshots["github"].tools_count == 2
        assert snapshots["github"].last_error is None
        assert snapshots["cloudflare"].connected is True
        assert snapshots["cloudflare"].tools_count == 1
        assert snapshots["cloudflare"].last_error is None

        await listener.shutdown()


@pytest.mark.asyncio
async def test_mcp_call_failed_recorded_on_snapshot():
    """MCPCallFailed → snapshot.connected=False + tools_count=None +
    last_error captures the failure message."""
    default_registry = MCPRegistryConfig(endpoints=list(DEFAULT_CATALOG))
    with patch(
        "kora_cli.listeners.mcp_consumption.load_effective_catalog",
        return_value=default_registry,
    ):
        listener = MCPConsumptionListener()
        await listener.startup()

        async def _failing_list_tools(prefix):
            raise MCPCallFailed(f"list_tools({prefix}) failed: transport boom")

        with patch.object(listener.pool, "list_tools", new=_failing_list_tools):
            await run_health_check()

        snapshots = current_health_snapshots()
        for prefix in ("github", "cloudflare"):
            assert snapshots[prefix].connected is False
            assert snapshots[prefix].tools_count is None
            assert "transport boom" in snapshots[prefix].last_error

        await listener.shutdown()


@pytest.mark.asyncio
async def test_unknown_exception_type_wrapped_with_type_prefix():
    """Defense in depth: a non-MCPCallFailed exception still gets
    captured, with ExceptionType prefix so operator can diagnose."""
    default_registry = MCPRegistryConfig(endpoints=list(DEFAULT_CATALOG))
    with patch(
        "kora_cli.listeners.mcp_consumption.load_effective_catalog",
        return_value=default_registry,
    ):
        listener = MCPConsumptionListener()
        await listener.startup()

        async def _broken_list_tools(prefix):
            raise ValueError("malformed response shape")

        with patch.object(listener.pool, "list_tools", new=_broken_list_tools):
            await run_health_check()

        snapshots = current_health_snapshots()
        for prefix in ("github", "cloudflare"):
            assert snapshots[prefix].connected is False
            assert "ValueError" in snapshots[prefix].last_error
            assert "malformed response shape" in snapshots[prefix].last_error

        await listener.shutdown()


@pytest.mark.asyncio
async def test_per_endpoint_failure_does_not_block_siblings():
    """Mixed outcomes: github fails, cloudflare succeeds. Both
    snapshots present, each reflecting its own outcome."""
    default_registry = MCPRegistryConfig(endpoints=list(DEFAULT_CATALOG))
    with patch(
        "kora_cli.listeners.mcp_consumption.load_effective_catalog",
        return_value=default_registry,
    ):
        listener = MCPConsumptionListener()
        await listener.startup()

        async def _mixed_list_tools(prefix):
            if prefix == "github":
                raise MCPCallFailed("github down")
            return [
                ToolDescriptor(name="d1_query", description=None, input_schema=None)
            ]

        with patch.object(listener.pool, "list_tools", new=_mixed_list_tools):
            await run_health_check()

        snapshots = current_health_snapshots()
        assert snapshots["github"].connected is False
        assert snapshots["github"].last_error is not None
        assert snapshots["cloudflare"].connected is True
        assert snapshots["cloudflare"].tools_count == 1

        await listener.shutdown()


# ---------------------------------------------------------------------------
# current_health_snapshots is a copy (cache isolation)
# ---------------------------------------------------------------------------


def test_current_health_snapshots_returns_copy():
    """Mutating the returned dict must not affect the internal
    cache — callers can freely modify their view."""
    now = datetime.now(timezone.utc)
    from kora_cli.listeners.mcp_consumption import _health_cache

    _health_cache["test"] = HealthSnapshot(
        connected=True, tools_count=5, last_check_at=now, last_error=None
    )
    view = current_health_snapshots()
    view["test"] = HealthSnapshot(
        connected=False, tools_count=0, last_check_at=now, last_error="tampered"
    )
    # Internal cache unchanged
    assert current_health_snapshots()["test"].connected is True
    assert current_health_snapshots()["test"].last_error is None


# ---------------------------------------------------------------------------
# Listener shutdown clears cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_clears_health_cache():
    """Cache persistence across daemon restarts would surface stale
    pre-restart data on the panel. Cleared on shutdown."""
    default_registry = MCPRegistryConfig(endpoints=list(DEFAULT_CATALOG))
    with patch(
        "kora_cli.listeners.mcp_consumption.load_effective_catalog",
        return_value=default_registry,
    ):
        listener = MCPConsumptionListener()
        await listener.startup()

        async def _ok(prefix):
            return [ToolDescriptor(name="x", description=None, input_schema=None)]

        with patch.object(listener.pool, "list_tools", new=_ok):
            await run_health_check()
        assert len(current_health_snapshots()) == 2

        await listener.shutdown()
        assert current_health_snapshots() == {}


@pytest.mark.asyncio
async def test_shutdown_without_startup_still_clears_cache():
    """If a prior daemon left a snapshot behind + the new daemon
    starts but immediately shuts down without populating, cache is
    still cleared. (No-pool shutdown short-circuit must still clear
    the cache.)"""
    now = datetime.now(timezone.utc)
    from kora_cli.listeners.mcp_consumption import _health_cache

    _health_cache["stale_from_prior_daemon"] = HealthSnapshot(
        connected=True, tools_count=99, last_check_at=now, last_error=None
    )
    assert current_health_snapshots() != {}

    listener = MCPConsumptionListener()
    # No startup; immediate shutdown
    await listener.shutdown()
    assert current_health_snapshots() == {}


# ---------------------------------------------------------------------------
# _read_health_check_interval
# ---------------------------------------------------------------------------


def test_interval_default_when_env_unset(monkeypatch):
    monkeypatch.delenv(HEALTH_CHECK_INTERVAL_ENV, raising=False)
    assert _read_health_check_interval() == DEFAULT_HEALTH_CHECK_INTERVAL_SEC


def test_interval_env_override(monkeypatch):
    monkeypatch.setenv(HEALTH_CHECK_INTERVAL_ENV, "60")
    assert _read_health_check_interval() == 60.0


def test_interval_env_override_floats(monkeypatch):
    monkeypatch.setenv(HEALTH_CHECK_INTERVAL_ENV, "45.5")
    assert _read_health_check_interval() == 45.5


def test_interval_invalid_value_falls_back(monkeypatch, caplog):
    import logging

    monkeypatch.setenv(HEALTH_CHECK_INTERVAL_ENV, "not-a-number")
    with caplog.at_level(
        logging.WARNING, logger="kora_cli.listeners.mcp_consumption"
    ):
        result = _read_health_check_interval()
    assert result == DEFAULT_HEALTH_CHECK_INTERVAL_SEC
    assert any("not numeric" in r.message for r in caplog.records)


def test_interval_zero_falls_back(monkeypatch, caplog):
    import logging

    monkeypatch.setenv(HEALTH_CHECK_INTERVAL_ENV, "0")
    with caplog.at_level(
        logging.WARNING, logger="kora_cli.listeners.mcp_consumption"
    ):
        result = _read_health_check_interval()
    assert result == DEFAULT_HEALTH_CHECK_INTERVAL_SEC
    assert any("must be > 0" in r.message for r in caplog.records)


def test_interval_negative_falls_back(monkeypatch):
    monkeypatch.setenv(HEALTH_CHECK_INTERVAL_ENV, "-30")
    assert _read_health_check_interval() == DEFAULT_HEALTH_CHECK_INTERVAL_SEC


# ---------------------------------------------------------------------------
# Periodic task registered at module-import time
# ---------------------------------------------------------------------------


def test_periodic_task_registered_in_registry():
    """Import-time side effect: ``register_periodic_task("mcp.health_check", ...)``
    fires when kora_cli.listeners.mcp_consumption imports. The
    HeartbeatScheduler picks it up at daemon start."""
    # Force import so registration fires (already imported, but
    # safe in this test path).
    from kora_cli.listeners import mcp_consumption  # noqa: F401

    names = {t.name for t in heartbeat_module.PERIODIC_TASK_REGISTRY}
    assert "mcp.health_check" in names

    task = next(
        t
        for t in heartbeat_module.PERIODIC_TASK_REGISTRY
        if t.name == "mcp.health_check"
    )
    # Cadence honors env (or default); the callable is run_health_check
    assert task.interval_seconds > 0
    assert task.callable is run_health_check


# ---------------------------------------------------------------------------
# Stale-snapshot derivation (mirrors what the panel endpoint computes)
# ---------------------------------------------------------------------------


def test_stale_snapshot_detection_via_threshold():
    """The endpoint compares (now - snapshot.last_check_at) against
    the cadence to decide stale. A snapshot taken longer ago than
    the cadence is "stale" → panel reports
    configured_but_unconnected instead of connected. This test pins
    the timedelta-based comparison the endpoint uses."""
    now = datetime.now(timezone.utc)
    cadence = timedelta(seconds=DEFAULT_HEALTH_CHECK_INTERVAL_SEC)

    fresh = HealthSnapshot(
        connected=True,
        tools_count=5,
        last_check_at=now - timedelta(seconds=60),
        last_error=None,
    )
    stale = HealthSnapshot(
        connected=True,
        tools_count=5,
        last_check_at=now - timedelta(seconds=DEFAULT_HEALTH_CHECK_INTERVAL_SEC + 60),
        last_error=None,
    )
    assert (now - fresh.last_check_at) <= cadence
    assert (now - stale.last_check_at) > cadence
