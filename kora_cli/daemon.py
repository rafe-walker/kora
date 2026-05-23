"""Kora daemon coordinator (KR-D-DAEMON ST1).

The daemon is the always-alive long-running process that hosts Kora's
concurrent async listeners (web admin UI, MCP server, heartbeat
scheduler, and ST3's webhook routers). ST1 ships the harness:
ST2/ST3/future features plug listeners in via ``register_daemon_listener``.

# Lifecycle shape

  1. ``DaemonCoordinator.run()`` is awaited from ``cmd_daemon``.
  2. It installs SIGTERM + SIGINT handlers.
  3. It walks ``LISTENER_REGISTRY`` (filtered by CLI flags) and awaits
     each ``startup()`` in **registration order** (FIFO).
  4. It blocks on the shutdown event.
  5. On shutdown signal — operator SIGTERM/SIGINT, programmatic
     ``request_shutdown()``, or a startup failure — it awaits each
     started listener's ``shutdown()`` in **reverse order** (LIFO)
     with a per-listener timeout (default 10s, configurable per
     listener).
  6. Returns the exit code.

# Startup-failure recovery

If any listener's ``startup()`` raises, the coordinator does NOT start
the remaining listeners. It calls ``shutdown()`` on the listeners
that DID start (LIFO), then returns a non-zero exit code. This is the
fail-CLOSED default (per ``feedback_fail_closed_by_default_security_infra``):
a daemon that boots partially is worse than one that refuses to boot.

# Boot-time deploy-env gate

The daemon refuses to start unless ``KORA_DEPLOY_ENV`` is set. The
local-dev path is preserved via ``KORA_DEV=1`` — when set, the
effective deploy env defaults to ``dev``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional

logger = logging.getLogger(__name__)


# Callable signatures — coroutine factories that the coordinator awaits.
StartupCallable = Callable[[], Awaitable[None]]
ShutdownCallable = Callable[[], Awaitable[None]]


# Default per-listener shutdown timeout (seconds). Listeners may
# override per-registration via the ``shutdown_timeout`` arg.
DEFAULT_SHUTDOWN_TIMEOUT = 10.0


@dataclass(frozen=True, slots=True)
class _Listener:
    """A single registered listener.

    Frozen so registration order is observable and immutable once
    the daemon starts; the coordinator iterates this in FIFO/LIFO.
    """

    name: str
    startup: StartupCallable
    shutdown: ShutdownCallable
    shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT


# ---------------------------------------------------------------------------
# Module-level listener registry
# ---------------------------------------------------------------------------

# Future feature modules call ``register_daemon_listener`` at import
# time to plug into the daemon. ``cmd_daemon`` walks this list on
# start. ST1 leaves it empty; ST2 lands web + MCP + heartbeat.
#
# Factory shape: ``(name, factory)`` where ``factory()`` returns a
# ``(startup, shutdown[, shutdown_timeout])`` tuple. The factory is
# called once per daemon-start (so a fresh listener instance is
# minted each time — no module-level mutable state).
_Factory = Callable[[], object]
LISTENER_REGISTRY: List[tuple] = []


def register_daemon_listener(
    name: str,
    factory: _Factory,
) -> None:
    """Module-level registration API for daemon listeners.

    Called at import time by listener modules
    (``kora_cli/listeners/*.py``). The ``factory`` callable returns
    ``(startup, shutdown)`` or ``(startup, shutdown, shutdown_timeout)``;
    the coordinator unpacks at daemon-start.

    Re-registering an existing name overwrites — useful for tests
    that monkey-patch listeners; production code should call once
    per name.
    """
    if not name or not name.isidentifier():
        raise ValueError(
            f"daemon listener name must be a non-empty identifier; got {name!r}"
        )
    # Drop any prior registration of the same name; preserve ordering
    # of the rest. Tests rely on this for clean teardown.
    global LISTENER_REGISTRY
    LISTENER_REGISTRY = [(n, f) for (n, f) in LISTENER_REGISTRY if n != name]
    LISTENER_REGISTRY.append((name, factory))


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class DaemonCoordinator:
    """Lifecycle coordinator for the Kora daemon.

    Tests instantiate this directly + call ``register_listener``;
    production code uses ``cmd_daemon`` which walks
    ``LISTENER_REGISTRY`` and registers them onto a fresh
    coordinator.
    """

    def __init__(self) -> None:
        self._listeners: List[_Listener] = []
        # Index of listeners that successfully started (so a startup
        # failure can call shutdown only on those that did start).
        self._started_idx: List[int] = []
        # Set when shutdown is requested (signal, programmatic, or
        # startup failure). The run-loop awaits this.
        self._shutdown_event: Optional[asyncio.Event] = None
        # Captured during run() so SIGTERM/SIGINT handlers can clear
        # themselves on graceful exit (avoid handler leaks across
        # multiple coordinator instances in the same process — tests
        # do this).
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Reason text passed by the first thing to request shutdown.
        # Logged + surfaced to listeners on shutdown.
        self._shutdown_reason: Optional[str] = None
        # Monotonic timestamp when all listeners finished startup.
        # ``None`` until startup completes; read by ``get_status()`` for
        # uptime computation. KR-D-DAEMON ST2 (kora__daemon_status).
        self._startup_completed_at: Optional[float] = None
        # KR-MCP-STOP-CONTROL ST2 — process-stable session id surfaced
        # via get_status() so MCP callers can echo it back as the
        # confirm_token on kora__request_stop. Re-generated on each
        # coordinator construction; constant for the daemon's lifetime.
        # Prevents a stale caller from replaying a stop request
        # against a different daemon instance (their cached
        # session_id won't match the new boot's value).
        self._daemon_session_id: str = uuid.uuid4().hex

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_listener(
        self,
        name: str,
        startup: StartupCallable,
        shutdown: ShutdownCallable,
        *,
        shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT,
    ) -> None:
        """Register a listener. FIFO startup, LIFO shutdown.

        Re-registering the same name raises — runtime collisions are
        a bug; the module-level ``register_daemon_listener`` allows
        overwrite for test convenience, but the coordinator instance
        is strict.
        """
        if any(l.name == name for l in self._listeners):
            raise ValueError(f"listener {name!r} already registered")
        self._listeners.append(
            _Listener(
                name=name,
                startup=startup,
                shutdown=shutdown,
                shutdown_timeout=shutdown_timeout,
            )
        )

    # ------------------------------------------------------------------
    # Programmatic shutdown
    # ------------------------------------------------------------------

    def request_shutdown(self, reason: str) -> None:
        """Trigger graceful shutdown from non-signal sources.

        Used by HTTP ``/admin/shutdown`` (a future endpoint),
        chain-event-driven self-stop, and tests. Safe to call
        multiple times — only the first reason is recorded.
        """
        if self._shutdown_reason is None:
            self._shutdown_reason = reason
            logger.info("[kora.daemon] shutdown requested: %s", reason)
        if self._shutdown_event is not None and not self._shutdown_event.is_set():
            self._shutdown_event.set()

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    async def run(self) -> int:
        """Start all listeners FIFO, block on shutdown, stop LIFO.

        Returns the process exit code: 0 on clean shutdown, non-zero
        on startup failure.
        """
        self._loop = asyncio.get_running_loop()
        self._shutdown_event = asyncio.Event()

        self._install_signal_handlers()

        # FIFO startup — bail at first failure.
        startup_failed = False
        for idx, listener in enumerate(self._listeners):
            try:
                logger.info("[kora.daemon] starting listener: %s", listener.name)
                await listener.startup()
                self._started_idx.append(idx)
                logger.info("[kora.daemon] listener %s started", listener.name)
            except Exception as exc:
                logger.error(
                    "[kora.daemon] listener %s startup FAILED: %r — "
                    "aborting daemon start, shutting down already-started "
                    "listeners",
                    listener.name,
                    exc,
                )
                startup_failed = True
                self.request_shutdown(f"startup failure in {listener.name}: {exc!r}")
                break

        # Block on shutdown signal — unless startup already failed,
        # in which case shutdown is already requested + we proceed
        # straight to teardown.
        if not startup_failed:
            self._startup_completed_at = time.monotonic()
            logger.info(
                "[kora.daemon] all %d listener(s) started; awaiting shutdown",
                len(self._listeners),
            )
            await self._shutdown_event.wait()

        # LIFO shutdown — only on listeners that actually started.
        # Each gets its own timeout; we log + continue past timeouts
        # rather than letting one stuck listener block the rest.
        for idx in reversed(self._started_idx):
            listener = self._listeners[idx]
            try:
                logger.info("[kora.daemon] shutting down listener: %s", listener.name)
                await asyncio.wait_for(
                    listener.shutdown(),
                    timeout=listener.shutdown_timeout,
                )
                logger.info("[kora.daemon] listener %s stopped", listener.name)
            except asyncio.TimeoutError:
                logger.error(
                    "[kora.daemon] listener %s shutdown TIMED OUT after %.1fs",
                    listener.name,
                    listener.shutdown_timeout,
                )
            except Exception as exc:
                logger.error(
                    "[kora.daemon] listener %s shutdown raised %r — continuing",
                    listener.name,
                    exc,
                )

        self._remove_signal_handlers()
        return 1 if startup_failed else 0

    # ------------------------------------------------------------------
    # Introspection (KR-D-DAEMON ST2 — read by kora__daemon_status MCP tool)
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Return a snapshot of coordinator state for the MCP status tool.

        Returns a JSON-serializable dict:
          - ``state``: ``"booting" | "running" | "shutting_down"``
          - ``uptime_seconds``: float (monotonic since startup completed),
            or ``None`` if not yet running
          - ``shutdown_reason``: str if shutdown requested, else None
          - ``listeners``: list of per-listener dicts with name +
            started bool + shutdown_timeout
        """
        if self._startup_completed_at is None:
            state = "booting"
            uptime = None
        elif (
            self._shutdown_event is not None and self._shutdown_event.is_set()
        ):
            state = "shutting_down"
            uptime = time.monotonic() - self._startup_completed_at
        else:
            state = "running"
            uptime = time.monotonic() - self._startup_completed_at

        started_set = set(self._started_idx)
        listeners = [
            {
                "name": l.name,
                "started": idx in started_set,
                "shutdown_timeout_seconds": l.shutdown_timeout,
            }
            for idx, l in enumerate(self._listeners)
        ]

        return {
            "state": state,
            "uptime_seconds": uptime,
            "shutdown_reason": self._shutdown_reason,
            "listeners": listeners,
            "daemon_session_id": self._daemon_session_id,
        }

    @property
    def daemon_session_id(self) -> str:
        """Per-process stable session id (hex uuid4).

        Echoed back by callers as the ``confirm_token`` on
        ``kora__request_stop`` to bind the stop request to a specific
        daemon instance. Changes only across process restarts.
        """
        return self._daemon_session_id

    # ------------------------------------------------------------------
    # Signal handling
    # ------------------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        """Install SIGTERM + SIGINT handlers via the asyncio loop.

        ``loop.add_signal_handler`` is the right primitive for asyncio
        — signal.signal() works from any thread but fights with
        asyncio's own SIGINT handling. add_signal_handler routes
        directly into the loop.

        On platforms where add_signal_handler raises NotImplementedError
        (Windows), we fall back to signal.signal — the daemon isn't
        targeted at Windows but the fallback keeps tests portable.
        """
        if self._loop is None:
            return
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self._loop.add_signal_handler(
                    sig,
                    lambda s=sig: self.request_shutdown(f"signal {s.name}"),
                )
            except NotImplementedError:
                # Best-effort fallback. Not exercised in Linux deploys.
                signal.signal(
                    sig,
                    lambda signum, _frame, s=sig: self.request_shutdown(
                        f"signal {s.name}"
                    ),
                )

    def _remove_signal_handlers(self) -> None:
        """Pair to _install_signal_handlers. Tests that spin up
        multiple coordinators in the same process need this to avoid
        the second coordinator's handler getting clobbered by the
        first's still-installed handler.
        """
        if self._loop is None:
            return
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self._loop.remove_signal_handler(sig)
            except (NotImplementedError, ValueError, RuntimeError):
                # ValueError: handler wasn't installed (already removed).
                # RuntimeError: loop closed during shutdown.
                pass


# ---------------------------------------------------------------------------
# Current-coordinator accessor (KR-D-DAEMON ST2)
# ---------------------------------------------------------------------------

# Set by ``cmd_daemon`` while the daemon is live; ``None`` otherwise.
# Listeners (notably the MCP status tool) read this to introspect the
# coordinator without taking a reference at construction time.
_CURRENT_COORDINATOR: Optional["DaemonCoordinator"] = None


def current_coordinator() -> Optional["DaemonCoordinator"]:
    """Return the running daemon's coordinator, or ``None``.

    Listeners use this to read coordinator state at request time.
    Outside of ``cmd_daemon``'s active run, returns ``None`` (so a
    test that instantiates a coordinator directly does not pollute
    the module state).
    """
    return _CURRENT_COORDINATOR


# ---------------------------------------------------------------------------
# Deploy-env gate
# ---------------------------------------------------------------------------


def resolve_deploy_env() -> str:
    """Return the effective ``KORA_DEPLOY_ENV`` value, or raise.

    - If ``KORA_DEPLOY_ENV`` is set + non-empty, return it.
    - Elif ``KORA_DEV=1`` is set, return ``"dev"``.
    - Else: raise ``SystemExit`` with a clear error.

    Per PM Q5 ruling: required in prod; ``KORA_DEV=1`` is the
    explicit local-dev opt-in.
    """
    env = os.environ.get("KORA_DEPLOY_ENV", "").strip()
    if env:
        return env
    if os.environ.get("KORA_DEV", "").strip() == "1":
        return "dev"
    raise SystemExit(
        "kora daemon refuses to start: KORA_DEPLOY_ENV is unset. "
        "Set KORA_DEPLOY_ENV to your deploy environment (e.g. 'prd' "
        "or 'staging'), OR set KORA_DEV=1 for local dev."
    )


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def add_daemon_parser(subparsers: argparse._SubParsersAction) -> None:
    """Wire the ``daemon`` subcommand into the top-level argparser.

    Called from ``kora_cli/main.py:main`` alongside the other
    subparser.add_parser sites. Keeps daemon-specific argparse
    config out of the main CLI file.
    """
    daemon_parser = subparsers.add_parser(
        "daemon",
        help="Run Kora as an always-alive daemon (HTTP + MCP + webhooks)",
        description=(
            "Long-running daemon that hosts Kora's concurrent async "
            "listeners — admin web UI, MCP server (internal), webhook "
            "routers (public on a dedicated port), and the heartbeat "
            "scheduler. ST1 ships the coordinator harness; ST2 and "
            "ST3 land the actual listeners."
        ),
    )
    daemon_parser.add_argument(
        "--no-web",
        action="store_true",
        help=(
            "Skip the web listener. Useful for testing the daemon "
            "harness in isolation."
        ),
    )
    daemon_parser.add_argument(
        "--listener",
        action="append",
        metavar="NAME",
        help=(
            "Filter to one or more registered listeners by name "
            "(repeat the flag for multiple). When omitted, all "
            "registered listeners run. Useful for debugging a single "
            "listener's lifecycle."
        ),
    )
    daemon_parser.set_defaults(func=cmd_daemon)


def cmd_daemon(args: argparse.Namespace) -> int:
    """Entry point for ``kora daemon`` — boot the coordinator."""
    # Boot-time gate first — fail fast before any imports of heavy
    # listener modules.
    deploy_env = resolve_deploy_env()
    logger.info("[kora.daemon] starting in deploy_env=%s", deploy_env)

    # Import the listeners package — its sub-modules register their
    # factories at import time via ``register_daemon_listener``. KR-D-DAEMON
    # ST2 wires web + MCP + heartbeat. Lazy import to keep ``kora daemon
    # --help`` fast + to avoid unconditional uvicorn / FastAPI startup in
    # contexts that only call ``resolve_deploy_env``.
    import kora_cli.listeners  # noqa: F401 — import side-effect registers listeners

    coordinator = DaemonCoordinator()
    _populate_listeners_from_registry(coordinator, args)

    global _CURRENT_COORDINATOR
    _CURRENT_COORDINATOR = coordinator
    try:
        return asyncio.run(coordinator.run())
    except KeyboardInterrupt:
        # asyncio.run() converts SIGINT-during-startup to
        # KeyboardInterrupt; treat as graceful shutdown request.
        logger.info("[kora.daemon] KeyboardInterrupt — exiting")
        return 130
    finally:
        _CURRENT_COORDINATOR = None


def _populate_listeners_from_registry(
    coordinator: DaemonCoordinator,
    args: argparse.Namespace,
) -> None:
    """Walk ``LISTENER_REGISTRY`` and register applicable listeners
    onto the given coordinator instance.

    Respects ``--no-web`` (drops the "web" listener) and ``--listener``
    (whitelist filter — exact name match, repeatable).
    """
    listener_whitelist = getattr(args, "listener", None) or None
    skip_web = bool(getattr(args, "no_web", False))

    for name, factory in LISTENER_REGISTRY:
        if skip_web and name == "web":
            logger.info("[kora.daemon] skipping listener 'web' (--no-web)")
            continue
        if listener_whitelist is not None and name not in listener_whitelist:
            logger.info(
                "[kora.daemon] skipping listener %s (--listener filter)", name
            )
            continue

        try:
            spec = factory()
        except Exception as exc:
            logger.error(
                "[kora.daemon] listener factory %s raised %r — "
                "skipping; daemon will refuse to register",
                name,
                exc,
            )
            # Fail-CLOSED: a broken factory is a bug; better to refuse
            # to start than to silently drop a listener.
            print(
                f"kora daemon: factory for listener {name!r} raised "
                f"during construction: {exc!r}",
                file=sys.stderr,
            )
            raise SystemExit(2)

        # Factories may return either (startup, shutdown) or
        # (startup, shutdown, shutdown_timeout). Be permissive.
        if isinstance(spec, tuple) and len(spec) == 2:
            startup, shutdown = spec
            coordinator.register_listener(name, startup, shutdown)
        elif isinstance(spec, tuple) and len(spec) == 3:
            startup, shutdown, timeout = spec
            coordinator.register_listener(
                name, startup, shutdown, shutdown_timeout=timeout
            )
        else:
            raise SystemExit(
                f"kora daemon: factory for listener {name!r} returned "
                f"an unexpected shape: {spec!r}"
            )
