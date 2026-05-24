"""Backward-compat shim — canonical location moved.

Per KR-PLUGIN-COST-LADDER: the cost-ladder code now lives at
``kora_cli/reasoning/kora_hermes_plugin/cost_ladder/`` (split
into ``selector.py`` for pure decision functions,
``constants.py`` for the model + env-var + pattern constants,
and ``plugin.py`` for the Hermes hook handler + sub-register).

This file re-exports the public surface from the new location
so existing imports keep working:

  - ``from kora_cli.router.cost_router import select_model_pre_call``
  - ``from kora_cli.router.cost_router import DEFAULT_HAIKU_MODEL``
  - ``from kora_cli.router import ...``  (via the package's
    ``__init__.py`` which imports from this module)

New code should import from the canonical location directly:

  - ``from kora_cli.reasoning.kora_hermes_plugin.cost_ladder import
    select_model_pre_call``

The shim is retained indefinitely (other modules in the
kora_cli tree + tests at multiple paths import from here);
deprecation is not on the roadmap.
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
    _LOW_CONFIDENCE_LONG_INPUT_THRESHOLD,
    _LOW_CONFIDENCE_SHORT_RESPONSE_THRESHOLD,
)
from kora_cli.reasoning.kora_hermes_plugin.cost_ladder.selector import (
    RoutingDecision,
    _compiled_decision_patterns,
    _compiled_low_confidence_patterns,
    _has_opus_prefix,
    _opus_prefix,
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
    "select_model_pre_call",
    "should_escalate_post_call",
    "strip_opus_prefix",
]
