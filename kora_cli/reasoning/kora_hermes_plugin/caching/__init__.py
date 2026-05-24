"""Caching sub-plugin — KR-PLUGIN-CACHING.

Owns the prompt-caching ``cache_control: ephemeral`` markers
that previously lived in ``kora_cli/reasoning/anthropic_engine.py``
(``_wrap_system_as_cacheable`` + ``_wrap_tools_as_cacheable``).

# Cross-dep cleanup

PR #185 (KR-PLUGIN-COST-LADDER) left the cost-ladder hook handler
importing the caching markers from the engine — a deliberate
v1 bundling that this extraction now cleans up. After this PR
the cost-ladder hook imports markers from
``kora_hermes_plugin.caching.markers`` (the canonical location),
and the engine retains a one-line re-import shim for any
external caller that still imports the wrappers from the engine.

# Hook ownership

The bundled ``pre_api_request_mutable`` hook handler in
``cost_ladder/plugin.py`` continues to do BOTH cost-ladder model
selection AND caching wrap in a single handler fire. The
caching sub-plugin provides ``markers.py`` as a library; its
``plugin.py`` exposes the standalone ``caching_hook`` so a
future split (KR-HERMES-LOCAL-EXT-REISSUE may motivate one) can
swap the bundled handler for two separate hook handlers without
moving code around. Today the orchestrator does NOT register
``caching_hook`` — single-handler fire preserved verbatim.
"""

from kora_cli.reasoning.kora_hermes_plugin.caching.constants import (
    CACHE_CONTROL_EPHEMERAL,
)
from kora_cli.reasoning.kora_hermes_plugin.caching.markers import (
    _wrap_system_as_cacheable,
    _wrap_tools_as_cacheable,
)
from kora_cli.reasoning.kora_hermes_plugin.caching.plugin import (
    caching_hook,
    register,
)

__all__ = [
    "CACHE_CONTROL_EPHEMERAL",
    "_wrap_system_as_cacheable",
    "_wrap_tools_as_cacheable",
    "caching_hook",
    "register",
]
