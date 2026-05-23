"""Tests for the Purelymail SMTP outbound client (KR-FEAT-EMAIL ST1).

Covers:
  - Construction: fail-CLOSED on missing username / password env
  - Config: host/port override; invalid port; default 465 SSL
  - Validation (BEFORE any SMTP traffic):
    * from-domain allowlist (unset env = operator-config error,
      not silent allow-all)
    * recipient cap (≤10)
    * malformed recipient (no @)
    * per-attachment size cap (10 MiB)
    * total batch attachment cap (25 MiB)
  - SMTP send happy path → SendResult with locally-generated
    Message-ID + smtp_code parsed + retry_count=0 + JSONL log entry
  - SMTP retry: 421/450/451/452 transient → 1 retry; 5xx → no retry;
    connection error → 1 retry
  - SECURITY: password never appears in JSONL / error / repr after
    diverse failure modes (auth-fail / 5xx / connect-fail / timeout)
  - JSONL log shape: body NOT included; subject + recipients + meta
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosmtplib
import pytest

from kora_cli.clients.purelymail_client import (
    ALLOWED_FROM_DOMAINS_ENV,
    DEFAULT_HOST,
    DEFAULT_PORT,
    MAX_ATTACHMENT_BYTES,
    MAX_TOTAL_ATTACHMENT_BYTES,
    PurelymailClient,
    PurelymailConfigError,
    PurelymailRejectError,
    _parse_smtp_code,
    _sanitize_error,
)
from kora_cli.clients.purelymail_types import Attachment, SendResult


# Test secret — used across tests so we can grep / assert absence.
_TEST_PASSWORD = "very-secret-app-password-xyz"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Isolate HERMES_HOME + reset env per test."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "kora_constants.get_kora_home", lambda: tmp_path, raising=False
    )
    # Default valid envs; individual tests override / delete.
    monkeypatch.setenv("KORA_PUREMAIL_SMTP_USERNAME", "kora@stormhavenenterprises.com")
    monkeypatch.setenv("KORA_PUREMAIL_SMTP_APP_PASSWORD", _TEST_PASSWORD)
    monkeypatch.setenv(
        ALLOWED_FROM_DOMAINS_ENV, "stormhavenenterprises.com"
    )
    return tmp_path


def _log_path(tmp_path: Path) -> Path:
    return tmp_path / "email_outbound_log.jsonl"


# ===========================================================================
# Construction — fail-CLOSED
# ===========================================================================


def test_construction_fails_when_username_unset(monkeypatch):
    monkeypatch.delenv("KORA_PUREMAIL_SMTP_USERNAME", raising=False)
    with pytest.raises(PurelymailConfigError, match="KORA_PUREMAIL_SMTP_USERNAME"):
        PurelymailClient()


def test_construction_fails_when_username_empty(monkeypatch):
    monkeypatch.setenv("KORA_PUREMAIL_SMTP_USERNAME", "")
    with pytest.raises(PurelymailConfigError):
        PurelymailClient()


def test_construction_fails_when_username_whitespace(monkeypatch):
    monkeypatch.setenv("KORA_PUREMAIL_SMTP_USERNAME", "   \n  ")
    with pytest.raises(PurelymailConfigError):
        PurelymailClient()


def test_construction_fails_when_password_unset(monkeypatch):
    monkeypatch.delenv("KORA_PUREMAIL_SMTP_APP_PASSWORD", raising=False)
    with pytest.raises(PurelymailConfigError, match="KORA_PUREMAIL_SMTP_APP_PASSWORD"):
        PurelymailClient()


def test_construction_fails_when_password_empty(monkeypatch):
    monkeypatch.setenv("KORA_PUREMAIL_SMTP_APP_PASSWORD", "")
    with pytest.raises(PurelymailConfigError):
        PurelymailClient()


def test_construction_uses_default_host_and_port():
    client = PurelymailClient()
    assert client._host == DEFAULT_HOST
    assert client._port == DEFAULT_PORT
    assert DEFAULT_PORT == 465


def test_construction_honors_host_and_port_overrides(monkeypatch):
    monkeypatch.setenv("KORA_PUREMAIL_SMTP_HOST", "smtp.staging.example.com")
    monkeypatch.setenv("KORA_PUREMAIL_SMTP_PORT", "587")
    client = PurelymailClient()
    assert client._host == "smtp.staging.example.com"
    assert client._port == 587


def test_construction_rejects_non_integer_port(monkeypatch):
    monkeypatch.setenv("KORA_PUREMAIL_SMTP_PORT", "not-a-port")
    with pytest.raises(PurelymailConfigError, match="not an integer"):
        PurelymailClient()


def test_construction_rejects_out_of_range_port(monkeypatch):
    monkeypatch.setenv("KORA_PUREMAIL_SMTP_PORT", "99999")
    with pytest.raises(PurelymailConfigError, match="must be in 1..65535"):
        PurelymailClient()


# ===========================================================================
# Client-side validation (BEFORE SMTP)
# ===========================================================================


@pytest.mark.asyncio
async def test_send_rejects_disallowed_from_domain(monkeypatch):
    monkeypatch.setenv(
        ALLOWED_FROM_DOMAINS_ENV, "stormhavenenterprises.com"
    )
    client = PurelymailClient()
    with pytest.raises(PurelymailRejectError, match="not in allowlist"):
        await client.send_email(
            from_addr="kora@notallowed.example.com",
            to=["joshua@stormhavenenterprises.com"],
            subject="x",
            body_text="x",
        )


@pytest.mark.asyncio
async def test_send_rejects_unset_allowlist_as_config_error(monkeypatch):
    """Empty allowlist is OPERATOR-CONFIG ERROR, not silent allow-all.
    Critical: defense against accidentally wide-open sends."""
    monkeypatch.delenv(ALLOWED_FROM_DOMAINS_ENV, raising=False)
    client = PurelymailClient()
    with pytest.raises(PurelymailConfigError, match="ALLOWED_FROM_DOMAINS"):
        await client.send_email(
            from_addr="kora@stormhavenenterprises.com",
            to=["joshua@stormhavenenterprises.com"],
            subject="x",
            body_text="x",
        )


@pytest.mark.asyncio
async def test_send_rejects_malformed_from_addr():
    client = PurelymailClient()
    with pytest.raises(PurelymailRejectError, match="malformed"):
        await client.send_email(
            from_addr="not-an-email",
            to=["joshua@stormhavenenterprises.com"],
            subject="x",
            body_text="x",
        )


@pytest.mark.asyncio
async def test_send_rejects_empty_recipient_list():
    client = PurelymailClient()
    with pytest.raises(PurelymailRejectError, match="empty"):
        await client.send_email(
            from_addr="kora@stormhavenenterprises.com",
            to=[],
            subject="x",
            body_text="x",
        )


@pytest.mark.asyncio
async def test_send_rejects_too_many_recipients():
    """Defense against accidental mass-send. Cap = 10."""
    client = PurelymailClient()
    with pytest.raises(PurelymailRejectError, match="too many recipients"):
        await client.send_email(
            from_addr="kora@stormhavenenterprises.com",
            to=[f"r{i}@example.com" for i in range(11)],
            subject="x",
            body_text="x",
        )


@pytest.mark.asyncio
async def test_send_rejects_malformed_recipient():
    client = PurelymailClient()
    with pytest.raises(PurelymailRejectError, match="malformed"):
        await client.send_email(
            from_addr="kora@stormhavenenterprises.com",
            to=["joshua@stormhavenenterprises.com", "not-an-email"],
            subject="x",
            body_text="x",
        )


@pytest.mark.asyncio
async def test_send_rejects_oversized_single_attachment():
    client = PurelymailClient()
    big = Attachment(
        filename="big.bin",
        content=b"x" * (MAX_ATTACHMENT_BYTES + 1),
        maintype="application",
        subtype="octet-stream",
    )
    with pytest.raises(PurelymailRejectError, match="per-attachment"):
        await client.send_email(
            from_addr="kora@stormhavenenterprises.com",
            to=["joshua@stormhavenenterprises.com"],
            subject="x",
            body_text="x",
            attachments=[big],
        )


@pytest.mark.asyncio
async def test_send_rejects_oversized_total_batch():
    """3 × 10 MiB = 30 MiB > 25 MiB total cap. Each attachment
    individually under the per-attachment limit, but combined
    exceeds the batch cap."""
    client = PurelymailClient()
    # Use 9 MiB each so per-attachment passes (≤10 MiB) and the
    # third one tips the total over 25 MiB.
    each = 9 * 1024 * 1024
    atts = [
        Attachment(
            filename=f"a{i}.bin",
            content=b"x" * each,
            maintype="application",
            subtype="octet-stream",
        )
        for i in range(3)
    ]
    assert 3 * each > MAX_TOTAL_ATTACHMENT_BYTES
    with pytest.raises(PurelymailRejectError, match="total attachment"):
        await client.send_email(
            from_addr="kora@stormhavenenterprises.com",
            to=["joshua@stormhavenenterprises.com"],
            subject="x",
            body_text="x",
            attachments=atts,
        )


# ===========================================================================
# SMTP send — happy path
# ===========================================================================


def _patch_smtp(*, response: str = "250 OK", login_ok: bool = True, send_exc=None):
    """Patch aiosmtplib.SMTP to a controllable mock."""
    fake_smtp = MagicMock()
    fake_smtp.connect = AsyncMock()
    fake_smtp.login = AsyncMock() if login_ok else AsyncMock(
        side_effect=aiosmtplib.SMTPAuthenticationError(
            535, "5.7.8 Auth failed"
        )
    )
    if send_exc is not None:
        fake_smtp.send_message = AsyncMock(side_effect=send_exc)
    else:
        fake_smtp.send_message = AsyncMock(return_value=({}, response))
    fake_smtp.quit = AsyncMock()
    return patch(
        "kora_cli.clients.purelymail_client.aiosmtplib.SMTP",
        return_value=fake_smtp,
    ), fake_smtp


@pytest.mark.asyncio
async def test_send_happy_path_returns_send_result_with_message_id(tmp_path):
    client = PurelymailClient()
    smtp_patch, fake = _patch_smtp(response="250 2.0.0 OK queued")
    with smtp_patch:
        result = await client.send_email(
            from_addr="kora@stormhavenenterprises.com",
            to=["joshua@stormhavenenterprises.com"],
            subject="hello",
            body_text="hi",
        )
    assert isinstance(result, SendResult)
    assert result.status == "ok"
    assert result.message_id.startswith("<") and result.message_id.endswith(">")
    assert "stormhavenenterprises.com" in result.message_id
    assert result.error is None
    assert result.smtp_code == 250
    assert result.retry_count == 0
    # SMTP roundtrip happened
    fake.connect.assert_awaited_once()
    fake.login.assert_awaited_once()
    fake.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_writes_outbound_jsonl_entry(_isolate):
    client = PurelymailClient()
    smtp_patch, _ = _patch_smtp(response="250 OK")
    with smtp_patch:
        result = await client.send_email(
            from_addr="kora@stormhavenenterprises.com",
            to=["joshua@stormhavenenterprises.com"],
            subject="hello",
            body_text="hi",
        )
    log_path = _log_path(_isolate)
    assert log_path.exists()
    entry = json.loads(log_path.read_text().splitlines()[-1])
    assert entry["from"] == "kora@stormhavenenterprises.com"
    assert entry["to"] == ["joshua@stormhavenenterprises.com"]
    assert entry["subject"] == "hello"
    assert entry["send_status"] == "ok"
    assert entry["message_id"] == result.message_id
    assert entry["smtp_code"] == 250
    assert entry["error"] is None
    assert entry["retry_count"] == 0
    # Body NEVER in log
    assert "body" not in entry
    assert "hi" not in entry["subject"]  # sanity — subject is the only str field that could carry body


@pytest.mark.asyncio
async def test_send_threading_in_reply_to_preserved_in_message(_isolate):
    """When in_reply_to is supplied, the MIME header is set + the
    JSONL log records it."""
    client = PurelymailClient()
    smtp_patch, fake = _patch_smtp()
    with smtp_patch:
        await client.send_email(
            from_addr="kora@stormhavenenterprises.com",
            to=["joshua@stormhavenenterprises.com"],
            subject="re: x",
            body_text="reply body",
            in_reply_to="<original@stormhavenenterprises.com>",
        )
    log_path = _log_path(_isolate)
    entry = json.loads(log_path.read_text().splitlines()[-1])
    assert entry["in_reply_to"] == "<original@stormhavenenterprises.com>"


# ===========================================================================
# SMTP retry policy
# ===========================================================================


@pytest.mark.asyncio
async def test_smtp_421_transient_retries_once_then_fails(_isolate):
    """421 service-not-available → 1 retry attempt then fail."""
    client = PurelymailClient()
    exc = aiosmtplib.SMTPResponseException(421, "Service not available")
    smtp_patch, fake = _patch_smtp(send_exc=exc)
    with smtp_patch:
        result = await client.send_email(
            from_addr="kora@stormhavenenterprises.com",
            to=["joshua@stormhavenenterprises.com"],
            subject="x",
            body_text="x",
        )
    assert result.status == "failed"
    assert result.smtp_code == 421
    assert result.retry_count == 1
    # send_message attempted twice
    assert fake.send_message.await_count == 2


@pytest.mark.parametrize("code", [421, 450, 451, 452])
@pytest.mark.asyncio
async def test_smtp_transient_codes_all_retry_once(_isolate, code):
    client = PurelymailClient()
    exc = aiosmtplib.SMTPResponseException(code, f"transient {code}")
    smtp_patch, fake = _patch_smtp(send_exc=exc)
    with smtp_patch:
        result = await client.send_email(
            from_addr="kora@stormhavenenterprises.com",
            to=["joshua@stormhavenenterprises.com"],
            subject="x",
            body_text="x",
        )
    assert result.status == "failed"
    assert result.smtp_code == code
    assert result.retry_count == 1
    assert fake.send_message.await_count == 2


@pytest.mark.parametrize("code", [550, 552, 553, 554])
@pytest.mark.asyncio
async def test_smtp_permanent_5xx_no_retry(_isolate, code):
    """5xx codes are deterministic permanent failures. No retry."""
    client = PurelymailClient()
    exc = aiosmtplib.SMTPResponseException(code, f"permanent {code}")
    smtp_patch, fake = _patch_smtp(send_exc=exc)
    with smtp_patch:
        result = await client.send_email(
            from_addr="kora@stormhavenenterprises.com",
            to=["joshua@stormhavenenterprises.com"],
            subject="x",
            body_text="x",
        )
    assert result.status == "failed"
    assert result.smtp_code == code
    assert result.retry_count == 0
    assert fake.send_message.await_count == 1


@pytest.mark.asyncio
async def test_connection_error_retries_once_then_fails(_isolate):
    client = PurelymailClient()
    smtp_patch, fake = _patch_smtp()
    # Override connect to raise on both attempts
    fake.connect = AsyncMock(
        side_effect=aiosmtplib.SMTPConnectError("connect refused")
    )
    with smtp_patch:
        result = await client.send_email(
            from_addr="kora@stormhavenenterprises.com",
            to=["joshua@stormhavenenterprises.com"],
            subject="x",
            body_text="x",
        )
    assert result.status == "failed"
    assert result.retry_count == 1
    # connect attempted twice (once per send attempt)
    assert fake.connect.await_count == 2


@pytest.mark.asyncio
async def test_auth_error_no_retry_immediate_fail(_isolate):
    """SMTPAuthenticationError is permanent — no retry."""
    client = PurelymailClient()
    smtp_patch, fake = _patch_smtp(login_ok=False)
    with smtp_patch:
        result = await client.send_email(
            from_addr="kora@stormhavenenterprises.com",
            to=["joshua@stormhavenenterprises.com"],
            subject="x",
            body_text="x",
        )
    assert result.status == "failed"
    assert result.smtp_code == 535
    assert result.retry_count == 0
    # connect attempted once (no retry)
    assert fake.connect.await_count == 1


# ===========================================================================
# SECURITY: password never leaks
# ===========================================================================


@pytest.mark.asyncio
async def test_password_never_appears_in_jsonl_after_success(_isolate):
    client = PurelymailClient()
    smtp_patch, _ = _patch_smtp(response="250 OK")
    with smtp_patch:
        await client.send_email(
            from_addr="kora@stormhavenenterprises.com",
            to=["joshua@stormhavenenterprises.com"],
            subject="hello",
            body_text="hi",
        )
    log_text = _log_path(_isolate).read_text()
    assert _TEST_PASSWORD not in log_text


@pytest.mark.asyncio
async def test_password_redacted_when_server_echoes_in_error(_isolate):
    """Some SMTP servers echo credentials in error responses (esp.
    misconfigured ones). Verify the sanitization pass strips the
    password before exposing in SendResult.error + JSONL log."""
    client = PurelymailClient()
    # Construct an SMTP error that includes the password text
    leaky = aiosmtplib.SMTPResponseException(
        550, f"Login failed for user with password '{_TEST_PASSWORD}'"
    )
    smtp_patch, _ = _patch_smtp(send_exc=leaky)
    with smtp_patch:
        result = await client.send_email(
            from_addr="kora@stormhavenenterprises.com",
            to=["joshua@stormhavenenterprises.com"],
            subject="x",
            body_text="x",
        )
    assert result.status == "failed"
    assert _TEST_PASSWORD not in result.error
    assert "<REDACTED>" in result.error
    log_text = _log_path(_isolate).read_text()
    assert _TEST_PASSWORD not in log_text


@pytest.mark.asyncio
async def test_password_never_appears_in_diverse_failure_paths(_isolate):
    """Run a battery of failure modes + assert password absent from
    every error/log field after each."""
    client = PurelymailClient()
    failure_modes = [
        aiosmtplib.SMTPResponseException(
            550, f"rejected (passwd={_TEST_PASSWORD})"
        ),
        aiosmtplib.SMTPResponseException(421, "transient transient"),
        aiosmtplib.SMTPConnectError(f"connect refused {_TEST_PASSWORD}"),
        aiosmtplib.SMTPServerDisconnected(
            f"server closed {_TEST_PASSWORD}"
        ),
    ]
    for exc in failure_modes:
        smtp_patch, _ = _patch_smtp(send_exc=exc)
        # For SMTPConnectError, patch connect instead since that
        # raises before send_message gets called
        with smtp_patch as p:
            fake = p.return_value
            if isinstance(exc, aiosmtplib.SMTPConnectError):
                fake.connect = AsyncMock(side_effect=exc)
            result = await client.send_email(
                from_addr="kora@stormhavenenterprises.com",
                to=["joshua@stormhavenenterprises.com"],
                subject="x",
                body_text="x",
            )
        assert result.status == "failed"
        assert _TEST_PASSWORD not in (result.error or "")

    log_text = _log_path(_isolate).read_text()
    assert _TEST_PASSWORD not in log_text, (
        "password leaked into outbound JSONL across diverse failure modes"
    )


def test_password_never_in_client_repr():
    client = PurelymailClient()
    assert _TEST_PASSWORD not in repr(client)


# ===========================================================================
# Sanitize + smtp-code-parse helpers
# ===========================================================================


def test_sanitize_error_redacts_password():
    out = _sanitize_error(f"bad auth: {_TEST_PASSWORD}", _TEST_PASSWORD)
    assert _TEST_PASSWORD not in out
    assert "<REDACTED>" in out


def test_sanitize_error_handles_empty_inputs():
    assert _sanitize_error("", "x") == ""
    assert _sanitize_error("foo", "") == "foo"


def test_parse_smtp_code_extracts_leading_int():
    assert _parse_smtp_code("250 OK") == 250
    assert _parse_smtp_code("421 Service not available") == 421
    assert _parse_smtp_code("550 5.7.1 rejected") == 550


def test_parse_smtp_code_returns_none_on_malformed():
    assert _parse_smtp_code("") is None
    assert _parse_smtp_code("OK") is None
    assert _parse_smtp_code("12 short") is None
    assert _parse_smtp_code("abcd not numeric") is None


# ===========================================================================
# send_email_internal convenience
# ===========================================================================


@pytest.mark.asyncio
async def test_send_email_internal_constructs_client_and_sends(_isolate):
    """Module-level convenience builds a fresh client per call."""
    from kora_cli.clients.purelymail_client import send_email_internal

    smtp_patch, fake = _patch_smtp(response="250 OK")
    with smtp_patch:
        result = await send_email_internal(
            from_addr="kora@stormhavenenterprises.com",
            to=["joshua@stormhavenenterprises.com"],
            subject="hello",
            body_text="hi",
        )
    assert result.status == "ok"
    fake.send_message.assert_awaited_once()


# ===========================================================================
# KR-EMAIL-OUTBOUND-REASONING-META — opt-in JSONL fields
# ===========================================================================


@pytest.mark.asyncio
async def test_outbound_log_omits_reasoning_meta_when_kwargs_unset(_isolate):
    """Backwards-compat: callers that don't pass the new kwargs get
    an entry without the reasoning-meta keys (matches pre-bucket
    shape)."""
    client = PurelymailClient()
    smtp_patch, _ = _patch_smtp(response="250 OK")
    with smtp_patch:
        await client.send_email(
            from_addr="kora@stormhavenenterprises.com",
            to=["joshua@stormhavenenterprises.com"],
            subject="hello",
            body_text="hi",
        )
    entry = json.loads(_log_path(_isolate).read_text().splitlines()[-1])
    for key in (
        "model_used",
        "input_tokens",
        "output_tokens",
        "reasoning_duration_ms",
        "reasoning_error",
        "caller_session_id",
    ):
        assert key not in entry, (
            f"non-reasoning send unexpectedly wrote {key} to JSONL"
        )


@pytest.mark.asyncio
async def test_outbound_log_includes_reasoning_meta_when_kwargs_set(_isolate):
    """Reasoning-driven send: all 6 new fields land in the JSONL
    entry. Mirrors slack_dm's post-#131 shape exactly."""
    client = PurelymailClient()
    smtp_patch, _ = _patch_smtp(response="250 OK")
    with smtp_patch:
        await client.send_email(
            from_addr="kora@stormhavenenterprises.com",
            to=["joshua@stormhavenenterprises.com"],
            subject="hello",
            body_text="hi",
            model_used="claude-opus-4-7",
            input_tokens=100,
            output_tokens=50,
            reasoning_duration_ms=1500,
            reasoning_error=None,
            caller_session_id="email:<msg-1@example.com>",
        )
    entry = json.loads(_log_path(_isolate).read_text().splitlines()[-1])
    assert entry["model_used"] == "claude-opus-4-7"
    assert entry["input_tokens"] == 100
    assert entry["output_tokens"] == 50
    assert entry["reasoning_duration_ms"] == 1500
    assert entry["caller_session_id"] == "email:<msg-1@example.com>"
    # reasoning_error None → still omitted (opt-in inclusion pattern;
    # only non-None fields land in JSONL).
    assert "reasoning_error" not in entry


@pytest.mark.asyncio
async def test_outbound_log_partial_meta_only_writes_non_none_fields(
    _isolate,
):
    """Engine-unavailable path: reasoning_error set, all other meta
    fields None → only reasoning_error appears in JSONL."""
    client = PurelymailClient()
    smtp_patch, _ = _patch_smtp(response="250 OK")
    with smtp_patch:
        await client.send_email(
            from_addr="kora@stormhavenenterprises.com",
            to=["joshua@stormhavenenterprises.com"],
            subject="canned",
            body_text="(canned)",
            reasoning_error="engine_unavailable",
            caller_session_id="email:<m@e>",
        )
    entry = json.loads(_log_path(_isolate).read_text().splitlines()[-1])
    assert entry["reasoning_error"] == "engine_unavailable"
    assert entry["caller_session_id"] == "email:<m@e>"
    assert "model_used" not in entry
    assert "input_tokens" not in entry


@pytest.mark.asyncio
async def test_outbound_log_includes_meta_on_smtp_failure_too(_isolate):
    """The reasoning meta is written based on what the CALLER passed,
    independent of whether SMTP succeeded — so a failed send still
    records the model/tokens used (the inference happened even if
    delivery didn't)."""
    client = PurelymailClient()
    smtp_patch, _ = _patch_smtp(
        send_exc=aiosmtplib.SMTPResponseException(550, "permanent reject")
    )
    with smtp_patch:
        await client.send_email(
            from_addr="kora@stormhavenenterprises.com",
            to=["joshua@stormhavenenterprises.com"],
            subject="x",
            body_text="y",
            model_used="claude-haiku-4-5",
            input_tokens=10,
            output_tokens=5,
            reasoning_duration_ms=300,
            caller_session_id="email:<failed@e>",
        )
    entry = json.loads(_log_path(_isolate).read_text().splitlines()[-1])
    assert entry["send_status"] == "failed"
    # Meta still recorded — useful for the panel to show "we tried
    # this model + spent these tokens, then SMTP rejected".
    assert entry["model_used"] == "claude-haiku-4-5"
    assert entry["input_tokens"] == 10
    assert entry["caller_session_id"] == "email:<failed@e>"


@pytest.mark.asyncio
async def test_send_email_internal_forwards_reasoning_meta(_isolate):
    """The module-level convenience must forward the new kwargs too
    so one-shot callers (notifications) can attach meta if relevant."""
    from kora_cli.clients.purelymail_client import send_email_internal

    smtp_patch, _ = _patch_smtp(response="250 OK")
    with smtp_patch:
        await send_email_internal(
            from_addr="kora@stormhavenenterprises.com",
            to=["joshua@stormhavenenterprises.com"],
            subject="hi",
            body_text="body",
            model_used="claude-opus-4-7",
            caller_session_id="email:<via-internal@e>",
        )
    entry = json.loads(_log_path(_isolate).read_text().splitlines()[-1])
    assert entry["model_used"] == "claude-opus-4-7"
    assert entry["caller_session_id"] == "email:<via-internal@e>"
