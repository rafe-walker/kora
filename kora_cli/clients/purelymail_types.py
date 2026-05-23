"""Types for the Purelymail SMTP outbound + IMAP inbound clients.

Pydantic models + dataclasses describing both the send-side and
receive-side surfaces. Kept thin — the client modules import these
but they don't depend on ``aiosmtplib`` / ``aioimaplib`` so they
can be re-exported / mocked in tests without dragging transports
in.

Originated in KR-FEAT-EMAIL ST1 (outbound); extended in
KR-FEAT-EMAIL-INBOUND-IMAP ST1 (inbound: ``AttachmentMeta`` +
``ParsedIncomingEmail``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True, slots=True)
class Attachment:
    """One email attachment.

    ``content`` is raw bytes (already-encoded payload). Size caps
    are enforced by :class:`PurelymailClient` before any SMTP
    activity. Filename is preserved verbatim in the MIME headers.

    Attributes:
        filename: Display name in the email (e.g. ``"report.pdf"``).
        content: Raw bytes payload.
        maintype: MIME main type (e.g. ``"application"``,
            ``"image"``, ``"text"``).
        subtype: MIME subtype (e.g. ``"pdf"``, ``"png"``,
            ``"plain"``).
    """

    filename: str
    content: bytes
    maintype: str
    subtype: str


class SendResult(BaseModel):
    """Result of one :meth:`PurelymailClient.send_email` call.

    The SMTP server does NOT return a server-assigned message id;
    ``message_id`` is generated locally via
    :func:`email.utils.make_msgid` and set in the outgoing
    ``Message-ID`` header. Operator threading + JSONL audit log
    use the same value.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "failed"]
    message_id: str = Field(min_length=1)
    error: Optional[str] = None
    smtp_code: Optional[int] = None
    sent_at: datetime
    retry_count: int = Field(ge=0, le=1)


# ---------------------------------------------------------------------------
# Inbound — KR-FEAT-EMAIL-INBOUND-IMAP ST1
# ---------------------------------------------------------------------------


class AttachmentMeta(BaseModel):
    """One inbound attachment — metadata only (bytes out of scope).

    Operator pulls the raw payload from Purelymail's webmail when
    they need the contents; the daemon's JSONL log records just
    enough to triage (name + size + content-type).
    """

    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    content_type: str = Field(min_length=1)


class ParsedIncomingEmail(BaseModel):
    """One inbound message extracted from an IMAP FETCH RFC822 response.

    Body fields decoded to text where possible; binary or oddly-
    encoded parts surface as the encoded form (operator triages via
    webmail). ``imap_uid`` is the per-mailbox uid the inbound
    handler later passes back to :meth:`PurelymailIMAPClient.mark_seen`
    after successful processing.
    """

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1)
    from_address: str = Field(min_length=1)
    to: List[str]
    subject: str
    body_text: str
    body_html: Optional[str] = None
    has_html: bool = False
    received_at: datetime
    attachments: List[AttachmentMeta] = Field(default_factory=list)
    imap_uid: int = Field(ge=0)
