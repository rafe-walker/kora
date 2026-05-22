"""Types for the Purelymail SMTP outbound client (KR-FEAT-EMAIL ST1).

Pydantic models + dataclasses describing the send-side surface.
Kept thin — the client module imports these but they don't depend
on ``aiosmtplib`` so they can be re-exported / mocked in tests
without dragging the SMTP transport in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional

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
