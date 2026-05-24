"""kora-runtime — Kora's Hermes-plugin orchestration, distributed as a pip package.

This package is the kora-runtime half of the KR-KORA-PIP-RESTRUCTURE-
PHASE-1 split (CC#3, 2026-05-24). It contains the 7-sub-plugin
orchestration code that previously lived at
``kora_cli/reasoning/kora_hermes_plugin/``. The Kora tree retains a
backward-compat shim at that location that re-exports from here, so
existing in-tree imports continue working unchanged.

# Sub-plugins (post KR-PLUGIN-EXTRACTIONS-BATCH-2 + KR-PLUGIN-IDENTITY)

  - ``cost_ladder/`` — KR-PLUGIN-COST-LADDER (#185). Owns the
    bundled ``pre_api_request_mutable`` hook (model selection +
    cache markers).
  - ``audit/`` — KR-PLUGIN-AUDIT. Owns ``post_tool_call`` +
    ``post_llm_call`` handlers + the ``_emit_tool_called_audit``
    writer helper.
  - ``caching/`` — KR-PLUGIN-CACHING. Owns ``cache_control:
    ephemeral`` markers (consumed by cost-ladder's bundled hook) +
    standalone ``caching_hook`` (not registered today).
  - ``short_circuit/`` — KR-PLUGIN-SHORT-CIRCUIT. Owns the regex +
    snapshot interpolation phrasebook matcher.
  - ``state_holders/`` — KR-PLUGIN-STATE-HOLDERS. Owns
    ``on_session_start`` + the holder accessor registry.
  - ``haiku_router/`` — KR-HAIKU-ROUTER-PLUGIN. Owns
    ``post_llm_call_can_reissue`` for parallel-Claude's Haiku-as-
    Opus-context escalation.
  - ``identity/`` — KR-PLUGIN-IDENTITY (Option C, closes 7-of-7
    plugin extractions). Owns the identity provider hook.

# Two-mode loading

  1. **Bundled (in-tree dev)**: ``plugins/kora_hermes/__init__.py``
     is the Hermes-discovery shim under the bundled-plugin convention.
     It re-exports from this package via a sys.path bootstrap (mirrors
     the ``plugins/marvin/`` POC pattern from #204). Tests at
     ``tests/plugins/test_kora_hermes_plugin*.py`` keep working.
  2. **Pip-installed**: ``pip install kora-runtime`` (after Hermes is
     source-only-installed; see README §"Installation"). Hermes's
     ``PluginManager._scan_entry_points`` finds the
     ``hermes_agent.plugins.kora = kora_runtime.register:register``
     entry point and wires the orchestrator.

# Source-only Hermes dependency

This package imports from ``agent.identity_spec``,
``agent.cost_state_holder``, and ``agent.operational_state_holder``
(the second two are deferred / lazy). Those modules ship inside
``hermes-agent``, which is NOT yet on PyPI per the 2026-05-25
operator decision — ``pip install kora-runtime`` requires Hermes to
already be source-installed (``pip install -e ../hermes-agent``).
See README.md.
"""

__version__ = "0.1.0a1"

from kora_runtime.plugin import KoraHermesPlugin, register

__all__ = ["KoraHermesPlugin", "register", "__version__"]
