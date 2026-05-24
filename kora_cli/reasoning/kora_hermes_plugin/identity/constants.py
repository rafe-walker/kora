"""Constants for the identity sub-plugin.

Default file paths + env-var names for Kora's identity sources.
Two distinct identity surfaces today (both required to fully
populate the IdentitySpec):

  1. ``kora_system_prompt.md`` — the reasoning-engine system
     prompt. Prepended to every Anthropic SDK call as the
     ``system`` field. Long-form structural prompt (~10-15 KB).
  2. ``SOUL.md`` — the operator-tunable persona prompt. Loaded
     by ``agent/prompt_builder.py:load_soul_md`` for the bypass-
     agent's identity slot. Shorter (~1-2 KB).

Both paths are env-override-able so multi-tenant deployments
can point a Kora-style plugin at custom files without touching
the canonical Kora paths.
"""

from __future__ import annotations

from pathlib import Path

# Env-overrides — operator escape hatches for testing + alt deployments.
ENV_SYSTEM_PROMPT_PATH = "KORA_SYSTEM_PROMPT_PATH"
ENV_SOUL_MD_PATH = "KORA_SOUL_MD_PATH"

# Disable env — when set "true", the identity sub-plugin no-ops and
# the engine falls back to its file-read default. Operator escape
# hatch for incident response.
ENV_DISABLE_IDENTITY_PROVIDER = "KORA_DISABLE_IDENTITY_PROVIDER"

# Canonical Kora paths — file locations the plugin reads when no
# env override is set. Resolve relative to the repository root so
# both the bypass-agent and the gateway path see the same files.
_REPO_ROOT = Path(__file__).resolve().parents[4]

DEFAULT_SYSTEM_PROMPT_PATH = (
    _REPO_ROOT / "kora_docs" / "00_canonical_current_state"
    / "kora_system_prompt.md"
)

DEFAULT_SOUL_MD_PATH = _REPO_ROOT / "SOUL.md"

# Identity metadata — populated into IdentitySpec.identity_metadata
# when the canonical Kora identity is resolved. Conventional shape
# (see agent/identity_spec.py:IdentitySpec docstring).
DEFAULT_AGENT_NAME = "Kora"
DEFAULT_AGENT_VERSION = "phase2"
DEFAULT_PLUGIN_NAME = "kora_hermes"


__all__ = [
    "DEFAULT_AGENT_NAME",
    "DEFAULT_AGENT_VERSION",
    "DEFAULT_PLUGIN_NAME",
    "DEFAULT_SOUL_MD_PATH",
    "DEFAULT_SYSTEM_PROMPT_PATH",
    "ENV_DISABLE_IDENTITY_PROVIDER",
    "ENV_SOUL_MD_PATH",
    "ENV_SYSTEM_PROMPT_PATH",
]
