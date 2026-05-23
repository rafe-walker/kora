"""Purelymail outbound SMTP client (KR-FEAT-EMAIL ST1).

Async SMTP client targeting ``smtp.purelymail.com:465`` (SSL) by
default, with operator overrides for host/port. Sends via
:class:`email.message.EmailMessage` + :mod:`aiosmtplib`.

# Why SMTP (not REST)

Verified during the bucket's second STOP-ASK: Purelymail offers
NO REST send API. SMTP is the only documented outbound
mechanism (https://purelymail.com/docs/setup/technical). The
external ``send_email`` surface in this module is transport-
agnostic — if Purelymail later ships REST or we switch
providers, the swap is internal to this client.

# Auth

  - ``KORA_PUREMAIL_SMTP_USERNAME`` — full email
    (e.g. ``kora@stormhavenenterprises.com``)
  - ``KORA_PUREMAIL_SMTP_APP_PASSWORD`` — App Password from
    Purelymail dashboard (assumes 2FA enabled on the account)
  - ``KORA_PUREMAIL_SMTP_HOST`` — override host (default
    ``smtp.purelymail.com``)
  - ``KORA_PUREMAIL_SMTP_PORT`` — override port (default ``465``
    SSL; ``587`` for STARTTLS)

Fail-CLOSED on missing username or password — raises at
:class:`PurelymailClient` construction time, not on first send,
so daemon startup surfaces the gap immediately.

# Retry policy (SMTP-specific, NOT HTTP)

  - 1 retry on SMTP transient codes: ``421`` (service not
    available / channel closing), ``450`` (mailbox unavailable),
    ``451`` (local processing error), ``452`` (insufficient
    storage)
  - 1 retry on connection-tier errors (``SMTPConnectError``,
    ``SMTPServerDisconnected``, ``asyncio.TimeoutError``)
  - NO retry on 5xx permanent (``SMTPDataError`` / ``SMTPSenderRefused``
    / ``SMTPAuthenticationError`` with 5xx code) — these are
    deterministic failures; retrying lands the runtime in
    greylist / spam-filter purgatory

Maximum 1 retry per call. Per-call timeout: 30s.

# Security contract

  - Password kept in private ``_password`` attribute; never
    logged, never serialized into errors / repr / JSONL.
  - SMTP error responses may echo credentials in their text; the
    :func:`_sanitize_error` pass strips any occurrence of the
    password before exposing in :class:`SendResult.error` or in
    the JSONL log.
  - Body text NOT included in JSONL log (subject + recipients
    only — operator can pull body from Purelymail's sent folder
    if needed).

# Domain allowlist

The ``from_addr``'s domain must be in
``KORA_EMAIL_KORA_ALLOWED_FROM_DOMAINS`` (comma-separated). An
unset/empty value is an operator-config error (raises at send),
NOT silently allow-all — defense against accidental sends from
arbitrary domains.

# Recipient + attachment caps

  - ``len(to)`` ≤ 10 (defense against accidental mass-send)
  - Each recipient must contain ``@``
  - Per-attachment size ≤ 10 MiB
  - Total batch attachment size ≤ 25 MiB
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Optional

import aiosmtplib

from kora_cli.clients.purelymail_types import Attachment, SendResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PurelymailClientError(RuntimeError):
    """Base class for client-tier failures."""


class PurelymailConfigError(PurelymailClientError):
    """Operator-config gap — missing env var, missing allowlist,
    invalid port, etc. Surfaces at instantiation OR before any
    SMTP activity."""


class PurelymailRejectError(PurelymailClientError):
    """A send was rejected client-side (before any SMTP traffic):
    disallowed from-domain, malformed recipient, too many
    recipients, oversized attachment."""


# ---------------------------------------------------------------------------
# Env + config
# ---------------------------------------------------------------------------


DEFAULT_HOST = "smtp.purelymail.com"
DEFAULT_PORT = 465  # SSL implicit; 587 = STARTTLS

PER_CALL_TIMEOUT_SECONDS = 30.0
MAX_RECIPIENTS = 10
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MiB
MAX_TOTAL_ATTACHMENT_BYTES = 25 * 1024 * 1024  # 25 MiB

# Transient SMTP codes the retry-once policy honors.
SMTP_TRANSIENT_CODES = frozenset({421, 450, 451, 452})

ALLOWED_FROM_DOMAINS_ENV = "KORA_EMAIL_KORA_ALLOWED_FROM_DOMAINS"


def _resolve_env(name: str) -> Optional[str]:
    raw = os.environ.get(name, "").strip()
    return raw or None


def _outbound_log_path() -> Path:
    """Resolve the outbound-log path via the canonical KORA_HOME
    lookup, mirroring CC#3's slack client pattern."""
    from kora_constants import get_kora_home

    return get_kora_home() / "email_outbound_log.jsonl"


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------


def _sanitize_error(text: str, password: str) -> str:
    """Strip the password from ``text`` before exposing.

    SMTP server error responses occasionally echo credentials in
    their reply text (esp. older / misconfigured servers).
    Replaces any occurrence of the password value with
    ``"<REDACTED>"`` — same pattern as the heartbeat-probes
    :func:`kora_cli.heartbeat_probes.base.sanitize_error` helper.
    """
    if not text or not password:
        return text
    return text.replace(password, "<REDACTED>")


def _parse_smtp_code(response_text: str) -> Optional[int]:
    """Extract the leading SMTP response code from a server reply.

    Returns ``None`` when the text doesn't start with a 3-digit
    code (unexpected; surfaces as ``smtp_code: null`` in the
    SendResult)."""
    if not response_text:
        return None
    first = response_text.strip().split()[:1]
    if not first or not first[0].isdigit() or len(first[0]) != 3:
        return None
    return int(first[0])


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class PurelymailClient:
    """SMTP-based outbound email client for Purelymail.

    Construct once per daemon (or process); call ``send_email``
    per outbound message. The client does NOT hold an open SMTP
    connection between sends — each call opens fresh, sends,
    quits. Purelymail's SMTP server is happy with this; we
    avoid stale-connection retry pathology.
    """

    def __init__(
        self,
        *,
        username_env: str = "KORA_PUREMAIL_SMTP_USERNAME",
        password_env: str = "KORA_PUREMAIL_SMTP_APP_PASSWORD",
        host_env: str = "KORA_PUREMAIL_SMTP_HOST",
        port_env: str = "KORA_PUREMAIL_SMTP_PORT",
    ) -> None:
        username = _resolve_env(username_env)
        if username is None:
            raise PurelymailConfigError(
                f"{username_env} is unset or empty; refusing to instantiate "
                f"PurelymailClient. Set the full email address (e.g. "
                f"'kora@stormhavenenterprises.com') in Doppler "
                f"kora-runtime-gateways."
            )
        password = _resolve_env(password_env)
        if password is None:
            raise PurelymailConfigError(
                f"{password_env} is unset or empty; refusing to instantiate "
                f"PurelymailClient. Mint a Purelymail App Password "
                f"(assumes 2FA enabled) and set in Doppler."
            )
        self._username = username
        self._password = password
        self._host = _resolve_env(host_env) or DEFAULT_HOST
        port_raw = _resolve_env(port_env)
        if port_raw is None:
            self._port = DEFAULT_PORT
        else:
            try:
                self._port = int(port_raw)
            except ValueError as exc:
                raise PurelymailConfigError(
                    f"{port_env}={port_raw!r} is not an integer"
                ) from exc
            if not (1 <= self._port <= 65535):
                raise PurelymailConfigError(
                    f"{port_env}={self._port} must be in 1..65535"
                )

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    async def send_email(
        self,
        *,
        from_addr: str,
        to: list[str],
        subject: str,
        body_text: str,
        body_html: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        attachments: Optional[list[Attachment]] = None,
        caller_actor_kind: Optional[str] = None,
    ) -> SendResult:
        """Send one email. Returns a :class:`SendResult` always —
        failures are surfaced via ``status="failed"`` + ``error``
        rather than raising. Client-side rejections (allowlist /
        cap violations) DO raise :class:`PurelymailRejectError`
        before any SMTP activity (operator should never accidentally
        ship malformed sends).

        The returned ``message_id`` is the locally-generated
        ``Message-ID`` header value (SMTP doesn't return a
        server-assigned ID); operator threading + audit log
        index on it.
        """
        # 1. Client-side validation (BEFORE any SMTP traffic).
        self._validate_allowed_from(from_addr)
        self._validate_recipients(to)
        self._validate_attachments(attachments or [])

        # 2. Build the MIME message + Message-ID.
        message_id = self._make_message_id(from_addr)
        message = self._build_message(
            from_addr=from_addr,
            to=to,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            in_reply_to=in_reply_to,
            attachments=attachments or [],
            message_id=message_id,
        )

        # 3. SMTP send (with 1-retry policy on transient codes).
        result = await self._send_with_retry(message=message, message_id=message_id)

        # 4. JSONL audit log.
        self._append_outbound_log(
            from_addr=from_addr,
            to=to,
            subject=subject,
            in_reply_to=in_reply_to,
            result=result,
            caller_actor_kind=caller_actor_kind,
        )

        return result

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_allowed_from(self, from_addr: str) -> None:
        if "@" not in from_addr:
            raise PurelymailRejectError(
                f"from_addr={from_addr!r} is malformed (no '@')"
            )
        allowed_raw = _resolve_env(ALLOWED_FROM_DOMAINS_ENV)
        if allowed_raw is None:
            # Empty / unset → operator-config error, NOT silently
            # allow-all. Defense against accidental wide-open sends.
            raise PurelymailConfigError(
                f"{ALLOWED_FROM_DOMAINS_ENV} is unset; refusing to send. "
                f"Set comma-separated allowed from-domains in Doppler "
                f"(e.g. 'stormhavenenterprises.com')."
            )
        allowed_domains = {
            d.strip().lower() for d in allowed_raw.split(",") if d.strip()
        }
        if not allowed_domains:
            raise PurelymailConfigError(
                f"{ALLOWED_FROM_DOMAINS_ENV} parsed to empty set; "
                f"refusing to send"
            )
        domain = from_addr.rsplit("@", 1)[1].lower()
        if domain not in allowed_domains:
            raise PurelymailRejectError(
                f"from_addr domain {domain!r} not in allowlist "
                f"({sorted(allowed_domains)})"
            )

    @staticmethod
    def _validate_recipients(to: list[str]) -> None:
        if not to:
            raise PurelymailRejectError("recipient list 'to' is empty")
        if len(to) > MAX_RECIPIENTS:
            raise PurelymailRejectError(
                f"too many recipients ({len(to)} > {MAX_RECIPIENTS})"
            )
        for addr in to:
            if not isinstance(addr, str) or "@" not in addr:
                raise PurelymailRejectError(
                    f"recipient {addr!r} is malformed (no '@')"
                )

    @staticmethod
    def _validate_attachments(attachments: list[Attachment]) -> None:
        total = 0
        for att in attachments:
            size = len(att.content)
            if size > MAX_ATTACHMENT_BYTES:
                raise PurelymailRejectError(
                    f"attachment {att.filename!r} is {size} bytes; max "
                    f"per-attachment {MAX_ATTACHMENT_BYTES}"
                )
            total += size
        if total > MAX_TOTAL_ATTACHMENT_BYTES:
            raise PurelymailRejectError(
                f"total attachment size {total} bytes exceeds "
                f"{MAX_TOTAL_ATTACHMENT_BYTES}"
            )

    # ------------------------------------------------------------------
    # MIME assembly
    # ------------------------------------------------------------------

    @staticmethod
    def _make_message_id(from_addr: str) -> str:
        # Use the from-address domain so the Message-ID is in a
        # domain we control (matches the SPF/DKIM/DMARC posture
        # operator set up).
        domain = (
            from_addr.rsplit("@", 1)[1] if "@" in from_addr else "localhost"
        )
        return make_msgid(domain=domain)

    @staticmethod
    def _build_message(
        *,
        from_addr: str,
        to: list[str],
        subject: str,
        body_text: str,
        body_html: Optional[str],
        in_reply_to: Optional[str],
        attachments: list[Attachment],
        message_id: str,
    ) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = from_addr
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        msg["Message-ID"] = message_id
        msg["Date"] = datetime.now(timezone.utc).strftime(
            "%a, %d %b %Y %H:%M:%S +0000"
        )
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            # Best-practice threading: also append to References.
            msg["References"] = in_reply_to
        msg.set_content(body_text)
        if body_html:
            msg.add_alternative(body_html, subtype="html")
        for att in attachments:
            msg.add_attachment(
                att.content,
                maintype=att.maintype,
                subtype=att.subtype,
                filename=att.filename,
            )
        return msg

    # ------------------------------------------------------------------
    # SMTP send + retry
    # ------------------------------------------------------------------

    async def _send_with_retry(
        self, *, message: EmailMessage, message_id: str
    ) -> SendResult:
        """Up to 2 attempts. Retry on SMTP transient codes (421/450/
        451/452) OR connection-tier errors (connect/disconnected/
        timeout). NO retry on 5xx permanent."""
        for attempt in range(2):
            try:
                response_text = await asyncio.wait_for(
                    self._send_once(message), timeout=PER_CALL_TIMEOUT_SECONDS
                )
            except aiosmtplib.SMTPResponseException as exc:
                # Has a numeric .code; decide retry by code.
                code = getattr(exc, "code", None)
                if (
                    isinstance(code, int)
                    and code in SMTP_TRANSIENT_CODES
                    and attempt == 0
                ):
                    logger.warning(
                        "[kora.purelymail] SMTP %s transient on attempt %d "
                        "of 2; retrying",
                        code,
                        attempt + 1,
                    )
                    continue
                return SendResult(
                    status="failed",
                    message_id=message_id,
                    error=_sanitize_error(str(exc), self._password),
                    smtp_code=code if isinstance(code, int) else None,
                    sent_at=datetime.now(timezone.utc),
                    retry_count=attempt,
                )
            except (
                aiosmtplib.SMTPConnectError,
                aiosmtplib.SMTPServerDisconnected,
                asyncio.TimeoutError,
            ) as exc:
                if attempt == 0:
                    logger.warning(
                        "[kora.purelymail] connection-tier failure on "
                        "attempt 1 (%s); retrying",
                        type(exc).__name__,
                    )
                    continue
                return SendResult(
                    status="failed",
                    message_id=message_id,
                    error=_sanitize_error(
                        f"{type(exc).__name__}: {exc}", self._password
                    ),
                    smtp_code=None,
                    sent_at=datetime.now(timezone.utc),
                    retry_count=attempt,
                )
            except aiosmtplib.SMTPException as exc:
                # Other SMTP exceptions (auth/recipient/sender refused)
                # → no retry, immediate fail.
                code = getattr(exc, "code", None)
                return SendResult(
                    status="failed",
                    message_id=message_id,
                    error=_sanitize_error(str(exc), self._password),
                    smtp_code=code if isinstance(code, int) else None,
                    sent_at=datetime.now(timezone.utc),
                    retry_count=attempt,
                )
            else:
                # Success.
                return SendResult(
                    status="ok",
                    message_id=message_id,
                    error=None,
                    smtp_code=_parse_smtp_code(response_text),
                    sent_at=datetime.now(timezone.utc),
                    retry_count=attempt,
                )
        # Unreachable — the loop always returns or continues exactly once.
        raise PurelymailClientError("send_with_retry exited unexpectedly")

    async def _send_once(self, message: EmailMessage) -> str:
        """One open-send-close cycle. Returns the final response str."""
        use_tls = self._port == 465
        start_tls = self._port == 587
        smtp = aiosmtplib.SMTP(
            hostname=self._host,
            port=self._port,
            use_tls=use_tls,
            start_tls=start_tls,
            timeout=PER_CALL_TIMEOUT_SECONDS,
        )
        await smtp.connect()
        try:
            await smtp.login(self._username, self._password)
            _refused, response = await smtp.send_message(message)
        finally:
            try:
                await smtp.quit()
            except Exception:  # pragma: no cover — best-effort close
                pass
        return response or ""

    # ------------------------------------------------------------------
    # JSONL outbound log
    # ------------------------------------------------------------------

    def _append_outbound_log(
        self,
        *,
        from_addr: str,
        to: list[str],
        subject: str,
        in_reply_to: Optional[str],
        result: SendResult,
        caller_actor_kind: Optional[str] = None,
    ) -> None:
        """Append one line to the outbound JSONL. Body NEVER logged.

        Fail-soft: a log write error must not crash a successful
        send (or mask a failed-send result). Logged at WARN if it
        ever happens.

        ``caller_actor_kind`` (KR-MCP-SEND-TOOLS): when a send is
        driven by an MCP tool call, the caller's actor_kind appears
        here for audit attribution. ``None`` for internal/runtime-
        driven sends (e.g. KR-FEAT-AI-RESPONSE-LOOP email replies).
        Backwards-compatible — consumers handle absence.
        """
        entry = {
            "sent_at": result.sent_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "from": from_addr,
            "to": list(to),
            "subject": subject,
            "in_reply_to": in_reply_to,
            "send_status": result.status,
            "message_id": result.message_id,
            "smtp_code": result.smtp_code,
            "error": result.error,
            "retry_count": result.retry_count,
            "caller_actor_kind": caller_actor_kind,
        }
        try:
            log_path = _outbound_log_path()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            logger.warning(
                "[kora.purelymail] outbound log append failed for "
                "message_id=%s",
                result.message_id,
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------


async def send_email_internal(
    *,
    from_addr: str,
    to: list[str],
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    attachments: Optional[list[Attachment]] = None,
    caller_actor_kind: Optional[str] = None,
) -> SendResult:
    """One-shot send for callers inside Kora's runtime.

    Builds a fresh :class:`PurelymailClient` (reads env each call)
    and sends. Suitable for low-frequency callers (notification
    paths, operator-debug sends). High-frequency callers should
    instantiate the client once and call ``send_email`` per
    message to avoid repeated env reads.

    KR-MCP-SEND-TOOLS update: ``caller_actor_kind`` propagates to
    the JSONL audit log when the send is driven by an MCP tool
    call. Daemon-coordinator-managed paths prefer the listener
    accessor (``current_purelymail_client``) over this one-shot
    helper to share a single client instance + reduce env-read
    overhead.
    """
    client = PurelymailClient()
    return await client.send_email(
        from_addr=from_addr,
        to=to,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        in_reply_to=in_reply_to,
        attachments=attachments,
        caller_actor_kind=caller_actor_kind,
    )
