"""Constants for the haiku_router sub-plugin.

Defaults + env-var names. The model identifiers are re-exported
from the cost_ladder sub-plugin so a single source-of-truth for
the long-form Anthropic IDs (cache-key stability + version-pin
discipline) stays at ``cost_ladder.constants``.
"""

from __future__ import annotations

from kora_cli.reasoning.kora_hermes_plugin.cost_ladder.constants import (
    DEFAULT_HAIKU_MODEL as MODEL_HAIKU,
)
from kora_cli.reasoning.kora_hermes_plugin.cost_ladder.constants import (
    DEFAULT_OPUS_MODEL as MODEL_OPUS,
)

# Re-issue prompt — short, terse instruction asked of Opus when it
# inherits a Haiku response as assistant context. Parallel-Claude's
# pattern (R3 origin): Opus often just confirms with a one-liner
# instead of redoing the work — ~30% cheaper escalations than a
# cold Opus call.
REISSUE_REVIEW_PROMPT = (
    "Please review my last response and improve it if needed. "
    "Be terse if confirming."
)

# Disable env — operator escape hatch if post-call escalation
# starts misbehaving. When set to "true" the plugin no-ops and
# the loop continues with the Haiku response unchanged.
ENV_DISABLE_POST_CALL_ESCALATION = "KORA_DISABLE_POST_CALL_ESCALATION"

__all__ = [
    "ENV_DISABLE_POST_CALL_ESCALATION",
    "MODEL_HAIKU",
    "MODEL_OPUS",
    "REISSUE_REVIEW_PROMPT",
]
