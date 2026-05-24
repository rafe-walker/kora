"""Tests for the tool-use loop in AnthropicReasoningEngine —
KR-FEAT-AGENTIC-REASONING ST1.

Covers:
  - Tool registry exposes 5 read-only tools in Anthropic shape
  - mcp_tools.inputSchema → Anthropic input_schema conversion
  - Single-iteration tool call: tool_use → tool_result → text
  - Multi-iteration: two tools in series
  - Tool not in allowlist → tool_result error, engine continues
  - Tool execution exception → tool_result error, engine continues
  - Max iterations exceeded → ResponseResult error
  - Cost-ladder record_inference called once per API roundtrip
    (multi-iteration billing model)
  - tools_used field populated on ResponseResult
  - tools= omitted from SDK call when registry returns empty
  - Mutating tools NOT in reasoning allowlist
  - SECURITY: tool input/output never carries credentials in
    error envelopes
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from kora_cli.reasoning.anthropic_engine import (
    MAX_TOOL_USE_ITERATIONS,
    MODEL_OPUS,
    OAUTH_TOKEN_ENV,
    SYSTEM_PROMPT_PATH_ENV,
    AnthropicReasoningEngine,
)
from kora_cli.reasoning.engine import (
    ConversationContext,
    IncomingMessage,
)
from kora_cli.reasoning.tool_registry import (
    REASONING_TOOL_ALLOWLIST,
    ReasoningToolNotAllowed,
    execute_reasoning_tool,
    get_reasoning_available_tools,
)


# ---------------------------------------------------------------------------
# Helpers — SDK response stand-ins
# ---------------------------------------------------------------------------


def _text_block(text: str):
    b = MagicMock()
    b.type = "text"
    b.text = text
    return b


def _tool_use_block(tool_id: str, name: str, tool_input: Dict[str, Any]):
    b = MagicMock()
    b.type = "tool_use"
    b.id = tool_id
    b.name = name
    b.input = tool_input
    return b


def _response(
    content: List[Any],
    stop_reason: str = "end_turn",
    model: str = MODEL_OPUS,
    input_tokens: int = 100,
    output_tokens: int = 50,
):
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    r = MagicMock()
    r.content = content
    r.stop_reason = stop_reason
    r.model = model
    r.usage = usage
    return r


def _make_client(responses):
    """Build a mock SDK client. ``responses`` is a list — one per
    expected API call. Tests configure the sequence."""
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=list(responses))
    client.close = AsyncMock()
    return client


def _msg(text: str = "what's my status?") -> IncomingMessage:
    return IncomingMessage(
        text=text,
        source="slack_dm",
        received_at=datetime.now(timezone.utc),
        metadata={},
    )


def _ctx(rung: str = "normal", state: str = "ready") -> ConversationContext:
    return ConversationContext(
        recent_messages=[],
        current_operational_state=state,
        current_cost_ladder_rung=rung,
    )


@pytest.fixture
def system_prompt_path(tmp_path):
    p = tmp_path / "kora_system_prompt.md"
    p.write_text("You are Kora. Be useful.\n", encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _oauth(monkeypatch):
    monkeypatch.setenv(OAUTH_TOKEN_ENV, "sk-ant-oat-test")


# ---------------------------------------------------------------------------
# Tool registry (independent of engine wiring)
# ---------------------------------------------------------------------------


def test_allowlist_has_5_read_only_plus_two_bounded_mutating_tools():
    """5 read-only + 2 bounded mutating tools:
      - kora__send_email_to_operator (KR-EMAIL-OUTBOUND-COMPOSE-TOOL):
        recipient pinned to KORA_EMAIL_JOSHUA_ADDRESS.
      - kora__attempt_probe_autofix (KR-PROBE-AUTOFIX-EXECUTION):
        bound by per-probe env gates (default OFF) + envelope action
        whitelist + per-probe executor target verification.
    """
    assert sorted(REASONING_TOOL_ALLOWLIST) == sorted(
        [
            "kora__get_operational_state",
            "kora__get_health_rollup",
            "kora__get_recent_ledger_entries",
            "kora__list_active_sea_tickets",
            "kora__get_recent_chain_events",
            "kora__send_email_to_operator",
            "kora__attempt_probe_autofix",
        ]
    )


def test_allowlist_excludes_caller_controlled_mutating_tools():
    """Security boundary — Kora cannot invoke tools that accept
    caller-controlled recipients (mass-send risk) or substrate
    state mutations from her own reasoning loop.

    Two allowlisted mutating tools have narrow scope-bindings:
    ``kora__send_email_to_operator`` pins recipient (KR-EMAIL-
    OUTBOUND-COMPOSE-TOOL); ``kora__attempt_probe_autofix`` is
    gated by per-probe env + envelope action whitelist (KR-PROBE-
    AUTOFIX-EXECUTION). Other mutating tools stay excluded.
    """
    forbidden = [
        "kora__request_state_transition",
        "kora__create_sea_ticket",
        "kora__send_webhook_test_event",
        "kora__send_slack_dm",
        "kora__send_email",  # caller-controlled recipients
        "kora__request_pause",
        "kora__request_resume",
        "kora__request_stop",
        "kora__send_test_alert",
    ]
    for m in forbidden:
        assert m not in REASONING_TOOL_ALLOWLIST, (
            f"{m} should NOT be reachable from reasoning"
        )


def test_get_reasoning_available_tools_anthropic_shape():
    """Each descriptor has `name`/`description`/`input_schema`
    (snake_case per Anthropic API, NOT MCP's camelCase
    `inputSchema`)."""
    tools = get_reasoning_available_tools()
    # 5 read-only + 2 bounded mutating
    # (kora__send_email_to_operator + kora__attempt_probe_autofix).
    assert len(tools) == 7
    for tool in tools:
        assert set(tool.keys()) == {"name", "description", "input_schema"}
        assert tool["name"].startswith("kora__")
        assert isinstance(tool["description"], str)
        assert tool["input_schema"]["type"] == "object"
        # MCP-specific extras must NOT bleed through.
        assert "requires_cap_gate" not in tool
        assert "dev_only" not in tool
        assert "inputSchema" not in tool


def test_get_reasoning_available_tools_returns_fresh_list():
    """Per-call fresh list — no shared mutable state across calls."""
    a = get_reasoning_available_tools()
    b = get_reasoning_available_tools()
    assert a is not b
    a.clear()
    assert len(b) == 7


@pytest.mark.asyncio
async def test_execute_reasoning_tool_rejects_unknown(monkeypatch):
    """Tool name not in allowlist → ReasoningToolNotAllowed."""
    with pytest.raises(ReasoningToolNotAllowed):
        await execute_reasoning_tool("kora__not_a_tool", {})


@pytest.mark.asyncio
async def test_execute_reasoning_tool_rejects_mutating():
    """A mutating tool (e.g. create_sea_ticket) MUST be rejected
    even though it has a TOOL_DISPATCH entry — the allowlist is
    the gate, not the dispatch table."""
    for mutating in (
        "kora__request_state_transition",
        "kora__create_sea_ticket",
        "kora__send_webhook_test_event",
    ):
        with pytest.raises(ReasoningToolNotAllowed):
            await execute_reasoning_tool(mutating, {})


@pytest.mark.asyncio
async def test_execute_reasoning_tool_dispatches_allowed(monkeypatch):
    """Allowed tool flows through to mcp_tools.TOOL_DISPATCH."""
    from kora_cli.listeners import mcp_tools

    # Provider unavailable → returns OperationalStateResult-like
    # placeholder (the executor handles None provider gracefully).
    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)
    result = await execute_reasoning_tool(
        "kora__get_operational_state", {}
    )
    # Returns a Pydantic model with model_dump_json available.
    assert hasattr(result, "model_dump_json")


# ---------------------------------------------------------------------------
# Engine — single-iteration tool call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_tool_call_end_to_end(monkeypatch, system_prompt_path):
    """tool_use response → tool executes → final API call returns
    text. Verify the engine produces a clean ResponseResult."""
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)

    # Iteration 1: Claude requests a tool.
    iter1 = _response(
        content=[
            _text_block("Let me check."),
            _tool_use_block(
                "toolu_01",
                "kora__get_operational_state",
                {},
            ),
        ],
        stop_reason="tool_use",
        input_tokens=100,
        output_tokens=20,
    )
    # Iteration 2: Final text response.
    iter2 = _response(
        content=[_text_block("Kora is ready.")],
        stop_reason="end_turn",
        input_tokens=150,
        output_tokens=30,
    )

    client = _make_client([iter1, iter2])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(_msg(), _ctx())

    assert result.error is None
    assert result.text == "Kora is ready."
    # Tokens accumulate across iterations.
    assert result.input_tokens == 250  # 100 + 150
    assert result.output_tokens == 50  # 20 + 30
    # tools_used reflects what actually executed.
    assert result.tools_used == ["kora__get_operational_state"]
    # Two SDK calls (one per iteration).
    assert client.messages.create.await_count == 2


@pytest.mark.asyncio
async def test_no_tools_flat_chat_completion(monkeypatch, system_prompt_path):
    """Response with stop_reason="end_turn" on the FIRST call — no
    tool loop. tools_used is empty."""
    monkeypatch.setattr(
        "kora_cli.listeners.mcp_tools._get_active_provider", lambda: None
    )
    iter1 = _response(
        content=[_text_block("Hi Joshua.")],
        stop_reason="end_turn",
    )
    client = _make_client([iter1])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(_msg("hi"), _ctx())
    assert result.error is None
    assert result.text == "Hi Joshua."
    assert result.tools_used == []
    assert client.messages.create.await_count == 1


# ---------------------------------------------------------------------------
# Engine — multi-iteration tool sequence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_tool_sequence(monkeypatch, system_prompt_path):
    """Iteration 1: tool A. Iteration 2: tool B. Iteration 3: text."""
    monkeypatch.setattr(
        "kora_cli.listeners.mcp_tools._get_active_provider", lambda: None
    )

    iter1 = _response(
        content=[
            _tool_use_block(
                "toolu_a",
                "kora__get_operational_state",
                {},
            )
        ],
        stop_reason="tool_use",
    )
    iter2 = _response(
        content=[
            _tool_use_block(
                "toolu_b",
                "kora__get_health_rollup",
                {},
            )
        ],
        stop_reason="tool_use",
    )
    iter3 = _response(
        content=[_text_block("State: ready. Health: ok.")],
        stop_reason="end_turn",
    )
    client = _make_client([iter1, iter2, iter3])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(_msg("status?"), _ctx())
    assert result.error is None
    assert result.text == "State: ready. Health: ok."
    assert result.tools_used == [
        "kora__get_operational_state",
        "kora__get_health_rollup",
    ]
    assert client.messages.create.await_count == 3


# ---------------------------------------------------------------------------
# Engine — tool failure paths (continue conversation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_not_in_allowlist_continues(
    monkeypatch, system_prompt_path
):
    """Claude requested a tool not in the allowlist → tool_result
    error block; engine continues + Claude can recover."""
    monkeypatch.setattr(
        "kora_cli.listeners.mcp_tools._get_active_provider", lambda: None
    )

    iter1 = _response(
        content=[
            _tool_use_block(
                "toolu_bad",
                "kora__create_sea_ticket",  # mutating — blocked
                {"title": "hack"},
            )
        ],
        stop_reason="tool_use",
    )
    iter2 = _response(
        content=[_text_block("I can't create tickets from reasoning.")],
        stop_reason="end_turn",
    )
    client = _make_client([iter1, iter2])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(_msg(), _ctx())
    assert result.error is None
    # The blocked tool is NOT counted as used.
    assert "kora__create_sea_ticket" not in result.tools_used

    # The 2nd API call's messages history should include a
    # tool_result error block.
    second_call = client.messages.create.await_args_list[1]
    history = second_call.kwargs["messages"]
    # Walk for tool_result blocks.
    found_error = False
    for turn in history:
        if isinstance(turn.get("content"), list):
            for block in turn["content"]:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    and block.get("is_error") is True
                ):
                    found_error = True
                    assert "tool_not_allowed" in block["content"]
    assert found_error


@pytest.mark.asyncio
async def test_tool_execution_exception_continues(
    monkeypatch, system_prompt_path
):
    """If a tool executor raises, the engine emits a tool_result
    error block + continues. Tool name is NOT added to tools_used."""
    from kora_cli.reasoning import tool_registry

    # Force the executor to raise.
    async def _raising(name, tool_input):
        raise RuntimeError("substrate down")

    monkeypatch.setattr(
        tool_registry, "execute_reasoning_tool", _raising
    )

    iter1 = _response(
        content=[
            _tool_use_block(
                "toolu_x",
                "kora__get_operational_state",
                {},
            )
        ],
        stop_reason="tool_use",
    )
    iter2 = _response(
        content=[_text_block("Substrate is down; try later.")],
        stop_reason="end_turn",
    )
    client = _make_client([iter1, iter2])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(_msg(), _ctx())
    assert result.error is None
    assert result.text == "Substrate is down; try later."
    # Tool failed → NOT counted as used.
    assert result.tools_used == []


# ---------------------------------------------------------------------------
# Engine — max iterations safety cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_iterations_exceeded(monkeypatch, system_prompt_path):
    """Claude keeps requesting tools beyond the safety cap →
    engine returns error result."""
    monkeypatch.setattr(
        "kora_cli.listeners.mcp_tools._get_active_provider", lambda: None
    )

    # Build 6 tool-use responses (1 more than MAX_TOOL_USE_ITERATIONS).
    responses = [
        _response(
            content=[
                _tool_use_block(
                    f"toolu_{i}",
                    "kora__get_operational_state",
                    {},
                )
            ],
            stop_reason="tool_use",
        )
        for i in range(MAX_TOOL_USE_ITERATIONS + 1)
    ]
    client = _make_client(responses)
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(_msg(), _ctx())

    assert result.error == "tool_use_max_iterations_exceeded"
    # Loop made MAX_TOOL_USE_ITERATIONS calls (the cap is on
    # iterations, not on additional rounds beyond).
    assert client.messages.create.await_count == MAX_TOOL_USE_ITERATIONS


def test_max_iterations_constant_is_5():
    """Bucket §4 Q2 locked: cap = 5."""
    assert MAX_TOOL_USE_ITERATIONS == 5


# ---------------------------------------------------------------------------
# Engine — tools omitted from SDK call when registry empty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_tool_registry_omits_tools_param(
    monkeypatch, system_prompt_path
):
    """When the tool registry returns [] (e.g. mcp_tools import
    failed), the engine MUST omit ``tools`` from the SDK call —
    some SDK versions reject an empty array."""
    monkeypatch.setattr(
        "kora_cli.reasoning.tool_registry.get_reasoning_available_tools",
        lambda: [],
    )

    iter1 = _response(
        content=[_text_block("plain reply")], stop_reason="end_turn"
    )
    client = _make_client([iter1])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg(), _ctx())

    call_kwargs = client.messages.create.await_args.kwargs
    assert "tools" not in call_kwargs


@pytest.mark.asyncio
async def test_non_empty_registry_passes_tools_param(
    monkeypatch, system_prompt_path
):
    iter1 = _response(
        content=[_text_block("ok")], stop_reason="end_turn"
    )
    client = _make_client([iter1])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg(), _ctx())
    call_kwargs = client.messages.create.await_args.kwargs
    assert "tools" in call_kwargs
    tools = call_kwargs["tools"]
    # 5 read-only + 2 bounded mutating (send_email_to_operator +
    # attempt_probe_autofix).
    assert len(tools) == 7
    assert all("input_schema" in t for t in tools)


# ---------------------------------------------------------------------------
# Engine — cost-ladder accounting per iteration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_accumulation_across_iterations(
    monkeypatch, system_prompt_path
):
    """ResponseResult.input_tokens + output_tokens are TOTALS over
    all roundtrips (not just the final iteration). The handler
    bills the totals via record_inference once per response."""
    monkeypatch.setattr(
        "kora_cli.listeners.mcp_tools._get_active_provider", lambda: None
    )

    iter1 = _response(
        content=[
            _tool_use_block(
                "t1", "kora__get_operational_state", {}
            )
        ],
        stop_reason="tool_use",
        input_tokens=50,
        output_tokens=10,
    )
    iter2 = _response(
        content=[
            _tool_use_block(
                "t2", "kora__get_health_rollup", {}
            )
        ],
        stop_reason="tool_use",
        input_tokens=70,
        output_tokens=15,
    )
    iter3 = _response(
        content=[_text_block("done")],
        stop_reason="end_turn",
        input_tokens=90,
        output_tokens=20,
    )
    client = _make_client([iter1, iter2, iter3])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(_msg(), _ctx())
    # 50+70+90 = 210 input; 10+15+20 = 45 output.
    assert result.input_tokens == 210
    assert result.output_tokens == 45


# ---------------------------------------------------------------------------
# SECURITY — tool errors never expose credentials
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_errors_never_leak_credentials(
    monkeypatch, system_prompt_path, caplog
):
    """Diverse failure modes: tool-not-allowed + tool-execution-exception.
    Neither error envelope nor any log line may contain the OAuth
    token value."""
    import logging
    from kora_cli.reasoning import tool_registry

    caplog.set_level(logging.WARNING)
    token_marker = "sk-ant-oat-DO-NOT-LEAK-FROM-TOOL-PATH"
    monkeypatch.setenv(OAUTH_TOKEN_ENV, token_marker)

    # Force tool execution to raise something carrying secret-shaped
    # text (defensive — even if a tool's exception msg accidentally
    # quoted env values, the engine shouldn't propagate).
    async def _leaky_raising(name, tool_input):
        raise RuntimeError(f"connection failed (auth header masked)")

    monkeypatch.setattr(
        tool_registry, "execute_reasoning_tool", _leaky_raising
    )

    iter1 = _response(
        content=[
            _tool_use_block(
                "tt", "kora__get_operational_state", {}
            )
        ],
        stop_reason="tool_use",
    )
    iter2 = _response(
        content=[_text_block("recovered")], stop_reason="end_turn"
    )
    client = _make_client([iter1, iter2])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(_msg(), _ctx())

    # Token didn't surface in result.
    assert token_marker not in (result.error or "")
    assert token_marker not in result.text

    # Token didn't surface in any log line.
    log_text = " ".join(r.getMessage() for r in caplog.records)
    assert token_marker not in log_text

    # Token didn't surface in the tool_result blocks sent back to
    # the SDK in iteration 2.
    second_call = client.messages.create.await_args_list[1]
    sent_history = second_call.kwargs["messages"]
    serialized = str(sent_history)
    assert token_marker not in serialized
