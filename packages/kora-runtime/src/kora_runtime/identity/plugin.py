"""Identity sub-plugin — Kora's identity provider for the new
``pre_agent_identity_set`` hook (KR-PLUGIN-IDENTITY Option C).

Registers a single identity provider that returns Kora's canonical
identity (kora_system_prompt.md + SOUL.md content + metadata) as
an :class:`agent.identity_spec.IdentitySpec`.

# Activation gates

  1. ``KORA_DISABLE_IDENTITY_PROVIDER`` env not "true" (operator
     escape hatch for incident response — falls through to the
     engine's file-read default, identical to pre-Option-C
     behavior).
  2. Canonical identity files load cleanly (non-empty system
     prompt). When either fails, the provider returns ``None``;
     other plugins (if any) can still claim identity, and the
     engine's file-read default acts as the final fallback.

# Why this plugin doesn't gate on KORA_ROUTES

Other Kora sub-plugins (cost_ladder, audit, haiku_router, etc.)
gate on ``_is_kora_call(route)`` so bare-Hermes-fork users who
load the plugin don't get Kora behavior on non-Kora calls.

Identity is DIFFERENT — it's set ONCE at engine construction,
NOT per-call. The plugin loader's job is to claim Kora's
identity for the engine instance; there is no per-call route
context at that point. Bare-Hermes users who load the plugin
DO want Kora's identity to be the agent's identity (that's the
whole point of installing the plugin). Operators who want a
different identity should write their own plugin (per
HOW_TO_BUILD_YOUR_OWN_AGENT.md).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from agent.identity_spec import IdentitySpec

from kora_runtime.identity.constants import (
    ENV_DISABLE_IDENTITY_PROVIDER,
)
from kora_runtime.identity.loader import (
    load_kora_identity,
)

logger = logging.getLogger(__name__)


def _is_disabled() -> bool:
    """Operator escape hatch — env-controlled disable."""
    return (
        os.environ.get(ENV_DISABLE_IDENTITY_PROVIDER, "")
        .strip()
        .lower()
        == "true"
    )


def kora_identity_provider(
    *,
    engine: Any = None,
    **kw: Any,
) -> Optional[IdentitySpec]:
    """Identity provider for the ``pre_agent_identity_set`` hook.

    Returns Kora's :class:`IdentitySpec` (loaded from canonical
    filesystem paths via the loader module) when the plugin is
    enabled + files load cleanly. ``None`` otherwise — the engine
    then falls back to its file-read default OR another plugin's
    identity (first-non-None-wins per the hook semantic).

    The ``engine`` kwarg is the firing-engine instance; carried
    here for future use (e.g. cost-ladder-aware identity, per-
    deployment overrides). Today's implementation doesn't read it
    — Kora's identity is fixed across engine instances.
    """
    if _is_disabled():
        logger.debug(
            "[kora_hermes.identity] disabled via %s — yielding to "
            "engine file-read default",
            ENV_DISABLE_IDENTITY_PROVIDER,
        )
        return None

    try:
        spec = load_kora_identity()
    except Exception as exc:
        logger.warning(
            "[kora_hermes.identity] load_kora_identity raised %r — "
            "yielding to engine file-read default",
            exc,
        )
        return None

    if spec is None:
        # Loader already logged the reason at DEBUG.
        return None

    logger.info(
        "[kora_hermes.identity] claiming identity (agent=%s, "
        "version=%s, system_prompt_chars=%d, soul_md_chars=%d)",
        spec.identity_metadata.get("agent_name"),
        spec.identity_metadata.get("agent_version"),
        len(spec.system_prompt_content),
        len(spec.soul_md_content),
    )
    return spec


def register(ctx) -> None:
    """Sub-plugin register. Wires
    :func:`kora_identity_provider` to the new
    ``pre_agent_identity_set`` hook via the PluginContext's
    ``register_identity_provider`` convenience method (which
    unwraps the IdentitySpec → ``{"identity": <spec>}`` envelope
    that the firing site expects)."""
    ctx.register_identity_provider(kora_identity_provider)
    logger.debug(
        "[kora_hermes.identity] sub-plugin registered"
    )
