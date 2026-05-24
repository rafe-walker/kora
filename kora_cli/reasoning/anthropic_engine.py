"""AnthropicReasoningEngine — KR-FEAT-AI-RESPONSE-LOOP ST1+ST2.

Implements :class:`kora_cli.reasoning.engine.ReasoningEngine` against
Anthropic's Python SDK (``anthropic==0.86.0``, runtime dep — promoted
from extra in ST2 per PM ruling 2026-05-22 since the reasoning_engine
listener imports it unconditionally at boot).

# Credential cascade — OAuth FIRST (PM ruling 2026-05-22 ST2)

Two supported credential sources. **OAuth-first** because Joshua's
Max 20x plan + the post-May-15 SDK billing split route the $200/mo
Agent SDK pool via the OAuth token path. OAuth = production;
API key = fallback for testing / dev / local-without-Max-setup.

  1. ``CLAUDE_CODE_OAUTH_TOKEN`` (if set) → SDK constructed with
     ``auth_token=...``. Billing: Max plan ($200/mo Agent SDK
     pool). Existing Doppler ``kora-runtime-anthropic`` secret.
     **Production path.**
  2. ``KORA_ANTHROPIC_API_KEY`` (fallback) → SDK constructed with
     ``api_key=...``. Billing: Anthropic Console (operator must
     provision an API key separately). Test / dev escape hatch.

Both unset → ``ReasoningEngineNotConfigured`` raised at
construction. **Fail-CLOSED** per
``feedback_fail_closed_by_default_security_infra``.

# Cost-ladder model selection

The active ``CostRung`` (read from the holder, mapped to the
``ConversationContext.current_cost_ladder_rung`` string by the
listener at call time) selects the model:

  - ``"normal"`` → ``claude-opus-4-7``
  - ``"warn_75"`` → ``claude-sonnet-4-6``
  - ``"downshift_90"`` → ``claude-haiku-4-5-20251001``
  - ``"hard_stop_100"`` → refuse with
    ``ResponseResult(error="cost_ladder_halted")`` — no API call

# Operational-state gating

Kora's primary state is also surfaced via ``ConversationContext``.
``paused`` / ``stopped`` → refuse with
``error="operational_state_paused"``. The handler maps this to a
canned acknowledgment so Joshua isn't met with silence during a
pause.

# Per-call timeout + retry

60s per-call timeout. **NO retry on 5xx** (per PM Q3 default —
preserves the cost-ladder budget; retries burn tokens). Single
attempt; failure returns a ResponseResult with the appropriate
``error`` code.

# Credential sanitization

The token NEVER appears in logs, error messages, or
``ResponseResult.error`` codes. A diverse-failure test
(``test_anthropic_engine.py::test_credential_never_in_errors``)
exercises 401 / 429 / 500 / timeout / network-error paths and
asserts the token's env value doesn't appear in any surface.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from kora_cli.reasoning.engine import (
    ConversationContext,
    ConversationTurn,
    IncomingMessage,
    ResponseResult,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_KEY_ENV = "KORA_ANTHROPIC_API_KEY"
OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
SYSTEM_PROMPT_PATH_ENV = "KORA_SYSTEM_PROMPT_PATH"

DEFAULT_SYSTEM_PROMPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "kora_docs"
    / "00_canonical_current_state"
    / "kora_system_prompt.md"
)

DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_OUTPUT_TOKENS = 2048

# KR-FEAT-AGENTIC-REASONING ST1 — safety cap on the tool-use loop.
# Each iteration is a separate Anthropic API roundtrip; 5 covers
# legitimate "check state + check ledger + check sea_tickets" chains
# while bounding tail-latency + cost-ladder burn. Bucket §4 Q2
# locked. Operator-tunable would require config; deferred per Q4.
MAX_TOOL_USE_ITERATIONS = 5

# Model identifiers per the canonical Claude 4.X family. Values
# verified against the project_kora memory + the latest SDK docs.
# Per KR-HAIKU-ROUTER (Lock R3-3), the default model is HAIKU.
# Opus is invoked only when an earning signal fires (router calls
# ``select_model_pre_call`` / ``should_escalate_post_call``).
MODEL_OPUS = "claude-opus-4-7"
MODEL_SONNET = "claude-sonnet-4-6"  # constant kept for grep; no
                                    # active call site post-router.
MODEL_HAIKU = "claude-haiku-4-5-20251001"

# Cost-ladder rung → model mapping. **DEPRECATED post KR-HAIKU-
# ROUTER**. The router (`kora_cli/router/cost_router.py`) takes
# cost_rung as input and clamps to Haiku on WARN_75 / DOWNSHIFT_90,
# fails fast on HARD_STOP_100. The map is preserved at module
# level for backwards-compat with any downstream consumer that
# imports it; the engine no longer consults it.
RUNG_MODEL_MAP: Dict[str, str] = {
    "normal": MODEL_HAIKU,        # was MODEL_OPUS pre-flip
    "warn_75": MODEL_HAIKU,       # was MODEL_SONNET pre-flip
    "downshift_90": MODEL_HAIKU,
    # "hard_stop_100" is special-cased — refuse, no API call.
}


# ---------------------------------------------------------------------------
# KR-CHEAP-PROMPT-CACHING — cacheable input wrappers
# ---------------------------------------------------------------------------


# Per Anthropic API docs:
# https://platform.claude.com/docs/en/agents-and-tools/prompt-caching
# Cache breakpoints are marked with ``cache_control: {"type":
# "ephemeral"}``. The marker on a block caches everything UP TO AND
# INCLUDING that block. We use TWO breakpoints (API allows up to 4):
#   1. The system prompt — static across all calls.
#   2. The tool list — static unless the tool registry mutates.
#
# Cache TTL is ~5 minutes on Anthropic's side. Active reasoning
# sessions hit the warm cache repeatedly (~90% discount on the
# cached portion). Idle agent pays the cache-write premium (~25%
# above base rate) on the first call post-idle, then reads cheap
# until idle again. Net expected effect: ~50% input-cost
# reduction on warm sessions; spec PR body documents the cost
# shape change for operator visibility.


# KR-PLUGIN-EXTRACTIONS-BATCH-2 (Deliverable B) — caching markers
# moved to ``kora_hermes_plugin.caching.markers``. Re-imported here
# so the engine's own ``_make_request_kwargs`` (~line 481) keeps
# resolving the names; canonical location is the plugin module.
from kora_cli.reasoning.kora_hermes_plugin.caching.markers import (  # noqa: E402
    _wrap_system_as_cacheable,
    _wrap_tools_as_cacheable,
)

__all_caching_shim__ = (
    "_wrap_system_as_cacheable",
    "_wrap_tools_as_cacheable",
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ReasoningEngineError(RuntimeError):
    """Base class for engine construction / config failures."""


class ReasoningEngineNotConfigured(ReasoningEngineError):
    """Both credential envs unset. Operator must provision either
    ``KORA_ANTHROPIC_API_KEY`` (Console billing) OR
    ``CLAUDE_CODE_OAUTH_TOKEN`` (Max plan billing) before the
    daemon can register the reasoning listener."""


class ReasoningSystemPromptError(ReasoningEngineError):
    """``kora_system_prompt.md`` missing or unreadable. Fail-CLOSED
    at engine construction — daemon won't register the reasoning
    listener until the prompt loads cleanly."""


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class AnthropicReasoningEngine:
    """Concrete ReasoningEngine backed by the anthropic SDK."""

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        system_prompt_path: Optional[Path] = None,
        # Test seam — inject a pre-configured Anthropic client OR a
        # stand-in (anything with an async ``messages.create``).
        client: Optional[Any] = None,
    ) -> None:
        # Credential cascade — OAuth FIRST (PM ruling 2026-05-22).
        # OAuth = production path (Max plan billing); API key =
        # fallback for dev/testing. Read once at construction
        # (production rotation pattern: redeploy, not hot-reload —
        # same as SlackClient's bot-token model).
        oauth_token = os.environ.get(OAUTH_TOKEN_ENV, "").strip() or None
        api_key = os.environ.get(API_KEY_ENV, "").strip() or None
        if not oauth_token and not api_key:
            raise ReasoningEngineNotConfigured(
                f"both {OAUTH_TOKEN_ENV} and {API_KEY_ENV} are unset — "
                "daemon cannot reason. Set CLAUDE_CODE_OAUTH_TOKEN in "
                "Doppler (kora-runtime-anthropic project) — the "
                "production path via Joshua's Max plan."
            )
        # OAuth wins when both are set.
        self._auth_mode: str = "oauth_token" if oauth_token else "api_key"
        # Stored under underscore-prefixed attrs to discourage casual
        # serialization. NEVER logged.
        self._api_key = api_key
        self._oauth_token = oauth_token

        # System prompt — fail-CLOSED on missing/unreadable.
        prompt_path = system_prompt_path or _resolve_system_prompt_path()
        try:
            self._system_prompt = prompt_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ReasoningSystemPromptError(
                f"system prompt unreadable at {prompt_path}: {exc!r}"
            ) from exc
        if not self._system_prompt.strip():
            raise ReasoningSystemPromptError(
                f"system prompt at {prompt_path} is empty"
            )

        self._timeout = timeout_seconds
        self._max_output_tokens = max_output_tokens
        self._client = client  # lazy-constructed in respond() if None
        self._client_close_method: Optional[Any] = None

    # ------------------------------------------------------------------
    # Public — ReasoningEngine protocol
    # ------------------------------------------------------------------

    async def respond(
        self,
        message: IncomingMessage,
        context: ConversationContext,
    ) -> ResponseResult:
        """Main entry. See :class:`ReasoningEngine.respond`.

        KR-FEAT-AGENTIC-REASONING ST1: drives a tool-use loop
        instead of a single chat completion. The Anthropic API
        returns either a pure-text response (stop_reason="end_turn")
        OR a response containing ``tool_use`` content blocks
        (stop_reason="tool_use"). On tool_use we execute each tool
        in-process via ``execute_reasoning_tool``, append the
        assistant turn + a user turn carrying ``tool_result`` blocks,
        and make the next API call. Loop until pure-text OR
        :data:`MAX_TOOL_USE_ITERATIONS` (5) exceeded.

        Each iteration is a separate Anthropic roundtrip, so a
        Joshua-message-with-3-tools = 4 ``record_inference`` calls
        against the $200/mo Agent SDK pool. PR body documents this
        cost-shape change for operator visibility.

        Tokens are accumulated across iterations; the returned
        ``ResponseResult.input_tokens`` / ``output_tokens`` are
        totals over all roundtrips, NOT just the final one.

        # KR-REASONING-ROUTE-THROUGH-GATEWAY-CORE ST1 — toggle

        When the env ``KORA_REASONING_USE_GATEWAY`` is set to
        ``"true"``, ``respond()`` routes through
        ``_respond_via_gateway()`` (which uses Hermes's
        ``AIAgent.run_conversation`` chokepoint + the new
        ``kora_hermes`` bundled plugin's hook callbacks). Default
        ``false`` preserves the existing bypass path; ST2 wires
        the gateway path's tool plumbing + behavior parity tests
        and flips the default.
        """
        import os

        if (
            os.environ.get("KORA_REASONING_USE_GATEWAY", "")
            .strip()
            .lower()
            == "true"
        ):
            return await self._respond_via_gateway(message, context)

        started_at = time.monotonic()

        # Refuse-paths first — these don't call the SDK.
        if context.current_operational_state in ("paused", "stopped"):
            return ResponseResult(
                text="",
                model_used="",
                input_tokens=0,
                output_tokens=0,
                reasoning_duration_ms=_elapsed_ms(started_at),
                error="operational_state_paused",
            )
        rung = context.current_cost_ladder_rung
        if rung == "hard_stop_100":
            return ResponseResult(
                text="",
                model_used="",
                input_tokens=0,
                output_tokens=0,
                reasoning_duration_ms=_elapsed_ms(started_at),
                error="cost_ladder_halted",
            )

        # KR-HAIKU-ROUTER (Lock R3-3) — model selection moved to
        # the per-iteration router inside ``_tool_use_loop``.
        # ``respond`` no longer pre-resolves a single model; it
        # threads ``cost_rung`` + ``message_text`` into the loop
        # so the router can pick Haiku vs Opus per iteration based
        # on the earning signals + cost-rung backstop.

        # Strip operator ``/opus`` prefix BEFORE building the
        # message history so the routing instruction doesn't leak
        # into the prompt. The prefix's role was detected by the
        # router via the raw text; we hand the cleaned text to the
        # SDK.
        from kora_cli.router import strip_opus_prefix

        message_text_for_router = message.text or ""
        message_text_for_sdk = strip_opus_prefix(message_text_for_router)

        # Reassign ``message.text`` for the history builder. We
        # avoid mutating the input dataclass — build a shallow
        # replacement when the prefix was present.
        if message_text_for_sdk != message_text_for_router:
            from dataclasses import replace as _dc_replace

            message = _dc_replace(message, text=message_text_for_sdk)

        # Assemble the message list (oldest→newest history + fresh
        # inbound as final user turn).
        messages = self._build_message_history(message, context)

        # Reasoning-tools available to Kora — read-only allowlist
        # (KR-FEAT-AGENTIC-REASONING ST1's security boundary). Empty
        # list if the registry can't load for any reason; engine
        # then degrades to flat chat completion + still works.
        try:
            from kora_cli.reasoning.tool_registry import (
                get_reasoning_available_tools,
            )

            tools = get_reasoning_available_tools()
        except Exception as exc:
            logger.warning(
                "[kora.reasoning] tool registry unavailable: %r — "
                "engine running tool-free",
                exc,
            )
            tools = []

        # KR-FEAT-AGENTIC-REASONING ST2 — audit identity derived from
        # the inbound message. ``triggered_by`` matches the source
        # field; ``caller_session_id`` is source-shaped so structured-
        # log analysis can correlate tool calls with the inbound
        # message that caused them.
        triggered_by = message.source
        caller_session_id = _derive_caller_session_id(message)

        client = await self._ensure_client()
        return await self._tool_use_loop(
            client=client,
            messages=messages,
            tools=tools,
            started_at=started_at,
            triggered_by=triggered_by,
            caller_session_id=caller_session_id,
            message_text=message_text_for_router,
            cost_rung=rung or "normal",
            source=message.source,
        )

    async def _tool_use_loop(
        self,
        *,
        client: Any,
        messages: list,
        tools: list,
        started_at: float,
        triggered_by: str = "unknown",
        caller_session_id: str = "",
        message_text: str = "",
        cost_rung: str = "normal",
        source: str = "unknown",
    ) -> ResponseResult:
        """Drive the tool-use roundtrip cascade.

        See ``respond`` docstring for the high-level flow. Token
        accumulation is per-iteration; SDK exceptions on any
        iteration short-circuit to a mapped error result.

        KR-HAIKU-ROUTER (Lock R3-3): model selection is per-
        iteration via the router. ``message_text`` + ``cost_rung``
        are inputs to the router's earning-signal decision tree.
        Iteration 1 may post-call-escalate to Opus when Haiku's
        response is low-confidence — the escalation re-issues the
        API call with Haiku's response woven into the messages so
        Opus has context rather than starting cold.

        ``source`` is used as the telemetry route literal (mapped
        via ``_source_to_telemetry_route``); subsequent iterations
        switch to ``ROUTE_TOOL_LOOP_ITERATION``.
        """
        from kora_cli.router import (
            DEFAULT_OPUS_MODEL,
            RoutingDecision,
            select_model_pre_call,
            should_escalate_post_call,
        )
        # Accumulators across iterations.
        total_input_tokens = 0
        total_output_tokens = 0
        # KR-CHEAP-PROMPT-CACHING — accumulate cache-write +
        # cache-read tokens separately. The SDK surfaces them as
        # ``usage.cache_creation_input_tokens`` (full-rate, billed
        # 1x base + ~25% write premium per Anthropic pricing) and
        # ``usage.cache_read_input_tokens`` (~90% discount vs base).
        # Handler reads ResponseResult.cache_* and bills via
        # CanonicalUsage(cache_write_tokens, cache_read_tokens) so
        # the cost-ladder PricingEntry's cache_*_cost_per_million
        # multipliers apply correctly.
        total_cache_creation_tokens = 0
        total_cache_read_tokens = 0
        # Track which tools Kora actually used — surfaced to the
        # handler via ResponseResult.tools_used (ST2 wires it into
        # the outbound JSONL; ST1 ships the field on the result
        # class so handler/test consumers don't churn between STs).
        tools_used: list[str] = []

        # KR-CHEAP-PROMPT-CACHING — build cacheable system block +
        # cacheable tool list ONCE outside the loop. Each iteration
        # sends the SAME structures so Anthropic recognizes the
        # cache key + hits the warm cache after the first roundtrip.
        # The ``cache_control: {type: ephemeral}`` marker covers
        # everything UP TO AND INCLUDING the block it's attached to.
        system_blocks = _wrap_system_as_cacheable(self._system_prompt)
        cacheable_tools = _wrap_tools_as_cacheable(tools)

        # Last-known model from the most recent SDK call — needed for
        # ResponseResult.model_used + for ``_map_sdk_exception`` if
        # any iteration raises mid-loop. Initialized to Haiku (the
        # router's default) so a pre-iteration failure is attributed
        # honestly to the default model.
        last_model = "claude-haiku-4-5-20251001"

        for iteration in range(1, MAX_TOOL_USE_ITERATIONS + 1):
            # KR-HAIKU-ROUTER — per-iteration model decision.
            decision = select_model_pre_call(
                message_text=message_text,
                iteration=iteration,
                cost_rung=cost_rung,
            )
            if decision.model is None:
                # cost_rung == hard_stop_100 — caller (respond) already
                # short-circuits on this rung BEFORE entering the loop,
                # so reaching here means the rung flipped mid-loop
                # (unlikely but possible). Fail-fast with the same
                # error code respond() uses.
                return ResponseResult(
                    text="",
                    model_used=last_model,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                    reasoning_duration_ms=_elapsed_ms(started_at),
                    error="cost_ladder_halted",
                    tools_used=tools_used,
                    cache_creation_input_tokens=total_cache_creation_tokens,
                    cache_read_input_tokens=total_cache_read_tokens,
                )
            current_model = decision.model
            last_model = current_model

            try:
                # tools= is optional per the SDK; omit when the
                # registry returned empty so we don't send an empty
                # array (some Anthropic SDK versions are strict).
                kwargs: Dict[str, Any] = {
                    "model": current_model,
                    "system": system_blocks,
                    "messages": messages,
                    "max_tokens": self._max_output_tokens,
                    "timeout": self._timeout,
                }
                if cacheable_tools:
                    kwargs["tools"] = cacheable_tools
                response = await client.messages.create(**kwargs)
            except Exception as exc:
                return self._map_sdk_exception(
                    exc, model=current_model, started_at=started_at
                )

            # Per-iteration token accumulation.
            usage = getattr(response, "usage", None)
            iter_input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            iter_output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            iter_cache_creation = int(
                getattr(usage, "cache_creation_input_tokens", 0) or 0
            )
            iter_cache_read = int(
                getattr(usage, "cache_read_input_tokens", 0) or 0
            )
            total_input_tokens += iter_input_tokens
            total_output_tokens += iter_output_tokens
            total_cache_creation_tokens += iter_cache_creation
            total_cache_read_tokens += iter_cache_read

            # KR-HAIKU-ROUTER — telemetry per API call.
            self._record_call_to_telemetry(
                source=source,
                iteration=iteration,
                model=current_model,
                escalated=decision.escalated,
                iter_input_tokens=iter_input_tokens,
                iter_output_tokens=iter_output_tokens,
                iter_cache_creation=iter_cache_creation,
                iter_cache_read=iter_cache_read,
            )

            # KR-HAIKU-ROUTER post-call escalation — only on
            # iteration 1, only if we didn't ALREADY escalate
            # (i.e. iteration 1 was Haiku per default), and only
            # when stop_reason is end_turn (a tool-use response
            # isn't a candidate for escalation; the iteration-2
            # signal already gives the next call Opus).
            stop_reason_for_escalation_check = getattr(
                response, "stop_reason", None
            )
            if (
                iteration == 1
                and not decision.escalated
                and stop_reason_for_escalation_check == "end_turn"
            ):
                haiku_text = self._extract_text_from_response(response)
                should_esc, esc_reason = should_escalate_post_call(
                    haiku_response_text=haiku_text,
                    original_message_text=message_text,
                )
                if should_esc:
                    # Re-issue iteration 1 to Opus with Haiku's
                    # response in the conversation history. Opus
                    # sees the prior attempt + a follow-up user
                    # turn asking it to review/improve. Cheaper
                    # than a cold-call Opus because the system
                    # prompt + tool cache is still warm.
                    escalation_messages = list(messages) + [
                        {
                            "role": "assistant",
                            "content": haiku_text,
                        },
                        {
                            "role": "user",
                            "content": (
                                "Please review my last response and "
                                "improve it if needed. Be terse if "
                                "you'd just confirm it."
                            ),
                        },
                    ]
                    escalation_kwargs = dict(kwargs)
                    escalation_kwargs["model"] = DEFAULT_OPUS_MODEL
                    escalation_kwargs["messages"] = escalation_messages
                    try:
                        response = await client.messages.create(
                            **escalation_kwargs
                        )
                    except Exception as exc:
                        # Escalation failed — surface honestly.
                        # Haiku tokens already accumulated; Opus
                        # call burned no tokens.
                        return self._map_sdk_exception(
                            exc,
                            model=DEFAULT_OPUS_MODEL,
                            started_at=started_at,
                        )
                    last_model = DEFAULT_OPUS_MODEL
                    # Accumulate Opus tokens too (BOTH paths billed
                    # — that's the cost of escalation).
                    usage = getattr(response, "usage", None)
                    iter_input_tokens = int(
                        getattr(usage, "input_tokens", 0) or 0
                    )
                    iter_output_tokens = int(
                        getattr(usage, "output_tokens", 0) or 0
                    )
                    iter_cache_creation = int(
                        getattr(usage, "cache_creation_input_tokens", 0)
                        or 0
                    )
                    iter_cache_read = int(
                        getattr(usage, "cache_read_input_tokens", 0) or 0
                    )
                    total_input_tokens += iter_input_tokens
                    total_output_tokens += iter_output_tokens
                    total_cache_creation_tokens += iter_cache_creation
                    total_cache_read_tokens += iter_cache_read
                    # Update decision to reflect the escalated path
                    # (for any downstream logic that checks it).
                    decision = RoutingDecision(
                        model=DEFAULT_OPUS_MODEL,
                        reason=f"escalated_post_haiku:{esc_reason}",
                        escalated=True,
                        haiku_context_for_opus=haiku_text,
                    )
                    self._record_call_to_telemetry(
                        source=source,
                        iteration=iteration,
                        model=DEFAULT_OPUS_MODEL,
                        escalated=True,
                        iter_input_tokens=iter_input_tokens,
                        iter_output_tokens=iter_output_tokens,
                        iter_cache_creation=iter_cache_creation,
                        iter_cache_read=iter_cache_read,
                    )

            # Detect tool-use vs end-of-turn. Anthropic SDK sets
            # ``response.stop_reason`` to one of:
            #   "end_turn" / "max_tokens" / "stop_sequence" / "tool_use"
            stop_reason = getattr(response, "stop_reason", None)
            if stop_reason != "tool_use":
                # Done — final text response. Project + return.
                return self._project_final_response(
                    response,
                    model=last_model,
                    started_at=started_at,
                    total_input_tokens=total_input_tokens,
                    total_output_tokens=total_output_tokens,
                    total_cache_creation_tokens=total_cache_creation_tokens,
                    total_cache_read_tokens=total_cache_read_tokens,
                    tools_used=tools_used,
                )

            # Tool-use iteration. Extract ``tool_use`` blocks +
            # the assistant's interleaved text (preserved in the
            # assistant turn we'll echo back).
            tool_use_blocks = self._extract_tool_use_blocks(response)
            if not tool_use_blocks:
                # Defensive: stop_reason said tool_use but no blocks.
                # Treat as end-of-turn so we don't loop forever.
                logger.warning(
                    "[kora.reasoning] stop_reason=tool_use but no "
                    "tool_use blocks — projecting as final"
                )
                return self._project_final_response(
                    response,
                    model=last_model,
                    started_at=started_at,
                    total_input_tokens=total_input_tokens,
                    total_output_tokens=total_output_tokens,
                    total_cache_creation_tokens=total_cache_creation_tokens,
                    total_cache_read_tokens=total_cache_read_tokens,
                    tools_used=tools_used,
                )

            # Append the assistant turn verbatim (with tool_use
            # blocks) so the next API call sees what Claude said.
            assistant_content = self._response_content_as_blocks(response)
            messages.append(
                {"role": "assistant", "content": assistant_content}
            )

            # Execute each tool + build tool_result content blocks.
            tool_result_blocks = await self._execute_tool_calls(
                tool_use_blocks,
                tools_used=tools_used,
                triggered_by=triggered_by,
                caller_session_id=caller_session_id,
            )
            messages.append(
                {"role": "user", "content": tool_result_blocks}
            )

            # Loop — next API call will see the tool results.
            continue

        # Loop exited via for-else — max iterations exceeded.
        logger.warning(
            "[kora.reasoning] tool-use loop hit MAX_TOOL_USE_ITERATIONS "
            "(%d) — returning error result",
            MAX_TOOL_USE_ITERATIONS,
        )
        return ResponseResult(
            text="",
            model_used=last_model,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            reasoning_duration_ms=_elapsed_ms(started_at),
            error="tool_use_max_iterations_exceeded",
            tools_used=tools_used,
            cache_creation_input_tokens=total_cache_creation_tokens,
            cache_read_input_tokens=total_cache_read_tokens,
        )

    @staticmethod
    def _extract_text_from_response(response: Any) -> str:
        """Concatenate all ``text`` blocks from an SDK response.

        Used by KR-HAIKU-ROUTER post-call escalation to capture
        Haiku's response for the Opus re-issue's context. Returns
        the empty string when the response has no text blocks
        (e.g. the response was purely tool_use blocks, which we
        don't escalate on).
        """
        parts: list[str] = []
        try:
            for block in getattr(response, "content", None) or []:
                if getattr(block, "type", "") == "text":
                    parts.append(getattr(block, "text", "") or "")
        except Exception:
            return ""
        return "".join(parts).strip()

    def _record_call_to_telemetry(
        self,
        *,
        source: str,
        iteration: int,
        model: str,
        escalated: bool,
        iter_input_tokens: int,
        iter_output_tokens: int,
        iter_cache_creation: int,
        iter_cache_read: int,
    ) -> None:
        """Record one API roundtrip's worth of usage to the
        per-route cost telemetry (PR #161). Best-effort — any
        failure logs + swallows so the hot reasoning path never
        sees a telemetry exception.

        Iteration 1 maps to the source's natural route
        (``slack_dm`` / ``email_inbound`` / ``mcp_tool``);
        iteration 2+ buckets to ``tool_loop_iteration`` so the
        cockpit can see Opus-on-iteration vs Opus-on-escalation
        spend as distinct slices.
        """
        try:
            from agent.usage_pricing import (
                CanonicalUsage,
                estimate_usage_cost,
            )
            from kora_cli.telemetry import (
                ROUTE_EMAIL_INBOUND,
                ROUTE_MCP_TOOL,
                ROUTE_SLACK_DM,
                ROUTE_TOOL_LOOP_ITERATION,
                ROUTE_UNKNOWN,
                get_telemetry,
            )
        except Exception as exc:
            logger.debug(
                "[kora.reasoning.telemetry] import failed: %r — skipping",
                exc,
            )
            return

        if iteration >= 2:
            route = ROUTE_TOOL_LOOP_ITERATION
        else:
            route = {
                "slack_dm": ROUTE_SLACK_DM,
                "email": ROUTE_EMAIL_INBOUND,
                "mcp": ROUTE_MCP_TOOL,
            }.get(source, ROUTE_UNKNOWN)

        usage = CanonicalUsage(
            input_tokens=iter_input_tokens,
            output_tokens=iter_output_tokens,
            cache_read_tokens=iter_cache_read,
            cache_write_tokens=iter_cache_creation,
        )

        cost_estimate: Any = None
        try:
            cost_result = estimate_usage_cost(
                model_name=model,
                usage=usage,
                provider="anthropic",
            )
            if cost_result.amount_usd is not None:
                cost_estimate = float(cost_result.amount_usd)
        except Exception as exc:
            logger.debug(
                "[kora.reasoning.telemetry] estimate_usage_cost raised "
                "%r — cost_estimate omitted",
                exc,
            )

        try:
            get_telemetry().record_call(
                route=route,
                model=model,
                canonical_usage=usage,
                cost_estimate_usd=cost_estimate,
                escalated_to_opus=escalated,
            )
        except Exception as exc:
            logger.debug(
                "[kora.reasoning.telemetry] record_call raised %r — "
                "counters not updated",
                exc,
            )

    def _extract_tool_use_blocks(self, response: Any) -> list:
        """Return the list of ``tool_use`` content blocks from an
        Anthropic SDK response. Each block has ``.id``, ``.name``,
        ``.input`` attributes (typed ``ToolUseBlock``).
        """
        out = []
        try:
            content = getattr(response, "content", None) or []
            for block in content:
                if getattr(block, "type", "") == "tool_use":
                    out.append(block)
        except Exception as exc:
            logger.warning(
                "[kora.reasoning] tool_use extraction failed: %r", exc
            )
        return out

    def _response_content_as_blocks(self, response: Any) -> list:
        """Re-serialize an assistant response's content as a list of
        block dicts suitable for the next ``messages.create`` call's
        history. The SDK's response blocks are typed objects; the
        ``messages=`` history accepts dicts.
        """
        blocks: list = []
        try:
            for block in getattr(response, "content", None) or []:
                btype = getattr(block, "type", None)
                if btype == "text":
                    blocks.append(
                        {"type": "text", "text": getattr(block, "text", "")}
                    )
                elif btype == "tool_use":
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": getattr(block, "id", ""),
                            "name": getattr(block, "name", ""),
                            "input": getattr(block, "input", {}) or {},
                        }
                    )
                # Other block types (thinking / etc.) are dropped —
                # they're not part of the reasoning-tool contract.
        except Exception as exc:
            logger.warning(
                "[kora.reasoning] response content re-serialization "
                "failed: %r — appending empty content",
                exc,
            )
        return blocks

    async def _execute_tool_calls(
        self,
        tool_use_blocks: list,
        *,
        tools_used: list[str],
        triggered_by: str = "unknown",
        caller_session_id: str = "",
    ) -> list:
        """Run each tool_use block + return the matching
        ``tool_result`` blocks for the next user turn.

        KR-FEAT-AGENTIC-REASONING ST2 — every call emits a
        ``[kora.reasoning.tool_called]`` structured-log entry with
        tool_name / triggered_by / caller_session_id /
        tool_duration_ms / tool_status (ok/not_allowed/execution_error).
        Operator finds these via flyctl logs or the REASONING-PANEL.

        Failure modes (each becomes a ``tool_result`` with
        ``is_error: true``):

          - Tool not in reasoning allowlist → tool_status="not_allowed"
          - Tool execution exception → tool_status="execution_error"
          - Tool returned non-serializable result (shouldn't happen
            with Pydantic models but guarded as execution_error)

        Tool exceptions become tool_result errors — they do NOT
        propagate. The engine can recover by letting Claude reason
        about the error in the next iteration.
        """
        import json
        import time as _time

        from kora_cli.reasoning.tool_registry import (
            ReasoningToolNotAllowed,
            execute_reasoning_tool,
        )

        results: list = []
        for block in tool_use_blocks:
            tool_use_id = getattr(block, "id", "")
            tool_name = getattr(block, "name", "")
            tool_input = getattr(block, "input", {}) or {}
            call_started_at = _time.monotonic()

            try:
                result_model = await execute_reasoning_tool(
                    name=tool_name, tool_input=tool_input
                )
                # Pydantic BaseModel → JSON string for tool_result text.
                result_text = (
                    result_model.model_dump_json()
                    if hasattr(result_model, "model_dump_json")
                    else json.dumps(result_model, default=str)
                )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": result_text,
                    }
                )
                tools_used.append(tool_name)
                _emit_tool_called_audit(
                    tool_name=tool_name,
                    triggered_by=triggered_by,
                    caller_session_id=caller_session_id,
                    tool_duration_ms=_elapsed_ms(call_started_at),
                    tool_status="ok",
                )
            except ReasoningToolNotAllowed:
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": (
                            f"tool_not_allowed: {tool_name!r} is not in "
                            f"the reasoning allowlist"
                        ),
                        "is_error": True,
                    }
                )
                _emit_tool_called_audit(
                    tool_name=tool_name,
                    triggered_by=triggered_by,
                    caller_session_id=caller_session_id,
                    tool_duration_ms=_elapsed_ms(call_started_at),
                    tool_status="not_allowed",
                )
            except Exception as exc:
                # ANY other exception → tool_result error. Engine
                # continues; Claude can reason about the failure.
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": (
                            f"tool_execution_error: {type(exc).__name__}"
                        ),
                        "is_error": True,
                    }
                )
                _emit_tool_called_audit(
                    tool_name=tool_name,
                    triggered_by=triggered_by,
                    caller_session_id=caller_session_id,
                    tool_duration_ms=_elapsed_ms(call_started_at),
                    tool_status="execution_error",
                    exc_type=type(exc).__name__,
                )
        return results

    def _project_final_response(
        self,
        response: Any,
        *,
        model: str,
        started_at: float,
        total_input_tokens: int,
        total_output_tokens: int,
        total_cache_creation_tokens: int,
        total_cache_read_tokens: int,
        tools_used: list[str],
    ) -> ResponseResult:
        """Multi-iteration variant of :meth:`_project_response`.

        Token totals are passed in (accumulated across all
        iterations) rather than read from the final response's
        usage block — important since intermediate roundtrips
        billed tokens too. Cache totals are surfaced separately
        so the handler can bill them at cache_read / cache_write
        rates rather than the full input-token rate.
        """
        text_parts: list[str] = []
        try:
            for block in getattr(response, "content", None) or []:
                if getattr(block, "type", "") == "text":
                    text_parts.append(getattr(block, "text", "") or "")
        except Exception as exc:
            logger.warning(
                "[kora.reasoning] final response content projection "
                "failed: %r",
                exc,
            )
            return ResponseResult(
                text="",
                model_used=getattr(response, "model", model) or model,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                reasoning_duration_ms=_elapsed_ms(started_at),
                error="response_projection_failed",
                tools_used=tools_used,
                cache_creation_input_tokens=total_cache_creation_tokens,
                cache_read_input_tokens=total_cache_read_tokens,
            )

        text = "".join(text_parts).strip()
        return ResponseResult(
            text=text,
            model_used=getattr(response, "model", model) or model,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            reasoning_duration_ms=_elapsed_ms(started_at),
            error=None,
            tools_used=tools_used,
            cache_creation_input_tokens=total_cache_creation_tokens,
            cache_read_input_tokens=total_cache_read_tokens,
        )

    # ------------------------------------------------------------------
    # KR-REASONING-ROUTE-THROUGH-GATEWAY-CORE ST1 — toggle path
    # ------------------------------------------------------------------

    async def _respond_via_gateway(
        self,
        message: IncomingMessage,
        context: ConversationContext,
    ) -> ResponseResult:
        """Route Kora's reply through Hermes's ``AIAgent.run_
        conversation`` chokepoint instead of the direct
        ``messages.create`` bypass.

        # ST2 — actual wire-up

        Builds a fresh ``AIAgent`` per call (one-shot reply
        pattern; AIAgent ctor is cheap — no I/O), pins
        ``max_iterations=5`` to match Kora's existing
        ``MAX_TOOL_USE_ITERATIONS`` (the spec's critical pin —
        Hermes default 90 would quintuple monthly cost if Kora
        silently inherited it), sets ``agent.route`` so
        ``kora_hermes`` plugin hooks fire correctly, then calls
        ``run_conversation`` + projects the result dict back
        into Kora's :class:`ResponseResult` shape.

        # Tools

        ST2 ships **toolless** route-through:
        ``agent.tools = []`` overrides Hermes's auto-loaded
        default toolset. Kora's reasoning tools live in a
        parallel registry (``kora_cli/reasoning/tool_registry``)
        and aren't bridged into Hermes's tool dispatch yet —
        that's the explicit ST2B follow-on. While the toggle is
        OFF in production (default), the bypass path still has
        full tool capability; toggling ON gives toolless
        reasoning, which is acceptable for the parity-validation
        phase the bucket requires.

        # ResponseResult projection

        ``run_conversation`` returns a dict with keys
        ``final_response`` / ``model`` / ``input_tokens`` /
        ``output_tokens`` / ``cache_read_tokens`` /
        ``cache_write_tokens`` / ``completed`` / ``interrupted``.
        We map these into the dataclass + derive
        ``reasoning_duration_ms`` from start/end timestamps;
        ``tools_used`` defaults to ``[]`` (toolless v1);
        ``error`` is set when ``completed`` is False AND
        ``interrupted`` is True.
        """
        import time as _time

        started_at = _time.monotonic()

        # Source → route mapping (uses ST1 helper).
        route = _source_to_kora_route(getattr(message, "source", ""))

        # Refuse-paths — preserve the bypass semantic so the toggle
        # is behavior-neutral on these paths.
        if context.current_operational_state in ("paused", "stopped"):
            return ResponseResult(
                text="",
                model_used="",
                input_tokens=0,
                output_tokens=0,
                reasoning_duration_ms=_elapsed_ms(started_at),
                error="operational_state_paused",
            )
        if context.current_cost_ladder_rung == "hard_stop_100":
            return ResponseResult(
                text="",
                model_used="",
                input_tokens=0,
                output_tokens=0,
                reasoning_duration_ms=_elapsed_ms(started_at),
                error="cost_ladder_halted",
            )

        # Build AIAgent. Keep ctor kwargs to the minimum that
        # works for Kora — see PR body's mapping table for what
        # we explicitly set vs accept from Hermes defaults. The
        # critical pin is ``max_iterations=5``; Hermes default
        # is 90 (~18x looser bound on tool-use depth) which
        # would re-introduce the cost-shape problem KR-HAIKU-
        # ROUTER #165 closed.
        from run_agent import AIAgent

        agent = AIAgent(
            model=MODEL_HAIKU,  # default; plugin's
                                 # pre_api_request_mutable hook
                                 # overrides per-call based on
                                 # cost-router's earning-signal
                                 # decision
            provider="anthropic",
            api_mode="anthropic_messages",
            max_iterations=MAX_TOOL_USE_ITERATIONS,  # 5 — PIN
            max_tokens=self._max_output_tokens,
            quiet_mode=True,  # daemon path; no print() to stdout
        )
        # KR-REASONING-ROUTE-THROUGH-GATEWAY-ST2B — populate
        # agent.tools from Kora's reasoning registry (replaces
        # ST2's ``agent.tools = []`` toolless-only posture). The
        # kora_hermes plugin's ``pre_tool_call_can_provide_result``
        # hook intercepts Hermes's dispatch for these tool names
        # and routes them to Kora's existing reasoning dispatch
        # (``execute_reasoning_tool``). Empty list — registry
        # unavailable / failed import — falls back to toolless
        # route-through same as ST2.
        try:
            from plugins.kora_hermes import get_kora_tools_for_agent

            kora_tools = get_kora_tools_for_agent()
            agent.tools = kora_tools
            agent.valid_tool_names = {
                t["function"]["name"]
                for t in kora_tools
                if isinstance(t, dict) and "function" in t
            }
        except Exception as exc:
            logger.warning(
                "[kora.reasoning.gateway] tool-bridge tool population "
                "failed: %r — falling back to toolless route-through",
                exc,
            )
            agent.tools = []
            agent.valid_tool_names = set()

        # Route field threading — kora_hermes plugin's hooks gate
        # on this. Setting it to "" (when source isn't mapped)
        # makes the plugin no-op on this call, matching ST1
        # semantic.
        agent.route = route

        # Drive the conversation. ``run_conversation`` runs sync
        # inside Hermes today (no async variant); offload to a
        # thread so we don't block the asyncio event loop the
        # daemon runs on.
        import asyncio

        try:
            result_dict = await asyncio.to_thread(
                agent.run_conversation,
                message.text or "",
                self._system_prompt,
            )
        except Exception as exc:
            logger.exception(
                "[kora.reasoning.gateway] run_conversation raised: %r",
                exc,
            )
            return ResponseResult(
                text="",
                model_used=getattr(agent, "model", "") or "",
                input_tokens=0,
                output_tokens=0,
                reasoning_duration_ms=_elapsed_ms(started_at),
                error=f"gateway_exception:{type(exc).__name__}",
            )

        # Project run_conversation's dict → ResponseResult.
        return _project_gateway_result(
            result_dict,
            started_at=started_at,
            fallback_model=getattr(agent, "model", "") or "",
        )

    async def close(self) -> None:
        """Close any open HTTP client. Idempotent."""
        if self._client is None:
            return
        # The Anthropic SDK's AsyncClient has a close() coroutine.
        close = getattr(self._client, "close", None)
        if close is not None:
            try:
                result = close()
                if hasattr(result, "__await__"):
                    await result
            except Exception as exc:
                logger.warning(
                    "[kora.reasoning] close raised %r — continuing", exc
                )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        # Import + construct here (not at module-import) so daemon
        # `kora --help` stays fast + tests that mock-out the SDK
        # never see the real client created.
        from anthropic import AsyncAnthropic

        # OAuth-first per PM ruling: prefer Max-plan billing path.
        # Falls back to API key only when OAuth is absent.
        if self._oauth_token:
            self._client = AsyncAnthropic(
                auth_token=self._oauth_token, timeout=self._timeout
            )
        else:
            self._client = AsyncAnthropic(
                api_key=self._api_key, timeout=self._timeout
            )
        return self._client

    def _build_message_history(
        self,
        message: IncomingMessage,
        context: ConversationContext,
    ) -> List[Dict[str, str]]:
        """Convert ConversationContext.recent_messages + the fresh
        IncomingMessage into the SDK's ``messages`` list shape.

        Direction → role:
          - ``inbound`` → ``"user"`` (Joshua said it)
          - ``outbound`` → ``"assistant"`` (Kora said it)

        Order: oldest → newest. The latest item is the FRESH
        ``IncomingMessage`` as a final user turn — that's what
        Kora is responding to.

        Constraint: Anthropic API requires alternating user /
        assistant turns. If the loader returned non-alternating
        history (rare but possible after race conditions), we
        collapse consecutive same-role turns by concatenation
        so the SDK doesn't reject.
        """
        history: List[Dict[str, str]] = []
        for turn in context.recent_messages:
            role = "user" if turn.direction == "inbound" else "assistant"
            if history and history[-1]["role"] == role:
                # Same-role consecutive — concatenate.
                history[-1]["content"] = (
                    history[-1]["content"] + "\n\n" + turn.text
                )
            else:
                history.append({"role": role, "content": turn.text})

        # Append fresh inbound. If the last history turn is already
        # "user", concatenate (covers the case where the loader
        # included the fresh inbound in the history slice).
        if history and history[-1]["role"] == "user":
            history[-1]["content"] = (
                history[-1]["content"] + "\n\n" + message.text
            )
        else:
            history.append({"role": "user", "content": message.text})

        return history

    def _project_response(
        self,
        response: Any,
        *,
        model: str,
        started_at: float,
    ) -> ResponseResult:
        """Map an SDK ``Message`` response → ``ResponseResult``."""
        # SDK shape: response.content is a list of content blocks;
        # for text-only responses, content[0].text is the reply.
        text_parts: List[str] = []
        try:
            for block in response.content:
                # Anthropic SDK: TextBlock has .type=="text" + .text.
                if getattr(block, "type", "") == "text":
                    text_parts.append(getattr(block, "text", "") or "")
        except Exception as exc:
            logger.warning(
                "[kora.reasoning] response content projection failed: %r",
                exc,
            )
            return ResponseResult(
                text="",
                model_used=getattr(response, "model", model) or model,
                input_tokens=0,
                output_tokens=0,
                reasoning_duration_ms=_elapsed_ms(started_at),
                error="response_projection_failed",
            )

        text = "".join(text_parts).strip()

        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)

        return ResponseResult(
            text=text,
            model_used=getattr(response, "model", model) or model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_duration_ms=_elapsed_ms(started_at),
            error=None,
        )

    def _map_sdk_exception(
        self,
        exc: BaseException,
        *,
        model: str,
        started_at: float,
    ) -> ResponseResult:
        """Map an Anthropic SDK exception → stable ``error`` code.

        NEVER includes the credential in the error code — only the
        exception class name + an HTTP-status hint when available.
        """
        # Avoid hard-importing anthropic.* exception classes at module
        # top (lazy-loaded by _ensure_client). Use duck-typing on
        # attribute presence.
        status_code = getattr(exc, "status_code", None)
        exc_name = type(exc).__name__

        if status_code == 401 or status_code == 403:
            error = "sdk_auth"
        elif status_code == 429:
            error = "sdk_rate_limited"
        elif status_code is not None and 500 <= int(status_code) < 600:
            error = "sdk_5xx"
        elif status_code is not None and 400 <= int(status_code) < 500:
            error = f"sdk_4xx_{status_code}"
        elif exc_name in ("APITimeoutError", "Timeout", "TimeoutException"):
            error = "sdk_timeout"
        elif "Connection" in exc_name or "Transport" in exc_name:
            error = "sdk_transport"
        else:
            # Generic — log the exception class + a sanitized
            # message snippet (NEVER the credential).
            error = f"sdk_unknown_{exc_name}"

        logger.warning(
            "[kora.reasoning] SDK call failed model=%s error=%s "
            "exc_type=%s",
            model,
            error,
            exc_name,
        )
        return ResponseResult(
            text="",
            model_used=model,
            input_tokens=0,
            output_tokens=0,
            reasoning_duration_ms=_elapsed_ms(started_at),
            error=error,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_system_prompt_path() -> Path:
    """Env override → in-repo default. Mirrors the SlackDMHandler
    log-path pattern."""
    override = os.environ.get(SYSTEM_PROMPT_PATH_ENV, "").strip()
    if override:
        return Path(override)
    return DEFAULT_SYSTEM_PROMPT_PATH


def _elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


# ---------------------------------------------------------------------------
# KR-REASONING-ROUTE-THROUGH-GATEWAY-CORE ST1 — source → route mapping
# ---------------------------------------------------------------------------


# Map ``IncomingMessage.source`` literals (slack_dm / email / mcp) to
# the canonical Kora route taxonomy used by cost_telemetry +
# kora_hermes plugin's KORA_ROUTES gate. Per the bucket spec §2(c).
_SOURCE_TO_ROUTE: dict = {
    "slack_dm": "slack_dm",
    "email": "email_inbound",
    "mcp": "mcp_tool",
    # The remaining route literals (alert_investigation,
    # probe_investigation, tool_loop_iteration, scheduled_task) are
    # set by their respective callers BEFORE invoking the engine —
    # see e.g. ``probe_wake_consumer.py``. We pass through any
    # already-set route literal verbatim.
}


def _source_to_kora_route(source: str) -> str:
    """Resolve a Kora route literal from an inbound source.

    Returns ``""`` (empty) when the source isn't mapped — that's
    the kora_hermes plugin's "no-op" sentinel. A future caller
    that wants to tag a route explicitly (probe-wake / cron /
    alert-investigation) can either set ``agent.route`` directly
    after construction OR extend this map.
    """
    if not isinstance(source, str):
        return ""
    return _SOURCE_TO_ROUTE.get(source, "")


# ---------------------------------------------------------------------------
# KR-REASONING-ROUTE-THROUGH-GATEWAY ST2 — result projection
# ---------------------------------------------------------------------------


def _project_gateway_result(
    result_dict: Dict[str, Any],
    *,
    started_at: float,
    fallback_model: str,
) -> ResponseResult:
    """Project Hermes ``run_conversation``'s return dict into
    Kora's :class:`ResponseResult` dataclass.

    Mapping (per agent/conversation_loop.py:4077-4102):
      * ``final_response`` → ``text``
      * ``model`` → ``model_used`` (with ``fallback_model`` if
        absent / falsy)
      * ``input_tokens`` / ``output_tokens`` → same
      * ``cache_write_tokens`` → ``cache_creation_input_tokens``
        (name differs; same concept — Hermes uses cache_write,
        Kora uses cache_creation_input per the Anthropic SDK
        usage object's field naming)
      * ``cache_read_tokens`` → ``cache_read_input_tokens``
      * ``completed`` + ``interrupted`` → ``error`` (set to
        ``"gateway_interrupted"`` when interrupted; ``None``
        when completed cleanly; ``"gateway_incomplete"`` for
        the rare uncompleted-uninterrupted case)

    ``tools_used`` defaults to ``[]`` — ST2 route-through is
    toolless; ST2B's tool-bridge wires this from the messages
    history.
    """
    if not isinstance(result_dict, dict):
        return ResponseResult(
            text="",
            model_used=fallback_model,
            input_tokens=0,
            output_tokens=0,
            reasoning_duration_ms=_elapsed_ms(started_at),
            error="gateway_returned_non_dict",
        )

    text = str(result_dict.get("final_response", "") or "")
    model_used = (
        result_dict.get("model")
        or fallback_model
        or ""
    )

    completed = bool(result_dict.get("completed", False))
    interrupted = bool(result_dict.get("interrupted", False))
    if interrupted:
        error = "gateway_interrupted"
    elif not completed:
        error = "gateway_incomplete"
    else:
        error = None

    return ResponseResult(
        text=text,
        model_used=str(model_used),
        input_tokens=int(result_dict.get("input_tokens", 0) or 0),
        output_tokens=int(result_dict.get("output_tokens", 0) or 0),
        reasoning_duration_ms=_elapsed_ms(started_at),
        error=error,
        tools_used=[],  # ST2B will populate from messages history
        cache_creation_input_tokens=int(
            result_dict.get("cache_write_tokens", 0) or 0
        ),
        cache_read_input_tokens=int(
            result_dict.get("cache_read_tokens", 0) or 0
        ),
    )


# ---------------------------------------------------------------------------
# KR-FEAT-AGENTIC-REASONING ST2 — audit + caller identity
# ---------------------------------------------------------------------------


def _derive_caller_session_id(message: IncomingMessage) -> str:
    """Build a stable session-id string for audit correlation.

    Per-source shape (chosen so structured-log analysis can join
    reasoning audit lines back to the inbound JSONL entry that
    triggered the reasoning):

      - ``slack_dm``: ``"{channel_id}:{event_ts}"`` — matches
        the inbound JSONL ``channel_id`` + ``event_ts`` pair.
      - ``email``: ``"email:{message_id}"`` — Purelymail
        message-id per the inbound webhook payload.
      - ``mcp``: ``"mcp:{caller_actor_kind}:{tool_name}"`` —
        identifies which MCP caller triggered which Kora tool.
      - other / missing metadata: ``"unknown"`` fallback.

    NEVER logs raw token / credential material — only stable
    public identifiers (channel ids, message ids, actor kinds).
    """
    meta = message.metadata or {}
    if message.source == "slack_dm":
        channel_id = meta.get("channel_id") or ""
        event_ts = meta.get("event_ts") or ""
        if channel_id and event_ts:
            return f"{channel_id}:{event_ts}"
        return f"slack_dm:{channel_id or 'unknown'}"
    if message.source == "email":
        message_id = meta.get("message_id") or ""
        return f"email:{message_id or 'unknown'}"
    if message.source == "mcp":
        actor_kind = meta.get("caller_actor_kind") or "unknown"
        tool_name = meta.get("tool_name") or "unknown"
        return f"mcp:{actor_kind}:{tool_name}"
    if message.source == "probe_investigation":
        # KR-PROBE-WAKE-CONSUMER — audit-correlation key joins the
        # reasoning.tool_called rows for this investigation back to
        # the originating probe.wake_requested row. Shape:
        # ``"probe:{probe_name}:{issue_category}"`` so the
        # KR-REASONING-PANEL-PROBE-XREF follow-on can group by it.
        probe = meta.get("probe_name") or "unknown"
        category = meta.get("issue_category") or "unknown"
        return f"probe:{probe}:{category}"
    return "unknown"


# KR-PLUGIN-EXTRACTIONS-BATCH-2 (Deliverable A) — the
# reasoning-tool audit helper moved to
# ``kora_hermes_plugin.audit.writer``. Re-imported here so the
# three in-file callers (``_execute_single_tool_block``) keep
# resolving the symbol; canonical location is the plugin module.
from kora_cli.reasoning.kora_hermes_plugin.audit.writer import (  # noqa: E402
    _emit_tool_called_audit,
)

__all_audit_shim__ = ("_emit_tool_called_audit",)
