"""Standalone smoke tests for the kora-runtime wheel.

Mirrors the §B.4 dry-run install check from the
KR-KORA-PIP-RESTRUCTURE-PHASE-1 bucket. CI runs this against a
freshly-built wheel installed into a clean venv (with isokron-client
pre-installed; Hermes optionally pre-installed source-only). Verifies
the import surface + entry-point discovery without requiring a live
Hermes / Kora context.

These tests do NOT call ``KoraHermesPlugin.register(ctx)`` — that path
loads the 7 sub-plugins, several of which eagerly import
``agent.identity_spec`` (the source-only Hermes dep). The dry-run
install is meant to be runnable WITHOUT Hermes on the path; sub-plugin
loading is exercised by the in-tree test suite at
``tests/plugins/test_kora_hermes_plugin_*.py`` and
``tests/kora_cli/reasoning/kora_hermes_plugin/`` (which run inside
Kora's editable-install venv where Hermes IS on the path).
"""

from __future__ import annotations

import importlib.metadata as md


def test_package_imports_with_only_isokron_client_on_path():
    """``import kora_runtime`` resolves with the wheel + isokron-
    client; sub-plugin module loads (which would require Hermes)
    are not triggered."""
    import kora_runtime

    assert kora_runtime.__version__ == "0.1.0a1"
    assert callable(kora_runtime.register)
    assert kora_runtime.KoraHermesPlugin.__name__ == "KoraHermesPlugin"


def test_hermes_agent_plugins_entry_point_is_discoverable():
    """The pyproject ``[project.entry-points."hermes_agent.plugins"]
    kora = "kora_runtime:register"`` block must be reachable via
    ``importlib.metadata.entry_points`` so Hermes's
    ``PluginManager._scan_entry_points`` finds it at boot."""
    eps = list(md.entry_points(group="hermes_agent.plugins"))
    names = {e.name: e.value for e in eps}
    assert "kora" in names, (
        f"hermes_agent.plugins.kora entry point not found; got: {names}"
    )
    assert names["kora"] == "kora_runtime:register"


def test_seven_sub_plugin_packages_resolve_at_import_time():
    """The 7 sub-package directories under ``kora_runtime/`` ship
    inside the wheel. We import each — sub-plugins that depend on
    Hermes (identity) skip eager import here; the rest load cleanly
    via the deferred-import pattern they use for Kora-side deps."""
    sub_packages = (
        "kora_runtime.cost_ladder",
        "kora_runtime.caching",
        "kora_runtime.audit",
        "kora_runtime.short_circuit",
        "kora_runtime.state_holders",
        "kora_runtime.haiku_router",
    )
    for name in sub_packages:
        __import__(name)
    # Plus the orchestrator module.
    __import__("kora_runtime.plugin")


def test_canonical_module_paths_are_kora_runtime_not_legacy():
    """Post KR-KORA-PIP-RESTRUCTURE-PHASE-1, the canonical
    ``__module__`` attribute for public symbols is
    ``kora_runtime.*`` — the legacy ``kora_cli.reasoning.
    kora_hermes_plugin.*`` path only resolves through the back-
    compat shim sys.modules alias."""
    from kora_runtime.cost_ladder.selector import RoutingDecision
    from kora_runtime.short_circuit.matcher import PhrasebookEntry
    from kora_runtime.plugin import KoraHermesPlugin

    assert RoutingDecision.__module__ == "kora_runtime.cost_ladder.selector"
    assert PhrasebookEntry.__module__ == "kora_runtime.short_circuit.matcher"
    assert KoraHermesPlugin.__module__ == "kora_runtime.plugin"


def test_kora_runtime_declares_isokron_client_as_dep():
    """The ``isokron-client`` Phase 1 sister package must be in the
    wheel METADATA — proves the dep declaration in pyproject.toml
    survived the wheel build."""
    meta = md.metadata("kora-runtime")
    requires = meta.get_all("Requires-Dist") or []
    assert any("isokron-client" in r for r in requires), (
        f"isokron-client not in kora-runtime Requires-Dist; got: {requires}"
    )
