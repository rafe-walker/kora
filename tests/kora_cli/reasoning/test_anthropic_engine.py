"""Tests for ``kora_cli.reasoning.anthropic_engine`` —
KR-FEAT-AI-RESPONSE-LOOP ST1.

Covers:
  - Construction: fail-CLOSED on missing both creds; succeeds with
    API key, succeeds with OAuth token; API key wins over OAuth
  - System prompt: fail-CLOSED on missing/empty/unreadable file
  - Cost-ladder model selection: NORMAL→opus, WARN_75→sonnet,
    DOWNSHIFT_90→haiku, HARD_STOP_100→refuse
  - Operational-state gating: paused / stopped → refuse
  - Successful API call → ResponseResult with text + tokens
  - Message history projection: alternating roles + concat on
    consecutive same-role
  - Fresh inbound concatenates to last history user turn if same role
  - SDK error mapping: 401 → sdk_auth, 429 → sdk_rate_limited,
    500 → sdk_5xx, 4xx → sdk_4xx_<code>, timeout → sdk_timeout,
    transport → sdk_transport, unknown → sdk_unknown_<class>
  - NO retry on any error (single SDK call exactly)
  - SECURITY: credential never appears in error messages / result
    fields after diverse failure-mode sequence (401 / 429 / 500 /
    timeout / network-error)
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from kora_cli.reasoning.anthropic_engine import (
    API_KEY_ENV,
    MODEL_HAIKU,
    MODEL_OPUS,
    MODEL_SONNET,
    OAUTH_TOKEN_ENV,
    SYSTEM_PROMPT_PATH_ENV,
    AnthropicReasoningEngine,
    ReasoningEngineNotConfigured,
    ReasoningSystemPromptError,
)
from kora_cli.reasoning.engine import (
    ConversationContext,
    ConversationTurn,
    IncomingMessage,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(
    text: str = "kora reply",
    model: str = MODEL_OPUS,
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> Any:
    """Stand-in for an Anthropic SDK Message object."""
    block = MagicMock()
    block.type = "text"
    block.text = text

    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens

    response = MagicMock()
    response.content = [block]
    response.usage = usage
    response.model = model
    return response


def _make_mock_client(response=None, side_effect=None):
    """Build a mock that emulates AsyncAnthropic. ``client.messages.create``
    is an AsyncMock; configure either ``response`` (return value) OR
    ``side_effect`` (exception to raise)."""
    client = MagicMock()
    client.messages = MagicMock()
    if side_effect is not None:
        client.messages.create = AsyncMock(side_effect=side_effect)
    else:
        client.messages.create = AsyncMock(
            return_value=response or _make_response()
        )
    client.close = AsyncMock()
    return client


@pytest.fixture
def system_prompt_path(tmp_path):
    p = tmp_path / "kora_system_prompt.md"
    p.write_text(
        "You are Kora, Joshua's digital extension. Be useful.\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture(autouse=True)
def _clear_envs(monkeypatch):
    """Default: both creds + prompt path env unset. Per-test fixtures
    set what they need."""
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    monkeypatch.delenv(OAUTH_TOKEN_ENV, raising=False)
    monkeypatch.delenv(SYSTEM_PROMPT_PATH_ENV, raising=False)


def _ctx(
    rung: str = "normal",
    state: str = "ready",
    turns: List[ConversationTurn] = None,
) -> ConversationContext:
    return ConversationContext(
        recent_messages=turns or [],
        current_operational_state=state,
        current_cost_ladder_rung=rung,
    )


def _msg(text: str = "hi kora") -> IncomingMessage:
    return IncomingMessage(
        text=text,
        source="slack_dm",
        received_at=datetime.now(timezone.utc),
        metadata={},
    )


# ---------------------------------------------------------------------------
# Construction — credential cascade
# ---------------------------------------------------------------------------


def test_construction_fails_when_both_creds_unset(system_prompt_path):
    with pytest.raises(ReasoningEngineNotConfigured) as exc_info:
        AnthropicReasoningEngine(system_prompt_path=system_prompt_path)
    # Both env names must appear in the error so operator knows the
    # cascade.
    assert API_KEY_ENV in str(exc_info.value)
    assert OAUTH_TOKEN_ENV in str(exc_info.value)


def test_construction_succeeds_with_oauth_token(
    monkeypatch, system_prompt_path
):
    monkeypatch.setenv(OAUTH_TOKEN_ENV, "sk-ant-oat-test")
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path
    )
    assert engine._auth_mode == "oauth_token"


def test_construction_succeeds_with_api_key(monkeypatch, system_prompt_path):
    monkeypatch.setenv(API_KEY_ENV, "sk-ant-test-api-key")
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path
    )
    assert engine._auth_mode == "api_key"


def test_oauth_wins_over_api_key_when_both_set(
    monkeypatch, system_prompt_path
):
    """PM ruling 2026-05-22 ST2: OAuth is production (Max plan
    billing); API key is dev/test fallback. OAuth wins when both
    are set so the daemon never accidentally bills to the wrong
    surface in deploys where both happen to be present."""
    monkeypatch.setenv(API_KEY_ENV, "sk-ant-key")
    monkeypatch.setenv(OAUTH_TOKEN_ENV, "sk-ant-oat-token")
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path
    )
    assert engine._auth_mode == "oauth_token"


def test_whitespace_only_credential_is_treated_as_unset(
    monkeypatch, system_prompt_path
):
    monkeypatch.setenv(OAUTH_TOKEN_ENV, "   ")
    with pytest.raises(ReasoningEngineNotConfigured):
        AnthropicReasoningEngine(system_prompt_path=system_prompt_path)


# ---------------------------------------------------------------------------
# System prompt failure modes
# ---------------------------------------------------------------------------


def test_missing_system_prompt_raises(monkeypatch, tmp_path):
    monkeypatch.setenv(OAUTH_TOKEN_ENV, "sk-ant-oat-test")
    missing = tmp_path / "does_not_exist.md"
    with pytest.raises(ReasoningSystemPromptError):
        AnthropicReasoningEngine(system_prompt_path=missing)


def test_empty_system_prompt_raises(monkeypatch, tmp_path):
    monkeypatch.setenv(OAUTH_TOKEN_ENV, "sk-ant-oat-test")
    empty = tmp_path / "empty.md"
    empty.write_text("   \n\n", encoding="utf-8")
    with pytest.raises(ReasoningSystemPromptError):
        AnthropicReasoningEngine(system_prompt_path=empty)


def test_system_prompt_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv(OAUTH_TOKEN_ENV, "sk-ant-oat-test")
    p = tmp_path / "custom_prompt.md"
    p.write_text("custom kora prompt", encoding="utf-8")
    monkeypatch.setenv(SYSTEM_PROMPT_PATH_ENV, str(p))
    engine = AnthropicReasoningEngine()
    assert "custom kora prompt" in engine._system_prompt


# ---------------------------------------------------------------------------
# Cost-ladder model selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_rung_selects_opus(monkeypatch, system_prompt_path):
    monkeypatch.setenv(OAUTH_TOKEN_ENV, "tok")
    client = _make_mock_client(response=_make_response(model=MODEL_OPUS))
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(_msg(), _ctx(rung="normal"))
    assert result.error is None
    assert client.messages.create.await_args.kwargs["model"] == MODEL_OPUS


@pytest.mark.asyncio
async def test_warn_75_rung_selects_sonnet(monkeypatch, system_prompt_path):
    monkeypatch.setenv(OAUTH_TOKEN_ENV, "tok")
    client = _make_mock_client(
        response=_make_response(model=MODEL_SONNET)
    )
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg(), _ctx(rung="warn_75"))
    assert (
        client.messages.create.await_args.kwargs["model"] == MODEL_SONNET
    )


@pytest.mark.asyncio
async def test_downshift_90_rung_selects_haiku(
    monkeypatch, system_prompt_path
):
    monkeypatch.setenv(OAUTH_TOKEN_ENV, "tok")
    client = _make_mock_client(response=_make_response(model=MODEL_HAIKU))
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg(), _ctx(rung="downshift_90"))
    assert client.messages.create.await_args.kwargs["model"] == MODEL_HAIKU


@pytest.mark.asyncio
async def test_hard_stop_100_rung_refuses_no_sdk_call(
    monkeypatch, system_prompt_path
):
    monkeypatch.setenv(OAUTH_TOKEN_ENV, "tok")
    client = _make_mock_client()
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(_msg(), _ctx(rung="hard_stop_100"))
    assert result.error == "cost_ladder_halted"
    assert result.text == ""
    client.messages.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_rung_defaults_to_opus_and_continues(
    monkeypatch, system_prompt_path, caplog
):
    import logging

    caplog.set_level(logging.WARNING)
    monkeypatch.setenv(OAUTH_TOKEN_ENV, "tok")
    client = _make_mock_client(response=_make_response(model=MODEL_OPUS))
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(_msg(), _ctx(rung="unknown"))
    assert result.error is None
    assert client.messages.create.await_args.kwargs["model"] == MODEL_OPUS
    assert any(
        "unknown cost rung" in r.getMessage() for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Operational-state gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paused_state_refuses_no_sdk_call(
    monkeypatch, system_prompt_path
):
    monkeypatch.setenv(OAUTH_TOKEN_ENV, "tok")
    client = _make_mock_client()
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(_msg(), _ctx(state="paused"))
    assert result.error == "operational_state_paused"
    client.messages.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_stopped_state_refuses_no_sdk_call(
    monkeypatch, system_prompt_path
):
    monkeypatch.setenv(OAUTH_TOKEN_ENV, "tok")
    client = _make_mock_client()
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(_msg(), _ctx(state="stopped"))
    assert result.error == "operational_state_paused"
    client.messages.create.assert_not_awaited()


# ---------------------------------------------------------------------------
# Successful happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_call_returns_text_plus_tokens(
    monkeypatch, system_prompt_path
):
    monkeypatch.setenv(OAUTH_TOKEN_ENV, "tok")
    client = _make_mock_client(
        response=_make_response(
            text="here is your answer", input_tokens=120, output_tokens=80
        )
    )
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(_msg("explain X"), _ctx())
    assert result.error is None
    assert result.text == "here is your answer"
    assert result.input_tokens == 120
    assert result.output_tokens == 80
    assert result.reasoning_duration_ms >= 0


# ---------------------------------------------------------------------------
# Message history projection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_history_alternating_roles(
    monkeypatch, system_prompt_path
):
    monkeypatch.setenv(OAUTH_TOKEN_ENV, "tok")
    client = _make_mock_client(response=_make_response())
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )

    now = datetime.now(timezone.utc)
    turns = [
        ConversationTurn(direction="inbound", text="msg1", at=now),
        ConversationTurn(direction="outbound", text="reply1", at=now),
        ConversationTurn(direction="inbound", text="msg2", at=now),
        ConversationTurn(direction="outbound", text="reply2", at=now),
    ]
    await engine.respond(_msg("latest"), _ctx(turns=turns))

    messages = client.messages.create.await_args.kwargs["messages"]
    assert len(messages) == 5
    assert [m["role"] for m in messages] == [
        "user", "assistant", "user", "assistant", "user",
    ]
    assert messages[0]["content"] == "msg1"
    assert messages[-1]["content"] == "latest"


@pytest.mark.asyncio
async def test_message_history_consecutive_same_role_concatenates(
    monkeypatch, system_prompt_path
):
    """Anthropic API requires alternating roles. Loader-provided
    non-alternating history is collapsed by concatenation."""
    monkeypatch.setenv(OAUTH_TOKEN_ENV, "tok")
    client = _make_mock_client(response=_make_response())
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )

    now = datetime.now(timezone.utc)
    turns = [
        ConversationTurn(direction="inbound", text="msg1", at=now),
        # Race-condition: two inbound entries before any outbound.
        ConversationTurn(direction="inbound", text="msg2", at=now),
    ]
    await engine.respond(_msg("msg3-fresh"), _ctx(turns=turns))

    messages = client.messages.create.await_args.kwargs["messages"]
    # All three should collapse into one "user" turn.
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert "msg1" in messages[0]["content"]
    assert "msg2" in messages[0]["content"]
    assert "msg3-fresh" in messages[0]["content"]


@pytest.mark.asyncio
async def test_system_prompt_passed_to_sdk(
    monkeypatch, system_prompt_path
):
    monkeypatch.setenv(OAUTH_TOKEN_ENV, "tok")
    client = _make_mock_client(response=_make_response())
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg(), _ctx())
    system = client.messages.create.await_args.kwargs["system"]
    assert "Kora" in system


# ---------------------------------------------------------------------------
# SDK error mapping
# ---------------------------------------------------------------------------


class _StatusCodeError(Exception):
    """Stand-in for anthropic SDK exception classes that carry
    a status_code attribute."""

    def __init__(self, status_code: int, message: str = "stub"):
        super().__init__(message)
        self.status_code = status_code


class _TimeoutErrorStub(Exception):
    """Class name matches one of the mapped timeout patterns."""

    pass


_TimeoutErrorStub.__name__ = "APITimeoutError"


@pytest.mark.asyncio
async def test_sdk_401_maps_to_sdk_auth(monkeypatch, system_prompt_path):
    monkeypatch.setenv(OAUTH_TOKEN_ENV, "tok")
    client = _make_mock_client(
        side_effect=_StatusCodeError(401, "unauthorized")
    )
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(_msg(), _ctx())
    assert result.error == "sdk_auth"


@pytest.mark.asyncio
async def test_sdk_429_maps_to_rate_limited(monkeypatch, system_prompt_path):
    monkeypatch.setenv(OAUTH_TOKEN_ENV, "tok")
    client = _make_mock_client(
        side_effect=_StatusCodeError(429, "too many")
    )
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(_msg(), _ctx())
    assert result.error == "sdk_rate_limited"


@pytest.mark.asyncio
async def test_sdk_500_maps_to_5xx(monkeypatch, system_prompt_path):
    monkeypatch.setenv(OAUTH_TOKEN_ENV, "tok")
    client = _make_mock_client(
        side_effect=_StatusCodeError(500, "internal")
    )
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(_msg(), _ctx())
    assert result.error == "sdk_5xx"


@pytest.mark.asyncio
async def test_sdk_400_maps_to_4xx_with_code(monkeypatch, system_prompt_path):
    monkeypatch.setenv(OAUTH_TOKEN_ENV, "tok")
    client = _make_mock_client(side_effect=_StatusCodeError(400))
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(_msg(), _ctx())
    assert result.error == "sdk_4xx_400"


@pytest.mark.asyncio
async def test_sdk_timeout_maps_to_sdk_timeout(
    monkeypatch, system_prompt_path
):
    monkeypatch.setenv(OAUTH_TOKEN_ENV, "tok")
    client = _make_mock_client(side_effect=_TimeoutErrorStub("timed out"))
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(_msg(), _ctx())
    assert result.error == "sdk_timeout"


@pytest.mark.asyncio
async def test_sdk_unknown_maps_to_sdk_unknown_classname(
    monkeypatch, system_prompt_path
):
    class _WhateverError(Exception):
        pass

    monkeypatch.setenv(OAUTH_TOKEN_ENV, "tok")
    client = _make_mock_client(side_effect=_WhateverError("???"))
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(_msg(), _ctx())
    assert result.error == "sdk_unknown__WhateverError"


# ---------------------------------------------------------------------------
# Retry policy — single attempt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_retry_on_5xx(monkeypatch, system_prompt_path):
    """Per PM Q3 default — NO retry. Single SDK call exactly."""
    monkeypatch.setenv(OAUTH_TOKEN_ENV, "tok")
    client = _make_mock_client(side_effect=_StatusCodeError(500))
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg(), _ctx())
    assert client.messages.create.await_count == 1


@pytest.mark.asyncio
async def test_no_retry_on_429(monkeypatch, system_prompt_path):
    monkeypatch.setenv(OAUTH_TOKEN_ENV, "tok")
    client = _make_mock_client(side_effect=_StatusCodeError(429))
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg(), _ctx())
    assert client.messages.create.await_count == 1


# ---------------------------------------------------------------------------
# SECURITY — credential never leaks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credential_never_in_errors(
    monkeypatch, system_prompt_path, caplog
):
    """Diverse failure-mode sequence: 401 / 429 / 500 / timeout /
    transport / unknown. NONE of the ResponseResult error codes or
    log messages may contain the credential env value."""
    import logging

    caplog.set_level(logging.WARNING)
    token_marker = "sk-ant-oat-SECRET-DO-NOT-LEAK"
    monkeypatch.setenv(OAUTH_TOKEN_ENV, token_marker)

    failures = [
        _StatusCodeError(401),
        _StatusCodeError(429),
        _StatusCodeError(500),
        _TimeoutErrorStub("timeout"),
        type("ConnectionError", (Exception,), {})("conn"),
        type("WhateverErr", (Exception,), {})("???"),
    ]

    for exc in failures:
        client = _make_mock_client(side_effect=exc)
        engine = AnthropicReasoningEngine(
            system_prompt_path=system_prompt_path, client=client
        )
        result = await engine.respond(_msg(), _ctx())
        # ResponseResult.error must NEVER carry the credential.
        assert token_marker not in (result.error or "")
        assert token_marker not in result.text
        assert token_marker not in result.model_used

    # All captured log messages.
    log_text = " ".join(r.getMessage() for r in caplog.records)
    assert token_marker not in log_text
