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
MODEL_OPUS = "claude-opus-4-7"
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_HAIKU = "claude-haiku-4-5-20251001"

# Cost-ladder rung → model mapping. Bucket spec maps the four rungs
# to {opus, sonnet, haiku, halt}. Keyed by the CostRung enum's
# canonical ``.value`` strings (NOT the bucket-spec paraphrases).
RUNG_MODEL_MAP: Dict[str, str] = {
    "normal": MODEL_OPUS,
    "warn_75": MODEL_SONNET,
    "downshift_90": MODEL_HAIKU,
    # "hard_stop_100" is special-cased — refuse, no API call.
}


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

        KR-FEAT-AGENTIC-REASONING ST1: now drives a tool-use loop
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
        """
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
        model = RUNG_MODEL_MAP.get(rung)
        if model is None:
            logger.warning(
                "[kora.reasoning] unknown cost rung %r — defaulting to "
                "opus + continuing",
                rung,
            )
            model = MODEL_OPUS

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
            model=model,
            messages=messages,
            tools=tools,
            started_at=started_at,
            triggered_by=triggered_by,
            caller_session_id=caller_session_id,
        )

    async def _tool_use_loop(
        self,
        *,
        client: Any,
        model: str,
        messages: list,
        tools: list,
        started_at: float,
        triggered_by: str = "unknown",
        caller_session_id: str = "",
    ) -> ResponseResult:
        """Drive the tool-use roundtrip cascade.

        See ``respond`` docstring for the high-level flow. Token
        accumulation is per-iteration; SDK exceptions on any
        iteration short-circuit to a mapped error result.
        """
        # Accumulators across iterations.
        total_input_tokens = 0
        total_output_tokens = 0
        # Track which tools Kora actually used — surfaced to the
        # handler via ResponseResult.tools_used (ST2 wires it into
        # the outbound JSONL; ST1 ships the field on the result
        # class so handler/test consumers don't churn between STs).
        tools_used: list[str] = []

        for iteration in range(1, MAX_TOOL_USE_ITERATIONS + 1):
            try:
                # tools= is optional per the SDK; omit when the
                # registry returned empty so we don't send an empty
                # array (some Anthropic SDK versions are strict).
                kwargs: Dict[str, Any] = {
                    "model": model,
                    "system": self._system_prompt,
                    "messages": messages,
                    "max_tokens": self._max_output_tokens,
                    "timeout": self._timeout,
                }
                if tools:
                    kwargs["tools"] = tools
                response = await client.messages.create(**kwargs)
            except Exception as exc:
                return self._map_sdk_exception(
                    exc, model=model, started_at=started_at
                )

            # Per-iteration token accumulation.
            usage = getattr(response, "usage", None)
            total_input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
            total_output_tokens += int(
                getattr(usage, "output_tokens", 0) or 0
            )

            # Detect tool-use vs end-of-turn. Anthropic SDK sets
            # ``response.stop_reason`` to one of:
            #   "end_turn" / "max_tokens" / "stop_sequence" / "tool_use"
            stop_reason = getattr(response, "stop_reason", None)
            if stop_reason != "tool_use":
                # Done — final text response. Project + return.
                return self._project_final_response(
                    response,
                    model=model,
                    started_at=started_at,
                    total_input_tokens=total_input_tokens,
                    total_output_tokens=total_output_tokens,
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
                    model=model,
                    started_at=started_at,
                    total_input_tokens=total_input_tokens,
                    total_output_tokens=total_output_tokens,
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
            model_used=model,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            reasoning_duration_ms=_elapsed_ms(started_at),
            error="tool_use_max_iterations_exceeded",
            tools_used=tools_used,
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
        """Run all tool_use blocks in PARALLEL + return the matching
        ``tool_result`` blocks for the next user turn.

        KR-FEAT-AGENTIC-REASONING-PARALLEL ST1 — when Claude returns
        multiple ``tool_use`` blocks in one response (the API's default
        behavior under ``tool_choice=auto`` per
        https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use),
        we dispatch them concurrently via :func:`asyncio.gather`
        rather than serially. The two reasons:

          1. **Latency**: 3 independent reads (operational state +
             ledger + chain events) finish in roughly the time of
             the slowest one instead of the sum. Joshua's
             "show me daemon status AND recent ledger" type DMs
             benefit.
          2. **API contract**: per the docs, "Tool calls in a single
             assistant turn are unordered. You can run them
             concurrently (Promise.all, asyncio.gather), sequentially,
             or in any order." Concurrency is the canonical pattern.

        Per-tool error isolation: each block's exception becomes its
        OWN ``tool_result`` with ``is_error: true``; sibling blocks in
        the same batch still complete normally. ``return_exceptions=
        True`` on the gather is **defensive** — the per-task helper
        :meth:`_execute_single_tool_block` catches and converts every
        exception internally, so gather should never see one. But if
        a future helper-edit lets one escape, gather hands back the
        exception object and we synthesize a generic
        ``tool_execution_error`` rather than crashing the whole batch.

        Audit emission: each task emits its own
        ``[kora.reasoning.tool_called]`` row at its own completion
        time (correct ``tool_duration_ms``). Ordering across tasks is
        **non-deterministic** — completion order, not block order.
        The reasoning panel's ``caller_session_id`` grouping (per
        PR #143) re-aggregates them; per-row ``emitted_at`` orders
        chronologically.

        ``tools_used`` is mutated by each task on success (in-process
        single-threaded asyncio — ``list.append`` is safe under
        concurrent tasks within the same event loop). Order in
        ``tools_used`` reflects completion order, not block order.

        Return order: matches input ``tool_use_blocks`` order even
        though completion order may differ. :func:`asyncio.gather`
        preserves input position in its result list, so the
        ``tool_result`` blocks line up with their ``tool_use`` blocks
        structurally. (Strictly the API binds them by ``tool_use_id``,
        not by position, but matching positions keeps the message
        history readable.)
        """
        import asyncio

        if not tool_use_blocks:
            return []

        tasks = [
            self._execute_single_tool_block(
                block,
                tools_used=tools_used,
                triggered_by=triggered_by,
                caller_session_id=caller_session_id,
            )
            for block in tool_use_blocks
        ]

        gathered = await asyncio.gather(*tasks, return_exceptions=True)

        results: list = []
        for block, outcome in zip(tool_use_blocks, gathered):
            if isinstance(outcome, BaseException):
                # Defensive: should be unreachable — the per-task
                # helper catches every exception class internally and
                # returns a tool_result error dict. If we ever see
                # one here it's a helper-bug; synthesize a tool_result
                # error so the loop can continue rather than abort.
                logger.exception(
                    "[kora.reasoning] _execute_single_tool_block "
                    "leaked exception %r — synthesizing tool_result "
                    "error so loop can continue",
                    outcome,
                )
                tool_use_id = getattr(block, "id", "")
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": (
                            f"tool_execution_error: "
                            f"{type(outcome).__name__}"
                        ),
                        "is_error": True,
                    }
                )
            else:
                results.append(outcome)
        return results

    async def _execute_single_tool_block(
        self,
        block: Any,
        *,
        tools_used: list[str],
        triggered_by: str,
        caller_session_id: str,
    ) -> dict:
        """Run one ``tool_use`` block and return its ``tool_result``
        dict. Audit-emits per-call. Catches every exception class
        internally so the surrounding ``asyncio.gather`` never sees a
        raised exception (its ``return_exceptions=True`` is defensive
        belt-and-suspenders against a future helper-edit, not a
        primary error path).

        Failure modes (each becomes a ``tool_result`` with
        ``is_error: true``):

          - Tool not in reasoning allowlist → tool_status="not_allowed"
          - Any other tool execution exception → tool_status=
            "execution_error" with the exception class name in the
            audit row's ``exc_type`` field.
        """
        import json
        import time as _time

        from kora_cli.reasoning.tool_registry import (
            ReasoningToolNotAllowed,
            execute_reasoning_tool,
        )

        tool_use_id = getattr(block, "id", "")
        tool_name = getattr(block, "name", "")
        tool_input = getattr(block, "input", {}) or {}
        call_started_at = _time.monotonic()

        try:
            result_model = await execute_reasoning_tool(
                name=tool_name, tool_input=tool_input
            )
            result_text = (
                result_model.model_dump_json()
                if hasattr(result_model, "model_dump_json")
                else json.dumps(result_model, default=str)
            )
            tools_used.append(tool_name)
            _emit_tool_called_audit(
                tool_name=tool_name,
                triggered_by=triggered_by,
                caller_session_id=caller_session_id,
                tool_duration_ms=_elapsed_ms(call_started_at),
                tool_status="ok",
            )
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": result_text,
            }
        except ReasoningToolNotAllowed:
            _emit_tool_called_audit(
                tool_name=tool_name,
                triggered_by=triggered_by,
                caller_session_id=caller_session_id,
                tool_duration_ms=_elapsed_ms(call_started_at),
                tool_status="not_allowed",
            )
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": (
                    f"tool_not_allowed: {tool_name!r} is not in "
                    f"the reasoning allowlist"
                ),
                "is_error": True,
            }
        except Exception as exc:
            _emit_tool_called_audit(
                tool_name=tool_name,
                triggered_by=triggered_by,
                caller_session_id=caller_session_id,
                tool_duration_ms=_elapsed_ms(call_started_at),
                tool_status="execution_error",
                exc_type=type(exc).__name__,
            )
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": (
                    f"tool_execution_error: {type(exc).__name__}"
                ),
                "is_error": True,
            }

    def _project_final_response(
        self,
        response: Any,
        *,
        model: str,
        started_at: float,
        total_input_tokens: int,
        total_output_tokens: int,
        tools_used: list[str],
    ) -> ResponseResult:
        """Multi-iteration variant of :meth:`_project_response`.

        Token totals are passed in (accumulated across all
        iterations) rather than read from the final response's
        usage block — important since intermediate roundtrips
        billed tokens too.
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
    return "unknown"


def _emit_tool_called_audit(
    *,
    tool_name: str,
    triggered_by: str,
    caller_session_id: str,
    tool_duration_ms: int,
    tool_status: str,
    exc_type: Optional[str] = None,
) -> None:
    """Stable audit per reasoning-tool call — KR-AUDIT-JSONL-SINK.

    **Dual-write**: existing ``[kora.reasoning.tool_called]``
    structured-log line preserved VERBATIM (operator grep workflows
    keep working) + :func:`emit_audit` writes a JSONL row to
    ``kora_audit_log.jsonl`` (panel consumption).

    NEVER logs tool input/output bodies (those may contain
    privileged operator data). Names + status codes only.
    """
    if exc_type is not None:
        logger.info(
            "[kora.reasoning.tool_called] tool=%s triggered_by=%s "
            "caller_session_id=%s tool_duration_ms=%d tool_status=%s "
            "exc_type=%s",
            tool_name,
            triggered_by,
            caller_session_id,
            tool_duration_ms,
            tool_status,
            exc_type,
        )
    else:
        logger.info(
            "[kora.reasoning.tool_called] tool=%s triggered_by=%s "
            "caller_session_id=%s tool_duration_ms=%d tool_status=%s",
            tool_name,
            triggered_by,
            caller_session_id,
            tool_duration_ms,
            tool_status,
        )

    # KR-AUDIT-JSONL-SINK — JSONL bridge to panels.
    from kora_cli.audit import emit_audit

    details: Dict[str, Any] = {
        "tool_name": tool_name,
        "triggered_by": triggered_by,
        "tool_duration_ms": tool_duration_ms,
        "tool_status": tool_status,
    }
    if exc_type is not None:
        details["exc_type"] = exc_type

    emit_audit(
        seam="reasoning.tool_called",
        details=details,
        caller_session_id=caller_session_id,
        source="reasoning",
    )
