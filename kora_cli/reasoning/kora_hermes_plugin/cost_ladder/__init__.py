"""Cost-ladder sub-plugin — default-Haiku with earned Opus escalation.

See ``selector.py`` for the pure decision functions,
``constants.py`` for env-var names + defaults, ``plugin.py``
for the ``pre_api_request_mutable`` hook handler + sub-register.

Public surface (re-exported for both plugin discovery and the
backward-compat shim at ``kora_cli/router/cost_router.py``):
"""

from kora_cli.reasoning.kora_hermes_plugin.cost_ladder.constants import (
    DEFAULT_DECISION_PATTERNS,
    DEFAULT_HAIKU_LOW_CONFIDENCE_PATTERNS,
    DEFAULT_HAIKU_MODEL,
    DEFAULT_OPUS_MODEL,
    ENV_FORCE_OPUS,
    ENV_HAIKU_LOW_CONFIDENCE_PATTERNS,
    ENV_OPUS_PREFIX,
    ENV_OPUS_TRIGGER_PATTERNS,
    RUNG_DOWNSHIFT_90,
    RUNG_HARD_STOP_100,
    RUNG_NORMAL,
    RUNG_WARN_75,
)
from kora_cli.reasoning.kora_hermes_plugin.cost_ladder.plugin import (
    cost_ladder_and_caching_hook,
    register,
)
from kora_cli.reasoning.kora_hermes_plugin.cost_ladder.selector import (
    RoutingDecision,
    select_model_pre_call,
    should_escalate_post_call,
    strip_opus_prefix,
)

__all__ = [
    "DEFAULT_DECISION_PATTERNS",
    "DEFAULT_HAIKU_LOW_CONFIDENCE_PATTERNS",
    "DEFAULT_HAIKU_MODEL",
    "DEFAULT_OPUS_MODEL",
    "ENV_FORCE_OPUS",
    "ENV_HAIKU_LOW_CONFIDENCE_PATTERNS",
    "ENV_OPUS_PREFIX",
    "ENV_OPUS_TRIGGER_PATTERNS",
    "RUNG_DOWNSHIFT_90",
    "RUNG_HARD_STOP_100",
    "RUNG_NORMAL",
    "RUNG_WARN_75",
    "RoutingDecision",
    "cost_ladder_and_caching_hook",
    "register",
    "select_model_pre_call",
    "should_escalate_post_call",
    "strip_opus_prefix",
]
