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
        """Main entry. See :class:`ReasoningEngine.respond`."""
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
            # Unknown rung (e.g. "unknown") — default to OPUS but log
            # a WARN. The cost-ladder listener should always inject a
            # valid rung; this path covers misconfiguration.
            logger.warning(
                "[kora.reasoning] unknown cost rung %r — defaulting to "
                "opus + continuing",
                rung,
            )
            model = MODEL_OPUS

        # Assemble the message list. Anthropic SDK expects:
        #   [{role: "user"|"assistant", content: "..."}]
        # Map ConversationTurn.direction → role. The fresh
        # IncomingMessage goes at the end as the latest user turn.
        messages = self._build_message_history(message, context)

        # SDK call. Single attempt — NO retry per PM Q3 default.
        client = await self._ensure_client()
        try:
            response = await client.messages.create(
                model=model,
                system=self._system_prompt,
                messages=messages,
                max_tokens=self._max_output_tokens,
                timeout=self._timeout,
            )
        except Exception as exc:
            return self._map_sdk_exception(
                exc, model=model, started_at=started_at
            )

        # Extract reply text + token counts.
        return self._project_response(
            response, model=model, started_at=started_at
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
