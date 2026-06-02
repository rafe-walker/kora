"""Tests for parallel tool calls in AnthropicReasoningEngine —
KR-FEAT-AGENTIC-REASONING-PARALLEL ST1.

Covers spec §2 ST1 test scope:

  - Multi-tool single-iteration: 3 tool_use blocks in one response →
    all 3 dispatched via asyncio.gather; all 3 tool_results returned
    in a single user message in input order; one API roundtrip per
    iteration (not three).
  - Concurrency proof: time-based assertion — three 50ms tools
    complete in <120ms (parallel) not 150ms+ (serial).
  - Per-tool error isolation: one tool raises → its tool_result has
    is_error=true with execution_error tag; sibling tools' results
    still present and clean.
  - Token accumulation: input + output tokens sum across iterations
    regardless of fan-out within an iteration.
  - Audit fires once per tool: 3 parallel tools → 3 audit rows; each
    carrying correct tool_status (ok / not_allowed /
    execution_error).
  - Max-iteration cap unchanged: parallel doesn't multiply the cap;
    after MAX_TOOL_USE_ITERATIONS iterations each with N tools we
    still return tool_use_max_iterations_exceeded.
  - Empty tool_use_blocks list returns empty result list (no
    asyncio.gather call needed; defensive).
  - Result order matches input block order (asyncio.gather contract
    preserves position even when completion order differs).
  - tool_use_id round-trip: each result's tool_use_id matches its
    block's id.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from kora_cli.reasoning.anthropic_engine import (
    MAX_TOOL_USE_ITERATIONS,
    MODEL_OPUS,
    OAUTH_TOKEN_ENV,
    AnthropicReasoningEngine,
)
from kora_cli.reasoning.engine import (
    ConversationContext,
    IncomingMessage,
)
from kora_cli.reasoning.tool_registry import ReasoningToolNotAllowed


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
    input_tokens: int = 100,
    output_tokens: int = 50,
):
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    r = MagicMock()
    r.content = content
    r.stop_reason = stop_reason
    r.model = MODEL_OPUS
    r.usage = usage
    return r


def _make_client(responses):
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=list(responses))
    client.close = AsyncMock()
    return client


def _msg(text: str = "what's my status and ledger?") -> IncomingMessage:
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
# Happy path — 3 parallel tools, single iteration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_three_parallel_tools_single_iteration(
    monkeypatch, system_prompt_path
):
    """3 tool_use blocks in one response → 3 tool_results in a
    single follow-up user message → final text response. Verify
    one tool API roundtrip per iteration (NOT one per tool)."""
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)

    iter1 = _response(
        content=[
            _text_block("Let me check both."),
            _tool_use_block("toolu_a", "kora__get_operational_state", {}),
            _tool_use_block("toolu_b", "kora__get_health_rollup", {}),
            _tool_use_block(
                "toolu_c", "kora__get_recent_ledger_entries", {}
            ),
        ],
        stop_reason="tool_use",
        input_tokens=150,
        output_tokens=40,
    )
    iter2 = _response(
        content=[_text_block("Daemon ready; health ok; 3 recent ledger entries.")],
        stop_reason="end_turn",
        input_tokens=300,
        output_tokens=60,
    )
    client = _make_client([iter1, iter2])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(_msg(), _ctx())

    assert result.error is None
    assert result.text.startswith("Daemon ready")
    # Three tools recorded.
    assert sorted(result.tools_used) == sorted(
        [
            "kora__get_operational_state",
            "kora__get_health_rollup",
            "kora__get_recent_ledger_entries",
        ]
    )
    # Token totals.
    assert result.input_tokens == 450  # 150 + 300
    assert result.output_tokens == 100  # 40 + 60
    # Two API roundtrips total (one tool_use iteration + one
    # end_turn). Parallel dispatch does NOT add roundtrips.
    assert client.messages.create.await_count == 2


@pytest.mark.asyncio
async def test_parallel_results_returned_in_single_user_message(
    monkeypatch, system_prompt_path
):
    """Spec §2 ST1 + Claude API docs: tool_results must be returned
    in ONE user message (NOT separate messages per tool). Verify by
    inspecting the second API call's messages payload."""
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)

    iter1 = _response(
        content=[
            _tool_use_block("toolu_a", "kora__get_operational_state", {}),
            _tool_use_block("toolu_b", "kora__get_health_rollup", {}),
        ],
        stop_reason="tool_use",
    )
    iter2 = _response(
        content=[_text_block("ok")], stop_reason="end_turn"
    )
    client = _make_client([iter1, iter2])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg(), _ctx())

    # Second create() call carries the messages list with the
    # assistant turn + the single user-tool-result turn.
    second_call_kwargs = client.messages.create.await_args_list[1].kwargs
    messages = second_call_kwargs["messages"]
    # Last user message is the tool_results bundle; it must contain
    # BOTH tool_result blocks in its single content array.
    user_results_msg = messages[-1]
    assert user_results_msg["role"] == "user"
    content = user_results_msg["content"]
    tool_results = [
        b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    assert len(tool_results) == 2, (
        "tool_result blocks MUST be bundled into a single user "
        "message — separate messages teach Claude to avoid parallel"
    )
    # tool_use_id round-trip preserved + in input order.
    assert tool_results[0]["tool_use_id"] == "toolu_a"
    assert tool_results[1]["tool_use_id"] == "toolu_b"


# ---------------------------------------------------------------------------
# Concurrency proof — timing-based
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_dispatch_uses_asyncio_gather(
    monkeypatch, system_prompt_path
):
    """Three 50ms tools complete in <120ms (parallel) not 150ms+
    (serial). Tolerance covers test-host jitter; the multiplicative
    speedup is what we're proving, not absolute wall time."""
    from kora_cli.reasoning import anthropic_engine

    sleep_ms = 50

    async def _slow_tool(name, tool_input):
        await asyncio.sleep(sleep_ms / 1000.0)
        # Return a Pydantic-like stub with model_dump_json.
        m = MagicMock()
        m.model_dump_json = lambda: '{"result": "ok"}'
        return m

    # Bypass the real tool registry — every name resolves to the
    # slow stub.
    monkeypatch.setattr(
        "kora_cli.reasoning.tool_registry.execute_reasoning_tool",
        _slow_tool,
    )

    iter1 = _response(
        content=[
            _tool_use_block("toolu_a", "kora__get_operational_state", {}),
            _tool_use_block("toolu_b", "kora__get_health_rollup", {}),
            _tool_use_block(
                "toolu_c", "kora__get_recent_ledger_entries", {}
            ),
        ],
        stop_reason="tool_use",
    )
    iter2 = _response(content=[_text_block("done")], stop_reason="end_turn")
    client = _make_client([iter1, iter2])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )

    t0 = time.monotonic()
    await engine.respond(_msg(), _ctx())
    elapsed_ms = (time.monotonic() - t0) * 1000

    # Serial would be 3*50 = 150ms minimum. Parallel should be ~50ms
    # + overhead. 120ms is a generous parallel ceiling allowing for
    # event loop scheduling jitter on a busy test host.
    assert elapsed_ms < 120, (
        f"parallel dispatch took {elapsed_ms:.0f}ms — too close to "
        f"serial baseline (150ms); suggests gather isn't firing"
    )


# ---------------------------------------------------------------------------
# Per-tool error isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_tool_fails_others_still_complete(
    monkeypatch, system_prompt_path
):
    """One tool raises mid-batch → its tool_result has is_error=true
    + tool_execution_error message; sibling tools' results unchanged
    + Claude's next iteration sees all 3."""

    async def _maybe_fail(name, tool_input):
        if name == "kora__get_health_rollup":
            raise RuntimeError("substrate connection refused")
        m = MagicMock()
        m.model_dump_json = lambda: '{"result": "ok"}'
        return m

    monkeypatch.setattr(
        "kora_cli.reasoning.tool_registry.execute_reasoning_tool",
        _maybe_fail,
    )

    iter1 = _response(
        content=[
            _tool_use_block("toolu_ok1", "kora__get_operational_state", {}),
            _tool_use_block("toolu_bad", "kora__get_health_rollup", {}),
            _tool_use_block(
                "toolu_ok2", "kora__get_recent_ledger_entries", {}
            ),
        ],
        stop_reason="tool_use",
    )
    iter2 = _response(
        content=[_text_block("Health unavailable; rest ok.")],
        stop_reason="end_turn",
    )
    client = _make_client([iter1, iter2])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(_msg(), _ctx())

    assert result.error is None
    # Only the 2 successful tools land in tools_used.
    assert sorted(result.tools_used) == sorted(
        [
            "kora__get_operational_state",
            "kora__get_recent_ledger_entries",
        ]
    )

    # Inspect the tool_results sent on the second API call.
    second_call = client.messages.create.await_args_list[1].kwargs
    user_results = second_call["messages"][-1]["content"]
    by_id = {b["tool_use_id"]: b for b in user_results}
    assert by_id["toolu_ok1"].get("is_error") is not True
    assert by_id["toolu_bad"]["is_error"] is True
    assert "tool_execution_error" in by_id["toolu_bad"]["content"]
    assert by_id["toolu_ok2"].get("is_error") is not True


@pytest.mark.asyncio
async def test_tool_not_in_allowlist_isolated(
    monkeypatch, system_prompt_path
):
    """Mutating tool requested mid-batch → ReasoningToolNotAllowed
    → tool_not_allowed tool_result; other tools complete normally."""

    async def _enforce(name, tool_input):
        from kora_cli.reasoning.tool_registry import (
            REASONING_TOOL_ALLOWLIST,
        )

        if name not in REASONING_TOOL_ALLOWLIST:
            raise ReasoningToolNotAllowed(
                f"tool {name!r} is not in the reasoning allowlist"
            )
        m = MagicMock()
        m.model_dump_json = lambda: '{"result": "ok"}'
        return m

    monkeypatch.setattr(
        "kora_cli.reasoning.tool_registry.execute_reasoning_tool",
        _enforce,
    )

    iter1 = _response(
        content=[
            _tool_use_block("toolu_ok", "kora__get_operational_state", {}),
            _tool_use_block(
                "toolu_mut", "kora__create_sea_ticket", {"title": "x"}
            ),
        ],
        stop_reason="tool_use",
    )
    iter2 = _response(content=[_text_block("filtered")], stop_reason="end_turn")
    client = _make_client([iter1, iter2])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg(), _ctx())

    second_call = client.messages.create.await_args_list[1].kwargs
    by_id = {
        b["tool_use_id"]: b
        for b in second_call["messages"][-1]["content"]
    }
    assert by_id["toolu_ok"].get("is_error") is not True
    assert by_id["toolu_mut"]["is_error"] is True
    assert "tool_not_allowed" in by_id["toolu_mut"]["content"]


# ---------------------------------------------------------------------------
# Order preservation — asyncio.gather contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_result_order_matches_input_order_even_when_completion_differs(
    monkeypatch, system_prompt_path
):
    """The slowest tool comes first in the input → it should STILL
    appear first in the returned tool_result list (asyncio.gather's
    position-preserving contract)."""

    async def _varying_speed(name, tool_input):
        delays = {
            "kora__get_operational_state": 0.05,  # slowest, listed first
            "kora__get_health_rollup": 0.01,  # fastest, listed second
        }
        await asyncio.sleep(delays.get(name, 0))
        m = MagicMock()
        m.model_dump_json = lambda: f'{{"result": "{name}"}}'
        return m

    monkeypatch.setattr(
        "kora_cli.reasoning.tool_registry.execute_reasoning_tool",
        _varying_speed,
    )

    iter1 = _response(
        content=[
            _tool_use_block(
                "toolu_slow", "kora__get_operational_state", {}
            ),
            _tool_use_block("toolu_fast", "kora__get_health_rollup", {}),
        ],
        stop_reason="tool_use",
    )
    iter2 = _response(content=[_text_block("ok")], stop_reason="end_turn")
    client = _make_client([iter1, iter2])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg(), _ctx())

    second_call = client.messages.create.await_args_list[1].kwargs
    tool_results = [
        b
        for b in second_call["messages"][-1]["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    # Position matches INPUT order, even though "fast" completed first.
    assert tool_results[0]["tool_use_id"] == "toolu_slow"
    assert tool_results[1]["tool_use_id"] == "toolu_fast"


# ---------------------------------------------------------------------------
# Empty batch — defensive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_tool_use_blocks_returns_empty_list(
    monkeypatch, system_prompt_path
):
    """Defensive: caller passes []; no gather call, no error."""
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=_make_client([])
    )
    out = await engine._execute_tool_calls(
        [],
        tools_used=[],
        triggered_by="test",
        caller_session_id="sess_x",
    )
    assert out == []


# ---------------------------------------------------------------------------
# Audit — one row per parallel tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_fires_once_per_parallel_tool(
    monkeypatch, system_prompt_path, caplog
):
    """3 parallel tools → 3 `kora.reasoning.tool_called` log lines.
    Ordering is non-deterministic (Q2 ruling); just count occurrence."""
    import logging

    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)
    caplog.set_level(logging.INFO)

    iter1 = _response(
        content=[
            _tool_use_block("toolu_a", "kora__get_operational_state", {}),
            _tool_use_block("toolu_b", "kora__get_health_rollup", {}),
            _tool_use_block(
                "toolu_c", "kora__get_recent_ledger_entries", {}
            ),
        ],
        stop_reason="tool_use",
    )
    iter2 = _response(content=[_text_block("ok")], stop_reason="end_turn")
    client = _make_client([iter1, iter2])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg(), _ctx())

    audit_lines = [
        rec.getMessage()
        for rec in caplog.records
        if "kora.reasoning.tool_called" in rec.getMessage()
    ]
    assert len(audit_lines) == 3
    # All three tool names appear (ordering varies).
    for name in (
        "kora__get_operational_state",
        "kora__get_health_rollup",
        "kora__get_recent_ledger_entries",
    ):
        assert any(name in line for line in audit_lines), name


@pytest.mark.asyncio
async def test_audit_records_per_tool_status(
    monkeypatch, system_prompt_path, caplog
):
    """One ok + one execution_error + one not_allowed in same
    batch → 3 audit rows with the correct discriminator each."""
    import logging

    caplog.set_level(logging.INFO)

    async def _mixed(name, tool_input):
        if name == "kora__get_health_rollup":
            raise RuntimeError("connection-refused")
        if name == "kora__create_sea_ticket":
            raise ReasoningToolNotAllowed(
                f"{name} not in allowlist"
            )
        m = MagicMock()
        m.model_dump_json = lambda: '{"ok": true}'
        return m

    monkeypatch.setattr(
        "kora_cli.reasoning.tool_registry.execute_reasoning_tool",
        _mixed,
    )

    iter1 = _response(
        content=[
            _tool_use_block("toolu_ok", "kora__get_operational_state", {}),
            _tool_use_block("toolu_err", "kora__get_health_rollup", {}),
            _tool_use_block(
                "toolu_mut", "kora__create_sea_ticket", {"title": "x"}
            ),
        ],
        stop_reason="tool_use",
    )
    iter2 = _response(content=[_text_block("end")], stop_reason="end_turn")
    client = _make_client([iter1, iter2])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg(), _ctx())

    audits = [
        rec.getMessage()
        for rec in caplog.records
        if "kora.reasoning.tool_called" in rec.getMessage()
    ]
    statuses = {"ok": 0, "execution_error": 0, "not_allowed": 0}
    for line in audits:
        for status in statuses:
            if f"tool_status={status}" in line:
                statuses[status] += 1
    assert statuses == {"ok": 1, "execution_error": 1, "not_allowed": 1}


# ---------------------------------------------------------------------------
# Max-iteration cap — parallel doesn't change it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_iteration_cap_unchanged_with_parallel(
    monkeypatch, system_prompt_path
):
    """A pathological model that returns 2 tool_use blocks on EVERY
    iteration must still hit the MAX_TOOL_USE_ITERATIONS cap after
    that many iterations — parallel doesn't multiply the cap."""
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)

    iter_with_parallel = _response(
        content=[
            _tool_use_block("toolu_x", "kora__get_operational_state", {}),
            _tool_use_block("toolu_y", "kora__get_health_rollup", {}),
        ],
        stop_reason="tool_use",
    )
    # MAX_TOOL_USE_ITERATIONS responses, all of them tool_use.
    responses = [iter_with_parallel] * MAX_TOOL_USE_ITERATIONS
    client = _make_client(responses)
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(_msg(), _ctx())
    assert result.error == "tool_use_max_iterations_exceeded"
    # Exactly MAX_TOOL_USE_ITERATIONS roundtrips (cap-respected),
    # even though each iter dispatched 2 tools in parallel.
    assert client.messages.create.await_count == MAX_TOOL_USE_ITERATIONS


# ---------------------------------------------------------------------------
# Default tool_choice preserved (auto, NOT forced "any")
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_choice_default_auto_not_forced_any(
    monkeypatch, system_prompt_path
):
    """The engine must NOT set tool_choice on the SDK call — the
    SDK default is auto. Forcing 'any' makes Claude call tools when
    she should just respond with text (over-eager) and changes the
    tool-system prompt token count per the API docs."""
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)

    iter1 = _response(
        content=[_text_block("hi")], stop_reason="end_turn"
    )
    client = _make_client([iter1])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg("hello"), _ctx())

    kwargs = client.messages.create.await_args_list[0].kwargs
    assert "tool_choice" not in kwargs, (
        "tool_choice must be left at the API default (auto); "
        "setting it forces tool use and over-eagers the model"
    )
    assert "disable_parallel_tool_use" not in kwargs, (
        "disable_parallel_tool_use must not be set — we WANT parallel"
    )
