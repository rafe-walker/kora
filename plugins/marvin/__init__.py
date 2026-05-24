"""Marvin plugin — demo identity provider for KR-PLUGIN-IDENTITY Option C.

Multi-tenant proof for the architecture landed in #199. Validates that
a non-Kora plugin can claim the agent's identity via the
``pre_agent_identity_set`` hook + ``ctx.register_identity_provider``
convenience.

# What this plugin does

Reads ``MARVIN.md`` + ``marvin_system_prompt.md`` from the plugin
directory at import time + registers a provider that returns an
``IdentitySpec`` carrying Marvin's persona + reasoning prompt.

# How the hook fires

When an ``AnthropicReasoningEngine`` is constructed (and the Marvin
plugin is loaded), the engine fires the ``pre_agent_identity_set``
hook. Marvin's provider returns Marvin's ``IdentitySpec``. Engine
uses Marvin's ``system_prompt_content`` for inference calls.

# First-non-None-wins ordering

When BOTH Marvin and Kora plugins are loaded, the engine consumes the
FIRST non-None ``IdentitySpec`` returned by the hook. Order is
controlled by ``plugins.enabled`` in ``config.yaml`` (FIFO). Operators
who want Marvin to win order Marvin's name before ``kora_hermes`` in
the list; vice versa for Kora.

# Operator activation

Add to ``~/.hermes/config.yaml``::

    plugins:
      enabled:
        - marvin

Optionally combine with ``kora_hermes`` for the per-engine-routing
scenario from ``HOW_TO_BUILD_YOUR_OWN_AGENT.md`` §4.

# Distribution surface (future)

When Marvin becomes ``pip install marvin-runtime``, the
``hermes_agent.plugins`` entry point declared in ``pyproject.toml``
takes over from the bundled-plugin discovery path. Same ``register(ctx)``
function, same ``IdentitySpec`` contract.
"""

from __future__ import annotations

import logging
from pathlib import Path

from agent.identity_spec import IdentitySpec

logger = logging.getLogger(__name__)


# Read identity files at module import time. Frozen for the engine's
# lifetime — daemon restart picks up file edits (same hot-reload
# semantic as Kora's identity provider).
_PLUGIN_DIR = Path(__file__).resolve().parent
_MARVIN_SOUL = (_PLUGIN_DIR / "MARVIN.md").read_text(encoding="utf-8")
_MARVIN_SYSTEM_PROMPT = (
    _PLUGIN_DIR / "marvin_system_prompt.md"
).read_text(encoding="utf-8")


def marvin_identity_provider(*, engine=None, **kw):
    """Return Marvin's ``IdentitySpec`` for the
    ``pre_agent_identity_set`` hook.

    ``None`` would be returned only if Marvin's file content were
    empty — which the import-time read above already guards against
    (a missing file would have raised at module import). Today this
    provider is unconditional: when Marvin plugin loads, Marvin
    claims the identity.

    The ``engine`` kwarg is the firing-engine instance; carried for
    future use (e.g. per-engine routing, multi-Marvin variants).
    Today's implementation ignores it — Marvin is a singleton
    identity for the demo.
    """
    return IdentitySpec(
        soul_md_content=_MARVIN_SOUL,
        system_prompt_content=_MARVIN_SYSTEM_PROMPT,
        identity_metadata={
            "agent_name": "Marvin",
            "agent_version": "0.1.0",
            "plugin_name": "marvin",
            "persona": "paranoid_android_demo",
        },
    )


def register(ctx) -> None:
    """Plugin entry point. Hermes ``PluginManager.discover_and_load``
    calls this with a ``PluginContext`` per the bundled-plugin
    convention.

    Wires Marvin's identity provider via the convenience method
    added in #199 (``ctx.register_identity_provider``), which
    handles the ``{"identity": <spec>}`` envelope semantics.
    """
    ctx.register_identity_provider(marvin_identity_provider)
    logger.info(
        "[marvin] identity provider registered "
        "(soul_chars=%d, system_prompt_chars=%d)",
        len(_MARVIN_SOUL),
        len(_MARVIN_SYSTEM_PROMPT),
    )


__all__ = ["marvin_identity_provider", "register"]
