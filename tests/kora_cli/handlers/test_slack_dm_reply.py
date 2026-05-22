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


@pytest.mark.asyncio
async def test_joshua_dm_triggers_echo_reply_with_locked_format(
    log_path, mock_client
):
    handler = SlackDMHandler(log_path=log_path, slack_client=mock_client)
    await handler.handle_event(_make_payload(text="ping"))

    mock_client.post_dm.assert_awaited_once()
    call_kwargs = mock_client.post_dm.await_args.kwargs
    assert call_kwargs["channel_id"] == "D01CHAN01"
    # Echo format LOCKED.
    assert call_kwargs["text"] == "Kora received: ping"
    # thread_ts defaults to event.ts when event.thread_ts is absent.
    assert call_kwargs["thread_ts"] == "1700000000.001"


@pytest.mark.asyncio
async def test_thread_ts_used_when_present(log_path, mock_client):
    handler = SlackDMHandler(log_path=log_path, slack_client=mock_client)
    await handler.handle_event(
        _make_payload(thread_ts="1699999999.000", ts="1700000000.001")
    )
    call_kwargs = mock_client.post_dm.await_args.kwargs
    # Reply threads under the original thread, not the latest message.
    assert call_kwargs["thread_ts"] == "1699999999.000"


@pytest.mark.asyncio
async def test_echo_text_truncated_at_200_chars(log_path, mock_client):
    long_text = "x" * 5000
    handler = SlackDMHandler(log_path=log_path, slack_client=mock_client)
    await handler.handle_event(_make_payload(text=long_text))
    sent_text = mock_client.post_dm.await_args.kwargs["text"]
    # "Kora received: " is 15 chars, plus up to 200 of original.
    assert sent_text.startswith("Kora received: ")
    assert len(sent_text) == len("Kora received: ") + 200


@pytest.mark.asyncio
async def test_jsonl_has_inbound_then_outbound_entry(log_path, mock_client):
    handler = SlackDMHandler(log_path=log_path, slack_client=mock_client)
    await handler.handle_event(_make_payload(text="hi"))

    entries = _read_lines(log_path)
    assert len(entries) == 2

    # Inbound first (received).
    assert entries[0]["handled_status"] == HANDLED_RECEIVED
    assert "received_at" in entries[0]
    assert "sent_at" not in entries[0]

    # Outbound second (ok).
    assert entries[1]["send_status"] == "ok"
    assert "sent_at" in entries[1]
    assert "received_at" not in entries[1]
    assert entries[1]["slack_message_ts"] == "1700000001.999"
    assert entries[1]["text"] == "Kora received: hi"


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
