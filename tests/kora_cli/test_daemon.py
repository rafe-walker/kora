"""Tests for ``kora_cli.daemon`` — KR-D-DAEMON ST1.

Covers the four scenarios the bucket spec called out:
  1. Coordinator starts/stops an empty listener set cleanly.
  2. Listener startup failure → coordinator aborts startup, calls
     already-started listeners' shutdown() in LIFO, exits non-zero.
  3. SIGTERM during run → graceful shutdown completes within timeout.
  4. LIFO shutdown order verified.

Plus auxiliary coverage:
  - register_listener name-collision rejection.
  - request_shutdown idempotency.
  - Per-listener shutdown timeout.
  - Shutdown timeout doesn't block subsequent listener shutdowns.
  - resolve_deploy_env (KORA_DEPLOY_ENV / KORA_DEV / fail).
  - LISTENER_REGISTRY mutation API.
"""

from __future__ import annotations

import asyncio
import os
import signal
from typing import List

import pytest

from kora_cli import daemon as daemon_mod
from kora_cli.daemon import (
    DEFAULT_SHUTDOWN_TIMEOUT,
    DaemonCoordinator,
    register_daemon_listener,
    resolve_deploy_env,
)


# ---------------------------------------------------------------------------
# Helpers — listener fakes
# ---------------------------------------------------------------------------


def _make_listener_pair(name: str, order_sink: List[str]):
    """Return (startup, shutdown) coroutines that append ``"name:startup"``
    / ``"name:shutdown"`` to ``order_sink``. Used to assert FIFO/LIFO."""

    async def startup() -> None:
        order_sink.append(f"{name}:startup")

    async def shutdown() -> None:
        order_sink.append(f"{name}:shutdown")

    return startup, shutdown


def _failing_startup(name: str, order_sink: List[str]):
    """A startup that records its call + raises."""

    async def startup() -> None:
        order_sink.append(f"{name}:startup-raise")
        raise RuntimeError(f"{name} boom")

    async def shutdown() -> None:
        # Should never be called — startup failed, so the listener
        # never reached the started set.
        order_sink.append(f"{name}:shutdown-UNEXPECTED")

    return startup, shutdown


# ---------------------------------------------------------------------------
# 1. Empty-listener clean lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coordinator_starts_and_stops_empty_listener_set_cleanly():
    coord = DaemonCoordinator()

    async def trigger_shutdown():
        # Give run() a tick to install handlers + reach wait().
        await asyncio.sleep(0)
        coord.request_shutdown("test-empty")

    _, exit_code = await asyncio.gather(trigger_shutdown(), coord.run())
    assert exit_code == 0


# ---------------------------------------------------------------------------
# 2. Listener startup failure → abort + LIFO unwind of started ones
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listener_startup_failure_aborts_and_unwinds_in_lifo():
    order: List[str] = []
    coord = DaemonCoordinator()

    a_start, a_stop = _make_listener_pair("a", order)
    b_start, b_stop = _make_listener_pair("b", order)
    fail_start, fail_stop = _failing_startup("c", order)
    # d should NEVER start — coordinator aborts after c fails.
    d_start, d_stop = _make_listener_pair("d", order)

    coord.register_listener("a", a_start, a_stop)
    coord.register_listener("b", b_start, b_stop)
    coord.register_listener("c", fail_start, fail_stop)
    coord.register_listener("d", d_start, d_stop)

    exit_code = await coord.run()

    assert exit_code == 1, "startup failure must produce non-zero exit"
    # FIFO startup until failure; LIFO shutdown of started ones only.
    # 'd' must NEVER appear; 'c:shutdown-UNEXPECTED' must NEVER appear.
    assert order == [
        "a:startup",
        "b:startup",
        "c:startup-raise",
        "b:shutdown",
        "a:shutdown",
    ]


# ---------------------------------------------------------------------------
# 3. SIGTERM during run → graceful shutdown within timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sigterm_triggers_graceful_shutdown():
    """Send SIGTERM to the current process after coordinator is running;
    coordinator must observe the signal and shut down cleanly.

    Skipped on platforms where ``loop.add_signal_handler`` is
    unsupported (Windows) — the fallback path is a best-effort
    ``signal.signal`` which would fight with pytest's own.
    """
    if not hasattr(signal, "SIGTERM"):
        pytest.skip("SIGTERM not available on this platform")

    order: List[str] = []
    coord = DaemonCoordinator()
    a_start, a_stop = _make_listener_pair("a", order)
    coord.register_listener("a", a_start, a_stop)

    async def send_sigterm():
        # One tick to let run() install handlers + enter the await.
        await asyncio.sleep(0.01)
        os.kill(os.getpid(), signal.SIGTERM)

    # Outer timeout enforces the bucket-spec's "graceful shutdown
    # completes within 30s" — generous for CI; should be ~milliseconds
    # in practice.
    async def driver():
        await asyncio.wait_for(
            asyncio.gather(send_sigterm(), coord.run()),
            timeout=30.0,
        )

    await driver()

    # Listener started + cleanly shut down.
    assert order == ["a:startup", "a:shutdown"]


# ---------------------------------------------------------------------------
# 4. LIFO shutdown order verified (explicit, multi-listener)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifo_shutdown_order_explicit():
    order: List[str] = []
    coord = DaemonCoordinator()
    for name in ("first", "second", "third", "fourth"):
        startup, shutdown = _make_listener_pair(name, order)
        coord.register_listener(name, startup, shutdown)

    async def trigger():
        await asyncio.sleep(0)
        coord.request_shutdown("test-lifo")

    _, exit_code = await asyncio.gather(trigger(), coord.run())

    assert exit_code == 0
    assert order == [
        "first:startup",
        "second:startup",
        "third:startup",
        "fourth:startup",
        "fourth:shutdown",
        "third:shutdown",
        "second:shutdown",
        "first:shutdown",
    ]


# ---------------------------------------------------------------------------
# Auxiliary — name collisions, idempotency, timeouts
# ---------------------------------------------------------------------------


def test_register_listener_name_collision_rejected():
    coord = DaemonCoordinator()

    async def noop():
        pass

    coord.register_listener("dup", noop, noop)
    with pytest.raises(ValueError, match="dup"):
        coord.register_listener("dup", noop, noop)


@pytest.mark.asyncio
async def test_request_shutdown_is_idempotent():
    coord = DaemonCoordinator()

    async def trigger():
        await asyncio.sleep(0)
        coord.request_shutdown("first")
        coord.request_shutdown("second")  # ignored — first wins
        coord.request_shutdown("third")  # ignored too

    _, exit_code = await asyncio.gather(trigger(), coord.run())
    assert exit_code == 0
    # The reason recorded is the first one (private attr but the
    # invariant is observable via the contract).
    assert coord._shutdown_reason == "first"


@pytest.mark.asyncio
async def test_shutdown_timeout_one_listener_does_not_block_others():
    """A listener whose shutdown() hangs hits its per-listener timeout
    and the coordinator continues to the NEXT listener's shutdown."""
    order: List[str] = []
    coord = DaemonCoordinator()

    async def fast_start():
        order.append("fast:startup")

    async def fast_shutdown():
        order.append("fast:shutdown")

    async def hung_start():
        order.append("hung:startup")

    async def hung_shutdown():
        order.append("hung:shutdown-begin")
        # Wait forever — should be killed by the timeout.
        await asyncio.Event().wait()
        order.append("hung:shutdown-NEVER")  # not reached

    coord.register_listener("fast", fast_start, fast_shutdown)
    coord.register_listener(
        "hung", hung_start, hung_shutdown, shutdown_timeout=0.2
    )

    async def trigger():
        await asyncio.sleep(0)
        coord.request_shutdown("test-timeout")

    _, exit_code = await asyncio.gather(trigger(), coord.run())

    assert exit_code == 0
    # LIFO: hung shuts down first (begins, hits timeout), then fast.
    assert order == [
        "fast:startup",
        "hung:startup",
        "hung:shutdown-begin",
        "fast:shutdown",
    ]


# ---------------------------------------------------------------------------
# resolve_deploy_env
# ---------------------------------------------------------------------------


def test_resolve_deploy_env_explicit_value(monkeypatch):
    monkeypatch.setenv("KORA_DEPLOY_ENV", "prd")
    monkeypatch.delenv("KORA_DEV", raising=False)
    assert resolve_deploy_env() == "prd"


def test_resolve_deploy_env_dev_fallback(monkeypatch):
    monkeypatch.delenv("KORA_DEPLOY_ENV", raising=False)
    monkeypatch.setenv("KORA_DEV", "1")
    assert resolve_deploy_env() == "dev"


def test_resolve_deploy_env_empty_is_treated_as_unset(monkeypatch):
    """Empty string is whitespace-stripped → treated as unset.
    Without the dev fallback, raises."""
    monkeypatch.setenv("KORA_DEPLOY_ENV", "   ")
    monkeypatch.delenv("KORA_DEV", raising=False)
    with pytest.raises(SystemExit):
        resolve_deploy_env()


def test_resolve_deploy_env_unset_no_dev_raises(monkeypatch):
    monkeypatch.delenv("KORA_DEPLOY_ENV", raising=False)
    monkeypatch.delenv("KORA_DEV", raising=False)
    with pytest.raises(SystemExit):
        resolve_deploy_env()


def test_resolve_deploy_env_kora_dev_only_when_unset(monkeypatch):
    """KORA_DEV=1 does NOT override an explicit KORA_DEPLOY_ENV."""
    monkeypatch.setenv("KORA_DEPLOY_ENV", "staging")
    monkeypatch.setenv("KORA_DEV", "1")
    assert resolve_deploy_env() == "staging"


# ---------------------------------------------------------------------------
# Module-level LISTENER_REGISTRY
# ---------------------------------------------------------------------------


def test_register_daemon_listener_idempotent_overwrite():
    """Re-registering the same name overwrites — test-fixture pattern."""
    original = list(daemon_mod.LISTENER_REGISTRY)
    try:
        def factory_v1():
            return (None, None)

        def factory_v2():
            return (None, None)

        register_daemon_listener("test_listener", factory_v1)
        register_daemon_listener("test_listener", factory_v2)
        entries = [
            f for (n, f) in daemon_mod.LISTENER_REGISTRY
            if n == "test_listener"
        ]
        assert entries == [factory_v2]
    finally:
        daemon_mod.LISTENER_REGISTRY[:] = original


def test_register_daemon_listener_rejects_bad_names():
    with pytest.raises(ValueError):
        register_daemon_listener("", lambda: (None, None))
    with pytest.raises(ValueError):
        register_daemon_listener("has spaces", lambda: (None, None))
    with pytest.raises(ValueError):
        register_daemon_listener("with-dash", lambda: (None, None))


def test_default_shutdown_timeout_constant():
    """The 10s default is a contract — listener authors rely on it."""
    assert DEFAULT_SHUTDOWN_TIMEOUT == 10.0
