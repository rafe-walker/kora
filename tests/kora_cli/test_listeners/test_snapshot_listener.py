"""Tests for the KR-CHEAP-PRE-WARMED-SNAPSHOT daemon listener.

Scenarios:
  1. Listener registered in LISTENER_REGISTRY at import time
  2. Periodic task `snapshot.compute` registered with heartbeat
  3. Default cadence 300s (5 min); env override respected
  4. Invalid env value falls back to default + WARNs
  5. Listener factory tuple shape correct

# KR-DAEMON-LISTENERS-VIA-GATEWAY Phase 1 — additional scenarios

  6. Listener ALSO registered against Hermes background_daemon_registry
  7. Hermes entry's periodic_task carries the same callback + interval
  8. Startup accepts the optional coordinator kwarg (Hermes consumer
     shape) without breaking the no-arg Kora-side call shape
  9. Hermes-side + Kora-side registrations point at the SAME listener
     singleton (so behavior stays identical under either consumer)
"""

from __future__ import annotations

import pytest

from agent.background_daemon_registry import (
    BackgroundDaemonEntry,
    background_daemon_registry,
)
from kora_cli import daemon as daemon_mod
from kora_cli.listeners.heartbeat import PERIODIC_TASK_REGISTRY
from kora_cli.listeners.snapshot_listener import (
    DEFAULT_INTERVAL_SEC,
    INTERVAL_ENV,
    SnapshotListener,
    _factory,
    _listener_singleton,
    _read_interval,
)
from kora_cli.snapshot import run_snapshot_cycle


def test_listener_registered_in_daemon_registry():
    names = {name for name, _f in daemon_mod.LISTENER_REGISTRY}
    assert "snapshot" in names


def test_periodic_task_registered():
    names = [t.name for t in PERIODIC_TASK_REGISTRY]
    assert "snapshot.compute" in names


def test_factory_tuple_shape():
    startup, shutdown, timeout = _factory()
    assert callable(startup)
    assert callable(shutdown)
    assert isinstance(timeout, (int, float))
    assert timeout > 0


def test_read_interval_default(monkeypatch):
    monkeypatch.delenv(INTERVAL_ENV, raising=False)
    assert _read_interval() == DEFAULT_INTERVAL_SEC == 300.0


def test_read_interval_env_override(monkeypatch):
    monkeypatch.setenv(INTERVAL_ENV, "60")
    assert _read_interval() == 60.0


def test_read_interval_invalid_falls_back(monkeypatch, caplog):
    monkeypatch.setenv(INTERVAL_ENV, "not-numeric")
    with caplog.at_level("WARNING"):
        assert _read_interval() == DEFAULT_INTERVAL_SEC
    assert any("is not numeric" in r.message for r in caplog.records)


def test_read_interval_zero_falls_back(monkeypatch):
    monkeypatch.setenv(INTERVAL_ENV, "0")
    assert _read_interval() == DEFAULT_INTERVAL_SEC


def test_read_interval_negative_falls_back(monkeypatch):
    monkeypatch.setenv(INTERVAL_ENV, "-5")
    assert _read_interval() == DEFAULT_INTERVAL_SEC


@pytest.mark.asyncio
async def test_listener_lifecycle_logs(caplog):
    """Listener startup + shutdown emit info-level lines so operator
    can confirm via boot logs."""
    listener = SnapshotListener()
    with caplog.at_level("INFO"):
        await listener.startup()
        await listener.shutdown()
    messages = " ".join(r.message for r in caplog.records)
    assert "snapshot periodic task registered" in messages
    assert "shutdown" in messages


# ---------------------------------------------------------------------------
# KR-DAEMON-LISTENERS-VIA-GATEWAY Phase 1 — dual-registry tests
# ---------------------------------------------------------------------------


def test_hermes_registry_has_snapshot_entry():
    """Phase 1 migration: snapshot listener is registered against
    Hermes's BackgroundDaemonRegistry, in addition to the Kora-side
    LISTENER_REGISTRY (verified by the existing
    test_listener_registered_in_daemon_registry above)."""
    entry = background_daemon_registry().by_name("snapshot")
    assert entry is not None
    assert isinstance(entry, BackgroundDaemonEntry)
    assert entry.name == "snapshot"
    assert entry.plugin_name == "kora"


def test_hermes_entry_carries_periodic_task_spec():
    """The Hermes entry's periodic_task carries the SAME callback
    + interval the heartbeat scheduler runs. Future gateway consumers
    can drive periodicity from this single source without having to
    also know about Kora's heartbeat scheduler."""
    entry = background_daemon_registry().by_name("snapshot")
    assert entry is not None
    assert entry.periodic_task is not None
    assert entry.periodic_task.callback is run_snapshot_cycle
    assert entry.periodic_task.interval_seconds == _read_interval()
    assert entry.periodic_task.name == "snapshot.compute"


def test_hermes_and_kora_registrations_share_singleton():
    """Behavior stays identical whether Kora's coordinator or the
    Hermes consumer drives the lifecycle — both consumers receive
    the same listener instance's bound methods.

    Pinning this prevents a future refactor from accidentally minting
    two separate SnapshotListener instances (which would double-log
    on startup, etc.)."""
    kora_startup, kora_shutdown, _ = _factory()
    hermes_entry = background_daemon_registry().by_name("snapshot")
    assert hermes_entry is not None
    assert kora_startup == _listener_singleton.startup
    assert kora_shutdown == _listener_singleton.shutdown
    assert hermes_entry.startup == _listener_singleton.startup
    assert hermes_entry.shutdown == _listener_singleton.shutdown


@pytest.mark.asyncio
async def test_startup_accepts_optional_coordinator():
    """The Hermes BackgroundDaemonEntry.startup signature is
    ``Callable[[Any], Any]`` (one positional arg = the coordinator).
    Kora's coordinator calls startup() with no arg today. The migrated
    startup accepts an optional coordinator kwarg so BOTH consumer
    shapes work without an adapter wrapper."""
    listener = SnapshotListener()
    # Kora-side: no arg
    await listener.startup()
    # Hermes-side: coordinator arg (just a stand-in object)
    await listener.startup(coordinator=object())
    # Hermes-side: positional
    await listener.startup(object())


def test_shutdown_timeout_matches_default():
    """Both registries use the same DEFAULT_SHUTDOWN_TIMEOUT so a
    future consumer-side timeout policy switch only needs to change
    one constant."""
    from kora_cli.daemon import DEFAULT_SHUTDOWN_TIMEOUT
    entry = background_daemon_registry().by_name("snapshot")
    assert entry is not None
    assert entry.shutdown_timeout == DEFAULT_SHUTDOWN_TIMEOUT
