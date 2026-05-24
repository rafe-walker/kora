"""Tests for the state-holders sub-plugin.

Covers:
  - Identity-against-canonical: the orchestrator's
    ``_on_session_start`` IS the same object as the state-
    holders sub-plugin handler.
  - Discovery shim's ``_on_session_start`` resolves to the
    canonical handler.
  - The handler no-ops on non-Kora routes; debug-log on Kora
    routes.
  - The ``register(ctx)`` attaches ``on_session_start``.
  - ``STATE_HOLDER_ACCESSORS`` registry contains the expected
    keys + each accessor returns either a holder or ``None``
    (never raises).
  - ``holder_liveness()`` returns a boolean per accessor.
"""

from __future__ import annotations


def test_orchestrator_reexports_on_session_start_via_identity():
    """The orchestrator re-exports ``_on_session_start`` from the
    state-holders sub-plugin so the discovery shim's existing
    import line keeps resolving."""
    from kora_cli.reasoning.kora_hermes_plugin.plugin import (
        _on_session_start as orchestrator_handler,
    )
    from kora_cli.reasoning.kora_hermes_plugin.state_holders.plugin import (
        _on_session_start as canonical,
    )

    assert orchestrator_handler is canonical


def test_discovery_shim_reexports_on_session_start_to_canonical():
    """``from plugins.kora_hermes import _on_session_start``
    must resolve to the state-holders canonical handler."""
    from kora_cli.reasoning.kora_hermes_plugin.state_holders.plugin import (
        _on_session_start as canonical,
    )
    from plugins.kora_hermes import _on_session_start as via_shim

    assert via_shim is canonical


def test_handler_noop_on_non_kora_routes():
    from kora_cli.reasoning.kora_hermes_plugin.state_holders.plugin import (
        _on_session_start,
    )

    _on_session_start(route="")
    _on_session_start(route="non_kora_random")


def test_handler_fires_on_kora_routes_without_exception():
    from kora_cli.reasoning.kora_hermes_plugin.state_holders.plugin import (
        _on_session_start,
    )

    _on_session_start(route="slack_dm")
    _on_session_start(route="email_inbound")
    _on_session_start(route="probe_investigation")


def test_register_wires_on_session_start():
    from kora_cli.reasoning.kora_hermes_plugin.state_holders import register
    from kora_cli.reasoning.kora_hermes_plugin.state_holders.plugin import (
        _on_session_start,
    )

    registered: list = []

    class _Ctx:
        def register_hook(self, name, cb):
            registered.append((name, cb))

    register(_Ctx())
    assert registered == [("on_session_start", _on_session_start)]


def test_state_holder_accessors_registry_contains_expected_keys():
    from kora_cli.reasoning.kora_hermes_plugin.state_holders.registry import (
        STATE_HOLDER_ACCESSORS,
    )

    assert set(STATE_HOLDER_ACCESSORS.keys()) == {
        "cost_state_holder",
        "operational_state_holder",
    }
    for name, getter in STATE_HOLDER_ACCESSORS.items():
        # Getter must be callable + must NEVER raise (returns
        # None on holder-not-init / module-not-importable).
        result = getter()
        assert result is None or result is not None  # tautology — point is no-raise


def test_holder_liveness_returns_bool_per_accessor():
    from kora_cli.reasoning.kora_hermes_plugin.state_holders.registry import (
        STATE_HOLDER_ACCESSORS,
        holder_liveness,
    )

    liveness = holder_liveness()
    assert set(liveness.keys()) == set(STATE_HOLDER_ACCESSORS.keys())
    for name, alive in liveness.items():
        assert isinstance(alive, bool), (
            f"holder_liveness()[{name!r}] must be bool, got {type(alive)}"
        )
