"""Hermes-plugin-discovery entry point for the Kora plugin.

Per KR-PLUGIN-COST-LADDER: the canonical Kora plugin code lives
at ``kora_cli/reasoning/kora_hermes_plugin/``. This file is the
**thin shim** Hermes's ``PluginManager.discover_and_load`` finds
under the bundled ``plugins/<name>/`` convention. It re-exports
the public surface from the canonical location so any test or
external consumer that imports ``plugins.kora_hermes`` keeps
working.

Why the split:
  - Hermes plugin discovery requires a ``plugins/<name>/``
    directory with a ``plugin.yaml`` manifest + ``__init__.py``
    with a ``register(ctx)`` function. That's the bundled-
    plugin convention.
  - Future ``pip install kora-cost-ladder-plugin`` distribution
    requires the plugin code be a normal importable Python
    package — that lives under ``kora_cli/reasoning/
    kora_hermes_plugin/`` so it's clean to extract from this
    repo into its own distribution when the time comes.

This shim re-exports the orchestrator + sub-plugin helpers so
tests at ``tests/plugins/test_kora_hermes_plugin*.py`` (and any
external consumer that learned the public surface at the old
location) keep working unchanged.
"""

from kora_cli.reasoning.kora_hermes_plugin.cost_ladder.plugin import (
    _current_cost_rung,
)

# Backward-compat alias for the pre-extraction handler name.
# Tests at ``tests/plugins/test_kora_hermes_plugin*.py`` import
# ``_pre_api_request_mutable`` from this module; the renamed
# handler lives in the cost_ladder sub-plugin now.
from kora_cli.reasoning.kora_hermes_plugin.cost_ladder.plugin import (
    cost_ladder_and_caching_hook as _pre_api_request_mutable,
)
from kora_cli.reasoning.kora_hermes_plugin.haiku_router.plugin import (
    haiku_router_post_call_escalation as _post_llm_call_can_reissue,
)
from kora_cli.reasoning.kora_hermes_plugin.identity.plugin import (
    kora_identity_provider as _pre_agent_identity_set,
)
from kora_cli.reasoning.kora_hermes_plugin.plugin import (
    KORA_ROUTES,
    KoraHermesPlugin,
    _is_kora_call,
    _is_kora_reasoning_tool,
    _on_session_start,
    _post_llm_call,
    _post_tool_call,
    _pre_tool_call,
    _pre_tool_list_finalized,
    _tool_bridge_provide_result,
    get_kora_tools_for_agent,
    register,
)

__all__ = [
    "KORA_ROUTES",
    "KoraHermesPlugin",
    "_current_cost_rung",
    "_is_kora_call",
    "_is_kora_reasoning_tool",
    "_on_session_start",
    "_post_llm_call",
    "_post_llm_call_can_reissue",
    "_post_tool_call",
    "_pre_agent_identity_set",
    "_pre_api_request_mutable",
    "_pre_tool_call",
    "_pre_tool_list_finalized",
    "_tool_bridge_provide_result",
    "get_kora_tools_for_agent",
    "register",
]
