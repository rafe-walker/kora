"""Email → Sea_Ticket intent recognition (KR-INTENT-EMAIL-TO-SEA-TICKET).

First real product-value use of Kora's inbound-email surface
(parsing intact since KR-FEAT-EMAIL-INBOUND-IMAP ST2; auto-reply
branch removed in PR #173 / Lock R3-8 (a)). Operator's R3 Q8a:
"I email Kora something I found that I want to save as an idea on
a Sea_Ticket."

# Flow

  1. Email passes the 5-step filter in
     :class:`EmailInboundHandler` (filters confirm sender ==
     ``KORA_EMAIL_JOSHUA_ADDRESS``).
  2. Handler calls :func:`process_email_intent` with the parsed
     message.
  3. :func:`recognize_intent` runs the regex patterns against
     subject + body, returns an :class:`EmailIntent`.
  4. If confidence ≥ configured floor AND the hourly cap allows,
     :func:`write_sea_ticket_from_intent` calls the in-process
     IsoKron MCP client to invoke ``sea__create_ticket``.
  5. :func:`confirm_via_dm` posts a confirmation DM to the
     ``KORA_SLACK_JOSHUA_USER_ID`` IM.
  6. One audit entry per email evaluated (seam
     ``intent.email_to_sea_ticket``) records the outcome —
     ``created`` / ``dry_run`` / ``logged_only`` /
     ``cap_exceeded`` / ``failed``.

# Configuration

  * ``KORA_EMAIL_INTENT_MIN_CONFIDENCE`` — ``"high"`` (default) or
    ``"medium"``. Recognition floor for write-through. Anything
    below is recorded but not acted on.
  * ``KORA_EMAIL_INTENT_DRY_RUN`` — ``"true"`` to skip the
    Sea_Ticket write + confirmation DM. Useful for tuning regex
    against real operator mail without producing artifacts.
  * ``KORA_EMAIL_INTENT_HOURLY_CAP`` — integer (default ``10``).
    Sliding-window per-hour cap on write-through (recognized +
    above-floor + non-dry-run). The 11th match in any 1-hour
    window is recorded as ``cap_exceeded`` (still audited; no
    write, no DM).
  * ``KORA_SLACK_JOSHUA_USER_ID`` — Slack IM channel id for the
    confirmation DM. Reused from KR-ALERT-NOTIFY.

# Fail-soft posture

This is in-process orchestration; the email handler is the
boundary. Any exception inside :func:`process_email_intent`
should be caught by the handler so the inbound flow's
``HANDLED_RECEIVED`` status is unaffected — the email is logged,
the audit + reply are best-effort. Per-step failures (audit
write, Sea_Ticket write, DM post) are all individually
try/except-wrapped here so one failing step doesn't poison the
others.
"""

from __future__ import annotations

import logging
import os
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Literal, Optional, Tuple

from kora_cli.audit.jsonl_sink import emit_audit

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env vars + defaults
# ---------------------------------------------------------------------------


MIN_CONFIDENCE_ENV = "KORA_EMAIL_INTENT_MIN_CONFIDENCE"
DRY_RUN_ENV = "KORA_EMAIL_INTENT_DRY_RUN"
HOURLY_CAP_ENV = "KORA_EMAIL_INTENT_HOURLY_CAP"
JOSHUA_SLACK_USER_ID_ENV = "KORA_SLACK_JOSHUA_USER_ID"

DEFAULT_MIN_CONFIDENCE = "high"
DEFAULT_HOURLY_CAP = 10
HOURLY_WINDOW = timedelta(hours=1)

# Body excerpt cap for the Sea_Ticket body. Operator-readable
# emails are typically short; cap protects against pathological
# forwarded chains pushing huge text into substrate.
BODY_EXCERPT_LIMIT = 8192


Confidence = Literal["high", "medium", "low", "unrecognized"]


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------


# Compile once at module-import. All patterns are case-insensitive.
#
# Pattern naming convention (used as the audit `pattern_matched`
# field and as a tag on the created Sea_Ticket):
#
#   subject_* — matched a structured subject prefix
#   body_*    — matched a marker anywhere in the body
#   fwd_*     — forwarded-message shape (subject "Fwd:" or body
#               "Forwarded message" / "----- Forwarded message")
#
# Highest-confidence pattern wins (subject_idea_prefix beats
# fwd_with_note when both apply).
_SUBJECT_NOTE_PREFIX_RE = re.compile(
    r"^\s*(?:idea|note|todo)\s*:\s*\S",
    re.IGNORECASE,
)
_SUBJECT_BRACKET_SAVE_RE = re.compile(
    r"\[\s*save\s*\]",
    re.IGNORECASE,
)
# Order matters in the union: longer phrases first so the regex
# doesn't shadow "save this" with the looser "save".
_BODY_EXPLICIT_SAVE_RE = re.compile(
    r"\b(?:"
    r"save\s+to\s+sea_ticket"
    r"|save\s+as\s+idea"
    r"|save\s+this"
    r"|add\s+to\s+sea"
    r"|add\s+idea"
    r")\b",
    re.IGNORECASE,
)
_SUBJECT_FWD_RE = re.compile(r"^\s*(?:fwd?|fw)\s*:", re.IGNORECASE)
_BODY_FWD_MARKER_RE = re.compile(
    r"(?:----+\s*forwarded\s+message|begin\s+forwarded\s+message)",
    re.IGNORECASE,
)
# Crude URL detector — used as a hint that a forward has content
# worth saving. Matches http:// + https:// plus bare www. prefixes.
_URL_RE = re.compile(
    r"\b(?:https?://|www\.)\S+",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# EmailIntent
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EmailIntent:
    """Result of :func:`recognize_intent`.

    ``proposed_sea_ticket`` is the dict the orchestrator forwards
    to ``sea__create_ticket`` when the intent is acted on. It's
    populated for every non-unrecognized intent so dry-run can
    log the proposed payload without writing.
    """

    confidence: Confidence
    pattern_matched: str
    tags: Tuple[str, ...] = field(default_factory=tuple)
    proposed_sea_ticket: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# recognize_intent
# ---------------------------------------------------------------------------


def _meets_min_confidence(actual: Confidence, floor: str) -> bool:
    """True if ``actual`` is at or above the configured floor."""
    if floor == "medium":
        return actual in ("high", "medium")
    # Default + any unrecognized floor value → high-only.
    return actual == "high"


def _derive_title(subject: str, body: str) -> str:
    """Pick a Sea_Ticket title from subject (preferred) or first
    sentence of body (fallback). Defensive against empty / generic
    "Fwd:" subjects.
    """
    stripped = (subject or "").strip()
    # Strip leading Fwd: / Re: prefixes (recursively) so a
    # "Fwd: Fwd: Re: Idea: foo" still yields "Idea: foo".
    prefix_re = re.compile(r"^\s*(?:fwd?|fw|re)\s*:\s*", re.IGNORECASE)
    while True:
        new = prefix_re.sub("", stripped, count=1)
        if new == stripped:
            break
        stripped = new
    if stripped:
        return stripped[:200]
    # Subject empty (or only prefixes) — fall back to first body
    # sentence, cropped to 200 chars.
    body_clean = (body or "").strip().splitlines()
    first_line = body_clean[0] if body_clean else ""
    if first_line:
        # Take up to the first sentence terminator or 200 chars.
        sentence_end = re.search(r"[.!?]\s", first_line)
        if sentence_end:
            first_line = first_line[: sentence_end.start() + 1]
        return first_line.strip()[:200]
    return "(email from Joshua — no subject)"


def _build_body(body_text: str, subject: str, sender: str) -> str:
    """Sea_Ticket body — full email text capped at
    :data:`BODY_EXCERPT_LIMIT`, with a small provenance header so
    operators reading the Sea_Ticket in the substrate UI know
    where it came from.
    """
    excerpt = (body_text or "").strip()
    if len(excerpt) > BODY_EXCERPT_LIMIT:
        excerpt = excerpt[:BODY_EXCERPT_LIMIT] + "\n\n[…truncated]"
    return (
        f"_Saved from email by Kora — sender `{sender}` "
        f"— subject `{subject or '(none)'}`._\n\n{excerpt}"
    )


def _extract_tags(
    pattern_matched: str, subject: str, body: str
) -> Tuple[str, ...]:
    """Project a small tag set from the matched pattern + content
    keywords. Tags are wire-stable strings that the future cockpit
    panel can filter on.
    """
    tags: List[str] = ["email"]
    # Pattern-driven tags.
    if "save" in pattern_matched or "idea" in pattern_matched:
        tags.append("idea")
    if "fwd" in pattern_matched:
        tags.append("forward")
    if "todo" in pattern_matched:
        tags.append("todo")
    if "note" in pattern_matched:
        tags.append("note")
    # Content-driven tag.
    combined = f"{subject}\n{body}".lower()
    if "todo" in combined and "todo" not in tags:
        tags.append("todo")
    return tuple(tags)


def recognize_intent(
    *, subject: str, body: str, sender: str
) -> EmailIntent:
    """Pure-function intent recognition over an email's subject + body.

    Returns an :class:`EmailIntent` with confidence + matched
    pattern name. Never raises.

    The ``sender`` parameter is included in the signature so future
    per-sender variants (e.g. different rule sets per allowlisted
    address) can branch without an API change; v1 doesn't use it
    for the rule decision (sender is already gated upstream by
    the handler's identity filter).
    """
    del sender  # reserved for future per-sender rule branching

    subject = subject or ""
    body = body or ""

    pattern: Optional[str] = None
    confidence: Confidence = "unrecognized"

    if _SUBJECT_NOTE_PREFIX_RE.match(subject):
        # "Idea: foo" / "Note: foo" / "TODO: foo"
        prefix = subject.split(":", 1)[0].strip().lower()
        pattern = f"subject_{prefix}_prefix"
        confidence = "high"
    elif _SUBJECT_BRACKET_SAVE_RE.search(subject) or _SUBJECT_BRACKET_SAVE_RE.search(body):
        pattern = "explicit_save_bracket"
        confidence = "high"
    elif _BODY_EXPLICIT_SAVE_RE.search(body) or _BODY_EXPLICIT_SAVE_RE.search(subject):
        pattern = "explicit_save_phrase"
        confidence = "high"
    elif _SUBJECT_FWD_RE.match(subject) or _BODY_FWD_MARKER_RE.search(body):
        # Forward-shaped. Confidence depends on whether the body
        # carries an operator note (a URL is the simplest proxy
        # for "Joshua found something worth saving"; without a
        # URL we still call it medium because the forward itself
        # is a meaningful signal).
        if _URL_RE.search(body):
            pattern = "fwd_with_url"
            confidence = "medium"
        else:
            pattern = "fwd_without_url"
            confidence = "medium"
    else:
        return EmailIntent(
            confidence="unrecognized",
            pattern_matched="unrecognized",
            tags=("email",),
            proposed_sea_ticket=None,
        )

    title = _derive_title(subject, body)
    ticket_body = _build_body(body, subject, sender="")
    tags = _extract_tags(pattern, subject, body)
    proposed: Dict[str, Any] = {
        "title": title,
        "body": ticket_body,
        "priority": "normal",
        "kind": "sea",
        "tags": list(tags),
    }
    return EmailIntent(
        confidence=confidence,
        pattern_matched=pattern,
        tags=tags,
        proposed_sea_ticket=proposed,
    )


# ---------------------------------------------------------------------------
# Sliding-window hourly rate limiter
# ---------------------------------------------------------------------------


# Module-level state — the email handler is single-process; one
# poll-cycle at a time runs through this module so a simple deque
# of recent-create timestamps is race-free under Python's GIL.
_recent_create_timestamps: Deque[datetime] = deque()


def _resolve_hourly_cap() -> int:
    """Read ``KORA_EMAIL_INTENT_HOURLY_CAP``. Defaults to
    :data:`DEFAULT_HOURLY_CAP`; malformed values warn + fall back
    to default; zero / negative values disable the cap (returns
    sentinel ``0`` which is treated as "no cap").
    """
    raw = os.environ.get(HOURLY_CAP_ENV, "").strip()
    if not raw:
        return DEFAULT_HOURLY_CAP
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "[kora.intent.email_to_sea_ticket] malformed %s=%r — "
            "falling back to default %d",
            HOURLY_CAP_ENV,
            raw,
            DEFAULT_HOURLY_CAP,
        )
        return DEFAULT_HOURLY_CAP
    if value < 0:
        logger.warning(
            "[kora.intent.email_to_sea_ticket] %s=%d is negative — "
            "falling back to default %d",
            HOURLY_CAP_ENV,
            value,
            DEFAULT_HOURLY_CAP,
        )
        return DEFAULT_HOURLY_CAP
    return value


def _hourly_cap_allows(now: Optional[datetime] = None) -> bool:
    """True when the recent-creates count in the last hour is
    below the configured cap. Cap of ``0`` means "no cap"
    (always allow).
    """
    cap = _resolve_hourly_cap()
    if cap == 0:
        return True
    current = now or datetime.now(timezone.utc)
    cutoff = current - HOURLY_WINDOW
    # Trim expired entries off the left edge.
    while _recent_create_timestamps and _recent_create_timestamps[0] < cutoff:
        _recent_create_timestamps.popleft()
    return len(_recent_create_timestamps) < cap


def _record_create(now: Optional[datetime] = None) -> None:
    """Stamp the cap-tracking deque after a successful Sea_Ticket
    write (or dry-run, see contract below). Dry-runs do NOT
    consume the cap — they're tuning-only, an operator running
    dry should be able to evaluate hundreds of emails without
    exhausting it.
    """
    _recent_create_timestamps.append(now or datetime.now(timezone.utc))


def _reset_rate_limiter_for_tests() -> None:
    """Test-only: clear the module-level deque. Production code
    MUST NOT call this."""
    _recent_create_timestamps.clear()


# ---------------------------------------------------------------------------
# Sea_Ticket write — in-process MCP client invocation
# ---------------------------------------------------------------------------


async def write_sea_ticket_from_intent(intent: EmailIntent) -> str:
    """Invoke ``sea__create_ticket`` via the in-process IsoKron
    MCP client. Returns the new ticket_id.

    Raises:
      RuntimeError — when the IsoKron provider isn't wired (daemon
        running without substrate-attached listeners; intent path
        can't proceed and the caller should DM the operator).
      Anything raised by the MCP client's transport layer — caller
        wraps + audits as ``action=failed``.
    """
    if intent.proposed_sea_ticket is None:
        raise RuntimeError("intent has no proposed_sea_ticket (unrecognized)")

    try:
        from plugins.memory.isokron import get_last_active_provider
    except Exception as exc:  # pragma: no cover — defensive
        raise RuntimeError(
            f"IsoKron provider module unavailable: {exc!r}"
        ) from exc

    provider = get_last_active_provider()
    if provider is None or getattr(provider, "_connection", None) is None:
        raise RuntimeError(
            "no active IsoKron provider — daemon running without "
            "substrate-attached listeners; cannot create Sea_Ticket"
        )

    mcp_client = provider._connection.get_mcp_client()
    payload = dict(intent.proposed_sea_ticket)
    # Tag the create-source so substrate-side audit logs attribute
    # the row to the email-intent path (operator can later filter
    # Sea_Tickets by origin).
    payload.setdefault("origin_actor_kind", "kora")
    payload.setdefault("origin_seam", "intent.email_to_sea_ticket")

    result = await mcp_client.invoke("sea__create_ticket", payload)

    ticket_id: Optional[str] = None
    if isinstance(result, dict):
        ticket_id = result.get("ticket_id") or result.get("id")
    if not ticket_id:
        raise RuntimeError(
            f"sea__create_ticket returned no ticket_id (raw response: "
            f"{result!r})"
        )
    return str(ticket_id)


# ---------------------------------------------------------------------------
# Slack confirmation DM
# ---------------------------------------------------------------------------


def _format_confirmation_text(
    *, ticket_id: str, subject: str, pattern_matched: str
) -> str:
    """Render the operator-facing confirmation message body.

    Stable string shape so operator's mental model is consistent
    across panels (the same phrasing appears in Slack + the future
    KR-FE-EMAIL-INTENT-LOG-PANEL cockpit panel).
    """
    subject_display = (subject or "").strip() or "(no subject)"
    return (
        f":seedling: Got it — saved *{subject_display}* as "
        f"Sea_Ticket `#{ticket_id}` (matched `{pattern_matched}`)."
    )


def _format_failure_text(
    *, subject: str, reason: str, pattern_matched: str
) -> str:
    subject_display = (subject or "").strip() or "(no subject)"
    return (
        f":warning: Could not save *{subject_display}* as Sea_Ticket "
        f"(matched `{pattern_matched}`): {reason}"
    )


async def _post_dm(
    *, slack_client: Any, channel_id: str, text: str
) -> None:
    """Wrapper around ``slack_client.post_dm`` with the same
    fail-soft posture used by KR-PROBE-WAKE-CONSUMER (#166): the
    DM is best-effort; transport failures log + are swallowed so
    the audit row still emits."""
    try:
        await slack_client.post_dm(channel_id=channel_id, text=text)
    except Exception as exc:
        logger.warning(
            "[kora.intent.email_to_sea_ticket.dm_failed] %r — "
            "audit row still emitted",
            exc,
        )


async def confirm_via_dm(
    *,
    slack_client: Any,
    ticket_id: str,
    subject: str,
    pattern_matched: str,
) -> None:
    """Post the success confirmation DM to ``KORA_SLACK_JOSHUA_USER_ID``.

    Caller passes the live slack_client (resolved from
    :func:`current_slack_client` at the orchestrator boundary so
    test code can inject a mock without touching the daemon
    singleton).
    """
    channel_id = os.environ.get(JOSHUA_SLACK_USER_ID_ENV, "").strip()
    if not channel_id:
        logger.warning(
            "[kora.intent.email_to_sea_ticket] %s unset — confirmation "
            "DM skipped (Sea_Ticket #%s created successfully)",
            JOSHUA_SLACK_USER_ID_ENV,
            ticket_id,
        )
        return
    text = _format_confirmation_text(
        ticket_id=ticket_id,
        subject=subject,
        pattern_matched=pattern_matched,
    )
    await _post_dm(
        slack_client=slack_client, channel_id=channel_id, text=text
    )


async def dm_failure(
    *,
    slack_client: Any,
    subject: str,
    pattern_matched: str,
    reason: str,
) -> None:
    """Post the failure DM to ``KORA_SLACK_JOSHUA_USER_ID``."""
    channel_id = os.environ.get(JOSHUA_SLACK_USER_ID_ENV, "").strip()
    if not channel_id:
        return
    text = _format_failure_text(
        subject=subject, pattern_matched=pattern_matched, reason=reason
    )
    await _post_dm(
        slack_client=slack_client, channel_id=channel_id, text=text
    )


# ---------------------------------------------------------------------------
# Orchestrator — called from EmailInboundHandler
# ---------------------------------------------------------------------------


def _is_dry_run() -> bool:
    raw = os.environ.get(DRY_RUN_ENV, "").strip().lower()
    return raw in {"true", "1", "yes", "on"}


def _resolve_slack_client() -> Optional[Any]:
    """Lazy import + read the daemon's live slack client singleton.
    Returns ``None`` when the listener isn't wired (caller treats
    as "DM-best-effort skipped"; the audit row still emits)."""
    try:
        from kora_cli.listeners.slack_client_listener import (
            current_slack_client,
        )
    except Exception:
        return None
    return current_slack_client()


def _email_caller_session_id(message_id: str) -> str:
    """Stable correlation key for audit ↔ future cockpit-panel xref.
    Matches the engine-side derivation for the ``email`` source
    (``f"email:{message_id}"``).
    """
    return f"email:{message_id or 'unknown'}"


async def process_email_intent(
    *,
    message_id: str,
    subject: str,
    body_text: str,
    sender: str,
    slack_client: Optional[Any] = None,
) -> Dict[str, Any]:
    """Top-level orchestrator. Called by EmailInboundHandler after
    inbound parsing + identity filters confirm a Joshua-authored
    message.

    Args:
      message_id / subject / body_text / sender: projected from
        :class:`ParsedIncomingEmail`. The handler does the
        projection so this module doesn't import the Pydantic
        type (lets test fixtures pass plain strings).
      slack_client: override for tests; production passes
        ``None`` and we resolve via
        :func:`current_slack_client`.

    Returns a small dict summarizing what happened (for the
    handler's structured log + test inspection):
      {
        "action": "created" | "dry_run" | "logged_only" |
                   "cap_exceeded" | "failed" | "no_action",
        "intent": EmailIntent,
        "ticket_id": str | None,
        "error": str | None,
      }

    Never raises — every failure path is captured as
    ``action="failed"`` so the email handler's HANDLED_RECEIVED
    return is never disturbed.
    """
    try:
        return await _process_email_intent_inner(
            message_id=message_id,
            subject=subject,
            body_text=body_text,
            sender=sender,
            slack_client=slack_client,
        )
    except Exception as exc:
        logger.exception(
            "[kora.intent.email_to_sea_ticket] orchestrator raised %r "
            "for message_id=%s — swallowed (email handler keeps "
            "HANDLED_RECEIVED)",
            exc,
            message_id,
        )
        return {
            "action": "failed",
            "intent": None,
            "ticket_id": None,
            "error": repr(exc),
        }


async def _process_email_intent_inner(
    *,
    message_id: str,
    subject: str,
    body_text: str,
    sender: str,
    slack_client: Optional[Any],
) -> Dict[str, Any]:
    intent = recognize_intent(
        subject=subject, body=body_text, sender=sender
    )
    caller_session_id = _email_caller_session_id(message_id)
    floor = (
        os.environ.get(MIN_CONFIDENCE_ENV, "").strip().lower()
        or DEFAULT_MIN_CONFIDENCE
    )

    # Branch 1: unrecognized → record for future promotion-loop
    # training data + return. Always audited so operator can
    # backfill a follow-up bucket that classifies the un-acted-on
    # emails.
    if intent.confidence == "unrecognized":
        _safe_audit(
            details={
                "action": "logged_only",
                "pattern_matched": intent.pattern_matched,
                "confidence": intent.confidence,
                "subject": subject,
                "reason": "no_pattern_matched",
            },
            caller_session_id=caller_session_id,
        )
        return {
            "action": "logged_only",
            "intent": intent,
            "ticket_id": None,
            "error": None,
        }

    # Branch 2: below configured confidence floor → log only.
    if not _meets_min_confidence(intent.confidence, floor):
        _safe_audit(
            details={
                "action": "logged_only",
                "pattern_matched": intent.pattern_matched,
                "confidence": intent.confidence,
                "subject": subject,
                "reason": f"below_floor_{floor}",
            },
            caller_session_id=caller_session_id,
        )
        return {
            "action": "logged_only",
            "intent": intent,
            "ticket_id": None,
            "error": None,
        }

    # Branch 3: dry-run → audit the proposed payload, no write,
    # no DM. Dry-runs don't consume the hourly cap.
    if _is_dry_run():
        _safe_audit(
            details={
                "action": "dry_run",
                "pattern_matched": intent.pattern_matched,
                "confidence": intent.confidence,
                "subject": subject,
                "proposed_title": (intent.proposed_sea_ticket or {}).get(
                    "title"
                ),
            },
            caller_session_id=caller_session_id,
        )
        return {
            "action": "dry_run",
            "intent": intent,
            "ticket_id": None,
            "error": None,
        }

    # Branch 4: hourly cap exceeded → audit + DM the operator so
    # they know mail is queueing past the limit.
    if not _hourly_cap_allows():
        cap = _resolve_hourly_cap()
        _safe_audit(
            details={
                "action": "cap_exceeded",
                "pattern_matched": intent.pattern_matched,
                "confidence": intent.confidence,
                "subject": subject,
                "hourly_cap": cap,
            },
            caller_session_id=caller_session_id,
        )
        client = slack_client or _resolve_slack_client()
        if client is not None:
            await dm_failure(
                slack_client=client,
                subject=subject,
                pattern_matched=intent.pattern_matched,
                reason=(
                    f"hourly cap of {cap} Sea_Tickets/hour reached; "
                    f"raise {HOURLY_CAP_ENV} or wait"
                ),
            )
        return {
            "action": "cap_exceeded",
            "intent": intent,
            "ticket_id": None,
            "error": "hourly_cap_exceeded",
        }

    # Branch 5: write + confirm. Sea_Ticket-write failure is
    # captured + DMed to operator; doesn't propagate.
    client = slack_client or _resolve_slack_client()
    try:
        ticket_id = await write_sea_ticket_from_intent(intent)
    except Exception as exc:
        logger.error(
            "[kora.intent.email_to_sea_ticket.create_failed] "
            "message_id=%s pattern=%s err=%r",
            message_id,
            intent.pattern_matched,
            exc,
        )
        _safe_audit(
            details={
                "action": "failed",
                "pattern_matched": intent.pattern_matched,
                "confidence": intent.confidence,
                "subject": subject,
                "error": repr(exc),
            },
            caller_session_id=caller_session_id,
        )
        if client is not None:
            await dm_failure(
                slack_client=client,
                subject=subject,
                pattern_matched=intent.pattern_matched,
                reason=str(exc),
            )
        return {
            "action": "failed",
            "intent": intent,
            "ticket_id": None,
            "error": repr(exc),
        }

    # Cap-tracking deque records the successful create only.
    _record_create()

    _safe_audit(
        details={
            "action": "created",
            "pattern_matched": intent.pattern_matched,
            "confidence": intent.confidence,
            "subject": subject,
            "ticket_id": ticket_id,
            "tags": list(intent.tags),
        },
        caller_session_id=caller_session_id,
    )

    if client is not None:
        await confirm_via_dm(
            slack_client=client,
            ticket_id=ticket_id,
            subject=subject,
            pattern_matched=intent.pattern_matched,
        )
    else:
        logger.warning(
            "[kora.intent.email_to_sea_ticket] slack client unavailable "
            "— Sea_Ticket #%s created but confirmation DM skipped",
            ticket_id,
        )

    return {
        "action": "created",
        "intent": intent,
        "ticket_id": ticket_id,
        "error": None,
    }


def _safe_audit(
    *, details: Dict[str, Any], caller_session_id: Optional[str]
) -> None:
    """Wrap :func:`emit_audit` so an audit-write failure can't
    blow up the orchestrator path. Mirrors the dual-write posture
    in jsonl_sink: best-effort, log on miss, never raise.
    """
    try:
        emit_audit(
            "intent.email_to_sea_ticket",
            details,
            caller_session_id=caller_session_id,
            source="email",
        )
    except Exception as exc:
        logger.warning(
            "[kora.intent.email_to_sea_ticket.audit_failed] %r — "
            "orchestrator continues",
            exc,
        )
