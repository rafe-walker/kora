"""Haiku-router sub-plugin — post-call Opus escalation.

See ``escalator.py`` for the pure helpers (text extraction +
re-issue kwargs construction), ``constants.py`` for the model
IDs + env-var names, ``plugin.py`` for the
``post_llm_call_can_reissue`` hook handler + sub-register.

Consumes ``should_escalate_post_call`` from the cost_ladder
sub-plugin's selector — present since #185 but had no caller
until KR-HERMES-LOCAL-EXT-REISSUE added the hook surface that
this plugin registers against.
"""

from kora_runtime.haiku_router.constants import (
    ENV_DISABLE_POST_CALL_ESCALATION,
    MODEL_HAIKU,
    MODEL_OPUS,
    REISSUE_REVIEW_PROMPT,
)
from kora_runtime.haiku_router.escalator import (
    build_opus_reissue_kwargs,
    extract_first_text,
    extract_last_user_text,
)
from kora_runtime.haiku_router.plugin import (
    haiku_router_post_call_escalation,
    register,
)

__all__ = [
    "ENV_DISABLE_POST_CALL_ESCALATION",
    "MODEL_HAIKU",
    "MODEL_OPUS",
    "REISSUE_REVIEW_PROMPT",
    "build_opus_reissue_kwargs",
    "extract_first_text",
    "extract_last_user_text",
    "haiku_router_post_call_escalation",
    "register",
]
