"""Identity sub-plugin — KR-PLUGIN-IDENTITY (Option C).

Kora's identity provider for the new ``pre_agent_identity_set``
hook. See ``loader.py`` for the file-read helpers, ``constants.py``
for env-var names + canonical paths, ``plugin.py`` for the
provider handler + sub-register.

Closes the 7th-of-7 plugin extraction (per Lock R3-2's original
plan, deferred until Option C was chosen this turn).

Consumes the Hermes-side surface in ``agent/identity_spec.py``
(IdentitySpec dataclass + IdentityProvider type) + the
``register_identity_provider`` convenience method on PluginContext
in ``kora_cli/plugins.py``. The hook itself fires inside
``AnthropicReasoningEngine.__init__`` — see that file for the
firing semantics.
"""

from kora_cli.reasoning.kora_hermes_plugin.identity.constants import (
    DEFAULT_AGENT_NAME,
    DEFAULT_AGENT_VERSION,
    DEFAULT_PLUGIN_NAME,
    DEFAULT_SOUL_MD_PATH,
    DEFAULT_SYSTEM_PROMPT_PATH,
    ENV_DISABLE_IDENTITY_PROVIDER,
    ENV_SOUL_MD_PATH,
    ENV_SYSTEM_PROMPT_PATH,
)
from kora_cli.reasoning.kora_hermes_plugin.identity.loader import (
    load_kora_identity,
    resolve_soul_md_path,
    resolve_system_prompt_path,
)
from kora_cli.reasoning.kora_hermes_plugin.identity.plugin import (
    kora_identity_provider,
    register,
)

__all__ = [
    "DEFAULT_AGENT_NAME",
    "DEFAULT_AGENT_VERSION",
    "DEFAULT_PLUGIN_NAME",
    "DEFAULT_SOUL_MD_PATH",
    "DEFAULT_SYSTEM_PROMPT_PATH",
    "ENV_DISABLE_IDENTITY_PROVIDER",
    "ENV_SOUL_MD_PATH",
    "ENV_SYSTEM_PROMPT_PATH",
    "kora_identity_provider",
    "load_kora_identity",
    "register",
    "resolve_soul_md_path",
    "resolve_system_prompt_path",
]
