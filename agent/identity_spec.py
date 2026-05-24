"""KR-PLUGIN-IDENTITY (Option C) — IdentitySpec dataclass + provider type.

# The gap

Hermes' reasoning engine reads its system prompt from a HARDCODED
file path at engine construction (``anthropic_engine.py:231``).
This couples the agent's identity to a specific filesystem
location, which works fine when the runtime is a single-tenant
fork but blocks two emerging use cases:

  1. **Multi-tenant Hermes deployments** — an operator wants to
     run "Kora" + "Marvin" + "Dave" agents off the same Hermes
     install; each needs its own identity prompt.
  2. **pip-installable agent bundles** — a future
     ``pip install kora-runtime`` shouldn't require shipping a
     specific filesystem layout. The bundle should be able to
     declare "here's my identity" via a plugin-provided value
     rather than a file-read at a Kora-specific path.

This module exposes the registration surface for that category
so plugin authors can register a single identity provider
without forking the runtime. The fallback file-read path stays
in the engine for bare-Hermes (no Kora plugin loaded) users.

# Shape

:class:`IdentitySpec` carries the resolved identity surface as a
frozen dataclass — the agent's persona prompt (``soul_md_content``;
historically loaded from ``~/.kora/SOUL.md``) AND the reasoning-
engine prompt (``system_prompt_content``; historically loaded
from ``kora_system_prompt.md``). Both fields are required strings;
``identity_metadata`` is an optional dict for free-form metadata
(agent name, version, kora-specific routing hints, etc.).

# Provider callable type

:data:`IdentityProvider` is the callable a plugin registers via
``PluginContext.register_identity_provider``. It receives the
firing kwargs (``engine`` plus any future-added context fields)
and returns either an :class:`IdentitySpec` OR ``None`` (fall
through to other providers / file-read default).

# Out of scope for this module (intentional)

This module is **specification-only**. It carries the dataclass +
provider type. The hook firing site lives in the reasoning
engine; the provider-registration convenience method lives on
``PluginContext`` in ``kora_cli/plugins.py``. Keeping the spec
separate makes it cleanly importable from tests, plugins, and
the engine without circular imports.

# Backward compatibility

This is a NEW module; no existing code paths are affected. The
engine's existing file-read fallback continues to work when no
plugin provides an identity. Plugins that do provide one take
precedence (first-non-None-wins per the existing hook semantic).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


@dataclass(frozen=True)
class IdentitySpec:
    """The resolved identity surface for one agent instance.

    Frozen so a plugin handler can't mutate the spec after returning
    it (the engine receives an immutable snapshot). All fields are
    required — a partial spec (only soul_md OR only system_prompt)
    should not be expressed via this dataclass; instead return
    ``None`` to fall through to other providers.

    Attributes:
      soul_md_content: The agent's persona / identity prompt — the
        text historically loaded from ``~/.kora/SOUL.md`` via
        ``agent/prompt_builder.py:load_soul_md``. Carried here so
        downstream consumers (prompt_builder, future skin layers)
        can resolve identity via the plugin surface instead of
        the filesystem.
      system_prompt_content: The reasoning-engine prompt — the
        text historically loaded from
        ``kora_docs/00_canonical_current_state/kora_system_prompt.md``
        by ``anthropic_engine.py:231``. Prepended to every
        inference request as the Anthropic SDK's ``system`` field.
      identity_metadata: Free-form dict for plugin-author-provided
        context. Conventional keys: ``"agent_name"`` (str),
        ``"agent_version"`` (str), ``"plugin_name"`` (str — set by
        the registration helper). Engine ignores unknown keys.
    """

    soul_md_content: str
    system_prompt_content: str
    identity_metadata: Dict[str, Any] = field(default_factory=dict)


# Type alias for plugin-author-supplied identity providers.
# Receives the engine instance + any future-added context kwargs;
# returns an IdentitySpec to claim the identity, or None to fall
# through to other providers (first-non-None-wins per the
# pre_agent_identity_set hook semantic).
IdentityProvider = Callable[..., Optional[IdentitySpec]]
