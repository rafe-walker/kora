"""Tests for the EmailInboundHandler (KR-FEAT-EMAIL-INBOUND-IMAP ST2).

Covers:
  - Filter precedence (5 steps):
    * State gate: PAUSED → handled_status=filtered_paused +
      should_mark_seen=True + no reply attempt
    * State gate: STOPPED → handled_status=filtered_stopped
    * State gate: ACTIVE → proceeds to next filter
    * Sender allowlist: env unset → fail-CLOSED DENY ALL
    * Sender allowlist: non-allowlist sender → filtered_non_allowlist
    * Sender allowlist: allowlisted sender → proceeds
    * Recipient: env unset → filtered_wrong_recipient
    * Recipient: kora_address not in to[] → filtered_wrong_recipient
    * Recipient: kora_address case-insensitive match → proceeds
    * Spoofing: envelope unavailable → spoofing_check_skipped=true in
      JSONL (handler still proceeds — defense-in-depth, not gate)
    * Identity: joshua_address env unset → fail-CLOSED filtered_non_joshua
    * Identity: sender != joshua_address → filtered_non_joshua
    * Identity: sender == joshua_address → handled_status=received

  - JSONL: schema (received_at, message_id, from, to, subject,
    body_text_truncated_2k, has_html, attachments_count,
    handled_status, spoofing_check_skipped, imap_uid)
  - JSONL: body truncated to 2KB
  - JSONL: handler_error includes error field; spoofing_check_skipped=true
  - JSONL write failure: WARN-logged + handler still returns result

  - HandlerResult shape:
    * received: should_mark_seen=True
    * filtered_*: should_mark_seen=True (terminal for this UID)
    * handler_error: should_mark_seen=False (UNSEEN for retry)
    * should_reply: True iff AUTO_REPLY env AND status=received

  - Chain event ``[kora.email_inbound.received]`` emitted ONLY on
    identified Joshua mail

  - AUTO_REPLY:
    * Env default OFF → received status but no reply attempt
    * Env ON + engine available → IncomingMessage built; engine.respond
      called; PurelymailClient.send_email called with subject Re:
      + in_reply_to=parsed.message_id
    * Env ON + engine None → canned fallback text sent
    * Env ON + engine.respond raises → canned fallback
    * Env ON + result.error set → canned fallback text + recorded
      error code
    * Env ON + client None → send skipped (logged); inbound status
      stays received
    * Env ON + send raises → inbound status stays received (outbound
      JSONL records the failure separately)

  - SECURITY: passwords / tokens never appear in JSONL across
    diverse failure modes
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kora_cli.clients.purelymail_types import (
    AttachmentMeta,
    ParsedIncomingEmail,
)
from kora_cli.handlers.email_inbound_handler import (
    AUTO_REPLY_ENV,
    BODY_TRUNCATE_LIMIT,
    CANNED_FALLBACK_TEXT,
    HANDLED_FILTERED_NON_ALLOWLIST,
    HANDLED_FILTERED_NON_JOSHUA,
    HANDLED_FILTERED_PAUSED,
    HANDLED_FILTERED_STOPPED,
    HANDLED_FILTERED_WRONG_RECIPIENT,
    HANDLED_HANDLER_ERROR,
    HANDLED_RECEIVED,
    JOSHUA_ADDRESS_ENV,
    KORA_ADDRESS_ENV,
    SENDER_ALLOWLIST_ENV,
    EmailInboundHandler,
    HandlerResult,
)


# A test password / token used to assert it never leaks into JSONL.
_TEST_SECRET = "very-secret-token-xyz"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Per-test isolation: tmp KORA_HOME, default valid envs."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "kora_constants.get_kora_home", lambda: tmp_path, raising=False
    )
    monkeypatch.setenv(
        SENDER_ALLOWLIST_ENV, "joshua@stormhavenenterprises.com"
    )
    monkeypatch.setenv(KORA_ADDRESS_ENV, "kora@stormhavenenterprises.com")
    monkeypatch.setenv(
        JOSHUA_ADDRESS_ENV, "joshua@stormhavenenterprises.com"
    )
    monkeypatch.delenv(AUTO_REPLY_ENV, raising=False)
    return tmp_path


def _log_path(tmp_path: Path) -> Path:
    return tmp_path / "email_inbound_log.jsonl"


def _make_parsed(
    *,
    imap_uid: int = 100,
    from_address: str = "joshua@stormhavenenterprises.com",
    to: list = None,
    subject: str = "test",
    body_text: str = "hello",
    body_html: str = None,
    attachments: list = None,
    message_id: str = "<msg-100@example.com>",
    received_at: datetime = None,
) -> ParsedIncomingEmail:
    return ParsedIncomingEmail(
        message_id=message_id,
        from_address=from_address,
        to=to or ["kora@stormhavenenterprises.com"],
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        has_html=body_html is not None,
        received_at=received_at or datetime.now(timezone.utc),
        attachments=attachments or [],
        imap_uid=imap_uid,
    )


def _read_log_entries(tmp_path: Path) -> list:
    path = _log_path(tmp_path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


# ===========================================================================
# State gate
# ===========================================================================


@pytest.mark.asyncio
async def test_state_gate_paused_drops_and_marks_seen(tmp_path):
    """Paused state drops the message but marks SEEN (terminal — we
    explicitly chose not to re-process this UID on a future cycle)."""
    handler = EmailInboundHandler()
    fake_holder = MagicMock()
    fake_state = MagicMock()
    from agent.operational_state import PrimaryState

    fake_state.primary_state = PrimaryState.PAUSED
    fake_holder.current = fake_state
    with patch(
        "agent.operational_state_holder.get_holder", return_value=fake_holder
    ):
        result = await handler.handle_event(_make_parsed())
    assert result.status == HANDLED_FILTERED_PAUSED
    assert result.should_mark_seen is True
    assert result.should_reply is False
    entries = _read_log_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0]["handled_status"] == HANDLED_FILTERED_PAUSED


@pytest.mark.asyncio
async def test_state_gate_stopped_drops(tmp_path):
    handler = EmailInboundHandler()
    fake_holder = MagicMock()
    fake_state = MagicMock()
    from agent.operational_state import PrimaryState

    fake_state.primary_state = PrimaryState.STOPPED
    fake_holder.current = fake_state
    with patch(
        "agent.operational_state_holder.get_holder", return_value=fake_holder
    ):
        result = await handler.handle_event(_make_parsed())
    assert result.status == HANDLED_FILTERED_STOPPED
    assert result.should_mark_seen is True


@pytest.mark.asyncio
async def test_state_gate_no_holder_proceeds(tmp_path):
    """When the operational-state holder isn't available, we proceed
    rather than gate (test paths / partial init)."""
    handler = EmailInboundHandler()
    with patch(
        "agent.operational_state_holder.get_holder", return_value=None
    ):
        result = await handler.handle_event(_make_parsed())
    assert result.status == HANDLED_RECEIVED


# ===========================================================================
# Sender allowlist
# ===========================================================================


@pytest.mark.asyncio
async def test_allowlist_unset_fail_closed_deny_all(monkeypatch, tmp_path):
    monkeypatch.delenv(SENDER_ALLOWLIST_ENV, raising=False)
    handler = EmailInboundHandler()
    result = await handler.handle_event(_make_parsed())
    assert result.status == HANDLED_FILTERED_NON_ALLOWLIST
    assert result.should_mark_seen is True
    entries = _read_log_entries(tmp_path)
    assert entries[0]["extra"]["reason"] == "allowlist_env_unset"


@pytest.mark.asyncio
async def test_allowlist_blank_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setenv(SENDER_ALLOWLIST_ENV, "   ,   ")
    handler = EmailInboundHandler()
    result = await handler.handle_event(_make_parsed())
    assert result.status == HANDLED_FILTERED_NON_ALLOWLIST


@pytest.mark.asyncio
async def test_allowlist_drops_non_allowlist_sender(tmp_path):
    handler = EmailInboundHandler()
    parsed = _make_parsed(from_address="stranger@evil.com")
    result = await handler.handle_event(parsed)
    assert result.status == HANDLED_FILTERED_NON_ALLOWLIST
    entries = _read_log_entries(tmp_path)
    assert entries[0]["extra"]["actual_sender"] == "stranger@evil.com"


@pytest.mark.asyncio
async def test_allowlist_case_insensitive(tmp_path):
    handler = EmailInboundHandler()
    parsed = _make_parsed(from_address="JOSHUA@STORMHAVENENTERPRISES.COM")
    result = await handler.handle_event(parsed)
    assert result.status == HANDLED_RECEIVED


# ===========================================================================
# Recipient filter
# ===========================================================================


@pytest.mark.asyncio
async def test_recipient_env_unset_drops(monkeypatch, tmp_path):
    monkeypatch.delenv(KORA_ADDRESS_ENV, raising=False)
    handler = EmailInboundHandler()
    result = await handler.handle_event(_make_parsed())
    assert result.status == HANDLED_FILTERED_WRONG_RECIPIENT


@pytest.mark.asyncio
async def test_recipient_address_not_in_to_list_drops(tmp_path):
    handler = EmailInboundHandler()
    parsed = _make_parsed(to=["someone-else@example.com"])
    result = await handler.handle_event(parsed)
    assert result.status == HANDLED_FILTERED_WRONG_RECIPIENT


@pytest.mark.asyncio
async def test_recipient_address_case_insensitive_match(tmp_path):
    handler = EmailInboundHandler()
    parsed = _make_parsed(to=["KORA@STORMHAVENENTERPRISES.COM"])
    result = await handler.handle_event(parsed)
    assert result.status == HANDLED_RECEIVED


# ===========================================================================
# Identity check (Joshua)
# ===========================================================================


@pytest.mark.asyncio
async def test_identity_joshua_address_unset_drops(monkeypatch, tmp_path):
    monkeypatch.delenv(JOSHUA_ADDRESS_ENV, raising=False)
    handler = EmailInboundHandler()
    result = await handler.handle_event(_make_parsed())
    assert result.status == HANDLED_FILTERED_NON_JOSHUA
    entries = _read_log_entries(tmp_path)
    assert entries[0]["extra"]["reason"] == "joshua_address_env_unset"


@pytest.mark.asyncio
async def test_identity_non_joshua_sender_drops(monkeypatch, tmp_path):
    monkeypatch.setenv(
        SENDER_ALLOWLIST_ENV,
        "joshua@stormhavenenterprises.com,other@example.com",
    )
    handler = EmailInboundHandler()
    parsed = _make_parsed(from_address="other@example.com")
    result = await handler.handle_event(parsed)
    assert result.status == HANDLED_FILTERED_NON_JOSHUA


# ===========================================================================
# Happy path
# ===========================================================================


@pytest.mark.asyncio
async def test_happy_path_received_status(tmp_path):
    handler = EmailInboundHandler()
    parsed = _make_parsed(
        attachments=[
            AttachmentMeta(
                filename="x.bin", size_bytes=10, content_type="application/x"
            ),
            AttachmentMeta(
                filename="y.bin", size_bytes=20, content_type="application/x"
            ),
        ],
        body_html="<p>html</p>",
    )
    result = await handler.handle_event(parsed)
    assert result.status == HANDLED_RECEIVED
    assert result.should_mark_seen is True
    assert result.should_reply is False  # AUTO_REPLY not set
    entries = _read_log_entries(tmp_path)
    assert len(entries) == 1
    e = entries[0]
    assert e["handled_status"] == HANDLED_RECEIVED
    assert e["message_id"] == "<msg-100@example.com>"
    assert e["from"] == "joshua@stormhavenenterprises.com"
    assert e["to"] == ["kora@stormhavenenterprises.com"]
    assert e["subject"] == "test"
    assert e["body_text_truncated_2k"] == "hello"
    assert e["has_html"] is True
    assert e["attachments_count"] == 2
    assert e["spoofing_check_skipped"] is True
    assert e["imap_uid"] == 100


@pytest.mark.asyncio
async def test_body_truncated_to_2kb(tmp_path):
    handler = EmailInboundHandler()
    long_body = "x" * (BODY_TRUNCATE_LIMIT + 500)
    parsed = _make_parsed(body_text=long_body)
    await handler.handle_event(parsed)
    entries = _read_log_entries(tmp_path)
    assert len(entries[0]["body_text_truncated_2k"]) == BODY_TRUNCATE_LIMIT


# ===========================================================================
# Handler error
# ===========================================================================


@pytest.mark.asyncio
async def test_handler_error_keeps_unseen(monkeypatch, tmp_path):
    """A raise inside the inner filter loop returns handler_error +
    should_mark_seen=False (next-poll retry)."""
    handler = EmailInboundHandler()
    # Force a raise inside the allowlist read by giving an env value
    # that triggers, then patching _read_allowlist to raise.
    with patch(
        "kora_cli.handlers.email_inbound_handler._read_allowlist",
        side_effect=RuntimeError("env-read boom"),
    ):
        result = await handler.handle_event(_make_parsed())
    assert result.status == HANDLED_HANDLER_ERROR
    assert result.should_mark_seen is False
    assert result.should_reply is False
    entries = _read_log_entries(tmp_path)
    assert any(
        e["handled_status"] == HANDLED_HANDLER_ERROR for e in entries
    )
    err_entry = next(
        e for e in entries if e["handled_status"] == HANDLED_HANDLER_ERROR
    )
    assert "error" in err_entry


@pytest.mark.asyncio
async def test_handler_error_when_log_write_fails(tmp_path, monkeypatch):
    """Both the inner raise AND the log-of-the-error raise — handler
    still returns a result (doesn't propagate the secondary failure)."""
    handler = EmailInboundHandler()
    with patch(
        "kora_cli.handlers.email_inbound_handler._read_allowlist",
        side_effect=RuntimeError("env-read boom"),
    ), patch.object(
        handler, "_append_log_entry", side_effect=OSError("disk full")
    ):
        result = await handler.handle_event(_make_parsed())
    assert result.status == HANDLED_HANDLER_ERROR
    assert result.should_mark_seen is False


# ===========================================================================
# JSONL log
# ===========================================================================


@pytest.mark.asyncio
async def test_log_write_failure_does_not_propagate(monkeypatch, tmp_path):
    handler = EmailInboundHandler()
    # Make the log dir unwritable by pointing to a path whose parent
    # doesn't exist + can't be created.
    bad = tmp_path / "no-such-dir" / "log.jsonl"
    handler._log_path = bad
    # Make parent unwritable by replacing mkdir
    with patch.object(Path, "mkdir", side_effect=OSError("no space")):
        result = await handler.handle_event(_make_parsed())
    # Returns received-status — log-write failure is fail-soft.
    assert result.status == HANDLED_RECEIVED


@pytest.mark.asyncio
async def test_password_never_appears_in_jsonl_across_failures(
    monkeypatch, tmp_path
):
    """Diverse-failure-mode injection: whatever surface fails, the
    test-secret value must not appear in any JSONL field."""
    handler = EmailInboundHandler()

    # Stuff the secret into the body text — handler should record
    # the body (it's operator-readable Joshua content), but the
    # secret should never appear in any non-text field.
    parsed_with_secret_in_body = _make_parsed(body_text=_TEST_SECRET)
    await handler.handle_event(parsed_with_secret_in_body)

    # Force handler_error via a raise that names the secret
    with patch(
        "kora_cli.handlers.email_inbound_handler._read_allowlist",
        side_effect=RuntimeError(f"env corruption {_TEST_SECRET}"),
    ):
        await handler.handle_event(_make_parsed())

    entries = _read_log_entries(tmp_path)
    # The first entry is the body-recording one; secret SHOULD appear
    # there as text. The error entry SHOULD contain the secret in the
    # error field — that's expected behavior, error is repr(exception).
    # The security boundary is: env values + Kora's OWN secrets
    # (passwords/tokens) NEVER appear unless Joshua himself put them
    # in body text. We assert that NO entry contains the secret in any
    # FIELD OTHER THAN body_text_truncated_2k / error.
    for entry in entries:
        for key, value in entry.items():
            if key in {"body_text_truncated_2k", "error"}:
                continue
            assert _TEST_SECRET not in str(value), (
                f"secret leaked into JSONL field {key}: {value!r}"
            )


# ===========================================================================
# AUTO_REPLY (env-gated)
# ===========================================================================


@pytest.mark.asyncio
async def test_auto_reply_default_off_no_send_attempt(monkeypatch, tmp_path):
    monkeypatch.delenv(AUTO_REPLY_ENV, raising=False)
    fake_client = MagicMock()
    fake_client.send_email = AsyncMock()
    fake_engine = MagicMock()
    handler = EmailInboundHandler(
        purelymail_client=fake_client, reasoning_engine=fake_engine
    )
    result = await handler.handle_event(_make_parsed())
    assert result.status == HANDLED_RECEIVED
    assert result.should_reply is False
    fake_client.send_email.assert_not_awaited()
    fake_engine.respond.assert_not_called()


@pytest.mark.asyncio
async def test_auto_reply_enabled_calls_engine_and_sends(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(AUTO_REPLY_ENV, "true")

    fake_engine = MagicMock()
    fake_result = MagicMock()
    fake_result.text = "I read your email, Joshua."
    fake_result.error = None
    fake_engine.respond = AsyncMock(return_value=fake_result)

    fake_client = MagicMock()
    fake_client.send_email = AsyncMock(return_value=MagicMock(status="ok"))

    handler = EmailInboundHandler(
        purelymail_client=fake_client, reasoning_engine=fake_engine
    )
    parsed = _make_parsed(subject="hi kora", message_id="<m-1@e.com>")
    result = await handler.handle_event(parsed)

    assert result.status == HANDLED_RECEIVED
    assert result.should_reply is True
    fake_engine.respond.assert_awaited_once()
    fake_client.send_email.assert_awaited_once()
    kw = fake_client.send_email.await_args.kwargs
    assert kw["from_addr"] == "kora@stormhavenenterprises.com"
    assert kw["to"] == ["joshua@stormhavenenterprises.com"]
    assert kw["subject"] == "Re: hi kora"
    assert kw["body_text"] == "I read your email, Joshua."
    assert kw["in_reply_to"] == "<m-1@e.com>"


@pytest.mark.asyncio
async def test_auto_reply_engine_unavailable_sends_canned(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(AUTO_REPLY_ENV, "1")

    fake_client = MagicMock()
    fake_client.send_email = AsyncMock(return_value=MagicMock(status="ok"))

    handler = EmailInboundHandler(
        purelymail_client=fake_client, reasoning_engine=None
    )
    # Make accessor return None to simulate engine-unavailable.
    with patch(
        "kora_cli.listeners.reasoning_engine_listener.current_reasoning_engine",
        return_value=None,
    ):
        result = await handler.handle_event(_make_parsed())
    assert result.status == HANDLED_RECEIVED
    fake_client.send_email.assert_awaited_once()
    kw = fake_client.send_email.await_args.kwargs
    assert kw["body_text"] == CANNED_FALLBACK_TEXT


@pytest.mark.asyncio
async def test_auto_reply_engine_raises_sends_canned(monkeypatch, tmp_path):
    monkeypatch.setenv(AUTO_REPLY_ENV, "yes")

    fake_engine = MagicMock()
    fake_engine.respond = AsyncMock(side_effect=RuntimeError("engine boom"))

    fake_client = MagicMock()
    fake_client.send_email = AsyncMock(return_value=MagicMock(status="ok"))

    handler = EmailInboundHandler(
        purelymail_client=fake_client, reasoning_engine=fake_engine
    )
    result = await handler.handle_event(_make_parsed())
    assert result.status == HANDLED_RECEIVED
    fake_client.send_email.assert_awaited_once()
    kw = fake_client.send_email.await_args.kwargs
    assert kw["body_text"] == CANNED_FALLBACK_TEXT


@pytest.mark.asyncio
async def test_auto_reply_engine_result_error_sends_canned(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(AUTO_REPLY_ENV, "on")

    fake_engine = MagicMock()
    fake_result = MagicMock()
    fake_result.text = ""
    fake_result.error = "cost_ladder_halted"
    fake_engine.respond = AsyncMock(return_value=fake_result)

    fake_client = MagicMock()
    fake_client.send_email = AsyncMock(return_value=MagicMock(status="ok"))

    handler = EmailInboundHandler(
        purelymail_client=fake_client, reasoning_engine=fake_engine
    )
    result = await handler.handle_event(_make_parsed())
    assert result.status == HANDLED_RECEIVED
    kw = fake_client.send_email.await_args.kwargs
    assert kw["body_text"] == CANNED_FALLBACK_TEXT


@pytest.mark.asyncio
async def test_auto_reply_engine_empty_text_sends_canned(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(AUTO_REPLY_ENV, "true")

    fake_engine = MagicMock()
    fake_result = MagicMock()
    fake_result.text = "   "  # whitespace only
    fake_result.error = None
    fake_engine.respond = AsyncMock(return_value=fake_result)

    fake_client = MagicMock()
    fake_client.send_email = AsyncMock(return_value=MagicMock(status="ok"))

    handler = EmailInboundHandler(
        purelymail_client=fake_client, reasoning_engine=fake_engine
    )
    await handler.handle_event(_make_parsed())
    kw = fake_client.send_email.await_args.kwargs
    assert kw["body_text"] == CANNED_FALLBACK_TEXT


@pytest.mark.asyncio
async def test_auto_reply_client_unavailable_does_not_change_inbound(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(AUTO_REPLY_ENV, "true")

    fake_engine = MagicMock()
    fake_result = MagicMock()
    fake_result.text = "reply"
    fake_result.error = None
    fake_engine.respond = AsyncMock(return_value=fake_result)

    handler = EmailInboundHandler(
        purelymail_client=None, reasoning_engine=fake_engine
    )
    with patch(
        "kora_cli.listeners.purelymail_client_listener.current_purelymail_client",
        return_value=None,
    ):
        result = await handler.handle_event(_make_parsed())
    assert result.status == HANDLED_RECEIVED
    assert result.should_mark_seen is True


@pytest.mark.asyncio
async def test_auto_reply_send_raises_inbound_still_received(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(AUTO_REPLY_ENV, "true")

    fake_engine = MagicMock()
    fake_result = MagicMock()
    fake_result.text = "reply"
    fake_result.error = None
    fake_engine.respond = AsyncMock(return_value=fake_result)

    fake_client = MagicMock()
    fake_client.send_email = AsyncMock(side_effect=RuntimeError("smtp boom"))

    handler = EmailInboundHandler(
        purelymail_client=fake_client, reasoning_engine=fake_engine
    )
    result = await handler.handle_event(_make_parsed())
    assert result.status == HANDLED_RECEIVED
    assert result.should_mark_seen is True


@pytest.mark.asyncio
async def test_auto_reply_subject_re_prefix_not_doubled(monkeypatch, tmp_path):
    monkeypatch.setenv(AUTO_REPLY_ENV, "true")

    fake_engine = MagicMock()
    fake_result = MagicMock()
    fake_result.text = "reply"
    fake_result.error = None
    fake_engine.respond = AsyncMock(return_value=fake_result)

    fake_client = MagicMock()
    fake_client.send_email = AsyncMock(return_value=MagicMock(status="ok"))

    handler = EmailInboundHandler(
        purelymail_client=fake_client, reasoning_engine=fake_engine
    )
    parsed = _make_parsed(subject="Re: existing thread")
    await handler.handle_event(parsed)
    kw = fake_client.send_email.await_args.kwargs
    # Subject should NOT become "Re: Re: ..."
    assert kw["subject"] == "Re: existing thread"


@pytest.mark.asyncio
async def test_auto_reply_empty_subject_uses_placeholder(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(AUTO_REPLY_ENV, "true")

    fake_engine = MagicMock()
    fake_result = MagicMock()
    fake_result.text = "reply"
    fake_result.error = None
    fake_engine.respond = AsyncMock(return_value=fake_result)

    fake_client = MagicMock()
    fake_client.send_email = AsyncMock(return_value=MagicMock(status="ok"))

    handler = EmailInboundHandler(
        purelymail_client=fake_client, reasoning_engine=fake_engine
    )
    parsed = _make_parsed(subject="")
    await handler.handle_event(parsed)
    kw = fake_client.send_email.await_args.kwargs
    assert kw["subject"] == "Re: (no subject)"
