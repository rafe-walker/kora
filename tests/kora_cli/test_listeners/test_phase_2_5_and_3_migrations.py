"""Batched dual-registry tests for KR-DAEMON-LISTENERS-VIA-GATEWAY
Phase 2.5 + Phase 3.

After this PR: 12/12 periodic-task listeners + 3/3 singleton-holders
on the gateway path. Only HTTP service-mounts (web/mcp/webhooks) +
the heartbeat scheduler dissolution remain.

Coverage per listener:
  1. Listener is in the Hermes BackgroundDaemonRegistry
  2. Listener-class shape: Kora LISTENER_REGISTRY still has it
     (back-compat Path B thin-shim)
  3. Pure-periodic-task shape (Phase 2.5): Kora LISTENER_REGISTRY
     does NOT have it (never did — no daemon registration history)
  4. Singleton-holder shape (Phase 3): both registries point at
     the same singleton's bound methods
  5. Periodic-task fields carry the expected callback (where
     applicable)
  6. reasoning_engine FATAL contract: documented in docstring +
     existing startup-raise propagation behavior preserved
"""

from __future__ import annotations

import inspect

import pytest

from agent.background_daemon_registry import (
    BackgroundDaemonEntry,
    background_daemon_registry,
)
from kora_cli import daemon as daemon_mod


# Trigger import-time registrations.
import kora_cli.listeners  # noqa: F401


# ---------------------------------------------------------------------------
# Inventory pin — Phase 2.5 + 3 bring total to 15 daemons
# ---------------------------------------------------------------------------


PHASE_2_5_LISTENERS = {
    "promote_probe_fix_envelopes",
    "promote_router_tuning",
    "promote_tool_trimming",
}

PHASE_3_LISTENERS = {
    "slack_client",
    "purelymail_client",
    "reasoning_engine",
}


@pytest.mark.parametrize("name", sorted(PHASE_2_5_LISTENERS))
def test_phase_2_5_listener_in_hermes_registry(name):
    entry = background_daemon_registry().by_name(name)
    assert entry is not None
    assert isinstance(entry, BackgroundDaemonEntry)
    assert entry.plugin_name == "kora"


@pytest.mark.parametrize("name", sorted(PHASE_2_5_LISTENERS))
def test_phase_2_5_listener_not_in_kora_registry(name):
    """Pure-periodic-task listeners (no Listener class, no daemon
    history) never had Kora-side ``register_daemon_listener`` calls.
    Phase 2.5 only adds them to the Hermes registry."""
    kora_names = {n for n, _f in daemon_mod.LISTENER_REGISTRY}
    assert name not in kora_names


@pytest.mark.parametrize("name", sorted(PHASE_3_LISTENERS))
def test_phase_3_listener_in_both_registries(name):
    """Singleton-holders are Path B thin-shim: both registries
    point at the same listener (back-compat preserved until
    Phase 6 dissolves the Kora-side LISTENER_REGISTRY)."""
    entry = background_daemon_registry().by_name(name)
    assert entry is not None
    assert isinstance(entry, BackgroundDaemonEntry)
    assert entry.plugin_name == "kora"
    kora_names = {n for n, _f in daemon_mod.LISTENER_REGISTRY}
    assert name in kora_names


# ---------------------------------------------------------------------------
# Phase 2.5 — periodic-task callback identity
# ---------------------------------------------------------------------------


def test_promote_probe_fix_envelopes_periodic_task():
    from kora_cli.listeners.promote_probe_fix_envelopes_listener import (
        _periodic_task,
    )
    entry = background_daemon_registry().by_name("promote_probe_fix_envelopes")
    assert entry is not None and entry.periodic_task is not None
    assert entry.periodic_task.name == "promote_probe_fix_envelopes_cycle"
    assert entry.periodic_task.callback is _periodic_task


def test_promote_router_tuning_periodic_task():
    from kora_cli.listeners.promote_router_tuning_listener import _periodic_task
    entry = background_daemon_registry().by_name("promote_router_tuning")
    assert entry is not None and entry.periodic_task is not None
    assert entry.periodic_task.name == "promote_router_tuning_cycle"
    assert entry.periodic_task.callback is _periodic_task


def test_promote_tool_trimming_periodic_task():
    from kora_cli.listeners.promote_tool_trimming_listener import _periodic_task
    entry = background_daemon_registry().by_name("promote_tool_trimming")
    assert entry is not None and entry.periodic_task is not None
    assert entry.periodic_task.name == "promote_tool_trimming_cycle"
    assert entry.periodic_task.callback is _periodic_task


# ---------------------------------------------------------------------------
# Phase 3 — singleton-holders carry NO periodic_task (event-driven)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(PHASE_3_LISTENERS))
def test_phase_3_listener_has_no_periodic_task(name):
    """Singleton-holders (slack_client / purelymail_client /
    reasoning_engine) are event-driven — they hold a client/engine
    that other code paths invoke; no scheduled work owned by the
    listener itself. The Hermes entry's ``periodic_task`` is None."""
    entry = background_daemon_registry().by_name(name)
    assert entry is not None
    assert entry.periodic_task is None, (
        f"{name} should NOT carry a periodic_task — singleton-holders "
        f"are event-driven"
    )


# ---------------------------------------------------------------------------
# Phase 3 — singleton invariant across registries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, module_path",
    [
        ("slack_client",
         "kora_cli.listeners.slack_client_listener"),
        ("purelymail_client",
         "kora_cli.listeners.purelymail_client_listener"),
        ("reasoning_engine",
         "kora_cli.listeners.reasoning_engine_listener"),
    ],
)
def test_singleton_holder_shared_across_registries(name, module_path):
    """Both registries point at the SAME singleton's bound methods.
    Prevents future regressions where one path constructs a fresh
    Listener instance and the other path holds a stale singleton."""
    import importlib

    mod = importlib.import_module(module_path)
    singleton = mod._listener_singleton
    hermes_entry = background_daemon_registry().by_name(name)
    assert hermes_entry is not None
    assert hermes_entry.startup == singleton.startup
    assert hermes_entry.shutdown == singleton.shutdown
    kora_lookup = dict(daemon_mod.LISTENER_REGISTRY)
    kora_factory = kora_lookup[name]
    kora_startup, kora_shutdown, _ = kora_factory()
    assert kora_startup == singleton.startup
    assert kora_shutdown == singleton.shutdown


# ---------------------------------------------------------------------------
# Phase 3 — startup signature accepts optional coordinator kwarg
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_path",
    [
        "kora_cli.listeners.slack_client_listener",
        "kora_cli.listeners.purelymail_client_listener",
        "kora_cli.listeners.reasoning_engine_listener",
    ],
)
def test_phase_3_startup_signature_accepts_coordinator_kwarg(module_path):
    """Every Phase 3 Listener-class startup must accept the optional
    coordinator kwarg so both consumer shapes work."""
    import importlib

    mod = importlib.import_module(module_path)
    listener = mod._listener_singleton
    sig = inspect.signature(listener.startup)
    params = sig.parameters
    assert "coordinator" in params
    assert params["coordinator"].default is None


# ---------------------------------------------------------------------------
# reasoning_engine — FATAL semantic contract
# ---------------------------------------------------------------------------


def test_reasoning_engine_fatal_contract_documented_in_listener():
    """The FATAL contract MUST be explicitly documented in the
    listener's module docstring so a future gateway-consumer author
    knows to propagate startup exceptions (vs swallowing them).

    Phase 3 implementation choice: documentation-driven contract
    rather than a new BackgroundDaemonEntry.startup_failure_is_fatal
    field. See the listener docstring for the explicit rationale."""
    from kora_cli.listeners import reasoning_engine_listener

    doc = reasoning_engine_listener.__doc__ or ""
    # Pin the explicit FATAL-contract section in the docstring.
    assert "FATAL semantic contract" in doc, (
        "reasoning_engine_listener module docstring must contain "
        "'FATAL semantic contract' section so future consumers know "
        "to propagate startup exceptions"
    )
    assert "propagate startup exception" in doc.lower() or (
        "abort" in doc.lower() and "boot" in doc.lower()
    ), (
        "FATAL contract section must explicitly require startup-"
        "exception propagation / boot-abort"
    )


@pytest.mark.asyncio
async def test_reasoning_engine_startup_still_raises_on_construction_failure(
    monkeypatch,
):
    """Behavioral pin for the FATAL contract: the listener's
    startup STILL raises ReasoningEngineError when engine
    construction fails. This is what Kora's DaemonCoordinator
    (today's consumer) catches to abort boot, and it's what the
    future gateway consumer must also catch + propagate."""
    from kora_cli.listeners.reasoning_engine_listener import (
        ReasoningEngineListener,
    )
    from kora_cli.reasoning.anthropic_engine import (
        ReasoningEngineError,
        ReasoningEngineNotConfigured,
    )

    # Unset both credential envs → construction raises
    # ReasoningEngineNotConfigured (a subclass of
    # ReasoningEngineError).
    monkeypatch.delenv("KORA_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    listener = ReasoningEngineListener()
    with pytest.raises(ReasoningEngineError):
        await listener.startup()
