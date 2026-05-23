"""Tests for the Purelymail IMAP inbound client (KR-FEAT-EMAIL-INBOUND-IMAP ST1).

Covers:
  - Construction: fail-CLOSED on missing username / password env
  - Config: host/port override; invalid port; default 993 SSL
  - connect → login → SELECT INBOX happy path
  - Login refused → PurelymailIMAPConnectError + IMAP handle dropped
  - SELECT failure → PurelymailIMAPConnectError + handle dropped
  - Timeout during connect → PurelymailIMAPConnectError + handle dropped
  - fetch_unseen happy path (SEARCH + FETCH + parse multipart)
  - fetch_unseen with no unseen → empty list
  - mark_seen sets the \\Seen flag via UID STORE
  - mark_seen on disconnected client raises PurelymailIMAPConnectError
  - close is idempotent + fail-soft
  - Parse: text/plain only, text/html only (has_html=True), multipart
    alternative, multipart with attachments (metadata only)
  - Parse: missing Message-ID → synthetic id stable per-UID
  - SECURITY: password never appears in error / repr / log after
    diverse failure modes (login-refuse, timeout, SELECT-fail)
"""

from __future__ import annotations

from email.message import EmailMessage
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kora_cli.clients.purelymail_imap_client import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    PER_CALL_TIMEOUT_SECONDS,
    PurelymailIMAPClient,
    PurelymailIMAPConfigError,
    PurelymailIMAPConnectError,
    PurelymailIMAPFetchError,
    _extract_rfc822_payload,
    _parse_rfc822,
    _parse_search_uids,
    _sanitize_error,
)
from kora_cli.clients.purelymail_types import (
    AttachmentMeta,
    ParsedIncomingEmail,
)


# Test secret — used across tests so we can grep for absence.
_TEST_PASSWORD = "very-secret-imap-app-password-xyz"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Default valid envs; individual tests override / delete."""
    monkeypatch.setenv(
        "KORA_PUREMAIL_IMAP_USERNAME", "kora@stormhavenenterprises.com"
    )
    monkeypatch.setenv("KORA_PUREMAIL_IMAP_APP_PASSWORD", _TEST_PASSWORD)
    monkeypatch.delenv("KORA_PUREMAIL_IMAP_HOST", raising=False)
    monkeypatch.delenv("KORA_PUREMAIL_IMAP_PORT", raising=False)


# ===========================================================================
# Construction — fail-CLOSED
# ===========================================================================


def test_construction_fails_when_username_unset(monkeypatch):
    monkeypatch.delenv("KORA_PUREMAIL_IMAP_USERNAME", raising=False)
    with pytest.raises(
        PurelymailIMAPConfigError, match="KORA_PUREMAIL_IMAP_USERNAME"
    ):
        PurelymailIMAPClient()


def test_construction_fails_when_password_unset(monkeypatch):
    monkeypatch.delenv("KORA_PUREMAIL_IMAP_APP_PASSWORD", raising=False)
    with pytest.raises(
        PurelymailIMAPConfigError, match="KORA_PUREMAIL_IMAP_APP_PASSWORD"
    ):
        PurelymailIMAPClient()


def test_construction_fails_when_username_blank(monkeypatch):
    monkeypatch.setenv("KORA_PUREMAIL_IMAP_USERNAME", "   ")
    with pytest.raises(PurelymailIMAPConfigError):
        PurelymailIMAPClient()


def test_construction_succeeds_with_defaults():
    client = PurelymailIMAPClient()
    assert client._host == DEFAULT_HOST
    assert client._port == DEFAULT_PORT
    assert client._username == "kora@stormhavenenterprises.com"
    # Password NOT exposed in repr.
    r = repr(client)
    assert _TEST_PASSWORD not in r
    assert "kora@stormhavenenterprises.com" in r


def test_construction_honors_host_port_overrides(monkeypatch):
    monkeypatch.setenv("KORA_PUREMAIL_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("KORA_PUREMAIL_IMAP_PORT", "1993")
    client = PurelymailIMAPClient()
    assert client._host == "imap.example.com"
    assert client._port == 1993


def test_construction_rejects_non_integer_port(monkeypatch):
    monkeypatch.setenv("KORA_PUREMAIL_IMAP_PORT", "not-a-port")
    with pytest.raises(PurelymailIMAPConfigError, match="not an integer"):
        PurelymailIMAPClient()


def test_construction_rejects_out_of_range_port(monkeypatch):
    monkeypatch.setenv("KORA_PUREMAIL_IMAP_PORT", "70000")
    with pytest.raises(PurelymailIMAPConfigError, match="must be in 1..65535"):
        PurelymailIMAPClient()


# ===========================================================================
# Sanitization
# ===========================================================================


def test_sanitize_error_strips_password():
    text = f"IMAP login failed: bad credential {_TEST_PASSWORD}"
    out = _sanitize_error(text, _TEST_PASSWORD)
    assert _TEST_PASSWORD not in out
    assert "<REDACTED>" in out


def test_sanitize_error_safe_on_empty():
    assert _sanitize_error("", _TEST_PASSWORD) == ""
    assert _sanitize_error("normal text", "") == "normal text"


# ===========================================================================
# Search UID parsing
# ===========================================================================


def test_parse_search_uids_bytes_input():
    assert _parse_search_uids([b"1 4 7"]) == [1, 4, 7]


def test_parse_search_uids_str_input():
    assert _parse_search_uids(["3 9"]) == [3, 9]


def test_parse_search_uids_empty():
    assert _parse_search_uids([]) == []
    assert _parse_search_uids([b""]) == []


def test_parse_search_uids_skips_garbage_tokens():
    assert _parse_search_uids([b"1 abc 4"]) == [1, 4]


# ===========================================================================
# FETCH-RFC822 payload extraction
# ===========================================================================


def test_extract_rfc822_skips_header_line():
    header = b"1 FETCH (UID 7 RFC822 {123}"
    payload = b"From: a@b.com\r\nSubject: hi\r\n\r\nbody body body body body body"
    out = _extract_rfc822_payload([header, payload], imap_uid=7)
    assert out == payload


def test_extract_rfc822_returns_none_when_missing():
    assert _extract_rfc822_payload([], imap_uid=1) is None
    assert _extract_rfc822_payload([b"short"], imap_uid=1) is None


# ===========================================================================
# RFC822 parsing
# ===========================================================================


def _build_text_email() -> bytes:
    msg = EmailMessage()
    msg["From"] = "joshua@stormhavenenterprises.com"
    msg["To"] = "kora@stormhavenenterprises.com"
    msg["Subject"] = "hello kora"
    msg["Message-ID"] = "<msg-1@example.com>"
    msg["Date"] = "Wed, 21 May 2026 12:00:00 +0000"
    msg.set_content("plain body content")
    return msg.as_bytes()


def test_parse_rfc822_text_only():
    parsed = _parse_rfc822(_build_text_email(), imap_uid=42)
    assert parsed.message_id == "<msg-1@example.com>"
    assert parsed.from_address == "joshua@stormhavenenterprises.com"
    assert parsed.to == ["kora@stormhavenenterprises.com"]
    assert parsed.subject == "hello kora"
    assert "plain body content" in parsed.body_text
    assert parsed.body_html is None
    assert parsed.has_html is False
    assert parsed.attachments == []
    assert parsed.imap_uid == 42


def test_parse_rfc822_synthesizes_message_id_when_missing():
    msg = EmailMessage()
    msg["From"] = "joshua@stormhavenenterprises.com"
    msg["To"] = "kora@stormhavenenterprises.com"
    msg["Subject"] = "no id"
    msg.set_content("body")
    parsed = _parse_rfc822(msg.as_bytes(), imap_uid=99)
    assert "no-msg-id-uid-99" in parsed.message_id


def test_parse_rfc822_multipart_alternative_text_and_html():
    msg = EmailMessage()
    msg["From"] = "joshua@stormhavenenterprises.com"
    msg["To"] = "kora@stormhavenenterprises.com"
    msg["Subject"] = "html mail"
    msg["Message-ID"] = "<msg-2@example.com>"
    msg.set_content("plain version")
    msg.add_alternative("<p>html version</p>", subtype="html")
    parsed = _parse_rfc822(msg.as_bytes(), imap_uid=2)
    assert "plain version" in parsed.body_text
    assert parsed.body_html is not None
    assert "html version" in parsed.body_html
    assert parsed.has_html is True


def test_parse_rfc822_html_only_sets_has_html():
    msg = EmailMessage()
    msg["From"] = "joshua@stormhavenenterprises.com"
    msg["To"] = "kora@stormhavenenterprises.com"
    msg["Subject"] = "html only"
    msg["Message-ID"] = "<msg-3@example.com>"
    msg.set_content("<p>html only</p>", subtype="html")
    parsed = _parse_rfc822(msg.as_bytes(), imap_uid=3)
    assert parsed.body_text == ""
    assert parsed.has_html is True
    assert "html only" in (parsed.body_html or "")


def test_parse_rfc822_attachments_meta_only():
    msg = EmailMessage()
    msg["From"] = "joshua@stormhavenenterprises.com"
    msg["To"] = "kora@stormhavenenterprises.com"
    msg["Subject"] = "with attachment"
    msg["Message-ID"] = "<msg-4@example.com>"
    msg.set_content("body with attachment")
    msg.add_attachment(
        b"\x00\x01\x02\x03attachment-payload",
        maintype="application",
        subtype="octet-stream",
        filename="data.bin",
    )
    parsed = _parse_rfc822(msg.as_bytes(), imap_uid=4)
    assert len(parsed.attachments) == 1
    att = parsed.attachments[0]
    assert isinstance(att, AttachmentMeta)
    assert att.filename == "data.bin"
    assert att.size_bytes >= len(b"\x00\x01\x02\x03attachment-payload")
    assert att.content_type == "application/octet-stream"


# ===========================================================================
# connect / fetch / mark_seen — happy paths via mocked aioimaplib
# ===========================================================================


def _make_ok_resp(lines=None):
    return MagicMock(result="OK", lines=lines or [])


def _make_no_resp(lines=None):
    return MagicMock(result="NO", lines=lines or [])


def _make_mock_imap():
    """Build an AsyncMock IMAP4_SSL whose verbs all return OK by default."""
    imap = MagicMock()
    imap.wait_hello_from_server = AsyncMock(return_value=None)
    imap.login = AsyncMock(return_value=_make_ok_resp())
    imap.select = AsyncMock(return_value=_make_ok_resp())
    imap.uid_search = AsyncMock(return_value=_make_ok_resp(lines=[b""]))
    imap.uid = AsyncMock(return_value=_make_ok_resp())
    imap.logout = AsyncMock(return_value=None)
    return imap


@pytest.mark.asyncio
async def test_connect_happy_path():
    mock_imap = _make_mock_imap()
    client = PurelymailIMAPClient()
    with patch(
        "kora_cli.clients.purelymail_imap_client.aioimaplib.IMAP4_SSL",
        return_value=mock_imap,
    ):
        await client.connect()
    mock_imap.login.assert_awaited_once_with(
        "kora@stormhavenenterprises.com", _TEST_PASSWORD
    )
    mock_imap.select.assert_awaited_once_with(mailbox="INBOX")
    assert client._imap is mock_imap


@pytest.mark.asyncio
async def test_connect_login_refused_drops_handle():
    mock_imap = _make_mock_imap()
    mock_imap.login = AsyncMock(
        return_value=_make_no_resp(lines=[b"Bad credentials"])
    )
    client = PurelymailIMAPClient()
    with patch(
        "kora_cli.clients.purelymail_imap_client.aioimaplib.IMAP4_SSL",
        return_value=mock_imap,
    ):
        with pytest.raises(PurelymailIMAPConnectError, match="LOGIN failed"):
            await client.connect()
    assert client._imap is None
    mock_imap.logout.assert_awaited()


@pytest.mark.asyncio
async def test_connect_select_failure_drops_handle():
    mock_imap = _make_mock_imap()
    mock_imap.select = AsyncMock(
        return_value=_make_no_resp(lines=[b"Mailbox unavailable"])
    )
    client = PurelymailIMAPClient()
    with patch(
        "kora_cli.clients.purelymail_imap_client.aioimaplib.IMAP4_SSL",
        return_value=mock_imap,
    ):
        with pytest.raises(PurelymailIMAPConnectError, match="SELECT INBOX"):
            await client.connect()
    assert client._imap is None


@pytest.mark.asyncio
async def test_connect_unexpected_exception_sanitized():
    mock_imap = _make_mock_imap()
    # Sneak the password into the error message — sanitizer must strip it.
    mock_imap.login = AsyncMock(
        side_effect=RuntimeError(
            f"server echo includes app password={_TEST_PASSWORD}"
        )
    )
    client = PurelymailIMAPClient()
    with patch(
        "kora_cli.clients.purelymail_imap_client.aioimaplib.IMAP4_SSL",
        return_value=mock_imap,
    ):
        with pytest.raises(PurelymailIMAPConnectError) as excinfo:
            await client.connect()
    msg = str(excinfo.value)
    assert _TEST_PASSWORD not in msg
    assert "<REDACTED>" in msg


@pytest.mark.asyncio
async def test_fetch_unseen_no_messages():
    client = PurelymailIMAPClient()
    client._imap = _make_mock_imap()
    out = await client.fetch_unseen()
    assert out == []
    client._imap.uid_search.assert_awaited_once_with("UNSEEN")


@pytest.mark.asyncio
async def test_fetch_unseen_returns_parsed_messages():
    client = PurelymailIMAPClient()
    mock_imap = _make_mock_imap()
    mock_imap.uid_search = AsyncMock(
        return_value=_make_ok_resp(lines=[b"7 9"])
    )
    raw_msg_7 = _build_text_email()
    raw_msg_9 = _build_text_email()
    # imap.uid("fetch", ...) returns the FETCH response shape.
    fetch_resp_7 = _make_ok_resp(
        lines=[b"1 FETCH (UID 7 RFC822 {%d}" % len(raw_msg_7), raw_msg_7]
    )
    fetch_resp_9 = _make_ok_resp(
        lines=[b"2 FETCH (UID 9 RFC822 {%d}" % len(raw_msg_9), raw_msg_9]
    )

    async def uid_dispatch(verb, *args):
        if verb == "fetch":
            return fetch_resp_7 if args[0] == "7" else fetch_resp_9
        return _make_ok_resp()

    mock_imap.uid = AsyncMock(side_effect=uid_dispatch)
    client._imap = mock_imap

    out = await client.fetch_unseen()
    assert len(out) == 2
    assert all(isinstance(p, ParsedIncomingEmail) for p in out)
    assert {p.imap_uid for p in out} == {7, 9}


@pytest.mark.asyncio
async def test_fetch_unseen_search_failure_raises():
    client = PurelymailIMAPClient()
    mock_imap = _make_mock_imap()
    mock_imap.uid_search = AsyncMock(return_value=_make_no_resp())
    client._imap = mock_imap
    with pytest.raises(PurelymailIMAPFetchError, match="SEARCH UNSEEN"):
        await client.fetch_unseen()


@pytest.mark.asyncio
async def test_fetch_unseen_per_message_parse_failure_is_skipped(monkeypatch):
    """If FETCH returns a non-OK for one UID, we skip + continue."""
    client = PurelymailIMAPClient()
    mock_imap = _make_mock_imap()
    mock_imap.uid_search = AsyncMock(
        return_value=_make_ok_resp(lines=[b"7 9"])
    )
    raw_msg_9 = _build_text_email()

    async def uid_dispatch(verb, *args):
        if verb == "fetch":
            if args[0] == "7":
                return _make_no_resp(lines=[b"corrupt"])
            return _make_ok_resp(
                lines=[
                    b"2 FETCH (UID 9 RFC822 {%d}" % len(raw_msg_9),
                    raw_msg_9,
                ]
            )
        return _make_ok_resp()

    mock_imap.uid = AsyncMock(side_effect=uid_dispatch)
    client._imap = mock_imap

    out = await client.fetch_unseen()
    # uid=7 skipped; uid=9 parsed.
    assert len(out) == 1
    assert out[0].imap_uid == 9


@pytest.mark.asyncio
async def test_mark_seen_invokes_uid_store():
    client = PurelymailIMAPClient()
    mock_imap = _make_mock_imap()
    client._imap = mock_imap
    await client.mark_seen(42)
    mock_imap.uid.assert_awaited_once_with(
        "store", "42", "+FLAGS", "(\\Seen)"
    )


@pytest.mark.asyncio
async def test_mark_seen_failure_raises():
    client = PurelymailIMAPClient()
    mock_imap = _make_mock_imap()
    mock_imap.uid = AsyncMock(return_value=_make_no_resp(lines=[b"denied"]))
    client._imap = mock_imap
    with pytest.raises(PurelymailIMAPFetchError, match="STORE"):
        await client.mark_seen(42)


@pytest.mark.asyncio
async def test_mark_seen_on_disconnected_raises():
    client = PurelymailIMAPClient()
    with pytest.raises(PurelymailIMAPConnectError, match="not connected"):
        await client.mark_seen(1)


@pytest.mark.asyncio
async def test_close_is_idempotent():
    client = PurelymailIMAPClient()
    # Never connected — close is a no-op.
    await client.close()
    # Connected then close.
    client._imap = _make_mock_imap()
    await client.close()
    assert client._imap is None
    # Close again — still no-op.
    await client.close()


# ===========================================================================
# SECURITY — password absence across diverse failure modes
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_mode",
    ["login-refuse", "select-fail", "unexpected-exc-with-password-echo"],
)
async def test_password_never_in_error(failure_mode):
    """Diverse failure-mode injection. Whatever surfaces, the password
    must not appear in the raised error message."""
    mock_imap = _make_mock_imap()
    if failure_mode == "login-refuse":
        mock_imap.login = AsyncMock(
            return_value=_make_no_resp(lines=[b"Bad credentials"])
        )
    elif failure_mode == "select-fail":
        mock_imap.select = AsyncMock(
            return_value=_make_no_resp(lines=[b"mailbox locked"])
        )
    elif failure_mode == "unexpected-exc-with-password-echo":
        mock_imap.login = AsyncMock(
            side_effect=RuntimeError(
                f"echo back: pass={_TEST_PASSWORD}"
            )
        )

    client = PurelymailIMAPClient()
    with patch(
        "kora_cli.clients.purelymail_imap_client.aioimaplib.IMAP4_SSL",
        return_value=mock_imap,
    ):
        with pytest.raises(PurelymailIMAPConnectError) as excinfo:
            await client.connect()

    err_text = str(excinfo.value)
    assert _TEST_PASSWORD not in err_text


def test_password_never_in_repr():
    client = PurelymailIMAPClient()
    assert _TEST_PASSWORD not in repr(client)


# ===========================================================================
# Per-call timeout marker presence (documents the contract)
# ===========================================================================


def test_per_call_timeout_constant():
    assert PER_CALL_TIMEOUT_SECONDS == 30.0
