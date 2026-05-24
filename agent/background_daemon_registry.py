"""KR-HERMES-LOCAL-EXTENSIONS — registry for plugin-provided background daemons.

# The gap

Hermes' ``PluginContext.register_platform`` is shaped for
interactive chat-platform adapters (Slack, IRC, Discord) that
bind to a ``PlatformConfig`` and run inside the gateway's
session-driven main loop. It is NOT the right surface for a
different category of plugin behavior: **background daemons**
that run from process boot to shutdown, independent of any
chat session, and may have periodic-task callbacks (e.g. a
snapshot collector that fires every 5 min; a telemetry counter
flusher that fires on a window-rollover schedule).

Per the KR-FORK-HOOK-VERIFY research (#168) finding, Kora's
``DaemonCoordinator`` is the right shape for this category;
this module exposes a Hermes-native equivalent so plugins
authored against any Hermes-fork can register the same shape
without forking the runtime.

# Out of scope for this PR (intentional)

This module is **registration-only**. It collects
``BackgroundDaemonEntry`` records into a singleton registry +
exposes them via :func:`background_daemon_registry`. It does
NOT drive their startup/shutdown lifecycle — that wiring is
the gateway/CLI consumer's responsibility, landing in
KR-REASONING-ROUTE-THROUGH-GATEWAY (the follow-on bucket that
will make Kora route through the Hermes gateway and thus need
this registration surface).

A consumer of this registry runs roughly:

.. code-block:: python

    from agent.background_daemon_registry import background_daemon_registry

    for entry in background_daemon_registry.list_entries():
        await entry.startup(coordinator)
    # ... main loop ...
    for entry in reversed(background_daemon_registry.list_entries()):
        await asyncio.wait_for(entry.shutdown(), timeout=entry.shutdown_timeout)

The Kora-side ``DaemonCoordinator`` (``kora_cli/daemon.py``)
already implements the consumer side; the route-through bucket
will plug the registry's entries into the same lifecycle.

# Backward compatibility

This is a NEW module; no existing code paths are affected. The
registry is a process-global singleton (matching Hermes' other
registry singletons: ``platform_registry``, ``context_engines``,
etc.) so plugin entry_points loaded at import time can populate
it before any consumer reads.

# Upstream-PR readiness

This module's surface is intentionally minimal so it packages
cleanly into a future Hermes-upstream PR (``feedback-local-
first-upstream-after``): one dataclass, one registry singleton,
one register method. The lifecycle execution + the periodic-
task driver are explicitly deferred so the upstream conversation
can ratify the API surface independently of the consumer wiring.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, List, Optional

logger = logging.getLogger(__name__)


# Type aliases for the daemon lifecycle callbacks. Callers may
# pass either sync or async functions; the consumer is
# responsible for awaiting / running them appropriately.
StartupCallable = Callable[[Any], Any]
ShutdownCallable = Callable[[], Any]
PeriodicCallable = Callable[[], Any]


@dataclass(frozen=True)
class PeriodicTaskSpec:
    """Optional periodic-task spec attached to a background daemon.

    The consumer (gateway / CLI) schedules ``callback`` to fire
    every ``interval_seconds`` after the daemon's startup
    completes and before its shutdown begins. Callback may be
    sync or async; consumer awaits async returns.
    """

    interval_seconds: float
    callback: PeriodicCallable
    name: str = ""  # human-readable for logs; defaults to callback __name__


@dataclass(frozen=True)
class BackgroundDaemonEntry:
    """One registered background-daemon plugin.

    Attributes:
      name: unique identifier (must be unique across the registry;
        a duplicate registration raises).
      startup: callable invoked once at consumer-driven start.
        Receives the coordinator object the consumer manages
        (typically a ``DaemonCoordinator``-shaped object); the
        callback's contract with that object is its own concern.
      shutdown: callable invoked once at consumer-driven shutdown.
        Reverse-LIFO ordering recommended by the consumer.
      periodic_task: optional :class:`PeriodicTaskSpec` for
        periodic-callback semantics. ``None`` (default) → daemon
        is event-driven only.
      shutdown_timeout: max seconds the consumer should wait on
        ``shutdown()`` before force-cancelling. Default 5s
        matches the Kora ``DaemonCoordinator`` default; consumer
        may override.
      plugin_name: name of the plugin that registered the daemon
        (set automatically when registered via
        :meth:`PluginContext.register_background_daemon`).
    """

    name: str
    startup: StartupCallable
    shutdown: ShutdownCallable
    periodic_task: Optional[PeriodicTaskSpec] = None
    shutdown_timeout: float = 5.0
    plugin_name: str = ""


class BackgroundDaemonRegistry:
    """Process-global registry for background-daemon plugin entries.

    Singleton-style: use :func:`background_daemon_registry` to
    access the process-wide instance. The class is instantiable
    for test isolation.

    Thread safety: every method is wrapped in an RLock. Plugin
    discovery typically happens at import time on the main
    thread, but consumers (gateway main loop, CLI startup) may
    iterate from a different thread; the lock keeps the
    iteration / mutation interleaving safe.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: List[BackgroundDaemonEntry] = []

    def register(self, entry: BackgroundDaemonEntry) -> None:
        """Register a new background-daemon entry.

        Raises ``ValueError`` if ``entry.name`` is already
        registered (same fail-loud semantic as
        ``platform_registry.register``).
        """
        with self._lock:
            for existing in self._entries:
                if existing.name == entry.name:
                    raise ValueError(
                        f"background daemon {entry.name!r} is already "
                        f"registered (by plugin "
                        f"{existing.plugin_name or '<unknown>'})"
                    )
            self._entries.append(entry)
            logger.debug(
                "[background_daemon_registry] registered %s (plugin=%s, "
                "periodic=%s)",
                entry.name,
                entry.plugin_name or "<unknown>",
                bool(entry.periodic_task),
            )

    def list_entries(self) -> List[BackgroundDaemonEntry]:
        """Return a shallow copy of registered entries in
        registration order. Consumer iterates this list at
        startup (FIFO) and at shutdown (LIFO recommended)."""
        with self._lock:
            return list(self._entries)

    def by_name(self, name: str) -> Optional[BackgroundDaemonEntry]:
        """Return the entry with the given name, or ``None``."""
        with self._lock:
            for entry in self._entries:
                if entry.name == name:
                    return entry
            return None

    def reset_for_tests(self) -> None:
        """Drop every registered entry. Tests-only — production
        code should never need this (registry is process-global +
        populated at plugin discovery)."""
        with self._lock:
            self._entries.clear()


_registry_singleton: Optional[BackgroundDaemonRegistry] = None
_registry_lock = threading.Lock()


def background_daemon_registry() -> BackgroundDaemonRegistry:
    """Return the process-wide singleton registry. Lazily
    constructed on first call so import order doesn't matter."""
    global _registry_singleton
    if _registry_singleton is None:
        with _registry_lock:
            if _registry_singleton is None:
                _registry_singleton = BackgroundDaemonRegistry()
    return _registry_singleton
