"""ST2 wire-up tests for KR-REASONING-ROUTE-THROUGH-GATEWAY.

Covers the real plugin handler bodies (ST2 replaced ST1's no-op
stubs with cost-router + caching invocations) + the
``_respond_via_gateway`` end-to-end pipeline + behavior parity
checks against the bypass path.

ST2 scope-limited:
  - Toolless route-through (Kora reasoning tools NOT bridged
    into Hermes's toolset model in v1; ST2B follow-on)
  - pre_tool_call (constitution) + post_tool_call (audit)
    handlers stay no-op (depend on the tool-bridge)
  - Behavior parity tests cover the in-scope behaviors:
      * Cost-ladder default Haiku
      * /opus prefix → Opus
      * Decision-language → Opus
      * Caching markers on system + tools
      * Force-Opus env wins
      * ResponseResult projection from run_conversation dict
      * Bypass-path unchanged when toggle OFF
  - Deferred to follow-on buckets:
      * Constitution pre-screen (KR-PLUGIN-CONSTITUTION)
      * Tool-call audit (depends on tool bridge)
      * Short-circuit (lives in handler layer; not engine)
      * State holders init (lives at daemon boot; not engine)
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# pre_api_request_mutable — cost-router + caching
# ---------------------------------------------------------------------------


def test_pre_api_request_mutable_non_kora_route_no_op():
    """No-op on non-Kora routes — protects Hermes-fork users."""
    from plugins.kora_hermes import _pre_api_request_mutable

    result = _pre_api_request_mutable(
        route="",
        api_kwargs={"model": "claude-opus-4-7", "system": "sys"},
        api_call_count=1,
        user_message="hi",
    )
    assert result is None


def test_pre_api_request_mutable_kora_default_returns_haiku():
    """Default Kora call (iteration 1, no signals, normal rung)
    → router picks Haiku → override sets model=Haiku."""
    from plugins.kora_hermes import _pre_api_request_mutable
    from kora_cli.router import DEFAULT_HAIKU_MODEL

    result = _pre_api_request_mutable(
        route="slack_dm",
        api_kwargs={"model": "claude-opus-4-7", "system": "sys"},
        api_call_count=1,
        user_message="hi there",
    )
    assert result is not None
    assert result["override"]["model"] == DEFAULT_HAIKU_MODEL


def test_pre_api_request_mutable_opus_prefix_escalates(monkeypatch):
    """/opus prefix → router escalates → Opus override."""
    monkeypatch.delenv("KORA_FORCE_OPUS", raising=False)
    monkeypatch.delenv("KORA_OPUS_TRIGGER_PATTERNS", raising=False)
    from plugins.kora_hermes import _pre_api_request_mutable
    from kora_cli.router import DEFAULT_OPUS_MODEL

    result = _pre_api_request_mutable(
        route="slack_dm",
        api_kwargs={"model": "haiku", "system": "sys"},
        api_call_count=1,
        user_message="/opus heavy lift",
    )
    assert result is not None
    assert result["override"]["model"] == DEFAULT_OPUS_MODEL


def test_pre_api_request_mutable_decision_language_escalates(monkeypatch):
    """Decision-language pattern → Opus override."""
    monkeypatch.delenv("KORA_FORCE_OPUS", raising=False)
    monkeypatch.delenv("KORA_OPUS_TRIGGER_PATTERNS", raising=False)
    from plugins.kora_hermes import _pre_api_request_mutable
    from kora_cli.router import DEFAULT_OPUS_MODEL

    result = _pre_api_request_mutable(
        route="slack_dm",
        api_kwargs={"model": "haiku", "system": "sys"},
        api_call_count=1,
        user_message="should i ship this?",
    )
    assert result is not None
    assert result["override"]["model"] == DEFAULT_OPUS_MODEL


def test_pre_api_request_mutable_iteration_two_escalates(monkeypatch):
    """Hermes's api_call_count >= 2 maps to router's iteration
    >= 2 earning signal → Opus."""
    monkeypatch.delenv("KORA_FORCE_OPUS", raising=False)
    from plugins.kora_hermes import _pre_api_request_mutable
    from kora_cli.router import DEFAULT_OPUS_MODEL

    result = _pre_api_request_mutable(
        route="slack_dm",
        api_kwargs={"model": "haiku", "system": "sys"},
        api_call_count=2,
        user_message="hi",
    )
    assert result is not None
    assert result["override"]["model"] == DEFAULT_OPUS_MODEL


def test_pre_api_request_mutable_force_opus_env_wins(monkeypatch):
    monkeypatch.setenv("KORA_FORCE_OPUS", "true")
    from plugins.kora_hermes import _pre_api_request_mutable
    from kora_cli.router import DEFAULT_OPUS_MODEL

    result = _pre_api_request_mutable(
        route="slack_dm",
        api_kwargs={"model": "haiku", "system": "sys"},
        api_call_count=1,
        user_message="hi",
    )
    assert result is not None
    assert result["override"]["model"] == DEFAULT_OPUS_MODEL


def test_pre_api_request_mutable_wraps_system_with_cache_control():
    """System prompt gets wrapped into a content-block list with
    cache_control: ephemeral (KR-CHEAP-PROMPT-CACHING semantic
    via the hook layer)."""
    from plugins.kora_hermes import _pre_api_request_mutable

    result = _pre_api_request_mutable(
        route="slack_dm",
        api_kwargs={"model": "h", "system": "You are Kora."},
        api_call_count=1,
        user_message="hi",
    )
    assert result is not None
    system_override = result["override"]["system"]
    assert isinstance(system_override, list)
    assert system_override[0]["type"] == "text"
    assert system_override[0]["text"] == "You are Kora."
    assert system_override[0]["cache_control"] == {"type": "ephemeral"}


def test_pre_api_request_mutable_marks_last_tool_with_cache_control():
    """Last tool in tools= gets cache_control: ephemeral."""
    from plugins.kora_hermes import _pre_api_request_mutable

    tools_in = [
        {"name": "a", "description": "A", "input_schema": {}},
        {"name": "b", "description": "B", "input_schema": {}},
    ]
    result = _pre_api_request_mutable(
        route="slack_dm",
        api_kwargs={"model": "h", "system": "sys", "tools": tools_in},
        api_call_count=1,
        user_message="hi",
    )
    assert result is not None
    tools_override = result["override"]["tools"]
    assert "cache_control" not in tools_override[0]
    assert tools_override[-1]["cache_control"] == {"type": "ephemeral"}
    # Source tools not mutated.
    assert "cache_control" not in tools_in[-1]


def test_pre_api_request_mutable_empty_tools_unchanged():
    """Empty tools list → no caching override on tools (preserves
    the SDK convention of omitting an empty tools= kwarg)."""
    from plugins.kora_hermes import _pre_api_request_mutable

    result = _pre_api_request_mutable(
        route="slack_dm",
        api_kwargs={"model": "h", "system": "sys", "tools": []},
        api_call_count=1,
        user_message="hi",
    )
    # override may have model + system but NOT tools (empty in).
    if result is not None:
        assert "tools" not in result["override"]


def test_pre_api_request_mutable_invalid_api_kwargs_no_op():
    from plugins.kora_hermes import _pre_api_request_mutable

    assert _pre_api_request_mutable(route="slack_dm", api_kwargs=None) is None
    assert (
        _pre_api_request_mutable(route="slack_dm", api_kwargs="garbage") is None
    )


# ---------------------------------------------------------------------------
# post_llm_call — structured-log marker
# ---------------------------------------------------------------------------


def test_post_llm_call_logs_for_kora_route(caplog):
    import logging

    from plugins.kora_hermes import _post_llm_call

    caplog.set_level(logging.INFO)
    _post_llm_call(route="slack_dm", model="claude-haiku-4-5-20251001")
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "kora.gateway.post_llm_call" in m and "slack_dm" in m for m in msgs
    )


def test_post_llm_call_no_log_for_non_kora_route(caplog):
    import logging

    from plugins.kora_hermes import _post_llm_call

    caplog.set_level(logging.INFO)
    _post_llm_call(route="", model="m")
    msgs = [r.getMessage() for r in caplog.records]
    assert not any("kora.gateway.post_llm_call" in m for m in msgs)


# ---------------------------------------------------------------------------
# _respond_via_gateway — end-to-end with mocked AIAgent
# ---------------------------------------------------------------------------


def _make_incoming(text: str = "hi", source: str = "slack_dm"):
    from kora_cli.reasoning.engine import IncomingMessage

    return IncomingMessage(
        text=text,
        source=source,
        received_at=datetime.now(timezone.utc),
        metadata={},
    )


def _make_context(rung: str = "normal", state: str = "ready"):
    from kora_cli.reasoning.engine import ConversationContext

    return ConversationContext(
        recent_messages=[],
        current_operational_state=state,
        current_cost_ladder_rung=rung,
    )


@pytest.fixture
def system_prompt_path(tmp_path):
    p = tmp_path / "kora_system_prompt.md"
    p.write_text("You are Kora.\n", encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _oauth_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-test")


def _make_engine(system_prompt_path):
    from kora_cli.reasoning.anthropic_engine import AnthropicReasoningEngine

    # client= isn't used by _respond_via_gateway (we mock AIAgent
    # construction directly), but the bypass path needs SOME
    # client to construct cleanly.
    return AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=MagicMock()
    )


@pytest.fixture
def mock_aiagent():
    """Mock AIAgent class so _respond_via_gateway doesn't actually
    construct a real Hermes agent (heavy + has side effects)."""
    fake_agent = MagicMock()
    fake_agent.model = "claude-haiku-4-5-20251001"
    fake_agent.tools = []
    fake_agent.valid_tool_names = set()
    fake_agent.route = ""

    # run_conversation returns the Hermes result dict shape.
    def _fake_run_conversation(user_message, system_message=None):
        return {
            "final_response": "kora reply",
            "model": "claude-haiku-4-5-20251001",
            "provider": "anthropic",
            "base_url": "",
            "input_tokens": 150,
            "output_tokens": 50,
            "cache_read_tokens": 200,
            "cache_write_tokens": 30,
            "reasoning_tokens": 0,
            "prompt_tokens": 380,
            "completion_tokens": 50,
            "total_tokens": 430,
            "completed": True,
            "interrupted": False,
            "last_reasoning": None,
            "messages": [],
            "api_calls": 1,
            "turn_exit_reason": "end_turn",
            "partial": False,
            "response_previewed": False,
            "last_prompt_tokens": 380,
            "estimated_cost_usd": 0.0,
            "cost_status": "ok",
            "cost_source": "test",
        }

    fake_agent.run_conversation = _fake_run_conversation

    fake_class = MagicMock(return_value=fake_agent)
    return fake_class, fake_agent


@pytest.mark.asyncio
async def test_respond_via_gateway_end_to_end(
    monkeypatch, system_prompt_path, mock_aiagent
):
    fake_class, fake_agent = mock_aiagent
    monkeypatch.setattr("run_agent.AIAgent", fake_class)
    monkeypatch.setenv("KORA_REASONING_USE_GATEWAY", "true")

    engine = _make_engine(system_prompt_path)
    result = await engine.respond(
        _make_incoming("hello"), _make_context()
    )

    # AIAgent constructed once with the critical pin.
    fake_class.assert_called_once()
    ctor_kwargs = fake_class.call_args.kwargs
    assert ctor_kwargs["max_iterations"] == 5, (
        "max_iterations MUST be pinned to 5 (Kora's "
        "MAX_TOOL_USE_ITERATIONS); Hermes default 90 would "
        "quintuple monthly cost"
    )
    assert ctor_kwargs["provider"] == "anthropic"
    assert ctor_kwargs["api_mode"] == "anthropic_messages"
    assert ctor_kwargs["quiet_mode"] is True

    # Post-construction tool override + route set.
    assert fake_agent.tools == []
    assert fake_agent.route == "slack_dm"

    # Result projection.
    assert result.error is None
    assert result.text == "kora reply"
    assert result.model_used == "claude-haiku-4-5-20251001"
    assert result.input_tokens == 150
    assert result.output_tokens == 50
    assert result.cache_creation_input_tokens == 30
    assert result.cache_read_input_tokens == 200
    assert result.tools_used == []


@pytest.mark.asyncio
async def test_respond_via_gateway_paused_short_circuits(
    monkeypatch, system_prompt_path
):
    """Operational paused → return error WITHOUT constructing
    AIAgent (refuse-path semantic preserved from bypass)."""
    monkeypatch.setenv("KORA_REASONING_USE_GATEWAY", "true")

    fake_class = MagicMock()
    monkeypatch.setattr("run_agent.AIAgent", fake_class)

    engine = _make_engine(system_prompt_path)
    result = await engine.respond(
        _make_incoming("hi"), _make_context(state="paused")
    )
    assert result.error == "operational_state_paused"
    fake_class.assert_not_called()


@pytest.mark.asyncio
async def test_respond_via_gateway_hard_stop_short_circuits(
    monkeypatch, system_prompt_path
):
    monkeypatch.setenv("KORA_REASONING_USE_GATEWAY", "true")

    fake_class = MagicMock()
    monkeypatch.setattr("run_agent.AIAgent", fake_class)

    engine = _make_engine(system_prompt_path)
    result = await engine.respond(
        _make_incoming("hi"), _make_context(rung="hard_stop_100")
    )
    assert result.error == "cost_ladder_halted"
    fake_class.assert_not_called()


@pytest.mark.asyncio
async def test_respond_via_gateway_interrupted_maps_to_error(
    monkeypatch, system_prompt_path
):
    """AIAgent.run_conversation returns interrupted=True → error
    field is set in projected ResponseResult."""
    fake_agent = MagicMock()
    fake_agent.model = "haiku"
    fake_agent.tools = []
    fake_agent.run_conversation = lambda u, s=None: {
        "final_response": "",
        "model": "haiku",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "interrupted": True,
        "completed": False,
    }
    fake_class = MagicMock(return_value=fake_agent)
    monkeypatch.setattr("run_agent.AIAgent", fake_class)
    monkeypatch.setenv("KORA_REASONING_USE_GATEWAY", "true")

    engine = _make_engine(system_prompt_path)
    result = await engine.respond(_make_incoming(), _make_context())
    assert result.error == "gateway_interrupted"


@pytest.mark.asyncio
async def test_respond_via_gateway_exception_maps_to_error(
    monkeypatch, system_prompt_path
):
    """run_conversation raises → projected ResponseResult.error
    captures the exception class name."""
    fake_agent = MagicMock()
    fake_agent.model = "haiku"
    fake_agent.tools = []

    def _boom(*a, **kw):
        raise RuntimeError("simulated SDK boom")

    fake_agent.run_conversation = _boom
    fake_class = MagicMock(return_value=fake_agent)
    monkeypatch.setattr("run_agent.AIAgent", fake_class)
    monkeypatch.setenv("KORA_REASONING_USE_GATEWAY", "true")

    engine = _make_engine(system_prompt_path)
    result = await engine.respond(_make_incoming(), _make_context())
    assert result.error == "gateway_exception:RuntimeError"


@pytest.mark.asyncio
async def test_respond_via_gateway_threads_route_to_agent(
    monkeypatch, system_prompt_path, mock_aiagent
):
    """Per source: route attribute set correctly on the agent
    before run_conversation fires."""
    fake_class, fake_agent = mock_aiagent
    monkeypatch.setattr("run_agent.AIAgent", fake_class)
    monkeypatch.setenv("KORA_REASONING_USE_GATEWAY", "true")

    engine = _make_engine(system_prompt_path)
    # email source → email_inbound route
    await engine.respond(
        _make_incoming(source="email"), _make_context()
    )
    assert fake_agent.route == "email_inbound"

    # mcp source → mcp_tool route
    await engine.respond(
        _make_incoming(source="mcp"), _make_context()
    )
    assert fake_agent.route == "mcp_tool"


# ---------------------------------------------------------------------------
# Toggle OFF / default — bypass path unchanged (no regression)
# ---------------------------------------------------------------------------


def _fake_anthropic_response(text: str = "bypass reply"):
    usage = MagicMock()
    usage.input_tokens = 10
    usage.output_tokens = 5
    usage.cache_creation_input_tokens = 0
    usage.cache_read_input_tokens = 0
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = text
    r = MagicMock()
    r.content = [text_block]
    r.stop_reason = "end_turn"
    r.model = "claude-haiku-4-5-20251001"
    r.usage = usage
    return r


@pytest.mark.asyncio
async def test_toggle_off_uses_bypass_path_unchanged(
    monkeypatch, system_prompt_path
):
    """Default behavior: toggle unset → bypass path runs +
    returns ResponseResult with the existing shape. AIAgent
    construction is NOT triggered."""
    monkeypatch.delenv("KORA_REASONING_USE_GATEWAY", raising=False)
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)

    fake_aiagent_class = MagicMock()
    monkeypatch.setattr("run_agent.AIAgent", fake_aiagent_class)

    # Build engine with mocked Anthropic SDK client (the bypass
    # path's actual call site).
    from kora_cli.reasoning.anthropic_engine import (
        AnthropicReasoningEngine,
    )

    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(
        return_value=_fake_anthropic_response("bypass works")
    )
    client.close = AsyncMock()
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )

    result = await engine.respond(_make_incoming(), _make_context())
    assert result.text == "bypass works"
    assert result.error is None
    # The gateway construction never fired.
    fake_aiagent_class.assert_not_called()


@pytest.mark.asyncio
async def test_toggle_explicit_false_uses_bypass(
    monkeypatch, system_prompt_path
):
    monkeypatch.setenv("KORA_REASONING_USE_GATEWAY", "false")
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)

    fake_aiagent_class = MagicMock()
    monkeypatch.setattr("run_agent.AIAgent", fake_aiagent_class)

    from kora_cli.reasoning.anthropic_engine import (
        AnthropicReasoningEngine,
    )

    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(
        return_value=_fake_anthropic_response("bypass works")
    )
    client.close = AsyncMock()
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )

    result = await engine.respond(_make_incoming(), _make_context())
    assert result.error is None
    fake_aiagent_class.assert_not_called()
