"""Cost-ladder selector — pure functions for model selection.

Per Council R3 Lock R3-3 (the cost-ladder FLIP): the reasoning
engine no longer defaults to Opus with reactive downshift; it
defaults to Haiku 4.5 and escalates to Opus 4.7 **only when a
signal earns it**. The existing cost-ladder rung downshift stays
as a Layer 2 defensive backstop — when budget pressure crosses
WARN_75/DOWNSHIFT_90 the router clamps every call to Haiku
regardless of any earning signal.

# Earning signals

Pre-call (decided BEFORE the first API call):

  1. **Operator override**: ``/opus`` prefix in the message text.
     Case-insensitive. Stripped from the prompt before the SDK
     call so the model doesn't see the routing instruction.
  2. **Operator defensive switch**: ``KORA_FORCE_OPUS=true`` env.
  3. **Decision-language regex**: message text matches a tunable
     pattern list (``KORA_OPUS_TRIGGER_PATTERNS``). Defaults
     cover the common decision-making phrases ("should I", "do I",
     "decide", "approve", "go/no-go", etc.).
  4. **Tool-use iteration ≥ 2**: when the engine has already
     dispatched tools once, the subsequent iterations get Opus.
     This catches Haiku-started reasoning that needs more compute
     after the model saw tool results.

Post-call (decided AFTER the Haiku attempt on iteration 1):

  5. **Haiku response low-confidence**: either an explicit
     uncertainty marker ("I'm not sure", "I don't have enough",
     etc.) OR a too-short response for a non-trivial input
     (heuristic: ``len(reply) < 50`` when ``len(input) > 200``).

# Layer 2: cost-ladder backstop

The cost-rung input to ``select_model_pre_call`` short-circuits:

  - ``hard_stop_100`` → ``model=None`` (caller fail-fasts)
  - ``warn_75`` / ``downshift_90`` → forced Haiku, ignoring any
    earning signal. The escalation count still tracks what
    WOULD have been Opus so the cockpit can show "cost-clamped"
    decisions.
  - ``normal`` (default) → router runs its full decision tree.

# Telemetry contract

Every routing decision is observable via the returned
``RoutingDecision.reason`` string. The engine emits per-call
``cost_telemetry.record_call(..., escalated_to_opus=...)`` so
PR #161's panels can show the Haiku-vs-Opus split + the
escalation rate per route.

# What this is NOT

This module is independent of ``agent/cost_downshift.py``. That
module governs **substrate Sea_Ticket queue** decisions
(defer vs run, criticality clamping) and runs on a different
code path than the slack-DM / email-inbound reasoning engine.
Both can co-exist; both name "downshift" in different domain
contexts (substrate-tier downshift vs LLM-tier downshift).

# Module structure (KR-PLUGIN-COST-LADDER)

This file holds pure functions only — no hook handlers, no
Hermes plugin integration. The hook handler lives in
``plugin.py``; the constants live in ``constants.py``. The
``register()`` entry on the top-level Hermes plugin
(``kora_hermes_plugin.plugin``) calls the sub-register here.

The shim at ``kora_cli/router/cost_router.py`` re-exports
everything in this module verbatim so existing call sites
keep working.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

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
    RUNG_WARN_75,
    _LOW_CONFIDENCE_LONG_INPUT_THRESHOLD,
    _LOW_CONFIDENCE_SHORT_RESPONSE_THRESHOLD,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RoutingDecision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutingDecision:
    """The router's per-iteration verdict.

    Attributes:
      model: The model identifier to send. ``None`` when the
        cost rung is HARD_STOP_100 — caller must fail-fast.
      reason: Stable human-readable + telemetry-readable code.
        Reasons: ``default_haiku`` / ``opus_prefix`` /
        ``force_opus_env`` / ``decision_language`` /
        ``tool_loop_iteration`` / ``cost_clamp:<rung>`` /
        ``cost_ladder_halted`` / ``escalated_post_haiku:<sub>``.
      escalated: ``True`` when the model is Opus AND the choice
        was an "earning signal" path. Cost-clamps that landed on
        Haiku set this False; explicit Opus signals that got
        clamped to Haiku ALSO set False (the call IS Haiku).
      haiku_context_for_opus: When a post-call escalation produces
        a Decision wrapping Opus, this carries Haiku's response so
        the caller can include it in the re-issue's messages.
        None on every pre-call decision.
    """

    model: Optional[str]
    reason: str
    escalated: bool
    haiku_context_for_opus: Optional[str] = None


# ---------------------------------------------------------------------------
# Pre-call decision
# ---------------------------------------------------------------------------


def _compiled_decision_patterns() -> List[re.Pattern[str]]:
    """Compile decision-language patterns from env or default."""
    raw = os.environ.get(ENV_OPUS_TRIGGER_PATTERNS, "").strip()
    sources = (
        [s for s in raw.split(",") if s.strip()]
        if raw
        else DEFAULT_DECISION_PATTERNS
    )
    out: List[re.Pattern[str]] = []
    for src in sources:
        try:
            out.append(re.compile(src.strip(), re.IGNORECASE))
        except re.error as exc:
            logger.warning(
                "[kora.router] decision pattern %r invalid: %r — "
                "skipping",
                src,
                exc,
            )
    return out


def _compiled_low_confidence_patterns() -> List[re.Pattern[str]]:
    raw = os.environ.get(ENV_HAIKU_LOW_CONFIDENCE_PATTERNS, "").strip()
    sources = (
        [s for s in raw.split(",") if s.strip()]
        if raw
        else DEFAULT_HAIKU_LOW_CONFIDENCE_PATTERNS
    )
    out: List[re.Pattern[str]] = []
    for src in sources:
        try:
            out.append(re.compile(src.strip(), re.IGNORECASE))
        except re.error as exc:
            logger.warning(
                "[kora.router] low-confidence pattern %r invalid: %r — "
                "skipping",
                src,
                exc,
            )
    return out


def _opus_prefix() -> str:
    return os.environ.get(ENV_OPUS_PREFIX, "/opus").strip() or "/opus"


def strip_opus_prefix(message_text: str) -> str:
    """Return ``message_text`` with the operator's Opus prefix
    removed (if present). Strips leading whitespace + the prefix
    + one trailing space. Idempotent on text without the prefix.

    The engine should call this on the inbound message text BEFORE
    handing it to ``messages.create`` so the routing instruction
    doesn't leak into the prompt and confuse the model.
    """
    if not isinstance(message_text, str):
        return message_text
    prefix = _opus_prefix()
    stripped = message_text.lstrip()
    if stripped.lower().startswith(prefix.lower()):
        # Strip prefix + at most one space after it.
        remainder = stripped[len(prefix) :]
        if remainder.startswith(" "):
            remainder = remainder[1:]
        return remainder
    return message_text


def _has_opus_prefix(message_text: str) -> bool:
    if not isinstance(message_text, str):
        return False
    prefix = _opus_prefix()
    return message_text.lstrip().lower().startswith(prefix.lower())


def select_model_pre_call(
    *,
    message_text: str,
    iteration: int,
    cost_rung: str,
    force_opus_env: Optional[bool] = None,
) -> RoutingDecision:
    """Pre-call model decision.

    Decision order (first match wins):

      1. ``cost_rung == hard_stop_100`` → ``model=None``,
         reason=``cost_ladder_halted``.
      2. ``cost_rung in {warn_75, downshift_90}`` → forced Haiku,
         reason=``cost_clamp:<rung>``. The escalation flag is
         False even if an earning signal also fired — the call
         IS Haiku and that's what telemetry should reflect.
      3. ``force_opus_env`` → Opus, reason=``force_opus_env``.
      4. ``iteration >= 2`` → Opus, reason=``tool_loop_iteration``.
      5. Operator ``/opus`` prefix → Opus, reason=``opus_prefix``.
      6. Decision-language pattern match → Opus,
         reason=``decision_language``.
      7. Default → Haiku, reason=``default_haiku``.

    Args:
        message_text: The raw inbound text. Used for prefix +
            pattern matching. Prefix detection is case-insensitive
            and tolerates leading whitespace.
        iteration: 1-indexed iteration count inside the tool-use
            loop. Iteration 1 is the first API call.
        cost_rung: One of the four CostRung literals.
        force_opus_env: Override for the ``KORA_FORCE_OPUS`` env
            check. ``None`` (default) → read the env. Explicit
            bool used by tests to avoid env mutation.
    """
    if cost_rung == RUNG_HARD_STOP_100:
        return RoutingDecision(
            model=None,
            reason="cost_ladder_halted",
            escalated=False,
        )

    if cost_rung in (RUNG_WARN_75, RUNG_DOWNSHIFT_90):
        return RoutingDecision(
            model=DEFAULT_HAIKU_MODEL,
            reason=f"cost_clamp:{cost_rung}",
            escalated=False,
        )

    if force_opus_env is None:
        force_opus_env = (
            os.environ.get(ENV_FORCE_OPUS, "").strip().lower() == "true"
        )
    if force_opus_env:
        return RoutingDecision(
            model=DEFAULT_OPUS_MODEL,
            reason="force_opus_env",
            escalated=True,
        )

    if iteration >= 2:
        return RoutingDecision(
            model=DEFAULT_OPUS_MODEL,
            reason="tool_loop_iteration",
            escalated=True,
        )

    if _has_opus_prefix(message_text):
        return RoutingDecision(
            model=DEFAULT_OPUS_MODEL,
            reason="opus_prefix",
            escalated=True,
        )

    for pattern in _compiled_decision_patterns():
        if pattern.search(message_text or ""):
            return RoutingDecision(
                model=DEFAULT_OPUS_MODEL,
                reason="decision_language",
                escalated=True,
            )

    return RoutingDecision(
        model=DEFAULT_HAIKU_MODEL,
        reason="default_haiku",
        escalated=False,
    )


# ---------------------------------------------------------------------------
# Post-call escalation
# ---------------------------------------------------------------------------


def should_escalate_post_call(
    *,
    haiku_response_text: str,
    original_message_text: str,
) -> Tuple[bool, str]:
    """Return ``(should_escalate, reason)`` based on the Haiku
    reply. Caller (engine's iteration 1 post-call hook) re-issues
    to Opus with Haiku's response as context when this returns True.

    Reasons:
      - ``low_confidence_marker`` — Haiku used an explicit
        uncertainty phrase.
      - ``short_response_for_long_input`` — Haiku gave a tiny
        reply to a substantive question (heuristic).
      - ``haiku_confident`` (escalate=False) — no signal fired.
    """
    if not isinstance(haiku_response_text, str):
        return (False, "haiku_confident")

    text = haiku_response_text.strip()

    # Length-heuristic FIRST so a long uncertainty-marker-containing
    # response (which is actually fine — Haiku explained the
    # uncertainty thoroughly) doesn't get short-response-tagged.
    # Actually run BOTH; pattern match is cheaper + more specific.
    for pattern in _compiled_low_confidence_patterns():
        if pattern.search(text):
            return (True, "low_confidence_marker")

    if (
        len(original_message_text or "") > _LOW_CONFIDENCE_LONG_INPUT_THRESHOLD
        and len(text) < _LOW_CONFIDENCE_SHORT_RESPONSE_THRESHOLD
    ):
        return (True, "short_response_for_long_input")

    return (False, "haiku_confident")
