"""Tests for agent.background_daemon_registry."""

from __future__ import annotations

import threading
import time

import pytest

from agent.background_daemon_registry import (
    BackgroundDaemonEntry,
    BackgroundDaemonRegistry,
    PeriodicTaskSpec,
    background_daemon_registry,
)


# ---------------------------------------------------------------------------
# BackgroundDaemonRegistry — fresh instance per test
# ---------------------------------------------------------------------------


@pytest.fixture
def registry() -> BackgroundDaemonRegistry:
    """Fresh BackgroundDaemonRegistry per test (bypasses the singleton
    so tests don't pollute each other)."""
    return BackgroundDaemonRegistry()


def test_register_then_list_returns_entry(registry):
    entry = BackgroundDaemonEntry(
        name="snapshot",
        startup=lambda c: None,
        shutdown=lambda: None,
    )
    registry.register(entry)
    entries = registry.list_entries()
    assert len(entries) == 1
    assert entries[0].name == "snapshot"


def test_register_preserves_order_fifo(registry):
    for name in ("a", "b", "c"):
        registry.register(BackgroundDaemonEntry(
            name=name, startup=lambda c: None, shutdown=lambda: None,
        ))
    assert [e.name for e in registry.list_entries()] == ["a", "b", "c"]


def test_register_duplicate_name_raises(registry):
    registry.register(BackgroundDaemonEntry(
        name="snap", startup=lambda c: None, shutdown=lambda: None,
        plugin_name="first_plugin",
    ))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(BackgroundDaemonEntry(
            name="snap", startup=lambda c: None, shutdown=lambda: None,
            plugin_name="second_plugin",
        ))


def test_register_with_periodic_task(registry):
    spec = PeriodicTaskSpec(
        interval_seconds=300.0,
        callback=lambda: None,
        name="snap.tick",
    )
    registry.register(BackgroundDaemonEntry(
        name="snap", startup=lambda c: None, shutdown=lambda: None,
        periodic_task=spec,
    ))
    entry = registry.list_entries()[0]
    assert entry.periodic_task is spec
    assert entry.periodic_task.interval_seconds == 300.0
    assert entry.periodic_task.name == "snap.tick"


def test_register_without_periodic_task_is_none(registry):
    registry.register(BackgroundDaemonEntry(
        name="x", startup=lambda c: None, shutdown=lambda: None,
    ))
    assert registry.list_entries()[0].periodic_task is None


def test_by_name_returns_entry_or_none(registry):
    registry.register(BackgroundDaemonEntry(
        name="a", startup=lambda c: None, shutdown=lambda: None,
    ))
    assert registry.by_name("a") is not None
    assert registry.by_name("a").name == "a"
    assert registry.by_name("missing") is None


def test_reset_for_tests_empties_registry(registry):
    for name in ("a", "b"):
        registry.register(BackgroundDaemonEntry(
            name=name, startup=lambda c: None, shutdown=lambda: None,
        ))
    assert len(registry.list_entries()) == 2
    registry.reset_for_tests()
    assert registry.list_entries() == []


def test_default_shutdown_timeout_is_5_seconds():
    entry = BackgroundDaemonEntry(
        name="x", startup=lambda c: None, shutdown=lambda: None,
    )
    assert entry.shutdown_timeout == 5.0


def test_shutdown_timeout_can_be_overridden():
    entry = BackgroundDaemonEntry(
        name="x", startup=lambda c: None, shutdown=lambda: None,
        shutdown_timeout=30.0,
    )
    assert entry.shutdown_timeout == 30.0


def test_concurrent_registration_thread_safe(registry):
    """Concurrent register from multiple threads doesn't lose entries.

    RLock around list mutation keeps the interleaving safe even
    when plugin discovery happens off the main thread.
    """
    n_threads = 10
    barrier = threading.Barrier(n_threads)

    def worker(i: int) -> None:
        barrier.wait()  # synchronize start
        registry.register(BackgroundDaemonEntry(
            name=f"daemon_{i}",
            startup=lambda c: None,
            shutdown=lambda: None,
        ))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(registry.list_entries()) == n_threads
    names = sorted(e.name for e in registry.list_entries())
    assert names == sorted(f"daemon_{i}" for i in range(n_threads))


# ---------------------------------------------------------------------------
# background_daemon_registry() — process-global singleton
# ---------------------------------------------------------------------------


def test_singleton_returns_same_instance_across_calls():
    a = background_daemon_registry()
    b = background_daemon_registry()
    assert a is b


def test_singleton_is_lazy_constructed():
    """Calling the accessor multiple times should be cheap + return
    the same instance. Lazy construction guards against import-order
    races between plugin entry_point loaders."""
    # Drop any state from prior tests to verify lazy semantics.
    background_daemon_registry().reset_for_tests()
    assert background_daemon_registry().list_entries() == []


# ---------------------------------------------------------------------------
# Entry shape — frozen dataclass invariants
# ---------------------------------------------------------------------------


def test_entry_is_frozen():
    entry = BackgroundDaemonEntry(
        name="x", startup=lambda c: None, shutdown=lambda: None,
    )
    with pytest.raises(Exception):  # FrozenInstanceError on dataclasses
        entry.name = "y"  # type: ignore[misc]


def test_periodic_task_spec_is_frozen():
    spec = PeriodicTaskSpec(interval_seconds=1.0, callback=lambda: None)
    with pytest.raises(Exception):
        spec.interval_seconds = 2.0  # type: ignore[misc]


def test_entry_plugin_name_defaults_empty():
    entry = BackgroundDaemonEntry(
        name="x", startup=lambda c: None, shutdown=lambda: None,
    )
    assert entry.plugin_name == ""


def test_periodic_task_spec_name_defaults_empty():
    spec = PeriodicTaskSpec(interval_seconds=1.0, callback=lambda: None)
    assert spec.name == ""
