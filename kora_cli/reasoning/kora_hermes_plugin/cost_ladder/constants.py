"""Constants for the cost-ladder plugin.

Default model identifiers + env-var names + the bundled
decision-language / low-confidence pattern lists. Operators
override the patterns via env (see ``selector.py`` for the
``_compiled_*_patterns`` accessors).

Moved verbatim from ``kora_cli.router.cost_router`` per
KR-PLUGIN-COST-LADDER; the shim at the old path re-exports
these symbols so existing call sites keep working.
"""

from __future__ import annotations

from typing import List

# Match the long-form ID the engine has used since KR-FEAT-AGENTIC-
# REASONING ST1 (``MODEL_HAIKU = "claude-haiku-4-5-20251001"``).
# Using the long form keeps cache-key stability with already-warm
# caches in production.
DEFAULT_HAIKU_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_OPUS_MODEL = "claude-opus-4-7"


# Env names — operator overrides.
ENV_FORCE_OPUS = "KORA_FORCE_OPUS"
ENV_OPUS_TRIGGER_PATTERNS = "KORA_OPUS_TRIGGER_PATTERNS"
ENV_OPUS_PREFIX = "KORA_OPUS_PREFIX"
ENV_HAIKU_LOW_CONFIDENCE_PATTERNS = "KORA_HAIKU_LOW_CONFIDENCE_PATTERNS"


# Default decision-language patterns (case-insensitive). Operator
# overrides via ``KORA_OPUS_TRIGGER_PATTERNS`` (comma-separated
# regex list). Tuned for the question shapes operators use when
# they actually want a careful answer rather than a status read.
DEFAULT_DECISION_PATTERNS: List[str] = [
    r"\b(should|do|can|will|would)\s+(i|we|you)\b",
    r"\b(decide|decision|approve|approval|reject|go/no-go|ship or not)\b",
    r"\bis it (better|worth|safe|right)\b",
    r"\bwhat should (i|we)\b",
    r"\b(plan|strategy|approach)\b.*\b(for|to)\b",
]


# Low-confidence markers in a Haiku response. Operator overrides
# via ``KORA_HAIKU_LOW_CONFIDENCE_PATTERNS``.
DEFAULT_HAIKU_LOW_CONFIDENCE_PATTERNS: List[str] = [
    r"i'?m not (sure|certain|confident)",
    r"i don'?t (have enough|know|understand)",
    r"i might be wrong",
    r"this is (a guess|just a guess|speculative)",
    r"i can'?t (tell|determine|verify|confirm)",
    r"unclear (whether|if|how)",
]


# Heuristic: a Haiku response that's clearly too short for a
# substantive question. Tunable inline; operator can edit constant
# in this module if they need to shift the threshold (no env for
# this one — it's a structural signal, not a phrase list).
_LOW_CONFIDENCE_SHORT_RESPONSE_THRESHOLD = 50
_LOW_CONFIDENCE_LONG_INPUT_THRESHOLD = 200


# Cost-rung literals (matching ``agent.cost_state_holder.CostRung``).
RUNG_NORMAL = "normal"
RUNG_WARN_75 = "warn_75"
RUNG_DOWNSHIFT_90 = "downshift_90"
RUNG_HARD_STOP_100 = "hard_stop_100"
