"""Batched dual-registry tests for KR-DAEMON-LISTENERS-VIA-GATEWAY Phase 2.

Phase 1 (#196) migrated the snapshot listener as proof-of-pattern;
Phase 2 (this PR) migrates the remaining 8 periodic-task listeners
to dual-registry shape (BOTH Hermes BackgroundDaemonRegistry AND
Kora LISTENER_REGISTRY, both pointing at the same singleton factory).

Two of the eight (``promote_phrasebook`` + ``promote_snapshot_expand``)
are pure-periodic-task listeners that never had Kora-side daemon
registration — for those, only the Hermes-side surface is verified.

Coverage per listener:
  1. Listener is in BOTH registries (or only Hermes for the two
     pure-periodic ones)
  2. The Hermes BackgroundDaemonEntry carries the expected
     periodic_task name + callback identity + interval
  3. Both registrations share the same listener singleton (for
     the 6 with Listener-class shape) → behavior parity guarantee
"""

from __future__ import annotations

import pytest

from agent.background_daemon_registry import (
    BackgroundDaemonEntry,
    background_daemon_registry,
)
from kora_cli import daemon as daemon_mod


# Trigger import-time registrations.
import kora_cli.listeners  # noqa: F401


# ---------------------------------------------------------------------------
# Inventory pin — Phase 2 brings the periodic-task count to 9
# ---------------------------------------------------------------------------


PHASE2_LISTENERS = {
    "snapshot",                  # Phase 1 (#196) — included in inventory pin
    "heartbeat_probes",
    "email_inbound_imap",
    "probe_wake",
    "mcp_consumption",
    "cost_telemetry",
    "alert_notifier",
    "promote_phrasebook",        # pure-periodic; no Kora-side entry
    "promote_snapshot_expand",   # pure-periodic; no Kora-side entry
}


PURE_PERIODIC_NO_KORA_ENTRY = {
    "promote_phrasebook",
    "promote_snapshot_expand",
}


@pytest.mark.parametrize("name", sorted(PHASE2_LISTENERS))
def test_listener_in_hermes_registry(name):
    """Every Phase 2 listener has a BackgroundDaemonEntry in the
    Hermes registry."""
    entry = background_daemon_registry().by_name(name)
    assert entry is not None, (
        f"{name} missing from BackgroundDaemonRegistry — Phase 2 "
        f"migration must add it"
    )
    assert isinstance(entry, BackgroundDaemonEntry)
    assert entry.plugin_name == "kora"


@pytest.mark.parametrize(
    "name",
    sorted(PHASE2_LISTENERS - PURE_PERIODIC_NO_KORA_ENTRY),
)
def test_listener_in_kora_registry(name):
    """Every Listener-class-shape Phase 2 listener stays in Kora's
    LISTENER_REGISTRY (Path B thin-shim — back-compat preserved
    until Phase 6 dissolution)."""
    kora_names = {n for n, _f in daemon_mod.LISTENER_REGISTRY}
    assert name in kora_names, (
        f"{name} missing from Kora LISTENER_REGISTRY — Phase 2 "
        f"migration must preserve the back-compat entry"
    )


@pytest.mark.parametrize("name", sorted(PURE_PERIODIC_NO_KORA_ENTRY))
def test_pure_periodic_listener_not_in_kora_registry(name):
    """The 2 promote_* listeners never had Kora-side daemon
    registration (no Listener class, no register_daemon_listener
    call). Phase 2 only adds them to the Hermes registry. The
    no-op startup/shutdown wrappers exist solely for the Hermes
    side."""
    kora_names = {n for n, _f in daemon_mod.LISTENER_REGISTRY}
    assert name not in kora_names, (
        f"{name} should NOT be in Kora LISTENER_REGISTRY — it's a "
        f"pure-periodic-task listener with no daemon-lifecycle "
        f"history. If it appears here, the migration shape drifted."
    )


# ---------------------------------------------------------------------------
# Periodic-task fields — Hermes entry carries the expected callback
# ---------------------------------------------------------------------------


def test_heartbeat_probes_periodic_task():
    from kora_cli.heartbeat_probes.runner import run_all_probes_scheduled

    entry = background_daemon_registry().by_name("heartbeat_probes")
    assert entry is not None and entry.periodic_task is not None
    assert entry.periodic_task.name == "heartbeat.service_probes"
    assert entry.periodic_task.callback is run_all_probes_scheduled


def test_email_inbound_imap_periodic_task():
    from kora_cli.listeners.email_inbound_imap_listener import run_poll_cycle

    entry = background_daemon_registry().by_name("email_inbound_imap")
    assert entry is not None and entry.periodic_task is not None
    assert entry.periodic_task.name == "email.imap_poll"
    assert entry.periodic_task.callback is run_poll_cycle


def test_probe_wake_periodic_task():
    from kora_cli.listeners.probe_wake_listener import run_tail_cycle

    entry = background_daemon_registry().by_name("probe_wake")
    assert entry is not None and entry.periodic_task is not None
    assert entry.periodic_task.name == "probe_wake.tail"
    assert entry.periodic_task.callback is run_tail_cycle


def test_mcp_consumption_periodic_task():
    from kora_cli.listeners.mcp_consumption import run_health_check

    entry = background_daemon_registry().by_name("mcp_consumption")
    assert entry is not None and entry.periodic_task is not None
    assert entry.periodic_task.name == "mcp.health_check"
    assert entry.periodic_task.callback is run_health_check


def test_cost_telemetry_periodic_task():
    """cost_telemetry has 3 periodic tasks; the Hermes entry carries
    the PRIMARY persist cycle (per §4.1 audit recommendation c). The
    two reset checks stay on Kora's heartbeat scheduler."""
    from kora_cli.listeners.cost_telemetry_listener import run_persist_cycle

    entry = background_daemon_registry().by_name("cost_telemetry")
    assert entry is not None and entry.periodic_task is not None
    assert entry.periodic_task.name == "cost_telemetry.persist"
    assert entry.periodic_task.callback is run_persist_cycle


def test_alert_notifier_periodic_task():
    """alert_notifier has 2 periodic tasks; the Hermes entry carries
    the PRIMARY notify cycle. The digest_flush task stays on Kora's
    heartbeat scheduler."""
    from kora_cli.listeners.alert_notifier_listener import (
        run_notification_cycle,
    )

    entry = background_daemon_registry().by_name("alert_notifier")
    assert entry is not None and entry.periodic_task is not None
    assert entry.periodic_task.name == "alerts.notify"
    assert entry.periodic_task.callback is run_notification_cycle


def test_promote_phrasebook_periodic_task():
    """Pure-periodic-task listener: no Listener class, no Kora-side
    daemon registration. Phase 2 only adds the Hermes-side entry
    (with no-op startup/shutdown wrappers + the existing periodic
    task)."""
    from kora_cli.listeners.promote_phrasebook_listener import _periodic_task

    entry = background_daemon_registry().by_name("promote_phrasebook")
    assert entry is not None and entry.periodic_task is not None
    assert entry.periodic_task.name == "promote_phrasebook_cycle"
    assert entry.periodic_task.callback is _periodic_task


def test_promote_snapshot_expand_periodic_task():
    from kora_cli.listeners.promote_snapshot_expand_listener import (
        _periodic_task,
    )

    entry = background_daemon_registry().by_name("promote_snapshot_expand")
    assert entry is not None and entry.periodic_task is not None
    assert entry.periodic_task.name == "promote_snapshot_expand_cycle"
    assert entry.periodic_task.callback is _periodic_task


# ---------------------------------------------------------------------------
# Singleton invariant — both registries point at the same listener
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, module_path",
    [
        ("heartbeat_probes",
         "kora_cli.listeners.heartbeat_probes_listener"),
        ("email_inbound_imap",
         "kora_cli.listeners.email_inbound_imap_listener"),
        ("probe_wake",
         "kora_cli.listeners.probe_wake_listener"),
        ("mcp_consumption",
         "kora_cli.listeners.mcp_consumption"),
        ("cost_telemetry",
         "kora_cli.listeners.cost_telemetry_listener"),
        ("alert_notifier",
         "kora_cli.listeners.alert_notifier_listener"),
    ],
)
def test_listener_singleton_shared_across_registries(name, module_path):
    """For Listener-class-shape listeners: both registries point at
    the SAME singleton's bound methods. Prevents double-log-on-
    startup if both consumers fire."""
    import importlib

    mod = importlib.import_module(module_path)
    singleton = mod._listener_singleton
    hermes_entry = background_daemon_registry().by_name(name)
    assert hermes_entry is not None
    assert hermes_entry.startup == singleton.startup
    assert hermes_entry.shutdown == singleton.shutdown
    # Kora-side factory tuple shares the same bound methods.
    kora_lookup = dict(daemon_mod.LISTENER_REGISTRY)
    kora_factory = kora_lookup[name]
    kora_startup, kora_shutdown, _ = kora_factory()
    assert kora_startup == singleton.startup
    assert kora_shutdown == singleton.shutdown


# ---------------------------------------------------------------------------
# Startup signature — accepts optional coordinator kwarg
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_path",
    [
        "kora_cli.listeners.heartbeat_probes_listener",
        "kora_cli.listeners.email_inbound_imap_listener",
        "kora_cli.listeners.probe_wake_listener",
        "kora_cli.listeners.mcp_consumption",
        "kora_cli.listeners.cost_telemetry_listener",
        "kora_cli.listeners.alert_notifier_listener",
    ],
)
def test_startup_signature_accepts_coordinator_kwarg(module_path):
    """Every Listener-class startup must accept the optional
    coordinator kwarg so both consumer shapes work (Kora's no-arg
    + Hermes's positional Callable[[Any], Any])."""
    import importlib
    import inspect

    mod = importlib.import_module(module_path)
    listener = mod._listener_singleton
    sig = inspect.signature(listener.startup)
    params = sig.parameters
    assert "coordinator" in params, (
        f"{module_path}.startup must accept a 'coordinator' kwarg"
    )
    # Default value should be None so no-arg calls don't fail.
    assert params["coordinator"].default is None
