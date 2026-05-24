"""KoraHermesPlugin package — canonical Kora-side home for the
Hermes-plugin behaviors that drive Kora's reasoning route-through.

Per KR-PLUGIN-COST-LADDER: this package is the new canonical
location for the Kora plugin code. The Hermes plugin discovery
entry at ``plugins/kora_hermes/`` re-exports from here so
``PluginManager.discover_and_load`` picks the plugin up while
the actual implementation lives in this importable Kora package
(future-proof for ``pip install kora-cost-ladder-plugin``-shape
distribution).

Sub-plugins:
  - ``cost_ladder/`` — first extraction (this bucket). Owns the
    ``pre_api_request_mutable`` hook (model selection + cache
    markers).
  - (future) ``audit/`` — KR-PLUGIN-AUDIT will move audit emit
    here (post_tool_call + post_llm_call audit JSONL writes)
  - (future) ``caching/`` — KR-PLUGIN-CACHING will split the
    caching half from cost_ladder
  - (future) ``short_circuit/`` — KR-PLUGIN-SHORT-CIRCUIT
  - (future) ``state_holders/`` — KR-PLUGIN-STATE-HOLDERS
"""

from kora_cli.reasoning.kora_hermes_plugin.plugin import (
    KoraHermesPlugin,
    register,
)

__all__ = ["KoraHermesPlugin", "register"]
