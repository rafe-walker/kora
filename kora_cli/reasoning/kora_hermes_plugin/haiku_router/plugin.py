"""Haiku-router sub-plugin — post-call Opus escalation.

Consumes
:func:`kora_cli.reasoning.kora_hermes_plugin.cost_ladder.selector.should_escalate_post_call`
to decide whether a low-confidence Haiku response should be
re-issued to Opus with the Haiku answer as assistant context
(parallel-Claude's pattern from R3).

Registered against the NEW local Hermes hook
``post_llm_call_can_reissue`` added by
KR-HERMES-LOCAL-EXT-REISSUE in
``agent/conversation_loop.py``. The hook contract:

    def post_llm_call_can_reissue(*, response, api_kwargs, agent,
                                  iteration, task_id, session_id,
                                  route, **kw) -> dict | None

Returns ``{"reissue_with": <new api_kwargs>}`` to trigger a re-
issue; ``None`` to fall through.

# Activation gates (all must be true to escalate)

  1. Call is a Kora-tagged route (``_is_kora_call(route)``).
  2. ``iteration == 1`` — post-call escalation only fires on the
     first iteration. Subsequent iterations already get Opus from
     ``tool_loop_iteration`` (cost_ladder pre-call rule).
  3. The original call's model is Haiku — Opus already-on-Opus
     calls have nothing to escalate to.
  4. :func:`should_escalate_post_call` returns ``(True, <reason>)``
     based on Haiku response heuristics.
  5. ``KORA_DISABLE_POST_CALL_ESCALATION`` env is not "true"
     (operator escape hatch).

When all gates pass, builds the Opus re-issue kwargs (Haiku text
as assistant turn + terse review prompt) and returns it. The
re-issue itself is performed by the conversation_loop;
telemetry attribution (``escalated_to_opus=True``) fires there
too — this plugin only describes WHAT to re-issue.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from kora_cli.reasoning.kora_hermes_plugin.cost_ladder.selector import (
    should_escalate_post_call,
)
from kora_cli.reasoning.kora_hermes_plugin.haiku_router.constants import (
    ENV_DISABLE_POST_CALL_ESCALATION,
    MODEL_HAIKU,
    MODEL_OPUS,
)
from kora_cli.reasoning.kora_hermes_plugin.haiku_router.escalator import (
    build_opus_reissue_kwargs,
    extract_first_text,
    extract_last_user_text,
)

logger = logging.getLogger(__name__)


def _is_kora_call(route_value: Any) -> bool:
    """Mirror of the top-level plugin's KORA_ROUTES gate. Lazy-
    imported from the discovery shim to avoid a circular import
    (same pattern as cost_ladder.plugin)."""
    from plugins.kora_hermes import KORA_ROUTES

    if not isinstance(route_value, str) or not route_value:
        return False
    return route_value in KORA_ROUTES


def _is_disabled() -> bool:
    return (
        os.environ.get(ENV_DISABLE_POST_CALL_ESCALATION, "")
        .strip()
        .lower()
        == "true"
    )


def haiku_router_post_call_escalation(
    *,
    response: Any = None,
    api_kwargs: Optional[dict] = None,
    agent: Any = None,
    iteration: int = 0,
    route: str = "",
    **kw: Any,
) -> Optional[dict]:
    """``post_llm_call_can_reissue`` handler.

    Returns ``{"reissue_with": <new_api_kwargs>}`` when a Haiku
    response should be re-issued to Opus; ``None`` otherwise.
    Fail-soft: any extraction or build failure logs DEBUG and
    returns None (the loop continues with the Haiku response).
    """
    if not _is_kora_call(route):
        return None

    if not isinstance(api_kwargs, dict):
        return None

    if iteration != 1:
        # Post-call escalation only fires on the first iteration.
        # Subsequent iterations already get Opus from the cost-
        # ladder pre-call rule (tool_loop_iteration).
        return None

    if api_kwargs.get("model") != MODEL_HAIKU:
        # The original call wasn't Haiku — nothing to escalate
        # from. This catches force_opus_env / opus_prefix /
        # decision_language paths that already routed to Opus
        # pre-call.
        return None

    if _is_disabled():
        logger.debug(
            "[kora_hermes.haiku_router] post-call escalation disabled "
            "via %s — skipping",
            ENV_DISABLE_POST_CALL_ESCALATION,
        )
        return None

    try:
        haiku_text = extract_first_text(response)
    except Exception as exc:
        logger.debug(
            "[kora_hermes.haiku_router] extract_first_text raised %r — "
            "skipping escalation",
            exc,
        )
        return None

    if not haiku_text:
        # No text content to escalate from (e.g. tool-use-only
        # response). Fall through; no escalation.
        return None

    try:
        original_user_text = extract_last_user_text(api_kwargs)
    except Exception as exc:
        logger.debug(
            "[kora_hermes.haiku_router] extract_last_user_text raised "
            "%r — skipping escalation",
            exc,
        )
        return None

    try:
        should_escalate, reason = should_escalate_post_call(
            haiku_response_text=haiku_text,
            original_message_text=original_user_text,
        )
    except Exception as exc:
        logger.debug(
            "[kora_hermes.haiku_router] should_escalate_post_call raised "
            "%r — skipping escalation",
            exc,
        )
        return None

    if not should_escalate:
        return None

    try:
        new_kwargs = build_opus_reissue_kwargs(
            api_kwargs=api_kwargs,
            haiku_response_text=haiku_text,
            opus_model=MODEL_OPUS,
        )
    except Exception as exc:
        logger.warning(
            "[kora_hermes.haiku_router] build_opus_reissue_kwargs raised "
            "%r — skipping escalation",
            exc,
        )
        return None

    logger.info(
        "[kora_hermes.haiku_router] escalating to Opus post-call "
        "(reason=%s, route=%s, haiku_chars=%d, user_chars=%d)",
        reason,
        route,
        len(haiku_text),
        len(original_user_text),
    )

    return {"reissue_with": new_kwargs}


def register(ctx) -> None:
    """Sub-plugin register. Wires
    :func:`haiku_router_post_call_escalation` to the new
    ``post_llm_call_can_reissue`` hook added by
    KR-HERMES-LOCAL-EXT-REISSUE."""
    ctx.register_hook(
        "post_llm_call_can_reissue", haiku_router_post_call_escalation
    )
    logger.debug(
        "[kora_hermes.haiku_router] sub-plugin registered"
    )
