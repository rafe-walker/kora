"""Purelymail inbound IMAP client (KR-FEAT-EMAIL-INBOUND-IMAP ST1).

Async IMAP4 client targeting ``imap.purelymail.com:993`` (SSL) by
default, with operator overrides for host / port. Polls INBOX for
UNSEEN messages, parses RFC822 to :class:`ParsedIncomingEmail`,
exposes a per-message :meth:`mark_seen` so the downstream handler
can keep retry-eligible messages UNSEEN on errors.

# Why IMAP polling (not webhooks)

Verified during the outbound bucket's first STOP-ASK: Purelymail
offers NO inbound webhooks. IMAP is the only documented mechanism.
This client makes that contract explicit; if Purelymail later
ships webhooks, the listener can swap transports without changing
the handler interface.

Per the verified-fresh K-DG check
(https://purelymail.com/docs/setup/technical, 2026-05-22):

  - Host: ``imap.purelymail.com``
  - Port: ``993``
  - Encryption: ``SSL/TLS`` (explicit; not STARTTLS)
  - Auth: full email + password, or App Password when 2FA enabled

# Auth

  - ``KORA_PUREMAIL_IMAP_USERNAME`` — full email
    (e.g. ``kora@stormhavenenterprises.com``)
  - ``KORA_PUREMAIL_IMAP_APP_PASSWORD`` — App Password from
    Purelymail dashboard (assumes 2FA enabled on the account)
  - ``KORA_PUREMAIL_IMAP_HOST`` — override host
  - ``KORA_PUREMAIL_IMAP_PORT`` — override port

Fail-CLOSED on missing username / password — raises at construction
time, mirrors the SMTP client's contract so daemon startup surfaces
the gap immediately.

The IMAP App Password may or may not be reusable with the SMTP one
depending on Purelymail's App Password scoping model (the docs
don't confirm protocol scoping explicitly — `App passwords give
full access to your email`). The runbook documents minting a
separate App Password as the safe default; operators may reuse if
they prefer.

# Connection lifetime

This client does NOT keep a long-lived IMAP connection. Each
``connect()`` opens, ``fetch_unseen()`` reads, ``mark_seen()``
flips flags, then ``close()`` logs out. Trade-off:

  - Open-per-cycle (chosen): clean recovery from transient network
    failures; no stale-connection retry pathology; idle TCP
    sessions don't accumulate at Purelymail's IMAP server.
  - Long-lived (rejected): would need IMAP IDLE for push, plus
    keep-alive heartbeat and reconnect logic that doubles the
    surface area for marginal latency wins. Inbound email is not
    high-throughput.

Per-call timeout: 30s (matches the SMTP client).

# Security contract

  - Password kept in private ``_password`` attribute; never
    logged, never serialized into errors / repr / JSONL.
  - IMAP server error responses may echo credentials; the
    :func:`_sanitize_error` pass strips any occurrence of the
    password before re-raising.
  - Body bytes ARE materialized here (the handler trims to 2KB
    for JSONL) — operator can pull full body from webmail.
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import logging
import os
from datetime import datetime, timezone
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from typing import List, Optional, Tuple

import aioimaplib

from kora_cli.clients.purelymail_types import (
    AttachmentMeta,
    ParsedIncomingEmail,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PurelymailIMAPError(RuntimeError):
    """Base class for IMAP client failures."""


class PurelymailIMAPConfigError(PurelymailIMAPError):
    """Operator-config gap — missing env var, invalid port, etc."""


class PurelymailIMAPConnectError(PurelymailIMAPError):
    """Transport-tier failure: cannot reach server, login refused,
    SELECT INBOX failed, etc. Raised by :meth:`connect`."""


class PurelymailIMAPFetchError(PurelymailIMAPError):
    """A SEARCH or FETCH command did not return ``OK``. Wrapped
    here so the listener can log + reset the connection on the
    next cycle."""


# ---------------------------------------------------------------------------
# Env + defaults
# ---------------------------------------------------------------------------


DEFAULT_HOST = "imap.purelymail.com"
DEFAULT_PORT = 993  # SSL implicit per Purelymail docs (2026-05-22)

PER_CALL_TIMEOUT_SECONDS = 30.0
INBOX_NAME = "INBOX"


def _resolve_env(name: str) -> Optional[str]:
    raw = os.environ.get(name, "").strip()
    return raw or None


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------


def _sanitize_error(text: str, password: str) -> str:
    """Strip the password from ``text`` before exposing.

    Matches :func:`kora_cli.clients.purelymail_client._sanitize_error`.
    """
    if not text or not password:
        return text
    return text.replace(password, "<REDACTED>")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _decode_header_str(raw: Optional[str]) -> str:
    """Decode an RFC2047-encoded header to a plain string.

    Empty / missing → ``""``. Wraps :func:`email.header.decode_header`
    so callers get a single str without juggling chunks.
    """
    if not raw:
        return ""
    from email.header import decode_header, make_header

    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


def _extract_text_and_html(msg: Message) -> Tuple[str, Optional[str], bool]:
    """Walk a possibly-multipart message and pull out text + html bodies.

    Returns ``(body_text, body_html_or_none, has_html)``. If the
    message is plain-text only, ``body_html`` is ``None`` and
    ``has_html`` is False. If only HTML is present, ``body_text``
    is empty and ``has_html`` is True (operator can read the HTML
    via webmail).
    """
    body_text = ""
    body_html: Optional[str] = None

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdisp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in cdisp:
                continue
            if ctype == "text/plain" and not body_text:
                body_text = _decode_part_payload(part)
            elif ctype == "text/html" and body_html is None:
                body_html = _decode_part_payload(part)
    else:
        ctype = msg.get_content_type()
        payload = _decode_part_payload(msg)
        if ctype == "text/html":
            body_html = payload
        else:
            body_text = payload

    return body_text, body_html, body_html is not None


def _decode_part_payload(part: Message) -> str:
    """Best-effort decode of one part's payload to str.

    Falls back to a latin-1 round-trip if the part's declared
    charset can't be honored — operator-readable but lossless for
    ASCII, which is the common case.
    """
    raw = part.get_payload(decode=True)
    if raw is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _collect_attachments(msg: Message) -> List[AttachmentMeta]:
    """Walk the message tree + return metadata for every attachment."""
    out: List[AttachmentMeta] = []
    if not msg.is_multipart():
        return out
    for part in msg.walk():
        cdisp = (part.get("Content-Disposition") or "").lower()
        if "attachment" not in cdisp:
            continue
        filename = part.get_filename() or "unnamed"
        ctype = part.get_content_type() or "application/octet-stream"
        payload = part.get_payload(decode=True) or b""
        out.append(
            AttachmentMeta(
                filename=_decode_header_str(filename),
                size_bytes=len(payload),
                content_type=ctype,
            )
        )
    return out


def _parse_rfc822(raw_bytes: bytes, *, imap_uid: int) -> ParsedIncomingEmail:
    """Build a :class:`ParsedIncomingEmail` from a fetched RFC822 blob.

    Uses :class:`email.policy.default` so header parsing handles
    RFC2047 encoded-words + RFC6532 international addresses
    consistently.
    """
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)

    message_id = (msg.get("Message-ID") or "").strip()
    if not message_id:
        # Synthetic id when the sender forgot — needed so the
        # JSONL log + dedupe keys remain stable per-UID.
        message_id = f"<no-msg-id-uid-{imap_uid}@kora.local>"

    from_raw = msg.get("From") or ""
    from_pairs = getaddresses([from_raw]) if from_raw else []
    from_address = from_pairs[0][1] if from_pairs else from_raw.strip()

    to_raw = msg.get_all("To") or []
    to_pairs = getaddresses(to_raw) if to_raw else []
    to_list = [addr for _name, addr in to_pairs if addr]

    subject = _decode_header_str(msg.get("Subject"))

    date_raw = msg.get("Date")
    received_at: datetime
    if date_raw:
        try:
            received_at = parsedate_to_datetime(date_raw)
            if received_at.tzinfo is None:
                received_at = received_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            received_at = datetime.now(timezone.utc)
    else:
        received_at = datetime.now(timezone.utc)

    body_text, body_html, has_html = _extract_text_and_html(msg)
    attachments = _collect_attachments(msg)

    return ParsedIncomingEmail(
        message_id=message_id,
        from_address=from_address,
        to=to_list,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        has_html=has_html,
        received_at=received_at,
        attachments=attachments,
        imap_uid=imap_uid,
    )


def _parse_search_uids(response_lines: List[bytes]) -> List[int]:
    """Extract integer UIDs from an IMAP SEARCH response.

    SEARCH returns lines like ``b"1 4 7"`` (UIDs space-separated)
    or ``b""`` when no matches. We accept either bytes or str.
    """
    uids: List[int] = []
    for line in response_lines or []:
        if isinstance(line, bytes):
            line = line.decode("ascii", errors="ignore")
        for tok in line.split():
            try:
                uids.append(int(tok))
            except ValueError:
                continue
    return uids


def _extract_rfc822_payload(
    fetch_lines: List, *, imap_uid: int
) -> Optional[bytes]:
    """Pull the RFC822 byte blob out of an IMAP FETCH response.

    aioimaplib's FETCH returns a list whose entries alternate
    between header-line bytes (e.g. ``b"1 FETCH (UID 7 RFC822 {1234}"``)
    and the literal payload bytes. We return the first ``bytes``
    entry that is at least 16 bytes long (header lines are short),
    which works for the common 'fetch one message' shape.
    """
    for entry in fetch_lines:
        if isinstance(entry, bytes) and len(entry) >= 16:
            # Heuristic: header lines start with the message seq +
            # 'FETCH' literal; payload doesn't. Skip the header.
            head = entry[:64].decode("ascii", errors="ignore").upper()
            if "FETCH" in head and "RFC822" in head:
                continue
            return entry
    logger.debug(
        "[kora.purelymail_imap] uid=%s: no RFC822 payload found in "
        "FETCH response (entries=%d)",
        imap_uid,
        len(fetch_lines or []),
    )
    return None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class PurelymailIMAPClient:
    """IMAP4 SSL inbound client for Purelymail INBOX.

    Construct once per daemon; the listener calls ``connect`` /
    ``fetch_unseen`` / ``mark_seen`` / ``close`` each poll cycle.
    No long-lived connection between cycles — see module docstring.
    """

    def __init__(
        self,
        *,
        username_env: str = "KORA_PUREMAIL_IMAP_USERNAME",
        password_env: str = "KORA_PUREMAIL_IMAP_APP_PASSWORD",
        host_env: str = "KORA_PUREMAIL_IMAP_HOST",
        port_env: str = "KORA_PUREMAIL_IMAP_PORT",
    ) -> None:
        username = _resolve_env(username_env)
        if username is None:
            raise PurelymailIMAPConfigError(
                f"{username_env} is unset or empty; refusing to instantiate "
                f"PurelymailIMAPClient. Set the full email address (e.g. "
                f"'kora@stormhavenenterprises.com') in Doppler "
                f"kora-runtime-gateways."
            )
        password = _resolve_env(password_env)
        if password is None:
            raise PurelymailIMAPConfigError(
                f"{password_env} is unset or empty; refusing to instantiate "
                f"PurelymailIMAPClient. Mint a Purelymail App Password "
                f"(assumes 2FA enabled) and set in Doppler. May be the "
                f"same value as KORA_PUREMAIL_SMTP_APP_PASSWORD if your "
                f"Purelymail App Password is protocol-shared; safer to "
                f"mint a fresh one."
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
                raise PurelymailIMAPConfigError(
                    f"{port_env}={port_raw!r} is not an integer"
                ) from exc
            if not (1 <= self._port <= 65535):
                raise PurelymailIMAPConfigError(
                    f"{port_env}={self._port} must be in 1..65535"
                )
        self._imap: Optional[aioimaplib.IMAP4_SSL] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open SSL IMAP connection + login + select INBOX.

        Replaces any prior connection (the listener's per-cycle
        ``connect → fetch → mark → close`` shape rebuilds each
        time). Raises :class:`PurelymailIMAPConnectError` on any
        transport / auth / select failure.
        """
        # Drop any stale handle defensively before opening a new one.
        await self._drop_handle()

        imap = aioimaplib.IMAP4_SSL(host=self._host, port=self._port)
        try:
            await asyncio.wait_for(
                imap.wait_hello_from_server(),
                timeout=PER_CALL_TIMEOUT_SECONDS,
            )
            login_resp = await asyncio.wait_for(
                imap.login(self._username, self._password),
                timeout=PER_CALL_TIMEOUT_SECONDS,
            )
            if login_resp.result != "OK":
                raise PurelymailIMAPConnectError(
                    f"IMAP LOGIN failed: result={login_resp.result!r} "
                    f"lines={login_resp.lines!r}"
                )
            select_resp = await asyncio.wait_for(
                imap.select(mailbox=INBOX_NAME),
                timeout=PER_CALL_TIMEOUT_SECONDS,
            )
            if select_resp.result != "OK":
                raise PurelymailIMAPConnectError(
                    f"IMAP SELECT {INBOX_NAME} failed: "
                    f"result={select_resp.result!r} "
                    f"lines={select_resp.lines!r}"
                )
        except PurelymailIMAPConnectError:
            await _safe_logout(imap)
            raise
        except asyncio.TimeoutError as exc:
            await _safe_logout(imap)
            raise PurelymailIMAPConnectError(
                _sanitize_error(
                    f"IMAP connect timeout after "
                    f"{PER_CALL_TIMEOUT_SECONDS}s",
                    self._password,
                )
            ) from exc
        except Exception as exc:
            await _safe_logout(imap)
            raise PurelymailIMAPConnectError(
                _sanitize_error(
                    f"IMAP connect failed: {type(exc).__name__}: {exc}",
                    self._password,
                )
            ) from exc

        self._imap = imap
        logger.info(
            "[kora.purelymail_imap] connected host=%s port=%d "
            "user=%s mailbox=%s",
            self._host,
            self._port,
            self._username,
            INBOX_NAME,
        )

    async def close(self) -> None:
        """LOGOUT + drop the IMAP handle. Idempotent + fail-soft."""
        if self._imap is None:
            return
        await _safe_logout(self._imap)
        self._imap = None
        logger.info("[kora.purelymail_imap] disconnected")

    # ------------------------------------------------------------------
    # Mail operations
    # ------------------------------------------------------------------

    async def fetch_unseen(self) -> List[ParsedIncomingEmail]:
        """SEARCH UNSEEN → FETCH each → parse RFC822.

        Does NOT mark messages as SEEN — that's the handler's
        responsibility after successful processing (so handler
        errors keep messages eligible for next-poll retry).
        Returns an empty list when no unseen messages.
        """
        imap = self._require_connected()

        try:
            search_resp = await asyncio.wait_for(
                imap.uid_search("UNSEEN"),
                timeout=PER_CALL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise PurelymailIMAPFetchError(
                _sanitize_error(
                    f"IMAP UID SEARCH UNSEEN timed out after "
                    f"{PER_CALL_TIMEOUT_SECONDS}s",
                    self._password,
                )
            ) from exc

        if search_resp.result != "OK":
            raise PurelymailIMAPFetchError(
                f"IMAP UID SEARCH UNSEEN failed: "
                f"result={search_resp.result!r} lines={search_resp.lines!r}"
            )

        uids = _parse_search_uids(search_resp.lines)
        if not uids:
            return []

        out: List[ParsedIncomingEmail] = []
        for uid in uids:
            parsed = await self._fetch_one(uid)
            if parsed is not None:
                out.append(parsed)
        return out

    async def mark_seen(self, imap_uid: int) -> None:
        """Set the ``\\Seen`` flag on one message.

        Handler calls this AFTER successful processing so transient
        handler errors keep the message UNSEEN for next-poll retry.
        """
        imap = self._require_connected()
        try:
            resp = await asyncio.wait_for(
                imap.uid("store", str(imap_uid), "+FLAGS", "(\\Seen)"),
                timeout=PER_CALL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise PurelymailIMAPFetchError(
                _sanitize_error(
                    f"IMAP STORE \\Seen timed out for uid={imap_uid}",
                    self._password,
                )
            ) from exc
        if resp.result != "OK":
            raise PurelymailIMAPFetchError(
                f"IMAP STORE \\Seen uid={imap_uid} failed: "
                f"result={resp.result!r} lines={resp.lines!r}"
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_connected(self) -> aioimaplib.IMAP4_SSL:
        if self._imap is None:
            raise PurelymailIMAPConnectError(
                "IMAP client is not connected; call connect() first"
            )
        return self._imap

    async def _drop_handle(self) -> None:
        if self._imap is None:
            return
        await _safe_logout(self._imap)
        self._imap = None

    async def _fetch_one(self, uid: int) -> Optional[ParsedIncomingEmail]:
        """FETCH one message by UID; parse + return ParsedIncomingEmail.

        A single-message fetch failure is logged + returns ``None``
        so a malformed message doesn't poison the whole poll cycle.
        Connection-tier failures still raise (caller resets the
        connection).
        """
        imap = self._require_connected()
        try:
            fetch_resp = await asyncio.wait_for(
                imap.uid("fetch", str(uid), "(RFC822)"),
                timeout=PER_CALL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[kora.purelymail_imap] FETCH uid=%d timed out — "
                "skipping this message; will retry next cycle (still UNSEEN)",
                uid,
            )
            return None
        if fetch_resp.result != "OK":
            logger.warning(
                "[kora.purelymail_imap] FETCH uid=%d result=%s "
                "lines=%r — skipping",
                uid,
                fetch_resp.result,
                fetch_resp.lines,
            )
            return None
        raw = _extract_rfc822_payload(fetch_resp.lines, imap_uid=uid)
        if raw is None:
            logger.warning(
                "[kora.purelymail_imap] FETCH uid=%d returned no RFC822 "
                "payload; skipping",
                uid,
            )
            return None
        try:
            return _parse_rfc822(raw, imap_uid=uid)
        except Exception as exc:
            logger.warning(
                "[kora.purelymail_imap] parse failed uid=%d: %r — skipping",
                uid,
                exc,
            )
            return None

    def __repr__(self) -> str:
        # Password explicitly excluded from repr — security carry-
        # forward from the SMTP client's contract.
        return (
            f"PurelymailIMAPClient(host={self._host!r} port={self._port} "
            f"user={self._username!r})"
        )


async def _safe_logout(imap: aioimaplib.IMAP4_SSL) -> None:
    """Best-effort LOGOUT; swallow any error so callers can always
    proceed to drop the handle. Per aioimaplib docs, ``close()``
    only closes the mailbox — ``logout()`` is what actually closes
    the TCP connection."""
    try:
        await asyncio.wait_for(imap.logout(), timeout=5.0)
    except Exception:
        pass
