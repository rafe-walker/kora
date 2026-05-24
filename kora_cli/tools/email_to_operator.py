"""Outbound email composer — KR-EMAIL-OUTBOUND-COMPOSE-TOOL.

R3 Q8a operator use case: *"the other way mostly, like 'Kora,
email me that pdf etc.'"*. Companion to KR-INTENT-EMAIL-TO-SEA-TICKET
(#176, inbound direction) — this module gives Kora's reasoning
engine a tool to compose and send email to the operator when the
response is too long, too formatted, or too attachment-heavy for
Slack DM.

# Security posture — recipient pinned

Recipient is ALWAYS ``KORA_EMAIL_JOSHUA_ADDRESS``. The tool does
NOT accept a ``to`` argument from the caller (this is the
critical defensive difference vs. the existing
``kora__send_email`` MCP tool, which accepts caller-controlled
recipients and is gated to other agents via cap_matrix). With
recipient pinned by the executor itself there is no mass-send
risk, which is why this tool — unlike the existing
``kora__send_email`` — is safe to expose to Kora's own reasoning
loop (see ``kora_cli/reasoning/tool_registry.py`` allowlist
docstring for the prior exclusion's reasoning).

# Caps + tunables

  * ``KORA_EMAIL_OUTBOUND_HOURLY_CAP`` — int, default 5. Sliding-
    window cap on successful sends per process. Zero disables.
    Lower than the INTENT-TO-SEA-TICKET cap (10) because outbound
    SMTP is a more visible operator action than a Sea_Ticket row;
    operator should be deliberate about Kora-initiated emails.
  * ``KORA_EMAIL_OUTBOUND_MAX_ATTACH_MB`` — int, default 20 (MB).
    Total combined attachment size cap. Below Purelymail's 25 MB
    SMTP ceiling for safety margin.
  * ``KORA_EMAIL_JOSHUA_ADDRESS`` — recipient. Already used by
    PR #173 (inbound identity check) + PR #176 (intent gate).
  * ``KORA_PUREMAIL_SMTP_USERNAME`` — from_addr; resolved by the
    daemon-singleton PurelymailClient.

# Fail-soft

Every step is wrapped so the tool always returns a structured
result dict (never raises). The reasoning engine sees the
``status`` field and can adapt (e.g. retry via Slack on
``smtp_failure`` / ``hourly_cap_exceeded``).
"""

from __future__ import annotations

import logging
import mimetypes
import os
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from kora_cli.audit.jsonl_sink import emit_audit
from kora_cli.clients.purelymail_types import Attachment

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env vars + defaults
# ---------------------------------------------------------------------------


RECIPIENT_ENV = "KORA_EMAIL_JOSHUA_ADDRESS"
HOURLY_CAP_ENV = "KORA_EMAIL_OUTBOUND_HOURLY_CAP"
MAX_ATTACH_MB_ENV = "KORA_EMAIL_OUTBOUND_MAX_ATTACH_MB"
SMTP_USERNAME_ENV = "KORA_PUREMAIL_SMTP_USERNAME"

DEFAULT_HOURLY_CAP = 5
DEFAULT_MAX_ATTACH_MB = 20
HOURLY_WINDOW = timedelta(hours=1)
SUBJECT_MAX_CHARS = 200

# Status enum the executor returns. Wire-stable; consumers (the
# reasoning engine + the future cockpit panel) branch on this.
STATUS_SENT = "sent"
STATUS_REJECTED = "rejected"
STATUS_SMTP_FAILURE = "smtp_failure"

# Rejection reasons — also wire-stable.
REASON_RECIPIENT_UNSET = "recipient_env_unset"
REASON_SMTP_FROM_UNSET = "smtp_from_env_unset"
REASON_CLIENT_UNAVAILABLE = "purelymail_client_unavailable"
REASON_SUBJECT_TOO_LONG = "subject_too_long"
REASON_SUBJECT_EMPTY = "subject_empty"
REASON_BODY_EMPTY = "body_empty"
REASON_ATTACHMENT_TOO_LARGE = "attachment_too_large"
REASON_ATTACHMENT_MISSING_FILE = "attachment_missing_file"
REASON_ATTACHMENT_UNREADABLE = "attachment_unreadable"
REASON_HOURLY_CAP_EXCEEDED = "hourly_cap_exceeded"


# ---------------------------------------------------------------------------
# Sliding-window hourly cap (module-level state)
# ---------------------------------------------------------------------------


_recent_send_timestamps: Deque[datetime] = deque()


def _resolve_hourly_cap() -> int:
    raw = os.environ.get(HOURLY_CAP_ENV, "").strip()
    if not raw:
        return DEFAULT_HOURLY_CAP
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "[kora.tool.email_to_operator] malformed %s=%r — falling "
            "back to default %d",
            HOURLY_CAP_ENV,
            raw,
            DEFAULT_HOURLY_CAP,
        )
        return DEFAULT_HOURLY_CAP
    if value < 0:
        logger.warning(
            "[kora.tool.email_to_operator] %s=%d negative — falling "
            "back to default %d",
            HOURLY_CAP_ENV,
            value,
            DEFAULT_HOURLY_CAP,
        )
        return DEFAULT_HOURLY_CAP
    return value


def _resolve_max_attach_bytes() -> int:
    raw = os.environ.get(MAX_ATTACH_MB_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_ATTACH_MB * 1024 * 1024
    try:
        mb = int(raw)
    except ValueError:
        logger.warning(
            "[kora.tool.email_to_operator] malformed %s=%r — falling "
            "back to default %d MB",
            MAX_ATTACH_MB_ENV,
            raw,
            DEFAULT_MAX_ATTACH_MB,
        )
        return DEFAULT_MAX_ATTACH_MB * 1024 * 1024
    if mb <= 0:
        logger.warning(
            "[kora.tool.email_to_operator] %s=%d must be > 0 — falling "
            "back to default %d MB",
            MAX_ATTACH_MB_ENV,
            mb,
            DEFAULT_MAX_ATTACH_MB,
        )
        return DEFAULT_MAX_ATTACH_MB * 1024 * 1024
    return mb * 1024 * 1024


def _hourly_cap_allows(now: Optional[datetime] = None) -> bool:
    cap = _resolve_hourly_cap()
    if cap == 0:
        return True
    current = now or datetime.now(timezone.utc)
    cutoff = current - HOURLY_WINDOW
    while _recent_send_timestamps and _recent_send_timestamps[0] < cutoff:
        _recent_send_timestamps.popleft()
    return len(_recent_send_timestamps) < cap


def _record_send(now: Optional[datetime] = None) -> None:
    _recent_send_timestamps.append(now or datetime.now(timezone.utc))


def _reset_rate_limiter_for_tests() -> None:
    """Test-only: clear the module-level deque. Production code
    MUST NOT call this."""
    _recent_send_timestamps.clear()


# ---------------------------------------------------------------------------
# Attachment ingestion
# ---------------------------------------------------------------------------


def _guess_mime(filename: str) -> tuple[str, str]:
    """Return ``(maintype, subtype)`` for a filename. Defaults to
    ``application/octet-stream`` when the MIME type can't be
    inferred — covers extensionless or unusual artifacts cleanly.
    """
    guessed, _ = mimetypes.guess_type(filename)
    if not guessed or "/" not in guessed:
        return ("application", "octet-stream")
    maintype, subtype = guessed.split("/", 1)
    return (maintype, subtype)


def _read_attachments(
    raw_attachments: List[Dict[str, Any]],
    max_total_bytes: int,
) -> tuple[Optional[List[Attachment]], Optional[Dict[str, Any]]]:
    """Read attachment files off disk + build :class:`Attachment` list.

    Returns ``(attachments, error_dict_or_None)``. On any per-file
    failure the function aborts (does NOT partial-send) and
    returns a rejection-shaped error dict the caller can fold into
    the result.

    Total size cap is enforced as bytes accumulate — a 50-MB
    second file aborts the read before reading it, not after.
    """
    if not raw_attachments:
        return ([], None)

    out: List[Attachment] = []
    total = 0
    for idx, entry in enumerate(raw_attachments):
        filename = (entry or {}).get("filename")
        path_str = (entry or {}).get("content_path")
        if not isinstance(filename, str) or not filename.strip():
            return (None, {
                "reason": REASON_ATTACHMENT_MISSING_FILE,
                "attachment_index": idx,
                "detail": "attachment missing 'filename'",
            })
        if not isinstance(path_str, str) or not path_str.strip():
            return (None, {
                "reason": REASON_ATTACHMENT_MISSING_FILE,
                "attachment_index": idx,
                "filename": filename,
                "detail": "attachment missing 'content_path'",
            })
        path = Path(path_str)
        if not path.is_file():
            return (None, {
                "reason": REASON_ATTACHMENT_MISSING_FILE,
                "attachment_index": idx,
                "filename": filename,
                "content_path": str(path),
            })
        try:
            size = path.stat().st_size
        except OSError as exc:
            return (None, {
                "reason": REASON_ATTACHMENT_UNREADABLE,
                "attachment_index": idx,
                "filename": filename,
                "detail": f"stat failed: {exc!r}",
            })
        if total + size > max_total_bytes:
            return (None, {
                "reason": REASON_ATTACHMENT_TOO_LARGE,
                "attachment_index": idx,
                "filename": filename,
                "size_bytes": size,
                "total_so_far_bytes": total,
                "max_total_bytes": max_total_bytes,
            })
        try:
            content = path.read_bytes()
        except OSError as exc:
            return (None, {
                "reason": REASON_ATTACHMENT_UNREADABLE,
                "attachment_index": idx,
                "filename": filename,
                "detail": f"read failed: {exc!r}",
            })
        total += len(content)
        maintype, subtype = _guess_mime(filename)
        out.append(
            Attachment(
                filename=filename,
                content=content,
                maintype=maintype,
                subtype=subtype,
            )
        )
    return (out, None)


# ---------------------------------------------------------------------------
# Audit emission
# ---------------------------------------------------------------------------


def _safe_audit(
    *,
    details: Dict[str, Any],
    caller_session_id: Optional[str],
) -> None:
    """Wrap :func:`emit_audit` so an audit-write failure can't
    blow up the tool path. Best-effort, log on miss, never raise."""
    try:
        emit_audit(
            "tool.email_to_operator_sent",
            details,
            caller_session_id=caller_session_id,
            source="reasoning",
        )
    except Exception as exc:
        logger.warning(
            "[kora.tool.email_to_operator.audit_failed] %r — "
            "tool continues",
            exc,
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _reject(
    *,
    reason: str,
    detail: Optional[Dict[str, Any]] = None,
    audit_details: Dict[str, Any],
    caller_session_id: Optional[str],
) -> Dict[str, Any]:
    audit_payload = dict(audit_details)
    audit_payload["status"] = STATUS_REJECTED
    audit_payload["rejection_reason"] = reason
    if detail:
        audit_payload["rejection_detail"] = detail
    _safe_audit(details=audit_payload, caller_session_id=caller_session_id)
    out: Dict[str, Any] = {"status": STATUS_REJECTED, "reason": reason}
    if detail:
        out["detail"] = detail
    return out


async def send_email_to_operator(
    *,
    subject: str,
    body: str,
    attachments: Optional[List[Dict[str, Any]]] = None,
    caller_session_id: Optional[str] = None,
    purelymail_client: Optional[Any] = None,
) -> Dict[str, Any]:
    """Compose and send an email to the operator. Recipient is
    PINNED to ``KORA_EMAIL_JOSHUA_ADDRESS`` — caller cannot
    override.

    Args:
      subject: Email subject. ≤200 chars, non-empty after trim.
      body: Email body. Plain text or markdown (sent verbatim
        as ``body_text``; HTML rendering is a follow-on).
      attachments: Optional list of
        ``{"filename": str, "content_path": str}`` dicts. The
        executor reads each file off disk.
      caller_session_id: Optional correlation key threaded into
        the audit row (engine passes its own per-respond session
        id).
      purelymail_client: Override for tests; production passes
        ``None`` and the executor resolves
        :func:`current_purelymail_client`.

    Returns one of:
      ``{"status": "sent", "smtp_message_id": str, "sent_at": str,
         "attachment_count": int, "attachment_total_bytes": int}``
      ``{"status": "rejected", "reason": str, "detail": dict | None}``
      ``{"status": "smtp_failure", "error": str}``

    Never raises — every failure path is captured as a result
    dict so the reasoning engine can adapt.
    """
    audit_details: Dict[str, Any] = {
        "subject_chars": len(subject or ""),
        "body_chars": len(body or ""),
        "attachment_count": len(attachments or []),
    }

    # 1. Recipient env present?
    recipient = os.environ.get(RECIPIENT_ENV, "").strip()
    if not recipient:
        return _reject(
            reason=REASON_RECIPIENT_UNSET,
            audit_details=audit_details,
            caller_session_id=caller_session_id,
        )

    # 2. Subject validation.
    subject_trim = (subject or "").strip()
    if not subject_trim:
        return _reject(
            reason=REASON_SUBJECT_EMPTY,
            audit_details=audit_details,
            caller_session_id=caller_session_id,
        )
    if len(subject_trim) > SUBJECT_MAX_CHARS:
        return _reject(
            reason=REASON_SUBJECT_TOO_LONG,
            detail={
                "subject_chars": len(subject_trim),
                "max_chars": SUBJECT_MAX_CHARS,
            },
            audit_details=audit_details,
            caller_session_id=caller_session_id,
        )

    # 3. Body validation.
    if not (body or "").strip():
        return _reject(
            reason=REASON_BODY_EMPTY,
            audit_details=audit_details,
            caller_session_id=caller_session_id,
        )

    # 4. Hourly cap.
    if not _hourly_cap_allows():
        cap = _resolve_hourly_cap()
        return _reject(
            reason=REASON_HOURLY_CAP_EXCEEDED,
            detail={"hourly_cap": cap},
            audit_details=audit_details,
            caller_session_id=caller_session_id,
        )

    # 5. Attachment read + size cap.
    max_total_bytes = _resolve_max_attach_bytes()
    attached, attach_err = _read_attachments(
        attachments or [], max_total_bytes
    )
    if attach_err is not None:
        return _reject(
            reason=attach_err["reason"],
            detail={k: v for k, v in attach_err.items() if k != "reason"},
            audit_details=audit_details,
            caller_session_id=caller_session_id,
        )
    attached_list: List[Attachment] = attached or []
    total_bytes = sum(len(a.content) for a in attached_list)
    audit_details["attachment_total_bytes"] = total_bytes

    # 6. Resolve from_addr + the live PurelymailClient.
    from_addr = os.environ.get(SMTP_USERNAME_ENV, "").strip()
    if not from_addr:
        return _reject(
            reason=REASON_SMTP_FROM_UNSET,
            audit_details=audit_details,
            caller_session_id=caller_session_id,
        )

    client = purelymail_client or _resolve_purelymail_client()
    if client is None:
        return _reject(
            reason=REASON_CLIENT_UNAVAILABLE,
            audit_details=audit_details,
            caller_session_id=caller_session_id,
        )

    # 7. Send. PurelymailClient enforces its own per/total
    # attachment-byte caps + recipient-count cap; failures bubble
    # back as exceptions which we capture as ``smtp_failure``.
    try:
        result = await client.send_email(
            from_addr=from_addr,
            to=[recipient],
            subject=subject_trim,
            body_text=body,
            body_html=None,
            in_reply_to=None,
            attachments=attached_list or None,
            caller_actor_kind="kora_reasoning_self",
        )
    except Exception as exc:
        logger.warning(
            "[kora.tool.email_to_operator.smtp_failure] %r — "
            "result returned to reasoning engine",
            exc,
        )
        failure_payload = {
            **audit_details,
            "status": STATUS_SMTP_FAILURE,
            "error": f"{type(exc).__name__}",
        }
        _safe_audit(
            details=failure_payload, caller_session_id=caller_session_id
        )
        return {
            "status": STATUS_SMTP_FAILURE,
            "error": f"{type(exc).__name__}",
        }

    # 8. Record + audit success.
    smtp_message_id = getattr(result, "message_id", None)
    sent_at_dt = getattr(result, "sent_at", None)
    sent_at_str: Optional[str] = None
    if isinstance(sent_at_dt, datetime):
        sent_at_str = sent_at_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    # PurelymailClient may return SendResult(status="failed") for
    # retried SMTP rejections; surface that as smtp_failure rather
    # than sent.
    status = getattr(result, "status", None)
    if status != "ok":
        failure_payload = {
            **audit_details,
            "status": STATUS_SMTP_FAILURE,
            "smtp_status": status,
            "error": getattr(result, "error", None),
        }
        _safe_audit(
            details=failure_payload, caller_session_id=caller_session_id
        )
        return {
            "status": STATUS_SMTP_FAILURE,
            "error": getattr(result, "error", None) or "smtp_status_not_ok",
        }

    _record_send()
    success_payload = {
        **audit_details,
        "status": STATUS_SENT,
        "smtp_message_id": smtp_message_id,
        "sent_at": sent_at_str,
    }
    _safe_audit(
        details=success_payload, caller_session_id=caller_session_id
    )
    return {
        "status": STATUS_SENT,
        "smtp_message_id": smtp_message_id,
        "sent_at": sent_at_str,
        "attachment_count": len(attached_list),
        "attachment_total_bytes": total_bytes,
    }


def _resolve_purelymail_client() -> Optional[Any]:
    """Lazy import + read the daemon's live PurelymailClient
    singleton. Returns ``None`` when the listener isn't wired
    (caller treats as ``REASON_CLIENT_UNAVAILABLE``).
    """
    try:
        from kora_cli.listeners.purelymail_client_listener import (
            current_purelymail_client,
        )
    except Exception:
        return None
    return current_purelymail_client()
