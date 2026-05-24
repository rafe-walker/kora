"""Tests for the ST2 outbound-reply integration in ``SlackDMHandler``.

Covers:
  - Joshua DM → inbound 'received' entry + outbound 'ok' entry,
    in that order
  - Bot token missing → outbound failed entry with
    'slack_client_not_configured' + reply_failed log emit
  - SlackTransportError (e.g. 429 exhausted) → outbound failed entry
    with 'transport:429'
  - SlackAPIError (channel_not_found) → outbound failed entry with
    'slack_api:channel_not_found'
  - Reply failure does NOT crash the handler (still returns ok)
  - Filtered events (non-Joshua, bot, subtype) do NOT trigger reply
  - PAUSED state drops do NOT trigger reply
  - Echo format LOCKED: "Kora received: {text[:200]}"
  - thread_ts = event.thread_ts if present, else event.ts
  - Bot token NEVER appears in JSONL (security)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest

from kora_cli.clients.slack_client import (
    SlackAPIError,
    SlackClientNotConfigured,
    SlackTransportError,
)
from kora_cli.handlers.slack_dm_handler import (
    HANDLED_FILTERED_NON_JOSHUA,
    HANDLED_RECEIVED,
    JOSHUA_USER_ID_ENV,
    SlackDMHandler,
)


JOSHUA_ID = "UJOSHUA01"


def _make_payload(
    *,
    user: str = JOSHUA_ID,
    channel: str = "D01CHAN01",
    text: str = "hello kora",
    ts: str = "1700000000.001",
    thread_ts: str | None = None,
    channel_type: str = "im",
    bot_id: str | None = None,
    subtype: str | None = None,
) -> Dict[str, Any]:
    event: Dict[str, Any] = {
        "type": "message",
        "user": user,
        "channel": channel,
        "channel_type": channel_type,
        "text": text,
        "ts": ts,
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    if bot_id is not None:
        event["bot_id"] = bot_id
    if subtype is not None:
        event["subtype"] = subtype
    return {"type": "event_callback", "event": event}


def _read_lines(log_path: Path) -> List[Dict[str, Any]]:
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.fixture
def log_path(tmp_path) -> Path:
    return tmp_path / "slack_dm_log.jsonl"


@pytest.fixture(autouse=True)
def _joshua_env(monkeypatch):
    monkeypatch.setenv(JOSHUA_USER_ID_ENV, JOSHUA_ID)


@pytest.fixture(autouse=True)
def _reset_holder(monkeypatch):
    from agent import operational_state_holder as h_mod

    monkeypatch.setattr(h_mod, "_HOLDER", None)


@pytest.fixture
def mock_client():
    """A SlackClient stand-in with a configurable post_dm AsyncMock."""

    class _MockClient:
        def __init__(self):
            self.post_dm = AsyncMock(
                return_value={"ok": True, "ts": "1700000001.999"}
            )

    return _MockClient()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def _make_reasoning_engine(
    text: str = "kora's thoughtful reply",
    model: str = "claude-opus-4-7",
    input_tokens: int = 120,
    output_tokens: int = 80,
    error: str | None = None,
):
    """Build an AsyncMock-style ReasoningEngine returning a fixed
    ResponseResult. ST2 wires this in place of the prior echo path —
    tests that previously asserted echo behavior now assert against
    the mocked engine's text."""
    from unittest.mock import AsyncMock

    from kora_cli.reasoning.engine import ResponseResult

    class _MockEngine:
        def __init__(self):
            self.respond = AsyncMock(
                return_value=ResponseResult(
                    text=text,
                    model_used=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    reasoning_duration_ms=42,
                    error=error,
                )
            )
            self.close = AsyncMock()

    return _MockEngine()


@pytest.mark.asyncio
async def test_joshua_dm_triggers_reasoning_reply(log_path, mock_client):
    """ST2 swap: handler now calls the reasoning engine instead of
    constructing the echo. The engine's `text` is what gets sent."""
    engine = _make_reasoning_engine(text="here is your answer")
    handler = SlackDMHandler(
        log_path=log_path,
        slack_client=mock_client,
        reasoning_engine=engine,
    )
    await handler.handle_event(_make_payload(text="ping"))

    engine.respond.assert_awaited_once()
    mock_client.post_dm.assert_awaited_once()
    call_kwargs = mock_client.post_dm.await_args.kwargs
    assert call_kwargs["channel_id"] == "D01CHAN01"
    # Engine's response text is what Slack receives — NOT an echo.
    assert call_kwargs["text"] == "here is your answer"
    # thread_ts defaults to event.ts when event.thread_ts is absent.
    assert call_kwargs["thread_ts"] == "1700000000.001"


@pytest.mark.asyncio
async def test_thread_ts_used_when_present(log_path, mock_client):
    """Reply threads under the original thread, not the latest msg.
    Independent of the reply text — the threading logic is the same
    pre + post ST2."""
    handler = SlackDMHandler(
        log_path=log_path,
        slack_client=mock_client,
        reasoning_engine=_make_reasoning_engine(),
    )
    await handler.handle_event(
        _make_payload(thread_ts="1699999999.000", ts="1700000000.001")
    )
    call_kwargs = mock_client.post_dm.await_args.kwargs
    assert call_kwargs["thread_ts"] == "1699999999.000"


@pytest.mark.asyncio
async def test_reasoning_engine_receives_full_inbound_text(
    log_path, mock_client
):
    """ST2 swap removes the prior 200-char echo truncation. The
    reasoning engine sees the full inbound text + decides its own
    response length per the system prompt."""
    long_text = "x" * 5000
    engine = _make_reasoning_engine(text="short engine reply")
    handler = SlackDMHandler(
        log_path=log_path,
        slack_client=mock_client,
        reasoning_engine=engine,
    )
    await handler.handle_event(_make_payload(text=long_text))

    # Engine got the full untruncated message.
    engine_call = engine.respond.await_args
    assert engine_call.args[0].text == long_text  # IncomingMessage.text
    # Slack received the engine's response, not a truncated echo.
    sent_text = mock_client.post_dm.await_args.kwargs["text"]
    assert sent_text == "short engine reply"


@pytest.mark.asyncio
async def test_jsonl_has_inbound_then_outbound_entry_with_reasoning_meta(
    log_path, mock_client
):
    """ST2 schema extension: outbound entries carry the new
    reasoning fields (model_used, input_tokens, output_tokens,
    reasoning_duration_ms) on successful reasoning calls."""
    engine = _make_reasoning_engine(
        text="thoughtful answer",
        model="claude-opus-4-7",
        input_tokens=150,
        output_tokens=60,
    )
    handler = SlackDMHandler(
        log_path=log_path,
        slack_client=mock_client,
        reasoning_engine=engine,
    )
    await handler.handle_event(_make_payload(text="hi"))

    entries = _read_lines(log_path)
    assert len(entries) == 2

    # Inbound first (received).
    assert entries[0]["handled_status"] == HANDLED_RECEIVED
    assert "received_at" in entries[0]
    assert "sent_at" not in entries[0]

    # Outbound second (ok) — with reasoning meta.
    out = entries[1]
    assert out["send_status"] == "ok"
    assert "sent_at" in out
    assert "received_at" not in out
    assert out["slack_message_ts"] == "1700000001.999"
    assert out["text"] == "thoughtful answer"
    # New ST2 fields populated on successful reasoning.
    assert out["model_used"] == "claude-opus-4-7"
    assert out["input_tokens"] == 150
    assert out["output_tokens"] == 60
    assert out["reasoning_duration_ms"] == 42
    # No reasoning_error key on success.
    assert "reasoning_error" not in out


# ---------------------------------------------------------------------------
# Bot token missing → outbound failed entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_bot_token_writes_failed_outbound_entry(
    log_path, monkeypatch, caplog
):
    """No KORA_SLACK_BOT_TOKEN → SlackClient lazy-construct fails →
    outbound entry with failure_reason='slack_client_not_configured'."""
    caplog.set_level(logging.WARNING)
    monkeypatch.delenv("KORA_SLACK_BOT_TOKEN", raising=False)

    # NO slack_client injected → handler tries to lazy-construct.
    handler = SlackDMHandler(log_path=log_path)
    result = await handler.handle_event(_make_payload(text="hi"))
    assert result == {"ok": True}

    entries = _read_lines(log_path)
    assert len(entries) == 2
    assert entries[1]["send_status"] == "failed"
    assert entries[1]["failure_reason"] == "slack_client_not_configured"
    assert entries[1]["slack_message_ts"] is None

    # reply_failed structured log emitted.
    assert any(
        "kora.slack_dm.reply_failed" in r.getMessage()
        and "slack_client_not_configured" in r.getMessage()
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# SlackTransportError → outbound failed entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_transport_error_429_writes_failed_outbound(
    log_path, mock_client, caplog
):
    caplog.set_level(logging.WARNING)
    mock_client.post_dm.side_effect = SlackTransportError(
        "rate-limited after retry", last_status=429
    )

    handler = SlackDMHandler(log_path=log_path, slack_client=mock_client)
    result = await handler.handle_event(_make_payload())
    # Inbound handler still returns ok despite outbound failure.
    assert result == {"ok": True}

    entries = _read_lines(log_path)
    assert entries[1]["send_status"] == "failed"
    assert entries[1]["failure_reason"] == "transport:429"


@pytest.mark.asyncio
async def test_slack_transport_error_500_writes_failed_outbound(
    log_path, mock_client
):
    mock_client.post_dm.side_effect = SlackTransportError(
        "5xx after retry", last_status=500
    )
    handler = SlackDMHandler(log_path=log_path, slack_client=mock_client)
    await handler.handle_event(_make_payload())
    [_, out] = _read_lines(log_path)
    assert out["failure_reason"] == "transport:500"


@pytest.mark.asyncio
async def test_slack_transport_error_timeout_writes_failed_outbound(
    log_path, mock_client
):
    """Transport-level failure (no HTTP status) → reason='transport:<exc_name>'."""
    mock_client.post_dm.side_effect = SlackTransportError(
        "timeout exhausted", last_status=None
    )
    handler = SlackDMHandler(log_path=log_path, slack_client=mock_client)
    await handler.handle_event(_make_payload())
    [_, out] = _read_lines(log_path)
    assert out["failure_reason"] == "transport:SlackTransportError"


# ---------------------------------------------------------------------------
# SlackAPIError → outbound failed entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_api_error_writes_failed_outbound(
    log_path, mock_client
):
    mock_client.post_dm.side_effect = SlackAPIError(
        "channel_not_found", raw_response={"ok": False, "error": "channel_not_found"}
    )
    handler = SlackDMHandler(log_path=log_path, slack_client=mock_client)
    await handler.handle_event(_make_payload())
    [_, out] = _read_lines(log_path)
    assert out["failure_reason"] == "slack_api:channel_not_found"


# ---------------------------------------------------------------------------
# Reply failure does NOT crash inbound handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reply_failure_does_not_crash_handler(log_path, mock_client):
    """Even an unexpected exception type (not in our error hierarchy)
    must NOT crash handle_event. Outbound entry should still be written."""
    mock_client.post_dm.side_effect = RuntimeError("unexpected boom")

    handler = SlackDMHandler(log_path=log_path, slack_client=mock_client)
    result = await handler.handle_event(_make_payload())
    assert result == {"ok": True}
    [_, out] = _read_lines(log_path)
    assert out["send_status"] == "failed"
    # Stable reason code mapping.
    assert out["failure_reason"] == "transport:RuntimeError"


# ---------------------------------------------------------------------------
# Filtered events do NOT trigger reply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_joshua_no_reply_call(log_path, mock_client):
    handler = SlackDMHandler(log_path=log_path, slack_client=mock_client)
    await handler.handle_event(_make_payload(user="USOMEONE"))
    mock_client.post_dm.assert_not_awaited()
    # JSONL has only the inbound filtered entry — no outbound.
    entries = _read_lines(log_path)
    assert len(entries) == 1
    assert entries[0]["handled_status"] == HANDLED_FILTERED_NON_JOSHUA


@pytest.mark.asyncio
async def test_bot_message_no_reply_call(log_path, mock_client):
    handler = SlackDMHandler(log_path=log_path, slack_client=mock_client)
    await handler.handle_event(_make_payload(bot_id="B01"))
    mock_client.post_dm.assert_not_awaited()


@pytest.mark.asyncio
async def test_subtype_no_reply_call(log_path, mock_client):
    handler = SlackDMHandler(log_path=log_path, slack_client=mock_client)
    await handler.handle_event(_make_payload(subtype="message_changed"))
    mock_client.post_dm.assert_not_awaited()


@pytest.mark.asyncio
async def test_paused_state_no_reply_call(log_path, mock_client, monkeypatch):
    """PAUSED state drops the message → reply MUST NOT fire (we don't
    process Joshua's DM during a pause)."""
    from agent.operational_state import OperationalState, PrimaryState
    from agent.operational_state_holder import OperationalStateHolder
    from agent import operational_state_holder as h_mod

    monkeypatch.setattr(
        h_mod,
        "_HOLDER",
        OperationalStateHolder(
            OperationalState(primary_state=PrimaryState.PAUSED)
        ),
    )

    handler = SlackDMHandler(log_path=log_path, slack_client=mock_client)
    await handler.handle_event(_make_payload())
    mock_client.post_dm.assert_not_awaited()


# ---------------------------------------------------------------------------
# Bot token NEVER in JSONL — security regression test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bot_token_never_in_jsonl(log_path, monkeypatch, mock_client):
    """Diverse paths: success, transport-error, api-error, missing-token,
    plus filtered events. After all paths, the bot token env value
    MUST NOT appear in the JSONL."""
    token_marker = "xoxb-secret-bot-token-DO-NOT-LEAK"
    monkeypatch.setenv("KORA_SLACK_BOT_TOKEN", token_marker)

    handler = SlackDMHandler(log_path=log_path, slack_client=mock_client)

    # Success path.
    await handler.handle_event(_make_payload(text="hi", ts="1.001"))

    # Transport error path.
    mock_client.post_dm.side_effect = SlackTransportError(
        "rate-limited", last_status=429
    )
    await handler.handle_event(_make_payload(text="oops", ts="1.002"))

    # API error path.
    mock_client.post_dm.side_effect = SlackAPIError(
        "channel_not_found", raw_response={"ok": False, "error": "channel_not_found"}
    )
    await handler.handle_event(_make_payload(text="bad", ts="1.003"))

    # Filtered (no reply call).
    await handler.handle_event(_make_payload(user="USOMEONE", ts="1.004"))

    contents = log_path.read_text(encoding="utf-8")
    assert token_marker not in contents, (
        "bot token env value appeared in JSONL — handler must NEVER "
        "log secret material"
    )


# ===========================================================================
# KR-FEAT-AI-RESPONSE-LOOP ST2 — reasoning integration paths
# ===========================================================================


@pytest.mark.asyncio
async def test_engine_unavailable_sends_canned_fallback(
    log_path, mock_client, caplog
):
    """No injected engine + listener accessor returns None →
    canned fallback text sent + outbound JSONL records
    reasoning_error='engine_unavailable'. Handler does NOT crash."""
    caplog.set_level(logging.WARNING)

    # No reasoning_engine injection — handler will call the listener
    # accessor, which returns None outside a running daemon.
    handler = SlackDMHandler(log_path=log_path, slack_client=mock_client)
    result = await handler.handle_event(_make_payload(text="hi"))
    assert result == {"ok": True}

    # Canned text sent to Slack.
    sent_text = mock_client.post_dm.await_args.kwargs["text"]
    assert sent_text == handler._CANNED_FALLBACK_TEXT

    # Outbound JSONL has reasoning_error.
    out = _read_lines(log_path)[1]
    assert out["send_status"] == "ok"
    assert out["text"] == handler._CANNED_FALLBACK_TEXT
    assert out["reasoning_error"] == "engine_unavailable"
    # No reasoning meta fields (None values not written per the
    # entry-builder's "if X is not None" gate).
    assert "model_used" not in out
    assert "input_tokens" not in out

    # Structured-log line recorded.
    assert any(
        "reasoning_skipped" in r.getMessage()
        and "engine_unavailable" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_engine_returns_error_sends_canned_fallback(
    log_path, mock_client, caplog
):
    """Engine returns ResponseResult(error='cost_ladder_halted') →
    canned text sent + outbound records the error code. Handler
    does NOT crash."""
    caplog.set_level(logging.WARNING)
    engine = _make_reasoning_engine(
        text="",  # engine signals "no real text" on error
        model="",
        input_tokens=0,
        output_tokens=0,
        error="cost_ladder_halted",
    )
    handler = SlackDMHandler(
        log_path=log_path,
        slack_client=mock_client,
        reasoning_engine=engine,
    )
    await handler.handle_event(_make_payload(text="hi"))

    sent_text = mock_client.post_dm.await_args.kwargs["text"]
    assert sent_text == handler._CANNED_FALLBACK_TEXT

    out = _read_lines(log_path)[1]
    assert out["reasoning_error"] == "cost_ladder_halted"
    assert out["text"] == handler._CANNED_FALLBACK_TEXT
    # Reasoning meta IS captured even on error (operator triage).
    assert out["reasoning_duration_ms"] == 42

    assert any(
        "reasoning_failed" in r.getMessage()
        and "cost_ladder_halted" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_engine_paused_state_sends_canned_fallback(
    log_path, mock_client
):
    """Engine error='operational_state_paused' → canned text."""
    engine = _make_reasoning_engine(
        text="",
        error="operational_state_paused",
    )
    handler = SlackDMHandler(
        log_path=log_path,
        slack_client=mock_client,
        reasoning_engine=engine,
    )
    await handler.handle_event(_make_payload(text="hi"))
    out = _read_lines(log_path)[1]
    assert out["reasoning_error"] == "operational_state_paused"
    assert out["text"] == handler._CANNED_FALLBACK_TEXT


@pytest.mark.asyncio
async def test_engine_raises_exception_sends_canned_fallback(
    log_path, mock_client, caplog
):
    """Engine itself raises (not just sets ResponseResult.error) —
    handler catches defensively + sends canned text + records
    'engine_exception:<class>'."""
    from unittest.mock import AsyncMock

    caplog.set_level(logging.WARNING)

    class _CrashingEngine:
        def __init__(self):
            self.respond = AsyncMock(side_effect=RuntimeError("boom"))
            self.close = AsyncMock()

    engine = _CrashingEngine()
    handler = SlackDMHandler(
        log_path=log_path,
        slack_client=mock_client,
        reasoning_engine=engine,
    )
    result = await handler.handle_event(_make_payload(text="hi"))
    assert result == {"ok": True}

    sent_text = mock_client.post_dm.await_args.kwargs["text"]
    assert sent_text == handler._CANNED_FALLBACK_TEXT

    out = _read_lines(log_path)[1]
    assert out["reasoning_error"] == "engine_exception:RuntimeError"


@pytest.mark.asyncio
async def test_empty_engine_text_on_success_falls_back(
    log_path, mock_client, caplog
):
    """Defensive: engine returns ResponseResult with error=None but
    empty text → canned fallback (Joshua shouldn't see a blank
    message)."""
    caplog.set_level(logging.WARNING)
    engine = _make_reasoning_engine(text="   ", error=None)
    handler = SlackDMHandler(
        log_path=log_path,
        slack_client=mock_client,
        reasoning_engine=engine,
    )
    await handler.handle_event(_make_payload(text="hi"))

    sent_text = mock_client.post_dm.await_args.kwargs["text"]
    assert sent_text == handler._CANNED_FALLBACK_TEXT
    out = _read_lines(log_path)[1]
    assert out["reasoning_error"] == "empty_response_text"


@pytest.mark.asyncio
async def test_reasoning_engine_receives_message_metadata(
    log_path, mock_client
):
    """Verify the IncomingMessage passed to engine.respond carries
    the source + channel_id + thread_ts + user_id metadata the
    engine may want to use in its prompt context."""
    engine = _make_reasoning_engine()
    handler = SlackDMHandler(
        log_path=log_path,
        slack_client=mock_client,
        reasoning_engine=engine,
    )
    await handler.handle_event(
        _make_payload(text="hi", thread_ts="1699999.000", ts="1700000.001")
    )

    msg = engine.respond.await_args.args[0]
    assert msg.text == "hi"
    assert msg.source == "slack_dm"
    assert msg.metadata["channel_id"] == "D01CHAN01"
    assert msg.metadata["thread_ts"] == "1699999.000"
    assert msg.metadata["user_id"] == JOSHUA_ID
    assert msg.metadata["event_ts"] == "1700000.001"


# ---------------------------------------------------------------------------
# Cost-ladder integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_ladder_record_inference_called_on_success(
    log_path, mock_client, monkeypatch
):
    """Successful reasoning call → cost-ladder record_inference()
    invoked with CanonicalUsage built from result tokens +
    provider='anthropic'."""
    from agent.cost_state_holder import (
        CostStateHolder,
        init_cost_holder,
        _reset_cost_holder_for_tests,
    )
    from datetime import datetime, timezone

    _reset_cost_holder_for_tests()
    init_cost_holder(
        billing_period_start=datetime.now(timezone.utc),
        credit_pool_usd=200.0,
        extra_usage_off=True,
    )

    # Spy on holder.record_inference to verify the call.
    from agent import cost_state_holder as csh_mod
    real_holder = csh_mod._HOLDER
    record_calls: list = []

    original = real_holder.record_inference

    def _spy(
        canonical_usage,
        *,
        model_name,
        provider=None,
        base_url=None,
        route="unknown",
        escalated_to_opus=False,
    ):
        # KR-CHEAP-COST-TELEMETRY (#161) added route +
        # escalated_to_opus; KR-HAIKU-ROUTER passes route="slack_dm"
        # from the handler's _record_inference_to_cost_ladder. Spy
        # accepts both so the assertion path stays clean.
        record_calls.append(
            {
                "input_tokens": canonical_usage.input_tokens,
                "output_tokens": canonical_usage.output_tokens,
                "model_name": model_name,
                "provider": provider,
                "route": route,
                "escalated_to_opus": escalated_to_opus,
            }
        )
        return original(
            canonical_usage,
            model_name=model_name,
            provider=provider,
            base_url=base_url,
            route=route,
            escalated_to_opus=escalated_to_opus,
        )

    monkeypatch.setattr(real_holder, "record_inference", _spy)

    engine = _make_reasoning_engine(
        text="answer",
        model="claude-opus-4-7",
        input_tokens=150,
        output_tokens=60,
    )
    handler = SlackDMHandler(
        log_path=log_path,
        slack_client=mock_client,
        reasoning_engine=engine,
    )
    await handler.handle_event(_make_payload(text="hi"))

    assert len(record_calls) == 1
    assert record_calls[0]["input_tokens"] == 150
    assert record_calls[0]["output_tokens"] == 60
    assert record_calls[0]["model_name"] == "claude-opus-4-7"
    assert record_calls[0]["provider"] == "anthropic"

    _reset_cost_holder_for_tests()


@pytest.mark.asyncio
async def test_cost_ladder_skipped_on_canned_fallback(
    log_path, mock_client, monkeypatch
):
    """Canned-fallback path must NOT call record_inference — there
    was no real inference to bill."""
    from agent.cost_state_holder import (
        init_cost_holder,
        _reset_cost_holder_for_tests,
    )
    from agent import cost_state_holder as csh_mod
    from datetime import datetime, timezone

    _reset_cost_holder_for_tests()
    init_cost_holder(
        billing_period_start=datetime.now(timezone.utc),
        credit_pool_usd=200.0,
        extra_usage_off=True,
    )

    record_calls: list = []
    original = csh_mod._HOLDER.record_inference

    def _spy(canonical_usage, *, model_name, provider=None, base_url=None):
        record_calls.append(model_name)

    monkeypatch.setattr(csh_mod._HOLDER, "record_inference", _spy)

    # Engine returns an error → canned fallback → NO cost-ladder write.
    engine = _make_reasoning_engine(
        text="", error="cost_ladder_halted"
    )
    handler = SlackDMHandler(
        log_path=log_path,
        slack_client=mock_client,
        reasoning_engine=engine,
    )
    await handler.handle_event(_make_payload(text="hi"))

    assert record_calls == []
    _reset_cost_holder_for_tests()


@pytest.mark.asyncio
async def test_cost_ladder_skipped_when_holder_uninitialized(
    log_path, mock_client
):
    """No cost holder → handler skips silently; no crash."""
    from agent import cost_state_holder as csh_mod

    # Ensure holder is None.
    csh_mod._reset_cost_holder_for_tests()

    engine = _make_reasoning_engine(
        text="answer", model="claude-opus-4-7", input_tokens=10, output_tokens=5
    )
    handler = SlackDMHandler(
        log_path=log_path,
        slack_client=mock_client,
        reasoning_engine=engine,
    )
    result = await handler.handle_event(_make_payload(text="hi"))
    assert result == {"ok": True}
    # Outbound still recorded.
    out = _read_lines(log_path)[1]
    assert out["send_status"] == "ok"
    assert out["model_used"] == "claude-opus-4-7"
