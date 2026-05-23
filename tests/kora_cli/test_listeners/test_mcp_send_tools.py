"""Tests for the 2 new MCP send tools (KR-MCP-SEND-TOOLS).

Covers:
  - kora__send_slack_dm dispatcher: input validation, channel_id
    constraints, SlackClient unavailable, successful call, JSONL
    outbound entry with caller_actor_kind, token absence in error
  - kora__send_email dispatcher: input validation, recipient cap,
    PurelymailClient unavailable, successful call, JSONL outbound
    entry with caller_actor_kind, password absence in error
  - Both tools registered in ST2_TOOL_DESCRIPTORS + ST2_TOOL_DISPATCH
  - Both tools have requires_cap_gate=True
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kora_cli.clients.purelymail_types import SendResult
from kora_cli.listeners.mcp_caller_auth import Caller
from kora_cli.listeners.mcp_tools import (
    SEND_EMAIL_TOOL,
    SEND_SLACK_DM_TOOL,
    ST2_TOOL_DESCRIPTORS,
    ST2_TOOL_DISPATCH,
    SendEmailResult,
    SendSlackDmResult,
    _dispatch_send_email,
    _dispatch_send_slack_dm,
    _ST2_ToolInputError,
)


_TEST_SLACK_TOKEN = "xoxb-secret-bot-token-xyz"
_TEST_SMTP_PASSWORD = "very-secret-app-password-xyz"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Reset env + singletons per test."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "kora_constants.get_kora_home", lambda: tmp_path, raising=False
    )
    # Clear all relevant envs; tests opt in.
    for env in (
        "KORA_SLACK_BOT_TOKEN",
        "KORA_SLACK_JOSHUA_USER_ID",
        "KORA_PUREMAIL_SMTP_USERNAME",
        "KORA_PUREMAIL_SMTP_APP_PASSWORD",
        "KORA_PUREMAIL_SMTP_HOST",
        "KORA_PUREMAIL_SMTP_PORT",
        "KORA_EMAIL_KORA_ALLOWED_FROM_DOMAINS",
    ):
        monkeypatch.delenv(env, raising=False)
    # Clear listener singletons.
    from kora_cli.listeners.slack_client_listener import (
        _clear_singleton as _clear_slack,
    )
    from kora_cli.listeners.purelymail_client_listener import (
        _clear_singleton as _clear_purelymail,
    )

    _clear_slack()
    _clear_purelymail()
    yield tmp_path
    _clear_slack()
    _clear_purelymail()


def _caller(*, actor_kind: str = "claude_pm_isokron", caps=None) -> Caller:
    return Caller(
        actor_kind=actor_kind,
        allowed_caps=frozenset(caps or ()),
    )


# ===========================================================================
# Registry-side wiring
# ===========================================================================


def test_send_slack_dm_in_st2_descriptors():
    names = {d["name"] for d in ST2_TOOL_DESCRIPTORS}
    assert "kora__send_slack_dm" in names


def test_send_email_in_st2_descriptors():
    names = {d["name"] for d in ST2_TOOL_DESCRIPTORS}
    assert "kora__send_email" in names


def test_send_slack_dm_requires_cap_gate():
    assert SEND_SLACK_DM_TOOL["requires_cap_gate"] is True


def test_send_email_requires_cap_gate():
    assert SEND_EMAIL_TOOL["requires_cap_gate"] is True


def test_both_dispatchers_registered():
    assert "kora__send_slack_dm" in ST2_TOOL_DISPATCH
    assert "kora__send_email" in ST2_TOOL_DISPATCH


def test_send_slack_dm_input_schema_has_4000_char_max():
    """Slack's per-message text cap is 4000 chars."""
    text_schema = SEND_SLACK_DM_TOOL["inputSchema"]["properties"]["text"]
    assert text_schema["maxLength"] == 4000


def test_send_email_input_schema_has_recipient_cap():
    """≤10 recipients per send."""
    to_schema = SEND_EMAIL_TOOL["inputSchema"]["properties"]["to"]
    assert to_schema["maxItems"] == 10


# ===========================================================================
# kora__send_slack_dm — input validation
# ===========================================================================


@pytest.mark.asyncio
async def test_slack_empty_channel_id_raises():
    with pytest.raises(_ST2_ToolInputError, match="channel_id"):
        await _dispatch_send_slack_dm(
            {"channel_id": "", "text": "hi"}, _caller()
        )


@pytest.mark.asyncio
async def test_slack_empty_text_raises():
    with pytest.raises(_ST2_ToolInputError, match="text"):
        await _dispatch_send_slack_dm(
            {"channel_id": "D123", "text": ""}, _caller()
        )


@pytest.mark.asyncio
async def test_slack_text_over_4000_raises():
    with pytest.raises(_ST2_ToolInputError, match="4000"):
        await _dispatch_send_slack_dm(
            {"channel_id": "D123", "text": "x" * 4001}, _caller()
        )


@pytest.mark.asyncio
async def test_slack_non_dm_channel_id_rejected():
    """C-prefix (channel) + U-prefix (user) rejected at MCP layer."""
    for bad in ("C12345", "U67890"):
        with pytest.raises(_ST2_ToolInputError, match="DM"):
            await _dispatch_send_slack_dm(
                {"channel_id": bad, "text": "hi"}, _caller()
            )


@pytest.mark.asyncio
async def test_slack_joshua_user_id_match_allowed(monkeypatch):
    """KORA_SLACK_JOSHUA_USER_ID env value matching channel_id IS
    allowed (Slack auto-resolves to DM). Defense was against
    arbitrary U... IDs."""
    monkeypatch.setenv("KORA_SLACK_JOSHUA_USER_ID", "UJOSHUA")

    fake_client = MagicMock()
    fake_client.post_dm = AsyncMock(return_value={"ts": "1700.123"})

    with patch(
        "kora_cli.listeners.slack_client_listener.current_slack_client",
        return_value=fake_client,
    ):
        result = await _dispatch_send_slack_dm(
            {"channel_id": "UJOSHUA", "text": "hi"}, _caller()
        )
    assert isinstance(result, SendSlackDmResult)
    assert result.success is True
    assert result.slack_message_ts == "1700.123"


# ===========================================================================
# kora__send_slack_dm — SlackClient unavailable
# ===========================================================================


@pytest.mark.asyncio
async def test_slack_client_unavailable_raises():
    """No SlackClient registered → -32001 with slack_client_unavailable
    surface (raised as _ST2_ToolInputError which mcp.py maps to JSON-RPC
    error envelope)."""
    with patch(
        "kora_cli.listeners.slack_client_listener.current_slack_client",
        return_value=None,
    ):
        with pytest.raises(
            _ST2_ToolInputError, match="slack_client_unavailable"
        ):
            await _dispatch_send_slack_dm(
                {"channel_id": "D123", "text": "hi"}, _caller()
            )


# ===========================================================================
# kora__send_slack_dm — happy path + JSONL entry
# ===========================================================================


@pytest.mark.asyncio
async def test_slack_happy_path_returns_send_result(_isolate):
    fake_client = MagicMock()
    fake_client.post_dm = AsyncMock(return_value={"ts": "1700.456"})

    with patch(
        "kora_cli.listeners.slack_client_listener.current_slack_client",
        return_value=fake_client,
    ):
        result = await _dispatch_send_slack_dm(
            {"channel_id": "D123", "text": "hello", "thread_ts": None},
            _caller(actor_kind="claude_pm_isokron"),
        )
    assert isinstance(result, SendSlackDmResult)
    assert result.success is True
    assert result.slack_message_ts == "1700.456"
    assert result.caller_actor_kind == "claude_pm_isokron"
    fake_client.post_dm.assert_awaited_once()


@pytest.mark.asyncio
async def test_slack_outbound_jsonl_entry_includes_caller_actor_kind(
    _isolate,
):
    fake_client = MagicMock()
    fake_client.post_dm = AsyncMock(return_value={"ts": "1700.789"})

    with patch(
        "kora_cli.listeners.slack_client_listener.current_slack_client",
        return_value=fake_client,
    ):
        await _dispatch_send_slack_dm(
            {"channel_id": "D123", "text": "audit me"},
            _caller(actor_kind="kora_drone_7"),
        )

    log_path = _isolate / "slack_dm_log.jsonl"
    assert log_path.exists()
    # Last line is the outbound entry from this MCP call
    last = json.loads(log_path.read_text().splitlines()[-1])
    assert last["caller_actor_kind"] == "kora_drone_7"
    assert last["channel_id"] == "D123"
    assert last["text"] == "audit me"
    assert last["send_status"] == "ok"


# ===========================================================================
# kora__send_slack_dm — token absence in error
# ===========================================================================


@pytest.mark.asyncio
async def test_slack_send_failure_does_not_leak_token(_isolate):
    """When post_dm raises with text that might contain the token,
    the dispatcher's error envelope strips it (sanitized to
    type-name only)."""
    fake_client = MagicMock()
    fake_client.post_dm = AsyncMock(
        side_effect=RuntimeError(
            f"transport failed (token={_TEST_SLACK_TOKEN})"
        )
    )

    with patch(
        "kora_cli.listeners.slack_client_listener.current_slack_client",
        return_value=fake_client,
    ):
        with pytest.raises(_ST2_ToolInputError) as exc_info:
            await _dispatch_send_slack_dm(
                {"channel_id": "D123", "text": "hi"}, _caller()
            )
    # Sanitization: only type-name surfaces; token absent
    assert _TEST_SLACK_TOKEN not in str(exc_info.value)
    assert "RuntimeError" in str(exc_info.value)


# ===========================================================================
# kora__send_email — input validation
# ===========================================================================


@pytest.mark.asyncio
async def test_email_empty_to_raises():
    with pytest.raises(_ST2_ToolInputError, match="to"):
        await _dispatch_send_email(
            {"to": [], "subject": "x", "body_text": "x"}, _caller()
        )


@pytest.mark.asyncio
async def test_email_empty_subject_raises():
    with pytest.raises(_ST2_ToolInputError, match="subject"):
        await _dispatch_send_email(
            {"to": ["a@b.com"], "subject": "", "body_text": "x"},
            _caller(),
        )


@pytest.mark.asyncio
async def test_email_too_many_recipients_raises():
    with pytest.raises(_ST2_ToolInputError, match="max 10"):
        await _dispatch_send_email(
            {
                "to": [f"r{i}@example.com" for i in range(11)],
                "subject": "x",
                "body_text": "x",
            },
            _caller(),
        )


@pytest.mark.asyncio
async def test_email_malformed_recipient_raises():
    with pytest.raises(_ST2_ToolInputError, match="malformed"):
        await _dispatch_send_email(
            {
                "to": ["a@b.com", "not-an-email"],
                "subject": "x",
                "body_text": "x",
            },
            _caller(),
        )


# ===========================================================================
# kora__send_email — PurelymailClient unavailable
# ===========================================================================


@pytest.mark.asyncio
async def test_email_client_unavailable_raises():
    with patch(
        "kora_cli.listeners.purelymail_client_listener.current_purelymail_client",
        return_value=None,
    ):
        with pytest.raises(
            _ST2_ToolInputError, match="purelymail_client_unavailable"
        ):
            await _dispatch_send_email(
                {"to": ["a@b.com"], "subject": "x", "body_text": "x"},
                _caller(),
            )


@pytest.mark.asyncio
async def test_email_missing_username_env_raises(monkeypatch):
    """Even with a registered PurelymailClient, the MCP tool reads
    KORA_PUREMAIL_SMTP_USERNAME to set from_addr — if it's unset
    we reject before any send."""
    fake_client = MagicMock()
    monkeypatch.delenv("KORA_PUREMAIL_SMTP_USERNAME", raising=False)
    with patch(
        "kora_cli.listeners.purelymail_client_listener.current_purelymail_client",
        return_value=fake_client,
    ):
        with pytest.raises(
            _ST2_ToolInputError, match="KORA_PUREMAIL_SMTP_USERNAME"
        ):
            await _dispatch_send_email(
                {"to": ["a@b.com"], "subject": "x", "body_text": "x"},
                _caller(),
            )


# ===========================================================================
# kora__send_email — happy path
# ===========================================================================


@pytest.mark.asyncio
async def test_email_happy_path_returns_send_result(_isolate, monkeypatch):
    monkeypatch.setenv(
        "KORA_PUREMAIL_SMTP_USERNAME", "kora@stormhavenenterprises.com"
    )

    fake_client = MagicMock()
    fake_send_result = SendResult(
        status="ok",
        message_id="<test@stormhavenenterprises.com>",
        error=None,
        smtp_code=250,
        sent_at=datetime.now(timezone.utc),
        retry_count=0,
    )
    fake_client.send_email = AsyncMock(return_value=fake_send_result)

    with patch(
        "kora_cli.listeners.purelymail_client_listener.current_purelymail_client",
        return_value=fake_client,
    ):
        result = await _dispatch_send_email(
            {
                "to": ["joshua@stormhavenenterprises.com"],
                "subject": "hello",
                "body_text": "hi",
            },
            _caller(actor_kind="claude_pm_isokron"),
        )
    assert isinstance(result, SendEmailResult)
    assert result.success is True
    assert result.smtp_code == 250
    assert result.caller_actor_kind == "claude_pm_isokron"
    assert result.error is None

    # Verify the client was called with from_addr derived from env
    # AND caller_actor_kind threaded through
    call_kwargs = fake_client.send_email.await_args.kwargs
    assert call_kwargs["from_addr"] == "kora@stormhavenenterprises.com"
    assert call_kwargs["caller_actor_kind"] == "claude_pm_isokron"
    # Attachments NOT supported in this bucket
    assert call_kwargs.get("attachments") is None


# ===========================================================================
# kora__send_email — password absence in error
# ===========================================================================


@pytest.mark.asyncio
async def test_email_failure_does_not_leak_password(_isolate, monkeypatch):
    monkeypatch.setenv(
        "KORA_PUREMAIL_SMTP_USERNAME", "kora@stormhavenenterprises.com"
    )

    fake_client = MagicMock()
    fake_client.send_email = AsyncMock(
        side_effect=RuntimeError(
            f"auth failed (password={_TEST_SMTP_PASSWORD})"
        )
    )

    with patch(
        "kora_cli.listeners.purelymail_client_listener.current_purelymail_client",
        return_value=fake_client,
    ):
        with pytest.raises(_ST2_ToolInputError) as exc_info:
            await _dispatch_send_email(
                {
                    "to": ["joshua@stormhavenenterprises.com"],
                    "subject": "x",
                    "body_text": "x",
                },
                _caller(),
            )
    # Sanitization: only type-name surfaces; password absent
    assert _TEST_SMTP_PASSWORD not in str(exc_info.value)
    assert "RuntimeError" in str(exc_info.value)


# ===========================================================================
# Defense-in-depth: caller_actor_kind propagation
# ===========================================================================


@pytest.mark.asyncio
async def test_email_caller_actor_kind_threaded_through_to_send_email(
    _isolate, monkeypatch
):
    """Verify the actor_kind from the caller object reaches
    PurelymailClient.send_email's caller_actor_kind kwarg."""
    monkeypatch.setenv(
        "KORA_PUREMAIL_SMTP_USERNAME", "kora@stormhavenenterprises.com"
    )

    fake_client = MagicMock()
    fake_client.send_email = AsyncMock(
        return_value=SendResult(
            status="ok",
            message_id="<x@y>",
            smtp_code=250,
            sent_at=datetime.now(timezone.utc),
            retry_count=0,
        )
    )

    with patch(
        "kora_cli.listeners.purelymail_client_listener.current_purelymail_client",
        return_value=fake_client,
    ):
        await _dispatch_send_email(
            {
                "to": ["joshua@stormhavenenterprises.com"],
                "subject": "x",
                "body_text": "x",
            },
            _caller(actor_kind="kora_drone_42"),
        )
    assert (
        fake_client.send_email.await_args.kwargs["caller_actor_kind"]
        == "kora_drone_42"
    )
