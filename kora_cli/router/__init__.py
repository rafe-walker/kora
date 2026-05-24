"""Cost-aware model router for the reasoning engine. See
``cost_router.py`` for the full surface + decision tree."""

from kora_cli.router.cost_router import (
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
    "select_model_pre_call",
    "should_escalate_post_call",
    "strip_opus_prefix",
]
