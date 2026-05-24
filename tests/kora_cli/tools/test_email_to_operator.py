"""Tests for KR-EMAIL-OUTBOUND-COMPOSE-TOOL.

Covers:
  Recipient pinning + validation:
   - happy path sends to KORA_EMAIL_JOSHUA_ADDRESS only
   - recipient env unset → rejected with recipient_env_unset
   - subject too long → rejected with subject_too_long
   - subject empty → rejected with subject_empty
   - body empty → rejected with body_empty

  Attachments:
   - valid attachments are read and forwarded to PurelymailClient
   - missing file → rejected with attachment_missing_file
   - total bytes > cap → rejected with attachment_too_large

  Caps + rate limit:
   - hourly cap of 3 → 4th send rejected with hourly_cap_exceeded
   - cap of 0 disables limit
   - malformed cap env warns + falls back to default

  SMTP failures:
   - SMTP raise → status=smtp_failure, audit emitted
   - SendResult status="failed" → status=smtp_failure

  Audit:
   - every invocation emits one tool.email_to_operator_sent row
   - audit details exclude body content; include sizes only

  Tool registry integration:
   - tool name appears in get_reasoning_available_tools()
   - execute_reasoning_tool routes to ST2 dispatcher with synthetic Caller
   - reasoning loop never sees other-recipient send_email tools
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kora_cli.clients.purelymail_types import Attachment, SendResult
from kora_cli.tools.email_to_operator import (
    DEFAULT_HOURLY_CAP,
    DEFAULT_MAX_ATTACH_MB,
    HOURLY_CAP_ENV,
    MAX_ATTACH_MB_ENV,
    RECIPIENT_ENV,
    REASON_ATTACHMENT_MISSING_FILE,
    REASON_ATTACHMENT_TOO_LARGE,
    REASON_BODY_EMPTY,
    REASON_CLIENT_UNAVAILABLE,
    REASON_HOURLY_CAP_EXCEEDED,
    REASON_RECIPIENT_UNSET,
    REASON_SMTP_FROM_UNSET,
    REASON_SUBJECT_EMPTY,
    REASON_SUBJECT_TOO_LONG,
    SMTP_USERNAME_ENV,
    STATUS_REJECTED,
    STATUS_SENT,
    STATUS_SMTP_FAILURE,
    SUBJECT_MAX_CHARS,
    _hourly_cap_allows,
    _record_send,
    _reset_rate_limiter_for_tests,
    send_email_to_operator,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Per-test env isolation + audit-log redirect + rate-limiter reset."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setattr(
        "kora_constants.get_kora_home", lambda: tmp_path, raising=False
    )
    monkeypatch.setenv(
        "KORA_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl")
    )
    monkeypatch.setenv(RECIPIENT_ENV, "joshua@stormhavenenterprises.com")
    monkeypatch.setenv(SMTP_USERNAME_ENV, "kora@stormhavenenterprises.com")
    monkeypatch.delenv(HOURLY_CAP_ENV, raising=False)
    monkeypatch.delenv(MAX_ATTACH_MB_ENV, raising=False)
    _reset_rate_limiter_for_tests()
    yield
    _reset_rate_limiter_for_tests()


def _read_audit(tmp_path) -> list:
    path = tmp_path / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _ok_send_result(message_id: str = "<msg-1@stormhaven.com>") -> SendResult:
    return SendResult(
        status="ok",
        message_id=message_id,
        smtp_code=250,
        sent_at=datetime.now(timezone.utc),
        retry_count=0,
    )


def _failed_send_result() -> SendResult:
    return SendResult(
        status="failed",
        message_id="<msg-fail@stormhaven.com>",
        error="smtp_relay_refused",
        smtp_code=550,
        sent_at=datetime.now(timezone.utc),
        retry_count=1,
    )


def _fake_pm_client(send_result=None) -> MagicMock:
    client = MagicMock()
    client.send_email = AsyncMock(return_value=send_result or _ok_send_result())
    return client


# ===========================================================================
# Happy path + recipient pinning
# ===========================================================================


@pytest.mark.asyncio
async def test_happy_path_sends_to_pinned_recipient_only(tmp_path):
    client = _fake_pm_client()
    result = await send_email_to_operator(
        subject="[Kora] morning summary",
        body="here's the rundown",
        purelymail_client=client,
    )
    assert result["status"] == STATUS_SENT
    assert result["smtp_message_id"] == "<msg-1@stormhaven.com>"
    assert result["attachment_count"] == 0

    client.send_email.assert_awaited_once()
    kw = client.send_email.await_args.kwargs
    # Recipient pinned — exactly the env value, no extras.
    assert kw["to"] == ["joshua@stormhavenenterprises.com"]
    assert kw["from_addr"] == "kora@stormhavenenterprises.com"
    assert kw["subject"] == "[Kora] morning summary"
    assert kw["body_text"] == "here's the rundown"
    assert kw["attachments"] is None
    assert kw["caller_actor_kind"] == "kora_reasoning_self"

    entries = _read_audit(tmp_path)
    assert len(entries) == 1
    assert entries[0]["seam"] == "tool.email_to_operator_sent"
    assert entries[0]["details"]["status"] == STATUS_SENT
    assert entries[0]["details"]["smtp_message_id"] == "<msg-1@stormhaven.com>"
    # Body content must NEVER appear in audit.
    assert "here's the rundown" not in json.dumps(entries[0])
    assert "body_chars" in entries[0]["details"]


# ===========================================================================
# Validation rejections
# ===========================================================================


@pytest.mark.asyncio
async def test_recipient_env_unset_rejected(monkeypatch):
    monkeypatch.delenv(RECIPIENT_ENV, raising=False)
    client = _fake_pm_client()
    result = await send_email_to_operator(
        subject="x", body="y", purelymail_client=client
    )
    assert result["status"] == STATUS_REJECTED
    assert result["reason"] == REASON_RECIPIENT_UNSET
    client.send_email.assert_not_awaited()


@pytest.mark.asyncio
async def test_smtp_from_unset_rejected(monkeypatch):
    monkeypatch.delenv(SMTP_USERNAME_ENV, raising=False)
    client = _fake_pm_client()
    result = await send_email_to_operator(
        subject="x", body="y", purelymail_client=client
    )
    assert result["status"] == STATUS_REJECTED
    assert result["reason"] == REASON_SMTP_FROM_UNSET
    client.send_email.assert_not_awaited()


@pytest.mark.asyncio
async def test_subject_too_long_rejected():
    client = _fake_pm_client()
    long_subject = "x" * (SUBJECT_MAX_CHARS + 5)
    result = await send_email_to_operator(
        subject=long_subject, body="y", purelymail_client=client
    )
    assert result["status"] == STATUS_REJECTED
    assert result["reason"] == REASON_SUBJECT_TOO_LONG
    assert result["detail"]["subject_chars"] == SUBJECT_MAX_CHARS + 5
    client.send_email.assert_not_awaited()


@pytest.mark.asyncio
async def test_subject_empty_rejected():
    client = _fake_pm_client()
    result = await send_email_to_operator(
        subject="   ", body="y", purelymail_client=client
    )
    assert result["status"] == STATUS_REJECTED
    assert result["reason"] == REASON_SUBJECT_EMPTY


@pytest.mark.asyncio
async def test_body_empty_rejected():
    client = _fake_pm_client()
    result = await send_email_to_operator(
        subject="x", body="   \n  ", purelymail_client=client
    )
    assert result["status"] == STATUS_REJECTED
    assert result["reason"] == REASON_BODY_EMPTY


@pytest.mark.asyncio
async def test_purelymail_client_unavailable_rejected(monkeypatch):
    monkeypatch.setattr(
        "kora_cli.tools.email_to_operator._resolve_purelymail_client",
        lambda: None,
    )
    result = await send_email_to_operator(
        subject="x", body="y", purelymail_client=None
    )
    assert result["status"] == STATUS_REJECTED
    assert result["reason"] == REASON_CLIENT_UNAVAILABLE


# ===========================================================================
# Attachments
# ===========================================================================


@pytest.mark.asyncio
async def test_attachments_read_and_forwarded(tmp_path):
    pdf_path = tmp_path / "report.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake pdf content")
    txt_path = tmp_path / "notes.txt"
    txt_path.write_bytes(b"hello world")
    client = _fake_pm_client()

    result = await send_email_to_operator(
        subject="weekly",
        body="see attached",
        attachments=[
            {"filename": "report.pdf", "content_path": str(pdf_path)},
            {"filename": "notes.txt", "content_path": str(txt_path)},
        ],
        purelymail_client=client,
    )
    assert result["status"] == STATUS_SENT
    assert result["attachment_count"] == 2
    assert result["attachment_total_bytes"] == 25 + 11

    kw = client.send_email.await_args.kwargs
    sent_attachments = kw["attachments"]
    assert len(sent_attachments) == 2
    assert isinstance(sent_attachments[0], Attachment)
    assert sent_attachments[0].filename == "report.pdf"
    assert sent_attachments[0].content == b"%PDF-1.4 fake pdf content"
    assert sent_attachments[0].maintype == "application"
    assert sent_attachments[0].subtype == "pdf"
    assert sent_attachments[1].maintype == "text"
    assert sent_attachments[1].subtype == "plain"


@pytest.mark.asyncio
async def test_attachment_missing_file_rejected(tmp_path):
    client = _fake_pm_client()
    result = await send_email_to_operator(
        subject="x",
        body="y",
        attachments=[
            {
                "filename": "ghost.pdf",
                "content_path": str(tmp_path / "does-not-exist.pdf"),
            }
        ],
        purelymail_client=client,
    )
    assert result["status"] == STATUS_REJECTED
    assert result["reason"] == REASON_ATTACHMENT_MISSING_FILE
    client.send_email.assert_not_awaited()


@pytest.mark.asyncio
async def test_attachment_total_size_rejected(tmp_path, monkeypatch):
    # Cap at 1 MB; one 600 KB + one 600 KB = 1.2 MB → second fails.
    monkeypatch.setenv(MAX_ATTACH_MB_ENV, "1")
    first = tmp_path / "a.bin"
    first.write_bytes(b"x" * (600 * 1024))
    second = tmp_path / "b.bin"
    second.write_bytes(b"y" * (600 * 1024))
    client = _fake_pm_client()
    result = await send_email_to_operator(
        subject="x",
        body="y",
        attachments=[
            {"filename": "a.bin", "content_path": str(first)},
            {"filename": "b.bin", "content_path": str(second)},
        ],
        purelymail_client=client,
    )
    assert result["status"] == STATUS_REJECTED
    assert result["reason"] == REASON_ATTACHMENT_TOO_LARGE
    assert result["detail"]["max_total_bytes"] == 1024 * 1024
    client.send_email.assert_not_awaited()


@pytest.mark.asyncio
async def test_attachment_missing_filename_field_rejected(tmp_path):
    f = tmp_path / "f"
    f.write_bytes(b"x")
    result = await send_email_to_operator(
        subject="x",
        body="y",
        attachments=[{"content_path": str(f)}],  # missing filename
        purelymail_client=_fake_pm_client(),
    )
    assert result["status"] == STATUS_REJECTED
    assert result["reason"] == REASON_ATTACHMENT_MISSING_FILE


# ===========================================================================
# Hourly rate cap
# ===========================================================================


@pytest.mark.asyncio
async def test_hourly_cap_blocks_excess_sends(monkeypatch):
    monkeypatch.setenv(HOURLY_CAP_ENV, "3")
    client = _fake_pm_client()

    for i in range(3):
        result = await send_email_to_operator(
            subject=f"x{i}", body="y", purelymail_client=client
        )
        assert result["status"] == STATUS_SENT

    overflow = await send_email_to_operator(
        subject="overflow", body="y", purelymail_client=client
    )
    assert overflow["status"] == STATUS_REJECTED
    assert overflow["reason"] == REASON_HOURLY_CAP_EXCEEDED
    assert overflow["detail"]["hourly_cap"] == 3
    assert client.send_email.await_count == 3  # not 4


def test_hourly_cap_zero_disables(monkeypatch):
    monkeypatch.setenv(HOURLY_CAP_ENV, "0")
    for _ in range(50):
        _record_send()
    assert _hourly_cap_allows() is True


def test_hourly_cap_malformed_falls_back_to_default(monkeypatch, caplog):
    import logging

    monkeypatch.setenv(HOURLY_CAP_ENV, "garbage")
    with caplog.at_level(logging.WARNING):
        _hourly_cap_allows()
    assert any(HOURLY_CAP_ENV in r.message for r in caplog.records)


# ===========================================================================
# SMTP failures
# ===========================================================================


@pytest.mark.asyncio
async def test_smtp_exception_returns_smtp_failure(tmp_path):
    client = MagicMock()
    client.send_email = AsyncMock(side_effect=RuntimeError("smtp gone"))
    result = await send_email_to_operator(
        subject="x", body="y", purelymail_client=client
    )
    assert result["status"] == STATUS_SMTP_FAILURE
    assert result["error"] == "RuntimeError"

    entries = _read_audit(tmp_path)
    assert entries[0]["details"]["status"] == STATUS_SMTP_FAILURE
    assert entries[0]["details"]["error"] == "RuntimeError"


@pytest.mark.asyncio
async def test_smtp_status_failed_returns_smtp_failure(tmp_path):
    client = _fake_pm_client(send_result=_failed_send_result())
    result = await send_email_to_operator(
        subject="x", body="y", purelymail_client=client
    )
    assert result["status"] == STATUS_SMTP_FAILURE
    assert result["error"] == "smtp_relay_refused"


# ===========================================================================
# Audit shape
# ===========================================================================


@pytest.mark.asyncio
async def test_audit_records_sizes_not_content(tmp_path):
    client = _fake_pm_client()
    body = "the quick brown fox" * 100
    result = await send_email_to_operator(
        subject="audit shape test",
        body=body,
        purelymail_client=client,
    )
    assert result["status"] == STATUS_SENT
    entries = _read_audit(tmp_path)
    assert len(entries) == 1
    details = entries[0]["details"]
    assert details["body_chars"] == len(body)
    assert details["subject_chars"] == len("audit shape test")
    # No body content stored verbatim anywhere.
    serialized = json.dumps(entries[0])
    assert "quick brown fox" not in serialized


@pytest.mark.asyncio
async def test_audit_records_rejection_reason(tmp_path):
    client = _fake_pm_client()
    await send_email_to_operator(
        subject="", body="y", purelymail_client=client
    )
    entries = _read_audit(tmp_path)
    assert entries[0]["details"]["status"] == STATUS_REJECTED
    assert entries[0]["details"]["rejection_reason"] == REASON_SUBJECT_EMPTY


# ===========================================================================
# Tool registry + dispatch integration
# ===========================================================================


def test_tool_name_advertised_in_reasoning_available_tools():
    from kora_cli.reasoning.tool_registry import get_reasoning_available_tools

    tools = get_reasoning_available_tools()
    names = [t["name"] for t in tools]
    assert "kora__send_email_to_operator" in names
    # Other-recipient send_email MUST NOT be in the reasoning surface
    # (only the operator-pinned variant).
    assert "kora__send_email" not in names


def test_other_mutating_tools_still_excluded_from_reasoning():
    """Defense-in-depth: the deliberate exclusions from the original
    docstring must hold (only kora__send_email_to_operator was
    deliberately added; nothing else)."""
    from kora_cli.reasoning.tool_registry import REASONING_TOOL_ALLOWLIST

    forbidden = {
        "kora__request_state_transition",
        "kora__create_sea_ticket",
        "kora__send_webhook_test_event",
        "kora__send_slack_dm",
        "kora__send_email",
        "kora__request_pause",
        "kora__request_resume",
        "kora__request_stop",
        "kora__send_test_alert",
    }
    assert forbidden.isdisjoint(REASONING_TOOL_ALLOWLIST)


@pytest.mark.asyncio
async def test_execute_reasoning_tool_dispatches_with_synthetic_caller(
    monkeypatch, tmp_path
):
    """execute_reasoning_tool must route kora__send_email_to_operator
    through ST2_TOOL_DISPATCH with a synthetic Caller whose
    actor_kind identifies the reasoning loop."""
    from kora_cli.reasoning.tool_registry import execute_reasoning_tool

    captured = {}

    async def fake_dispatcher(params, caller):
        captured["params"] = params
        captured["caller"] = caller
        from kora_cli.listeners.mcp_tools import SendEmailToOperatorResult

        return SendEmailToOperatorResult(status="sent")

    from kora_cli.listeners.mcp_tools import ST2_TOOL_DISPATCH

    monkeypatch.setitem(
        ST2_TOOL_DISPATCH,
        "kora__send_email_to_operator",
        fake_dispatcher,
    )

    result = await execute_reasoning_tool(
        name="kora__send_email_to_operator",
        tool_input={"subject": "x", "body": "y"},
    )
    assert result.status == "sent"
    assert captured["params"] == {"subject": "x", "body": "y"}
    assert captured["caller"].actor_kind == "kora_reasoning_self"
    # The synthetic caller's allowed_caps is scoped to JUST the tool
    # being called — so no other tool can be invoked via this caller
    # if a future executor adds a caller.allows() check.
    assert captured["caller"].allowed_caps == frozenset(
        {"kora__send_email_to_operator"}
    )


@pytest.mark.asyncio
async def test_execute_reasoning_tool_rejects_unknown_tool():
    from kora_cli.reasoning.tool_registry import (
        ReasoningToolNotAllowed,
        execute_reasoning_tool,
    )

    with pytest.raises(ReasoningToolNotAllowed):
        await execute_reasoning_tool(
            name="kora__delete_everything", tool_input={}
        )
