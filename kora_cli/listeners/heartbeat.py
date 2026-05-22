"""Heartbeat scheduler listener (KR-D-DAEMON ST2 — listener "heartbeat").

Pure asyncio periodic-task runner; no APScheduler dependency. Feature
modules register tasks via ``register_periodic_task(name, interval,
callable)`` at import time; the scheduler reads the module-level
registry when its factory mints the instance + spawns one
``asyncio.create_task`` loop per registered task on startup.

# ST2 scope

The scheduler is plumbing only this ST. Single pre-registered task
``kora.daemon.alive`` every 30s emits a no-op log line — proves the
loop wiring without depending on Feature 2's chain-event emit code,
which lands in a separate bucket.

# Graceful shutdown

On shutdown: cancel every task loop, then ``asyncio.wait`` for them
to finish with a 10s timeout. A task currently mid-callback gets up
to 10s to return; if it doesn't, the cancel propagates and the
scheduler returns.

# How a Feature 2 task gets added later

  from kora_cli.listeners.heartbeat import register_periodic_task

  async def emit_dashboard_heartbeat() -> None:
      ...

  register_periodic_task(
      "kora.dashboard.heartbeat",
      interval_seconds=15.0,
      callable=emit_dashboard_heartbeat,
  )

The call must happen at import time, BEFORE ``cmd_daemon`` walks the
LISTENER_REGISTRY + the heartbeat factory mints the scheduler. The
Feature 2 module should be imported by ``kora_cli/listeners/__init__.py``
in the order it wants.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional

from kora_cli.daemon import DEFAULT_SHUTDOWN_TIMEOUT, register_daemon_listener

logger = logging.getLogger(__name__)


PeriodicCallable = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _PeriodicTask:
    """A single registered periodic task."""

    name: str
    interval_seconds: float
    callable: PeriodicCallable


# Module-level registry. Feature modules append to this at import time.
PERIODIC_TASK_REGISTRY: List[_PeriodicTask] = []


def register_periodic_task(
    name: str,
    interval_seconds: float,
    callable: PeriodicCallable,
) -> None:
    """Register a coroutine to be called every ``interval_seconds``.

    Repeated names overwrite — the test-fixture pattern mirrors
    ``daemon.register_daemon_listener``.
    """
    if not name:
        raise ValueError("periodic-task name must be non-empty")
    if interval_seconds <= 0:
        raise ValueError(
            f"periodic-task interval must be > 0; got {interval_seconds!r}"
        )
    if not callable:
        raise ValueError("periodic-task callable must be provided")
    global PERIODIC_TASK_REGISTRY
    PERIODIC_TASK_REGISTRY = [
        t for t in PERIODIC_TASK_REGISTRY if t.name != name
    ]
    PERIODIC_TASK_REGISTRY.append(
        _PeriodicTask(
            name=name, interval_seconds=interval_seconds, callable=callable
        )
    )


class HeartbeatScheduler:
    """Owns one ``asyncio.Task`` per registered periodic task.

    Constructed by the listener factory; one fresh instance per
    daemon-start.
    """

    def __init__(self, tasks: List[_PeriodicTask]) -> None:
        # Defensive copy — the registry may mutate after construction
        # (e.g. a test re-imports modules), and we want stable behavior
        # for the lifetime of this instance.
        self._tasks: List[_PeriodicTask] = list(tasks)
        self._running: List[asyncio.Task] = []
        self._stopping: bool = False

    async def startup(self) -> None:
        for task in self._tasks:
            asyncio_task = asyncio.create_task(
                self._loop(task), name=f"heartbeat:{task.name}"
            )
            self._running.append(asyncio_task)
        logger.info(
            "[kora.heartbeat] started %d periodic task(s): %s",
            len(self._running),
            [t.name for t in self._tasks],
        )

    async def shutdown(self) -> None:
        self._stopping = True
        for asyncio_task in self._running:
            asyncio_task.cancel()
        # Give in-flight callbacks the per-listener timeout (10s) to
        # return. The coordinator wraps shutdown() in wait_for, so we
        # don't enforce a separate per-task timeout here.
        if self._running:
            await asyncio.gather(*self._running, return_exceptions=True)
        logger.info("[kora.heartbeat] all tasks stopped")

    async def _loop(self, task: _PeriodicTask) -> None:
        """Per-task loop: sleep interval, run callable, repeat."""
        # First fire delayed by interval — avoids a thundering-herd
        # of every task firing at t=0 on daemon start.
        try:
            while not self._stopping:
                try:
                    await asyncio.sleep(task.interval_seconds)
                except asyncio.CancelledError:
                    return
                if self._stopping:
                    return
                try:
                    await task.callable()
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    # Don't let one task's failure kill the loop —
                    # log + continue. Operator triages via dashboard.
                    logger.warning(
                        "[kora.heartbeat] task %s raised %r — continuing loop",
                        task.name,
                        exc,
                    )
        except asyncio.CancelledError:
            return


# ---------------------------------------------------------------------------
# Pre-registered placeholder task
# ---------------------------------------------------------------------------


async def _alive_placeholder() -> None:
    """ST2 placeholder — actual chain-event emit lands in Feature 2."""
    logger.info("[kora.heartbeat] kora.daemon.alive")


register_periodic_task(
    "kora.daemon.alive",
    interval_seconds=30.0,
    callable=_alive_placeholder,
)


# ---------------------------------------------------------------------------
# Listener registration
# ---------------------------------------------------------------------------


def _factory():
    """Mint a fresh HeartbeatScheduler bound to the current registry."""
    scheduler = HeartbeatScheduler(PERIODIC_TASK_REGISTRY)
    return (scheduler.startup, scheduler.shutdown, DEFAULT_SHUTDOWN_TIMEOUT)


register_daemon_listener("heartbeat", _factory)
