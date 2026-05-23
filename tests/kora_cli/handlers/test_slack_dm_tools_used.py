"""Tests for the ST2 outbound-JSONL ``tools_used`` extension —
KR-FEAT-AGENTIC-REASONING ST2.

Covers:
  - Successful reasoning with no tools → tools_used=[] in JSONL
  - Successful reasoning with N tools → tools_used=[<names>]
  - Canned fallback paths (engine unavailable / engine error /
    engine exception) → tools_used key OMITTED from JSONL (None
    placeholder distinguishes "engine bypassed" from "engine ran
    with zero tools")
  - Distinct tool names + duplicates preserved
  - Schema backwards-compat: existing test fixtures that don't
    set tools_used still produce valid JSONL
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest

from kora_cli.handlers.slack_dm_handler import (
    HANDLED_RECEIVED,
    JOSHUA_USER_ID_ENV,
    SlackDMHandler,
)
from kora_cli.reasoning.engine import ResponseResult


JOSHUA_ID = "UJOSHUA01"


def _make_payload(
    *,
    user: str = JOSHUA_ID,
    channel: str = "D01CHAN01",
    text: str = "hello",
    ts: str = "1700000000.001",
    thread_ts: str | None = None,
) -> Dict[str, Any]:
    event: Dict[str, Any] = {
        "type": "message",
        "user": user,
        "channel": channel,
        "channel_type": "im",
        "text": text,
        "ts": ts,
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    return {"type": "event_callback", "event": event}


def _read_outbound_entries(log_path: Path) -> List[Dict[str, Any]]:
    """Filter to outbound-only entries (key=sent_at)."""
    if not log_path.exists():
        return []
    return [
        entry
        for entry in (
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        if "sent_at" in entry
    ]


@pytest.fixture
def log_path(tmp_path) -> Path:
    return tmp_path / "slack_dm_log.jsonl"


@pytest.fixture(autouse=True)
def _joshua_env(monkeypatch):
    monkeypatch.setenv(JOSHUA_USER_ID_ENV, JOSHUA_ID)


@pytest.fixture(autouse=True)
def _reset_state_holder(monkeypatch):
    from agent import operational_state_holder as h_mod

    monkeypatch.setattr(h_mod, "_HOLDER", None)


@pytest.fixture
def slack_mock():
    class _Mock:
        def __init__(self):
            self.post_dm = AsyncMock(
                return_value={"ok": True, "ts": "1700000001.999"}
            )

    return _Mock()


def _make_engine(
    *,
    text: str = "thoughtful reply",
    tools_used: List[str] = None,
    error: str | None = None,
):
    """Build a mock ReasoningEngine returning a configurable
    ResponseResult."""

    class _MockEngine:
        def __init__(self):
            self.respond = AsyncMock(
                return_value=ResponseResult(
                    text=text,
                    model_used="claude-opus-4-7",
                    input_tokens=120,
                    output_tokens=60,
                    reasoning_duration_ms=42,
                    error=error,
                    tools_used=tools_used or [],
                )
            )
            self.close = AsyncMock()

    return _MockEngine()


# ---------------------------------------------------------------------------
# tools_used populated correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_used_empty_list_when_engine_used_no_tools(
    log_path, slack_mock
):
    """Engine ran successfully + chose to use no tools → JSONL
    outbound entry has ``tools_used: []`` (NOT omitted) so
    consumers can distinguish from canned-fallback path."""
    engine = _make_engine(tools_used=[])
    handler = SlackDMHandler(
        log_path=log_path, slack_client=slack_mock, reasoning_engine=engine
    )
    await handler.handle_event(_make_payload(text="hi"))

    [out] = _read_outbound_entries(log_path)
    assert out["send_status"] == "ok"
    assert "tools_used" in out
    assert out["tools_used"] == []


@pytest.mark.asyncio
async def test_tools_used_populated_with_tool_names(log_path, slack_mock):
    """Engine called tools → JSONL has the tool names in order."""
    engine = _make_engine(
        tools_used=[
            "kora__get_operational_state",
            "kora__get_health_rollup",
        ]
    )
    handler = SlackDMHandler(
        log_path=log_path, slack_client=slack_mock, reasoning_engine=engine
    )
    await handler.handle_event(_make_payload())

    [out] = _read_outbound_entries(log_path)
    assert out["tools_used"] == [
        "kora__get_operational_state",
        "kora__get_health_rollup",
    ]


@pytest.mark.asyncio
async def test_tools_used_preserves_duplicates(log_path, slack_mock):
    """Same tool called across multiple iterations → list keeps
    every entry. Operator counting tool calls relies on this."""
    engine = _make_engine(
        tools_used=[
            "kora__get_operational_state",
            "kora__get_operational_state",
            "kora__get_health_rollup",
        ]
    )
    handler = SlackDMHandler(
        log_path=log_path, slack_client=slack_mock, reasoning_engine=engine
    )
    await handler.handle_event(_make_payload())

    [out] = _read_outbound_entries(log_path)
    assert out["tools_used"].count("kora__get_operational_state") == 2
    assert out["tools_used"].count("kora__get_health_rollup") == 1


# ---------------------------------------------------------------------------
# tools_used OMITTED on canned-fallback paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_used_omitted_when_engine_unavailable(
    log_path, slack_mock
):
    """No reasoning engine injected + no listener singleton →
    canned-fallback path. JSONL must NOT contain tools_used key
    (distinguishes from engine-ran-with-zero-tools)."""
    handler = SlackDMHandler(log_path=log_path, slack_client=slack_mock)
    await handler.handle_event(_make_payload())

    [out] = _read_outbound_entries(log_path)
    assert "tools_used" not in out
    assert out["reasoning_error"] == "engine_unavailable"


@pytest.mark.asyncio
async def test_tools_used_omitted_when_engine_returns_error(
    log_path, slack_mock
):
    """Engine ResponseResult.error set → canned fallback;
    tools_used omitted."""
    engine = _make_engine(text="", error="cost_ladder_halted")
    handler = SlackDMHandler(
        log_path=log_path, slack_client=slack_mock, reasoning_engine=engine
    )
    await handler.handle_event(_make_payload())

    [out] = _read_outbound_entries(log_path)
    assert "tools_used" not in out
    assert out["reasoning_error"] == "cost_ladder_halted"


@pytest.mark.asyncio
async def test_tools_used_omitted_when_engine_raises(log_path, slack_mock):
    """Engine itself raises → canned fallback; tools_used omitted."""

    class _CrashingEngine:
        def __init__(self):
            self.respond = AsyncMock(side_effect=RuntimeError("boom"))
            self.close = AsyncMock()

    engine = _CrashingEngine()
    handler = SlackDMHandler(
        log_path=log_path, slack_client=slack_mock, reasoning_engine=engine
    )
    await handler.handle_event(_make_payload())

    [out] = _read_outbound_entries(log_path)
    assert "tools_used" not in out
    assert "engine_exception" in out["reasoning_error"]


# ---------------------------------------------------------------------------
# Schema backwards-compatibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_jsonl_consumers_see_consistent_outbound_shape(
    log_path, slack_mock
):
    """Mix of paths in the same JSONL file — consumer code can
    branch on key presence + always finds well-formed entries."""
    # Path 1: engine ran + used tools.
    engine_tools = _make_engine(
        tools_used=["kora__get_operational_state"]
    )
    handler = SlackDMHandler(
        log_path=log_path,
        slack_client=slack_mock,
        reasoning_engine=engine_tools,
    )
    await handler.handle_event(_make_payload(text="check status", ts="1.001"))

    # Path 2: engine ran + chose no tools.
    engine_no_tools = _make_engine(tools_used=[])
    handler2 = SlackDMHandler(
        log_path=log_path,
        slack_client=slack_mock,
        reasoning_engine=engine_no_tools,
    )
    await handler2.handle_event(_make_payload(text="hi there", ts="1.002"))

    # Path 3: engine unavailable (canned fallback).
    handler3 = SlackDMHandler(log_path=log_path, slack_client=slack_mock)
    await handler3.handle_event(_make_payload(text="anything", ts="1.003"))

    outbound = _read_outbound_entries(log_path)
    assert len(outbound) == 3

    # Each entry: validate the schema branches consumers will use.
    by_text = {e["text"]: e for e in outbound}
    real_with_tools = next(
        e for e in outbound if e.get("tools_used")
    )
    real_no_tools = next(
        e for e in outbound if e.get("tools_used") == []
    )
    canned = next(
        e for e in outbound if "tools_used" not in e
    )

    assert real_with_tools["tools_used"] == ["kora__get_operational_state"]
    assert real_no_tools["tools_used"] == []
    assert "reasoning_error" in canned
    assert canned["reasoning_error"] == "engine_unavailable"
