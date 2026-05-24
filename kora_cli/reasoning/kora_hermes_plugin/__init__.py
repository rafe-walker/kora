"""KoraHermesPlugin package — canonical Kora-side home for the
Hermes-plugin behaviors that drive Kora's reasoning route-through.

Per KR-PLUGIN-COST-LADDER: this package is the new canonical
location for the Kora plugin code. The Hermes plugin discovery
entry at ``plugins/kora_hermes/`` re-exports from here so
``PluginManager.discover_and_load`` picks the plugin up while
the actual implementation lives in this importable Kora package
(future-proof for ``pip install kora-cost-ladder-plugin``-shape
distribution).

Sub-plugins (post KR-PLUGIN-EXTRACTIONS-BATCH-2):
  - ``cost_ladder/`` — KR-PLUGIN-COST-LADDER (#185). Owns the
    bundled ``pre_api_request_mutable`` hook (model selection +
    cache markers).
  - ``audit/`` — KR-PLUGIN-AUDIT (Deliverable A). Owns
    ``post_tool_call`` + ``post_llm_call`` handlers + the
    ``_emit_tool_called_audit`` writer helper.
  - ``caching/`` — KR-PLUGIN-CACHING (Deliverable B). Owns
    ``cache_control: ephemeral`` markers + standalone
    ``caching_hook`` (not registered today; cost-ladder hook
    still does the wrap).
  - ``short_circuit/`` — KR-PLUGIN-SHORT-CIRCUIT (Deliverable C).
    Owns the regex + snapshot interpolation phrasebook matcher;
    ``short_circuit_hook`` not registered today.
  - ``state_holders/`` — KR-PLUGIN-STATE-HOLDERS (Deliverable D).
    Owns ``on_session_start`` + the holder accessor registry.
"""

from kora_cli.reasoning.kora_hermes_plugin.plugin import (
    KoraHermesPlugin,
    register,
)

__all__ = ["KoraHermesPlugin", "register"]
