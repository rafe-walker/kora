"""Webhook dead-letter logging (KR-D-DAEMON ST3).

Records inbound webhook verification failures in a way the operator
can audit. Per the bucket spec the persistence target was
``kora_operation_ledger``, but that table's schema (per migration
0093) requires ``work_attempt_id`` (FK to ``work_attempts``,
NOT NULL) + ``workspace_id`` + ``ticket_id`` + ``tool_name`` — all
tied to Sea_Ticket dispatch. A webhook arrives with none of those.
Adding an ``op_kind`` column + relaxing the FKs is a substrate-side
schema change, out of scope for this ST.

# What this module does today (scaffold)

Structured log line at WARNING level with a stable
``[kora.webhook.dead_letter]`` prefix + the metadata operators need
to investigate (source, reason, request_id, header summary, peer
IP, timestamp). NO body content — bodies could carry user data, and
the spec is explicit: headers-only.

Operators investigate via the existing log-streaming surface
(OPS-PANEL chain-event tail, or ``flyctl logs``). When the substrate
team ships either:
  - a ``webhook_dead_letters`` table, OR
  - a permissive ``kora_operation_ledger`` shape that admits non-
    work-attempt rows, OR
  - a ``kora.webhook.dead_letter`` chain-event vocab literal
… this module's body extends to write durable records too. The
log-line API is the stable seam.

# Why structured logging instead of `kora__append_event` today

Calling ``kora__append_event`` requires a substrate-side vocab
literal under PG's CHECK constraint on ``event_log.event_type``.
``kora.webhook.dead_letter`` is not in the
``foundation/0159_*`` vocab migration as of feature/phase2-upgrades
HEAD. A vocab-migration PR is the cleanest substrate-side path;
the runtime adapts when it lands.
"""

from __future__ import annotations

import logging
import time
from typing import Mapping, Optional

logger = logging.getLogger(__name__)


# Header allow-list — only these are logged, even if the request
# carries more. Keeps the dead-letter record compact + ensures we
# never accidentally log Authorization / Cookie / signature secrets.
_LOGGED_HEADERS = frozenset(
    [
        "content-type",
        "content-length",
        "user-agent",
        "x-slack-request-timestamp",
        "x-forwarded-for",
        "x-real-ip",
        # We deliberately log the PRESENCE of signature headers but
        # truncate the value to first 12 chars (see _summarize_headers).
        "x-slack-signature",
        "x-purelymail-signature",
    ]
)


def _summarize_headers(headers: Mapping[str, str]) -> dict:
    """Filter to allow-list + truncate signature values."""
    out: dict[str, str] = {}
    for name, value in headers.items():
        lname = name.lower()
        if lname not in _LOGGED_HEADERS:
            continue
        if "signature" in lname and value:
            # Log presence + a short prefix to support
            # "which-version-of-the-secret" triage without leaking
            # the full HMAC.
            out[lname] = value[:12] + "...(truncated)"
        else:
            out[lname] = value
    return out


def emit_webhook_dead_letter(
    *,
    source: str,
    reason: str,
    headers: Mapping[str, str],
    peer_ip: Optional[str] = None,
    request_id: Optional[str] = None,
    body_bytes: Optional[int] = None,
    now: Optional[float] = None,
) -> None:
    """Record a webhook dead-letter event.

    Args:
      source: One of ``"slack"`` / ``"email"`` — identifies which
        verifier rejected the request.
      reason: Machine-readable code from
        :class:`VerificationOutcome.reason` (e.g.
        ``"slack_signature_mismatch"``).
      headers: Mapping of header name → value. Only the allow-list
        is logged; signature values are truncated.
      peer_ip: Best-effort client IP (FastAPI ``request.client.host``).
      request_id: Optional correlation id (e.g.
        ``X-Request-ID`` header) — propagated for downstream triage.
      body_bytes: Size of the request body in bytes. Captured for
        operator visibility into "was it a junk-empty hit or a
        crafted forgery attempt". NEVER captures body content.
      now: Injectable timestamp for tests. Defaults to ``time.time()``.
    """
    ts = now if now is not None else time.time()
    logger.warning(
        "[kora.webhook.dead_letter] source=%s reason=%s ts=%.3f "
        "peer_ip=%s request_id=%s body_bytes=%s headers=%s",
        source,
        reason,
        ts,
        peer_ip or "-",
        request_id or "-",
        body_bytes if body_bytes is not None else "-",
        _summarize_headers(headers),
    )
