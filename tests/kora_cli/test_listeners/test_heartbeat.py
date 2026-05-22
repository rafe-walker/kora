"""Tests for ``kora_cli.listeners.heartbeat`` — KR-D-DAEMON ST2.

Covers:
  - register_periodic_task validation
  - Periodic task fires N times in M seconds
  - Cancellation on shutdown
  - One task's exception doesn't kill the loop
  - Module-level pre-registered kora.daemon.alive present
"""

from __future__ import annotations

import asyncio
from typing import List

import pytest

from kora_cli.listeners import heartbeat as hb_mod
from kora_cli.listeners.heartbeat import (
    HeartbeatScheduler,
    PERIODIC_TASK_REGISTRY,
    _PeriodicTask,
    register_periodic_task,
)


# ---------------------------------------------------------------------------
# register_periodic_task validation
# ---------------------------------------------------------------------------


def test_register_periodic_task_validates_inputs():
    async def noop():
        pass

    with pytest.raises(ValueError, match="non-empty"):
        register_periodic_task("", 1.0, noop)
    with pytest.raises(ValueError, match="> 0"):
        register_periodic_task("x", 0, noop)
    with pytest.raises(ValueError, match="> 0"):
        register_periodic_task("x", -1, noop)
    with pytest.raises(ValueError, match="callable"):
        register_periodic_task("x", 1, None)


def test_register_periodic_task_overwrites_same_name():
    """Test-fixture pattern — re-registering replaces."""
    original = list(hb_mod.PERIODIC_TASK_REGISTRY)

    async def v1():
        pass

    async def v2():
        pass

    try:
        register_periodic_task("test_task", 1.0, v1)
        register_periodic_task("test_task", 2.0, v2)
        matching = [t for t in hb_mod.PERIODIC_TASK_REGISTRY if t.name == "test_task"]
        assert len(matching) == 1
        assert matching[0].interval_seconds == 2.0
        assert matching[0].callable is v2
    finally:
        hb_mod.PERIODIC_TASK_REGISTRY[:] = original


def test_module_preregisters_kora_daemon_alive():
    """``kora.daemon.alive`` is the ST2 placeholder, must always be
    in the registry after module import."""
    names = [t.name for t in PERIODIC_TASK_REGISTRY]
    assert "kora.daemon.alive" in names


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_periodic_task_fires_multiple_times():
    """A 50ms-interval task fires >=3 times in ~200ms."""
    call_count = 0

    async def increment():
        nonlocal call_count
        call_count += 1

    sched = HeartbeatScheduler(
        [_PeriodicTask("test", interval_seconds=0.05, callable=increment)]
    )
    await sched.startup()
    try:
        # First fire at t=0.05, second at t=0.10, third at t=0.15.
        await asyncio.sleep(0.18)
    finally:
        await sched.shutdown()

    assert call_count >= 3, f"expected >=3 fires, got {call_count}"


@pytest.mark.asyncio
async def test_shutdown_cancels_in_flight_tasks():
    """A long-running callback is cancelled by shutdown."""
    started_event = asyncio.Event()

    async def long_running():
        started_event.set()
        try:
            await asyncio.sleep(60)  # would hang test if not cancelled
        except asyncio.CancelledError:
            raise

    sched = HeartbeatScheduler(
        [_PeriodicTask("long", interval_seconds=0.01, callable=long_running)]
    )
    await sched.startup()
    # Wait for the first invocation to begin.
    await asyncio.wait_for(started_event.wait(), timeout=1.0)
    # Shutdown must return promptly via cancellation.
    await asyncio.wait_for(sched.shutdown(), timeout=2.0)


@pytest.mark.asyncio
async def test_failing_task_does_not_kill_loop():
    """One task raising doesn't stop the OTHER task's loop."""
    good_count = 0

    async def good_task():
        nonlocal good_count
        good_count += 1

    async def bad_task():
        raise RuntimeError("intentional")

    sched = HeartbeatScheduler(
        [
            _PeriodicTask("good", interval_seconds=0.03, callable=good_task),
            _PeriodicTask("bad", interval_seconds=0.03, callable=bad_task),
        ]
    )
    await sched.startup()
    try:
        await asyncio.sleep(0.15)  # ~5 fires of each
    finally:
        await sched.shutdown()
    # The good task should have continued firing despite the bad task
    # raising on every interval.
    assert good_count >= 3, f"good_task fired only {good_count} times"


@pytest.mark.asyncio
async def test_empty_scheduler_starts_and_stops_cleanly():
    sched = HeartbeatScheduler([])
    await sched.startup()
    await sched.shutdown()


@pytest.mark.asyncio
async def test_first_fire_delayed_by_interval():
    """Tasks DON'T fire immediately on startup — they wait one
    interval first. Avoids a thundering-herd at daemon-start."""
    fired = False

    async def task():
        nonlocal fired
        fired = True

    sched = HeartbeatScheduler(
        [_PeriodicTask("delayed", interval_seconds=1.0, callable=task)]
    )
    await sched.startup()
    try:
        await asyncio.sleep(0.05)  # well below interval
        assert fired is False, "task fired immediately — should wait interval"
    finally:
        await sched.shutdown()
