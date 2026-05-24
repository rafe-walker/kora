"""Tests for the KR-CHEAP-PRE-WARMED-SNAPSHOT daemon listener.

Scenarios:
  1. Listener registered in LISTENER_REGISTRY at import time
  2. Periodic task `snapshot.compute` registered with heartbeat
  3. Default cadence 300s (5 min); env override respected
  4. Invalid env value falls back to default + WARNs
  5. Listener factory tuple shape correct
"""

from __future__ import annotations

import pytest

from kora_cli import daemon as daemon_mod
from kora_cli.listeners.heartbeat import PERIODIC_TASK_REGISTRY
from kora_cli.listeners.snapshot_listener import (
    DEFAULT_INTERVAL_SEC,
    INTERVAL_ENV,
    SnapshotListener,
    _factory,
    _read_interval,
)


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
