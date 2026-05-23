"""Tests for ST2 audit logging in the reasoning engine —
KR-FEAT-AGENTIC-REASONING ST2.

Covers:
  - [kora.reasoning.tool_called] structured log per tool call
  - Each log line carries tool_name + triggered_by +
    caller_session_id + tool_duration_ms + tool_status
  - tool_status enum: "ok" / "not_allowed" / "execution_error"
  - caller_session_id derivation for slack_dm / email / mcp
  - Tool-call audit log NEVER contains tool input or output bodies
  - Malformed Claude tool input → tool_result error +
    audit "execution_error" + engine does NOT crash (validation
    strictness per spec §2 ST2(c))
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from kora_cli.reasoning.anthropic_engine import (
    AnthropicReasoningEngine,
    OAUTH_TOKEN_ENV,
    _derive_caller_session_id,
)
from kora_cli.reasoning.engine import ConversationContext, IncomingMessage


# ---------------------------------------------------------------------------
# Helpers — share with the existing tool-use test file
# ---------------------------------------------------------------------------


def _text_block(text: str):
    b = MagicMock()
    b.type = "text"
    b.text = text
    return b


def _tool_use_block(tid: str, name: str, tool_input: Dict[str, Any]):
    b = MagicMock()
    b.type = "tool_use"
    b.id = tid
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
    r.model = "claude-opus-4-7"
    r.usage = usage
    return r


def _make_client(responses):
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=list(responses))
    client.close = AsyncMock()
    return client


def _slack_msg(
    text: str = "what's my status?",
    channel: str = "D01CHAN01",
    event_ts: str = "1700000000.001",
) -> IncomingMessage:
    return IncomingMessage(
        text=text,
        source="slack_dm",
        received_at=datetime.now(timezone.utc),
        metadata={"channel_id": channel, "event_ts": event_ts},
    )


def _ctx() -> ConversationContext:
    return ConversationContext(
        recent_messages=[],
        current_operational_state="ready",
        current_cost_ladder_rung="normal",
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
# caller_session_id derivation
# ---------------------------------------------------------------------------


def test_session_id_slack_dm_combines_channel_and_event_ts():
    msg = IncomingMessage(
        text="hi",
        source="slack_dm",
        received_at=datetime.now(timezone.utc),
        metadata={"channel_id": "D02FOO", "event_ts": "1700000111.222"},
    )
    assert _derive_caller_session_id(msg) == "D02FOO:1700000111.222"


def test_session_id_slack_dm_missing_event_ts_uses_channel_only():
    msg = IncomingMessage(
        text="hi",
        source="slack_dm",
        received_at=datetime.now(timezone.utc),
        metadata={"channel_id": "D02FOO"},
    )
    assert _derive_caller_session_id(msg) == "slack_dm:D02FOO"


def test_session_id_slack_dm_empty_metadata_falls_back():
    msg = IncomingMessage(
        text="hi",
        source="slack_dm",
        received_at=datetime.now(timezone.utc),
        metadata={},
    )
    assert _derive_caller_session_id(msg) == "slack_dm:unknown"


def test_session_id_email_uses_message_id():
    msg = IncomingMessage(
        text="hello",
        source="email",
        received_at=datetime.now(timezone.utc),
        metadata={"message_id": "<abc-123@purelymail>"},
    )
    assert _derive_caller_session_id(msg) == "email:<abc-123@purelymail>"


def test_session_id_mcp_uses_actor_and_tool():
    msg = IncomingMessage(
        text="trigger",
        source="mcp",
        received_at=datetime.now(timezone.utc),
        metadata={
            "caller_actor_kind": "claude_pm_isokron",
            "tool_name": "kora__create_sea_ticket",
        },
    )
    assert (
        _derive_caller_session_id(msg)
        == "mcp:claude_pm_isokron:kora__create_sea_ticket"
    )


# ---------------------------------------------------------------------------
# Audit log emission — tool_status="ok"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_log_on_successful_tool_call(
    monkeypatch, system_prompt_path, caplog
):
    """A successful tool call emits one
    [kora.reasoning.tool_called] log with the expected fields."""
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(
        "kora_cli.listeners.mcp_tools._get_active_provider", lambda: None
    )

    iter1 = _response(
        content=[
            _tool_use_block("t1", "kora__get_operational_state", {})
        ],
        stop_reason="tool_use",
    )
    iter2 = _response(
        content=[_text_block("ready")],
        stop_reason="end_turn",
    )
    client = _make_client([iter1, iter2])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(
        _slack_msg(channel="DCHAN42", event_ts="1700.001"),
        _ctx(),
    )

    audits = [
        r for r in caplog.records
        if "kora.reasoning.tool_called" in r.getMessage()
    ]
    assert len(audits) == 1
    msg = audits[0].getMessage()
    assert "tool=kora__get_operational_state" in msg
    assert "triggered_by=slack_dm" in msg
    assert "caller_session_id=DCHAN42:1700.001" in msg
    assert "tool_status=ok" in msg
    assert "tool_duration_ms=" in msg


# ---------------------------------------------------------------------------
# Audit log — tool_status="not_allowed"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_log_on_tool_not_allowed(
    monkeypatch, system_prompt_path, caplog
):
    """Claude requests a mutating tool → tool_status='not_allowed'
    audit log + engine continues."""
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(
        "kora_cli.listeners.mcp_tools._get_active_provider", lambda: None
    )

    iter1 = _response(
        content=[
            _tool_use_block(
                "tbad",
                "kora__create_sea_ticket",  # mutating; blocked
                {"title": "x"},
            )
        ],
        stop_reason="tool_use",
    )
    iter2 = _response(
        content=[_text_block("can't do that")],
        stop_reason="end_turn",
    )
    client = _make_client([iter1, iter2])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_slack_msg(), _ctx())

    audits = [
        r for r in caplog.records
        if "kora.reasoning.tool_called" in r.getMessage()
    ]
    assert len(audits) == 1
    msg = audits[0].getMessage()
    assert "tool=kora__create_sea_ticket" in msg
    assert "tool_status=not_allowed" in msg


# ---------------------------------------------------------------------------
# Audit log — tool_status="execution_error"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_log_on_execution_error(
    monkeypatch, system_prompt_path, caplog
):
    """Tool executor raises → tool_status='execution_error' audit
    log with exc_type + engine continues."""
    caplog.set_level(logging.INFO)
    from kora_cli.reasoning import tool_registry

    async def _raises(name, tool_input):
        raise RuntimeError("substrate connection refused")

    monkeypatch.setattr(
        tool_registry, "execute_reasoning_tool", _raises
    )

    iter1 = _response(
        content=[
            _tool_use_block("tx", "kora__get_operational_state", {})
        ],
        stop_reason="tool_use",
    )
    iter2 = _response(
        content=[_text_block("got an error")],
        stop_reason="end_turn",
    )
    client = _make_client([iter1, iter2])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_slack_msg(), _ctx())

    audits = [
        r for r in caplog.records
        if "kora.reasoning.tool_called" in r.getMessage()
    ]
    assert len(audits) == 1
    msg = audits[0].getMessage()
    assert "tool_status=execution_error" in msg
    assert "exc_type=RuntimeError" in msg


# ---------------------------------------------------------------------------
# Audit log — NEVER contains tool input or output bodies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_log_excludes_tool_input_body(
    monkeypatch, system_prompt_path, caplog
):
    """Tool input args + the inbound Joshua text must NEVER appear
    in the audit log lines — operator-data hygiene."""
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(
        "kora_cli.listeners.mcp_tools._get_active_provider", lambda: None
    )

    sensitive_marker = "TOP_SECRET_BODY_CONTENT_DO_NOT_LEAK"
    iter1 = _response(
        content=[
            _tool_use_block(
                "tt",
                "kora__get_recent_ledger_entries",
                # Tool input that includes sensitive-shaped data.
                {"status": "allocated", "limit": 50, "notes": sensitive_marker},
            )
        ],
        stop_reason="tool_use",
    )
    iter2 = _response(
        content=[_text_block(f"some content with {sensitive_marker}")],
        stop_reason="end_turn",
    )
    client = _make_client([iter1, iter2])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(
        _slack_msg(text=f"please check {sensitive_marker}"),
        _ctx(),
    )

    # Audit lines never contain the marker (which lived only in tool
    # input + final text). They only carry tool_name / status / ids.
    audit_text = " ".join(
        r.getMessage()
        for r in caplog.records
        if "kora.reasoning.tool_called" in r.getMessage()
    )
    assert sensitive_marker not in audit_text


# ---------------------------------------------------------------------------
# Multiple tool calls → multiple audit lines
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_log_one_line_per_tool_call(
    monkeypatch, system_prompt_path, caplog
):
    """3 tools across 2 iterations → 3 audit log entries."""
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(
        "kora_cli.listeners.mcp_tools._get_active_provider", lambda: None
    )

    # Iteration 1: 2 tools in parallel (single response with 2
    # tool_use blocks).
    iter1 = _response(
        content=[
            _tool_use_block("t1", "kora__get_operational_state", {}),
            _tool_use_block("t2", "kora__get_health_rollup", {}),
        ],
        stop_reason="tool_use",
    )
    # Iteration 2: 1 more tool.
    iter2 = _response(
        content=[
            _tool_use_block("t3", "kora__list_active_sea_tickets", {})
        ],
        stop_reason="tool_use",
    )
    iter3 = _response(
        content=[_text_block("all good")],
        stop_reason="end_turn",
    )
    client = _make_client([iter1, iter2, iter3])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_slack_msg(), _ctx())

    audits = [
        r for r in caplog.records
        if "kora.reasoning.tool_called" in r.getMessage()
    ]
    assert len(audits) == 3
    # All three carry tool_status=ok (no failures).
    assert all("tool_status=ok" in r.getMessage() for r in audits)


# ---------------------------------------------------------------------------
# Validation strictness — malformed Claude tool input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_claude_tool_input_does_not_crash(
    monkeypatch, system_prompt_path, caplog
):
    """Claude generates tool input with the wrong shape (e.g.
    unknown kwarg name that the executor rejects with TypeError).
    Engine MUST surface this as a tool_result error + continue,
    NOT crash."""
    caplog.set_level(logging.INFO)
    from kora_cli.reasoning import tool_registry

    # Force the executor to raise TypeError as if it received a
    # bad kwarg from Claude.
    async def _typeerror(name, tool_input):
        # Mirrors what a Pydantic validation failure / unknown kwarg
        # looks like at the dispatcher boundary.
        raise TypeError(
            f"_execute_get_recent_ledger_entries() got unexpected "
            f"keyword argument {list(tool_input.keys())[0]!r}"
        )

    monkeypatch.setattr(
        tool_registry, "execute_reasoning_tool", _typeerror
    )

    iter1 = _response(
        content=[
            _tool_use_block(
                "tbad",
                "kora__get_recent_ledger_entries",
                # Claude hallucinated a kwarg name.
                {"made_up_kwarg": "value"},
            )
        ],
        stop_reason="tool_use",
    )
    iter2 = _response(
        content=[_text_block("recovered")],
        stop_reason="end_turn",
    )
    client = _make_client([iter1, iter2])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )

    # Engine returns cleanly with the recovery text.
    result = await engine.respond(_slack_msg(), _ctx())
    assert result.error is None
    assert result.text == "recovered"

    # Audit log records execution_error for the bad call.
    audits = [
        r for r in caplog.records
        if "kora.reasoning.tool_called" in r.getMessage()
    ]
    # Engine catches the TypeError at execute_tool_calls + emits one
    # audit. Other audit entries from the 2nd-iter (no tool call)
    # shouldn't exist.
    assert len(audits) == 1
    assert "tool_status=execution_error" in audits[0].getMessage()
    assert "exc_type=TypeError" in audits[0].getMessage()

    # The 2nd API call's messages history includes a tool_result
    # error block — Claude got to see the error + recover.
    second_call = client.messages.create.await_args_list[1]
    history = second_call.kwargs["messages"]
    found_tool_result_error = False
    for turn in history:
        if isinstance(turn.get("content"), list):
            for block in turn["content"]:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    and block.get("is_error") is True
                ):
                    found_tool_result_error = True
                    assert "tool_execution_error" in block["content"]
    assert found_tool_result_error
