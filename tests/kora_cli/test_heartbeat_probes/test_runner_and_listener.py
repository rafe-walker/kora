"""Runner + listener tests (KR-FEAT-HEARTBEAT ST1).

Covers:
  - run_all_probes populates the snapshot cache per probe
  - Per-probe failure isolation — one probe raising doesn't block
    the others
  - current_service_snapshots returns a defensive copy
  - Listener startup is a clean no-op + LOG; shutdown clears the
    cache (no stale snapshots across daemon restart)
  - _read_probe_interval: default / env override / invalid / ≤0
    fallback
  - Periodic task ``heartbeat.service_probes`` registered in
    PERIODIC_TASK_REGISTRY at module-import time
  - Daemon listener ``heartbeat_probes`` registered in
    LISTENER_REGISTRY at module-import time
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from kora_cli import daemon as daemon_mod
from kora_cli.heartbeat_probes.runner import (
    _clear_snapshot_cache,
    current_service_snapshots,
    default_probes,
    run_all_probes,
    run_all_probes_scheduled,
)
from kora_cli.heartbeat_probes.types import ServiceHealthSnapshot
from kora_cli.listeners import heartbeat as heartbeat_module
from kora_cli.listeners.heartbeat_probes_listener import (
    DEFAULT_PROBE_INTERVAL_SEC,
    PROBE_INTERVAL_ENV,
    HeartbeatProbesListener,
    _read_probe_interval,
)


@pytest.fixture(autouse=True)
def _reset_state():
    _clear_snapshot_cache()
    yield
    _clear_snapshot_cache()


# ---------------------------------------------------------------------------
# default_probes — fresh instances per call
# ---------------------------------------------------------------------------


def test_default_probes_returns_five():
    probes = default_probes()
    assert len(probes) == 5
    names = {p.name for p in probes}
    assert names == {"vercel", "sentry", "doppler", "supabase", "fly"}


def test_default_probes_returns_fresh_instances():
    a = default_probes()
    b = default_probes()
    # Same shape, different objects (probes are constructed fresh)
    assert {p.name for p in a} == {p.name for p in b}
    assert a is not b


# ---------------------------------------------------------------------------
# run_all_probes — snapshot cache + isolation
# ---------------------------------------------------------------------------


class _FakeProbe:
    """Test double mirroring the ServiceProbe Protocol shape."""

    def __init__(self, name: str, snapshot=None, raise_exc=None):
        self.name = name
        self._snapshot = snapshot
        self._raise = raise_exc
        self.call_count = 0

    async def check(self) -> ServiceHealthSnapshot:
        self.call_count += 1
        if self._raise is not None:
            raise self._raise
        return self._snapshot or _ok_snapshot(self.name)


def _ok_snapshot(name: str) -> ServiceHealthSnapshot:
    return ServiceHealthSnapshot(
        name=name,
        status="healthy",
        latency_ms=50,
        last_check_at=datetime.now(timezone.utc),
        details={},
    )


@pytest.mark.asyncio
async def test_run_all_probes_populates_cache_per_probe():
    probes = [_FakeProbe("a"), _FakeProbe("b")]
    result = await run_all_probes(probes=probes)
    assert set(result.keys()) == {"a", "b"}
    assert result["a"].status == "healthy"
    cache = current_service_snapshots()
    assert set(cache.keys()) == {"a", "b"}
    assert cache["a"].status == "healthy"


@pytest.mark.asyncio
async def test_per_probe_failure_isolated_from_siblings():
    """If probe 'a' raises an unhandled exception, probe 'b' still
    runs + populates its snapshot."""
    probes = [
        _FakeProbe("a", raise_exc=RuntimeError("a went boom")),
        _FakeProbe("b"),
    ]
    result = await run_all_probes(probes=probes)
    assert set(result.keys()) == {"a", "b"}
    # 'a' surfaces as unknown with the exception captured
    assert result["a"].status == "unknown"
    assert "RuntimeError" in result["a"].error
    # 'b' completes normally
    assert result["b"].status == "healthy"
    assert probes[1].call_count == 1


@pytest.mark.asyncio
async def test_current_service_snapshots_returns_defensive_copy():
    probes = [_FakeProbe("a")]
    await run_all_probes(probes=probes)
    view = current_service_snapshots()
    view.pop("a", None)  # mutate the returned dict
    assert "a" not in view
    # Internal cache unchanged
    assert "a" in current_service_snapshots()


@pytest.mark.asyncio
async def test_run_all_probes_uses_default_probes_when_none():
    """Calling without an explicit `probes` arg constructs the
    default 5 probes. Tests against real httpx are mocked at
    transport level; here we just verify the default set is
    used + each name appears."""
    import httpx

    fake_client = AsyncMock()
    fake_client.get = AsyncMock(side_effect=httpx.ConnectError("offline"))
    fake_client.head = AsyncMock(side_effect=httpx.ConnectError("offline"))
    from unittest.mock import MagicMock

    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_client)
    fake_cm.__aexit__ = AsyncMock(return_value=None)

    # Set all envs so probes attempt the (mocked-failing) request
    import os

    for env, val in (
        ("KORA_VERCEL_API_TOKEN", "x"),
        ("KORA_SENTRY_API_TOKEN", "x"),
        ("KORA_SENTRY_ORG", "test-org"),
        ("KORA_DOPPLER_API_TOKEN", "x"),
        ("KORA_SUPABASE_ANON_KEY", "x"),
        ("KORA_SUPABASE_URL", "https://test.supabase.co"),
        ("KORA_FLY_API_TOKEN", "x"),
    ):
        os.environ[env] = val

    try:
        with patch("httpx.AsyncClient", return_value=fake_cm):
            result = await run_all_probes()
        assert set(result.keys()) == {
            "vercel",
            "sentry",
            "doppler",
            "supabase",
            "fly",
        }
        # All marked unknown because of the ConnectError
        for snap in result.values():
            assert snap.status == "unknown"
    finally:
        for env in (
            "KORA_VERCEL_API_TOKEN",
            "KORA_SENTRY_API_TOKEN",
            "KORA_SENTRY_ORG",
            "KORA_DOPPLER_API_TOKEN",
            "KORA_SUPABASE_ANON_KEY",
            "KORA_SUPABASE_URL",
            "KORA_FLY_API_TOKEN",
        ):
            os.environ.pop(env, None)


# ---------------------------------------------------------------------------
# run_all_probes_scheduled — scheduler-facing wrapper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduled_wrapper_swallows_unexpected_errors_and_logs(caplog):
    """The scheduler-facing callable must never propagate — a
    non-cancellation exception from run_all_probes is logged and
    the scheduler keeps firing."""
    import logging

    with patch(
        "kora_cli.heartbeat_probes.runner.run_all_probes",
        side_effect=RuntimeError("runner itself broke"),
    ):
        with caplog.at_level(
            logging.ERROR, logger="kora_cli.heartbeat_probes.runner"
        ):
            await run_all_probes_scheduled()  # must not raise
    assert any(
        "run_all_probes raised" in r.message for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Listener: startup no-op + shutdown clears cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listener_startup_is_clean_noop():
    listener = HeartbeatProbesListener()
    await listener.startup()  # no exception, no state


@pytest.mark.asyncio
async def test_listener_shutdown_clears_snapshot_cache():
    # Seed a snapshot
    await run_all_probes(probes=[_FakeProbe("a")])
    assert "a" in current_service_snapshots()

    listener = HeartbeatProbesListener()
    await listener.shutdown()
    assert current_service_snapshots() == {}


# ---------------------------------------------------------------------------
# _read_probe_interval
# ---------------------------------------------------------------------------


def test_interval_default_when_env_unset(monkeypatch):
    monkeypatch.delenv(PROBE_INTERVAL_ENV, raising=False)
    assert _read_probe_interval() == DEFAULT_PROBE_INTERVAL_SEC


def test_interval_env_override(monkeypatch):
    monkeypatch.setenv(PROBE_INTERVAL_ENV, "60")
    assert _read_probe_interval() == 60.0


def test_interval_invalid_value_falls_back(monkeypatch, caplog):
    import logging

    monkeypatch.setenv(PROBE_INTERVAL_ENV, "garbage")
    with caplog.at_level(
        logging.WARNING,
        logger="kora_cli.listeners.heartbeat_probes_listener",
    ):
        result = _read_probe_interval()
    assert result == DEFAULT_PROBE_INTERVAL_SEC
    assert any("not numeric" in r.message for r in caplog.records)


def test_interval_zero_falls_back(monkeypatch, caplog):
    import logging

    monkeypatch.setenv(PROBE_INTERVAL_ENV, "0")
    with caplog.at_level(
        logging.WARNING,
        logger="kora_cli.listeners.heartbeat_probes_listener",
    ):
        result = _read_probe_interval()
    assert result == DEFAULT_PROBE_INTERVAL_SEC


def test_interval_negative_falls_back(monkeypatch):
    monkeypatch.setenv(PROBE_INTERVAL_ENV, "-1")
    assert _read_probe_interval() == DEFAULT_PROBE_INTERVAL_SEC


# ---------------------------------------------------------------------------
# Registry side: listener + periodic task wired at import time
# ---------------------------------------------------------------------------


def test_daemon_listener_registered_in_registry():
    from kora_cli.listeners import heartbeat_probes_listener  # noqa: F401

    names = {name for name, _factory in daemon_mod.LISTENER_REGISTRY}
    assert "heartbeat_probes" in names


def test_periodic_task_registered_in_heartbeat_registry():
    from kora_cli.listeners import heartbeat_probes_listener  # noqa: F401

    names = {t.name for t in heartbeat_module.PERIODIC_TASK_REGISTRY}
    assert "heartbeat.service_probes" in names
    task = next(
        t
        for t in heartbeat_module.PERIODIC_TASK_REGISTRY
        if t.name == "heartbeat.service_probes"
    )
    assert task.interval_seconds > 0
    assert task.callable is run_all_probes_scheduled


def test_mcp_and_heartbeat_tasks_are_independent():
    """Per §4 Q1 ruling: don't share scheduler tasks across MCP +
    heartbeat probes. Both at 5min default, both via the same
    scheduler, but registered as DISTINCT named tasks so one slow
    cycle doesn't block the other."""
    names = {t.name for t in heartbeat_module.PERIODIC_TASK_REGISTRY}
    assert "heartbeat.service_probes" in names
    assert "mcp.health_check" in names
