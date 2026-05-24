"""Tests for KR-PIP-PACKAGING-FOUNDATION-AND-DAEMON-FATAL-FLAG (#204):
``fatal_on_startup_failure`` structural enforcement of the FATAL
contract CC#3 #200 documented in ``reasoning_engine_listener``.

Coverage:

  1. ``BackgroundDaemonEntry.fatal_on_startup_failure`` field exists
     + defaults to ``False``
  2. ``PluginContext.register_background_daemon`` accepts the kwarg
     and stores it on the entry
  3. ``DaemonCoordinator._is_startup_failure_fatal`` returns the
     entry's flag for listeners WITH a Hermes registry entry
  4. ``DaemonCoordinator._is_startup_failure_fatal`` defaults to
     ``True`` for listeners WITHOUT a Hermes entry (backward compat
     for Kora-only HTTP service mounts)
  5. Behavioral: synthetic listener with ``fatal_on_startup_failure
     =True`` + raising startup → coordinator aborts boot
  6. Behavioral: synthetic listener with ``fatal_on_startup_failure
     =False`` + raising startup → coordinator logs + continues
  7. Production pin: ``reasoning_engine`` has the flag set to
     ``True`` in its Hermes-registry entry
  8. Production pin: non-critical Phase-1/2/3 listeners default to
     ``False`` (snapshot / slack_client / etc. preserve documented
     fail-soft behavior under the new structural contract)
"""

from __future__ import annotations

import asyncio

import pytest

from agent.background_daemon_registry import (
    BackgroundDaemonEntry,
    BackgroundDaemonRegistry,
    background_daemon_registry,
)
from kora_cli.daemon import DaemonCoordinator


# Trigger listener registrations so production-pin tests can read
# the live registry.
import kora_cli.listeners  # noqa: F401


# ---------------------------------------------------------------------------
# Dataclass-level pins
# ---------------------------------------------------------------------------


def test_background_daemon_entry_has_fatal_flag_default_false():
    """The flag exists + defaults to False so a future listener
    added without thinking about it doesn't accidentally make the
    whole runtime crash on a spurious startup error."""
    entry = BackgroundDaemonEntry(
        name="t",
        startup=lambda coordinator: None,
        shutdown=lambda: None,
    )
    assert entry.fatal_on_startup_failure is False


def test_background_daemon_entry_fatal_flag_settable():
    entry = BackgroundDaemonEntry(
        name="t",
        startup=lambda coordinator: None,
        shutdown=lambda: None,
        fatal_on_startup_failure=True,
    )
    assert entry.fatal_on_startup_failure is True


def test_background_daemon_entry_remains_frozen():
    """The dataclass is frozen — adding a field doesn't make it
    mutable. Pin this so future refactors don't accidentally
    introduce per-instance mutation paths."""
    entry = BackgroundDaemonEntry(
        name="t",
        startup=lambda coordinator: None,
        shutdown=lambda: None,
    )
    with pytest.raises(Exception):  # FrozenInstanceError
        entry.fatal_on_startup_failure = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# PluginContext.register_background_daemon kwarg plumbing
# ---------------------------------------------------------------------------


def test_register_background_daemon_passes_fatal_flag_through():
    """The PluginContext convenience method must thread the new
    kwarg into BackgroundDaemonEntry. Without this, plugin authors
    can't opt-in to fatal.

    Uses unique names that don't collide with production registrations
    so the global singleton isn't reset (resetting it would wipe the
    15 production listener entries other tests in this module depend
    on)."""
    from kora_cli.plugins import PluginContext, PluginManager, PluginManifest

    mgr = PluginManager()
    ctx = PluginContext(
        manifest=PluginManifest(
            name="test_fatal_plugin", version="0.0.1", description="test"
        ),
        manager=mgr,
    )
    ctx.register_background_daemon(
        name="test_fatal_flag_passthrough_fatal",
        startup=lambda coordinator: None,
        shutdown=lambda: None,
        fatal_on_startup_failure=True,
    )
    entry = background_daemon_registry().by_name(
        "test_fatal_flag_passthrough_fatal"
    )
    assert entry is not None
    assert entry.fatal_on_startup_failure is True
    # And the default-False path:
    ctx.register_background_daemon(
        name="test_fatal_flag_passthrough_nonfatal",
        startup=lambda coordinator: None,
        shutdown=lambda: None,
    )
    nonfatal = background_daemon_registry().by_name(
        "test_fatal_flag_passthrough_nonfatal"
    )
    assert nonfatal is not None
    assert nonfatal.fatal_on_startup_failure is False


# ---------------------------------------------------------------------------
# DaemonCoordinator._is_startup_failure_fatal lookup
# ---------------------------------------------------------------------------


def test_lookup_returns_true_for_listener_with_hermes_entry_fatal():
    """Listener with Hermes entry + fatal flag True → returns True."""
    coordinator = DaemonCoordinator()
    # reasoning_engine has fatal_on_startup_failure=True per #204.
    assert coordinator._is_startup_failure_fatal("reasoning_engine") is True


def test_lookup_returns_false_for_listener_with_hermes_entry_nonfatal():
    """Listener with Hermes entry + fatal flag False (default) →
    returns False."""
    coordinator = DaemonCoordinator()
    # snapshot defaults to False (fail-soft listener — catches its
    # own exceptions internally).
    assert coordinator._is_startup_failure_fatal("snapshot") is False


def test_lookup_returns_true_for_listener_without_hermes_entry():
    """Kora-only listener (no Hermes registration) → defaults to
    True (backward compat for HTTP service mounts: web/mcp/webhooks
    which today abort on any startup raise)."""
    coordinator = DaemonCoordinator()
    # 'web' is a Kora-only listener (no Hermes entry per #200 audit
    # §4.2 NO-OP confirmation).
    assert coordinator._is_startup_failure_fatal("web") is True


def test_lookup_returns_true_for_unknown_listener():
    """Defensive: unknown name → fatal default. Preserves "be
    conservative on unknown infrastructure" posture."""
    coordinator = DaemonCoordinator()
    assert coordinator._is_startup_failure_fatal(
        "definitely_not_a_real_listener_name_xyz123"
    ) is True


# ---------------------------------------------------------------------------
# Behavioral — DaemonCoordinator startup loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coordinator_aborts_on_fatal_listener_startup_raise():
    """End-to-end: register a synthetic listener whose Hermes entry
    has fatal_on_startup_failure=True. Make its startup raise.
    Coordinator must abort boot (run returns non-zero exit code +
    request_shutdown was called).

    Uses unique names that don't collide with production registrations
    so the global singleton stays intact for other tests."""
    coordinator = DaemonCoordinator()

    async def boom_startup():
        raise RuntimeError("synthetic fatal failure")

    async def noop_shutdown():
        pass

    # Register with the SAME name in BOTH registries (Path B shape).
    # Unique name doesn't collide with any of the 15 production
    # listener entries — global singleton stays intact.
    coordinator.register_listener(
        name="test_fatal_coord_synth",
        startup=boom_startup,
        shutdown=noop_shutdown,
    )
    background_daemon_registry().register(
        BackgroundDaemonEntry(
            name="test_fatal_coord_synth",
            startup=boom_startup,
            shutdown=noop_shutdown,
            fatal_on_startup_failure=True,
        )
    )

    exit_code = await coordinator.run()
    # Non-zero exit code indicates startup failure was treated as fatal.
    assert exit_code != 0, (
        f"coordinator should exit non-zero on fatal startup failure; "
        f"got {exit_code}"
    )


@pytest.mark.asyncio
async def test_coordinator_continues_on_nonfatal_listener_startup_raise():
    """End-to-end: register a synthetic listener whose Hermes entry
    has fatal_on_startup_failure=False. Make its startup raise.
    Coordinator must LOG + CONTINUE (run a second listener
    successfully + exit cleanly when shutdown is requested).

    Uses unique names that don't collide with production registrations."""
    coordinator = DaemonCoordinator()

    second_started = asyncio.Event()

    async def boom_startup():
        raise RuntimeError("synthetic non-fatal failure")

    async def noop_shutdown():
        pass

    async def second_startup():
        second_started.set()

    coordinator.register_listener(
        name="test_nonfatal_coord_synth_1",
        startup=boom_startup,
        shutdown=noop_shutdown,
    )
    background_daemon_registry().register(
        BackgroundDaemonEntry(
            name="test_nonfatal_coord_synth_1",
            startup=boom_startup,
            shutdown=noop_shutdown,
            fatal_on_startup_failure=False,
        )
    )
    coordinator.register_listener(
        name="test_nonfatal_coord_synth_2",
        startup=second_startup,
        shutdown=noop_shutdown,
    )
    background_daemon_registry().register(
        BackgroundDaemonEntry(
            name="test_nonfatal_coord_synth_2",
            startup=second_startup,
            shutdown=noop_shutdown,
            fatal_on_startup_failure=False,
        )
    )

    # Trigger shutdown shortly after startup to let run() return.
    async def request_shutdown_soon():
        await asyncio.sleep(0.05)
        coordinator.request_shutdown("test-controlled exit")

    # Run with the synchronizer.
    shutdown_task = asyncio.create_task(request_shutdown_soon())
    exit_code = await coordinator.run()
    await shutdown_task

    # Coordinator exited 0 (clean shutdown after non-fatal startup
    # failure was logged + continued past).
    assert exit_code == 0, (
        f"coordinator should exit 0 after non-fatal failure + "
        f"clean shutdown; got {exit_code}"
    )
    # Second listener actually started — proving coordinator continued
    # past the first listener's raise.
    assert second_started.is_set(), (
        "second listener should have started after the first raised "
        "non-fatally"
    )


# ---------------------------------------------------------------------------
# Production pins
# ---------------------------------------------------------------------------


def test_reasoning_engine_listener_is_fatal_in_production():
    """The canonical critical daemon. If this regresses to False,
    Kora will silently degrade when the engine fails to construct
    (reverting #200's hard-won fail-CLOSED semantic). Pin loudly."""
    entry = background_daemon_registry().by_name("reasoning_engine")
    assert entry is not None
    assert entry.fatal_on_startup_failure is True, (
        "reasoning_engine MUST have fatal_on_startup_failure=True. "
        "Engine construction failure means Kora cannot reason; better "
        "to abort boot loud than ship a degraded daemon."
    )


@pytest.mark.parametrize(
    "listener_name",
    [
        "snapshot",
        "heartbeat_probes",
        "slack_client",
        "purelymail_client",
        "alert_notifier",
        "cost_telemetry",
        "mcp_consumption",
    ],
)
def test_phase_1_2_3_listeners_default_to_non_fatal(listener_name):
    """Non-critical listeners (fail-soft on missing auth env, etc.)
    must keep the default False. Their startup catches its own
    exceptions internally; the FATAL flag is for unexpected
    exceptions that escape that handling — for non-critical
    listeners, those should log + continue rather than crash the
    runtime."""
    entry = background_daemon_registry().by_name(listener_name)
    assert entry is not None, f"{listener_name} missing from Hermes registry"
    assert entry.fatal_on_startup_failure is False, (
        f"{listener_name} should default to False (non-fatal). "
        f"Setting True silently could mean a missing Slack token "
        f"or similar capability-config takes down the daemon."
    )
