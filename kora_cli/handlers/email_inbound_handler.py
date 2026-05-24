"""Email-inbound handler — KR-FEAT-EMAIL-INBOUND-IMAP ST2.

Called from :func:`kora_cli.listeners.email_inbound_imap_listener.run_poll_cycle`
once per :class:`ParsedIncomingEmail` returned by the IMAP poll.
Mirrors :class:`SlackDMHandler`'s shape: filter precedence, JSONL
append, structured-log emit on identified Joshua mail.

# Filter precedence (5 steps, ordered per bucket §2.ST2.(a))

  1. State gate — :class:`PrimaryState` ``PAUSED`` / ``STOPPED``
     → drop. Mirrors the Slack DM state gate.
  2. Sender allowlist — ``KORA_EMAIL_SENDER_ALLOWLIST`` env, comma-
     separated. **Empty default = fail-CLOSED DENY ALL** (defense
     against accidental wide-open inbound).
  3. Recipient filter — ``KORA_EMAIL_KORA_ADDRESS`` env. The
     parsed ``to`` list must include this address (case-insensitive)
     or the message is dropped.
  4. Spoofing check — compares the parsed ``From:`` header against
     the envelope sender. IMAP doesn't expose envelope sender for
     already-delivered messages; we flag this with
     ``spoofing_check_skipped: true`` in the JSONL + proceed.
     Defense-in-depth — the allowlist + identity-check gates above
     are the real authentication, this is just an audit hook.
  5. Identity check — sender must match ``KORA_EMAIL_JOSHUA_ADDRESS``
     env (case-insensitive). Non-Joshua messages drop silently.

Each filter writes a JSONL entry. The handler returns a
:class:`HandlerResult` indicating whether the listener should mark
the IMAP message SEEN (success-path statuses) or keep it UNSEEN
for next-poll retry (handler errors only).

# JSONL schema (``email_inbound_log.jsonl``)

Per-line fields (all required unless marked optional):

  - ``received_at`` (ISO 8601 UTC)
  - ``message_id``
  - ``from``
  - ``to`` (list[str])
  - ``subject``
  - ``body_text_truncated_2k`` (max 2048 chars)
  - ``has_html`` (bool)
  - ``attachments_count`` (int)
  - ``handled_status`` (one of the ``HANDLED_*`` constants below)
  - ``spoofing_check_skipped`` (bool) — true when envelope sender
    is unavailable so the check ran on the header alone
  - ``imap_uid`` (int)
  - ``error`` (optional; populated on ``handler_error``)
  - ``extra`` (optional dict; per-filter diagnostic data)

Chain event ``[kora.email_inbound.received]`` is emitted ONLY on
identified Joshua mail (status ``"received"``). Filtered events
log per-filter but don't emit the structured chain event.

# No auto-reply (Lock R3-8 (a), KR-EMAIL-AUTOREPLY-BRANCH-REMOVAL)

The handler does NOT draft and send AI replies to inbound senders.
The previous ``KORA_EMAIL_AUTO_REPLY`` opt-in branch — which called
the reasoning engine on every identified Joshua mail and sent the
result back via ``PurelymailClient.send_email`` — was cut per
operator Lock R3-8 (a) during the R3 walkthrough.

What stays:
  * Inbound parsing + the 5-filter precedence + JSONL emission
  * IMAP listener polling (separate module)
  * Outbound send pathways for Kora-originated artifacts (PDFs,
    reports — these never lived in the handler; they're driven
    from other modules using ``PurelymailClient`` directly)
  * The email-thread context loader (``kora_cli/reasoning/context_loader.py``)
    which other features may use to read prior email threads

What's gone:
  * ``KORA_EMAIL_AUTO_REPLY`` env (legacy operator settings are
    ignored cleanly; no error if the env is still present in Doppler)
  * The reasoning-engine invocation from the inbound path
  * The ``email_inbound`` cost-telemetry route was already reserved
    by PR #161 (``cost_telemetry.ROUTE_EMAIL_INBOUND``); it stays
    reserved for the future KR-EMAIL-COST-BILL wiring when
    KR-INTENT-EMAIL-TO-SEA-TICKET ships a real consumer

# Security contract

  - IMAP password / SMTP password / bot tokens NEVER in JSONL
    (test pinned across diverse failure modes).
  - Body text logged truncated to 2KB — operator pulls full body
    from Purelymail webmail when needed.
  - Spoofing-check absence is recorded as a flag, not a silent
    bypass.

# Exception posture

Any uncaught exception during ``handle_event`` returns
``HandlerResult(status=handler_error, should_mark_seen=False)`` so
the listener keeps the IMAP message UNSEEN for next-poll retry. A
separate JSONL entry records the failure for operator triage.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from kora_cli.clients.purelymail_types import ParsedIncomingEmail

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env vars + defaults
# ---------------------------------------------------------------------------


SENDER_ALLOWLIST_ENV = "KORA_EMAIL_SENDER_ALLOWLIST"
KORA_ADDRESS_ENV = "KORA_EMAIL_KORA_ADDRESS"
JOSHUA_ADDRESS_ENV = "KORA_EMAIL_JOSHUA_ADDRESS"
LOG_PATH_ENV = "KORA_EMAIL_INBOUND_LOG_PATH"  # test override

BODY_TRUNCATE_LIMIT = 2048

# Handled-status enum for the JSONL ``handled_status`` field.
HANDLED_RECEIVED = "received"
HANDLED_FILTERED_PAUSED = "filtered_paused"
HANDLED_FILTERED_STOPPED = "filtered_stopped"
HANDLED_FILTERED_NON_ALLOWLIST = "filtered_non_allowlist"
HANDLED_FILTERED_WRONG_RECIPIENT = "filtered_wrong_recipient"
HANDLED_FILTERED_SPOOFING = "filtered_spoofing"
HANDLED_FILTERED_NON_JOSHUA = "filtered_non_joshua"
HANDLED_HANDLER_ERROR = "handler_error"

# Success-path statuses get marked SEEN; handler_error keeps UNSEEN
# for next-poll retry. Filter-drops are SEEN (they're terminal for
# this UID — re-fetching the same drop wastes Purelymail's quota).
_MARK_SEEN_STATUSES = frozenset(
    {
        HANDLED_RECEIVED,
        HANDLED_FILTERED_PAUSED,
        HANDLED_FILTERED_STOPPED,
        HANDLED_FILTERED_NON_ALLOWLIST,
        HANDLED_FILTERED_WRONG_RECIPIENT,
        HANDLED_FILTERED_SPOOFING,
        HANDLED_FILTERED_NON_JOSHUA,
    }
)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HandlerResult:
    """One handler-call outcome.

    Attributes:
      status: One of the ``HANDLED_*`` constants.
      should_mark_seen: Listener should call
        :meth:`PurelymailIMAPClient.mark_seen` for this UID iff true.
        Always false on ``handler_error`` so the message survives
        for next-poll retry.
    """

    status: str
    should_mark_seen: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_log_path() -> Path:
    """Return the inbound JSONL path: env override → ``KORA_HOME/email_inbound_log.jsonl``."""
    override = os.environ.get(LOG_PATH_ENV, "").strip()
    if override:
        return Path(override)
    from kora_constants import get_kora_home

    return get_kora_home() / "email_inbound_log.jsonl"


def _read_allowlist() -> Optional[set[str]]:
    """Return the lowercased sender allowlist set, or ``None`` if env unset.

    ``None`` is the fail-CLOSED signal — the handler treats it as
    "deny all" rather than "allow all".
    """
    raw = os.environ.get(SENDER_ALLOWLIST_ENV, "").strip()
    if not raw:
        return None
    parsed = {part.strip().lower() for part in raw.split(",") if part.strip()}
    return parsed or None


def _env_address(name: str) -> Optional[str]:
    """Read an address env, lowercased + stripped, ``None`` if unset."""
    raw = os.environ.get(name, "").strip().lower()
    return raw or None


def _truncate_body(text: str) -> str:
    if not text:
        return ""
    if len(text) <= BODY_TRUNCATE_LIMIT:
        return text
    return text[:BODY_TRUNCATE_LIMIT]


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class EmailInboundHandler:
    """Process one :class:`ParsedIncomingEmail` through the 5-step filter.

    Stateless across calls — the daemon constructs one instance at
    listener wire-up and reuses it. Per-message state is purely
    request-scoped; persistent state is the JSONL log on disk.
    """

    def __init__(self, log_path: Optional[Path] = None) -> None:
        """Construct the handler.

        Args:
          log_path: Override the JSONL log path; tests inject tmp_path.
        """
        self._log_path = log_path or _resolve_log_path()

    async def handle_event(
        self, parsed: ParsedIncomingEmail
    ) -> HandlerResult:
        """Run the 5-step filter; write JSONL entry.

        Returns a :class:`HandlerResult` the listener inspects to
        decide ``mark_seen`` per-message. Exceptions are caught at
        the boundary so the listener always gets a result back —
        ``handler_error`` keeps the message UNSEEN for next-poll
        retry.
        """
        try:
            return await self._handle_event_inner(parsed)
        except Exception as exc:
            logger.warning(
                "[kora.email_inbound] handler raised %r for uid=%d — "
                "keeping UNSEEN for next-poll retry",
                exc,
                parsed.imap_uid,
            )
            try:
                self._append_log_entry(
                    parsed,
                    HANDLED_HANDLER_ERROR,
                    spoofing_check_skipped=True,
                    error=repr(exc),
                )
            except Exception as inner:
                logger.warning(
                    "[kora.email_inbound] failed to log handler_error: %r",
                    inner,
                )
            return HandlerResult(
                status=HANDLED_HANDLER_ERROR,
                should_mark_seen=False,
            )

    async def _handle_event_inner(
        self, parsed: ParsedIncomingEmail
    ) -> HandlerResult:
        # Filter 1: state gate
        gate_status = self._check_state_gate()
        if gate_status is not None:
            self._append_log_entry(
                parsed, gate_status, spoofing_check_skipped=True
            )
            logger.info(
                "[kora.email_inbound] %s — uid=%d dropped",
                gate_status,
                parsed.imap_uid,
            )
            return HandlerResult(
                status=gate_status,
                should_mark_seen=gate_status in _MARK_SEEN_STATUSES,
            )

        # Filter 2: sender allowlist
        allowlist = _read_allowlist()
        sender = parsed.from_address.strip().lower()
        if allowlist is None:
            logger.warning(
                "[kora.email_inbound] %s unset — all inbound dropped "
                "(fail-CLOSED DENY ALL)",
                SENDER_ALLOWLIST_ENV,
            )
            self._append_log_entry(
                parsed,
                HANDLED_FILTERED_NON_ALLOWLIST,
                spoofing_check_skipped=True,
                extra={"reason": "allowlist_env_unset"},
            )
            return HandlerResult(
                status=HANDLED_FILTERED_NON_ALLOWLIST,
                should_mark_seen=True,
            )
        if sender not in allowlist:
            self._append_log_entry(
                parsed,
                HANDLED_FILTERED_NON_ALLOWLIST,
                spoofing_check_skipped=True,
                extra={"actual_sender": sender},
            )
            return HandlerResult(
                status=HANDLED_FILTERED_NON_ALLOWLIST,
                should_mark_seen=True,
            )

        # Filter 3: recipient filter
        kora_address = _env_address(KORA_ADDRESS_ENV)
        recipients_lower = [r.strip().lower() for r in parsed.to]
        if kora_address is None or kora_address not in recipients_lower:
            self._append_log_entry(
                parsed,
                HANDLED_FILTERED_WRONG_RECIPIENT,
                spoofing_check_skipped=True,
                extra={
                    "expected_recipient": kora_address,
                    "actual_recipients": recipients_lower,
                },
            )
            return HandlerResult(
                status=HANDLED_FILTERED_WRONG_RECIPIENT,
                should_mark_seen=True,
            )

        # Filter 4: spoofing check
        # IMAP-delivered messages don't expose envelope sender; we
        # always log spoofing_check_skipped=true. The header-only
        # check below is defense-in-depth — it WOULD reject obviously
        # malformed From: headers if an envelope hint were available.
        # Currently a no-op skip — kept as an extension point.
        spoofing_skipped = True

        # Filter 5: identity check — sender must be Joshua
        joshua_address = _env_address(JOSHUA_ADDRESS_ENV)
        if joshua_address is None:
            # Misconfigured — fail-CLOSED. Without the Joshua address
            # set we can't verify identity even if the sender is on
            # the allowlist.
            logger.warning(
                "[kora.email_inbound] %s unset — dropping (fail-CLOSED)",
                JOSHUA_ADDRESS_ENV,
            )
            self._append_log_entry(
                parsed,
                HANDLED_FILTERED_NON_JOSHUA,
                spoofing_check_skipped=spoofing_skipped,
                extra={"reason": "joshua_address_env_unset"},
            )
            return HandlerResult(
                status=HANDLED_FILTERED_NON_JOSHUA,
                should_mark_seen=True,
            )
        if sender != joshua_address:
            self._append_log_entry(
                parsed,
                HANDLED_FILTERED_NON_JOSHUA,
                spoofing_check_skipped=spoofing_skipped,
                extra={"actual_sender": sender},
            )
            return HandlerResult(
                status=HANDLED_FILTERED_NON_JOSHUA,
                should_mark_seen=True,
            )

        # All filters passed — Joshua mail received. Log + emit the
        # chain event. No reply sent (Lock R3-8 (a)); future
        # consumers (KR-INTENT-EMAIL-TO-SEA-TICKET) read from the
        # JSONL.
        self._append_log_entry(
            parsed,
            HANDLED_RECEIVED,
            spoofing_check_skipped=spoofing_skipped,
        )
        self._emit_received_event(parsed)

        return HandlerResult(
            status=HANDLED_RECEIVED,
            should_mark_seen=True,
        )

    # ------------------------------------------------------------------
    # State gate
    # ------------------------------------------------------------------

    def _check_state_gate(self) -> Optional[str]:
        """Return a handled_status if the operational state should
        drop this message; otherwise None.

        Mirrors :meth:`SlackDMHandler._check_state_gate`. The
        ``PrimaryState`` import is lazy so this handler stays
        importable in test paths without the agent module on the
        path.
        """
        try:
            from agent.operational_state import PrimaryState
            from agent.operational_state_holder import get_holder
        except Exception:
            return None

        holder = get_holder()
        if holder is None:
            return None

        state = holder.current
        ps = state.primary_state

        if ps is PrimaryState.PAUSED:
            return HANDLED_FILTERED_PAUSED
        if ps is PrimaryState.STOPPED:
            return HANDLED_FILTERED_STOPPED
        return None

    # ------------------------------------------------------------------
    # JSONL append
    # ------------------------------------------------------------------

    def _append_log_entry(
        self,
        parsed: ParsedIncomingEmail,
        handled_status: str,
        *,
        spoofing_check_skipped: bool,
        extra: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        """Append one JSONL entry. Best-effort; OSError logged + swallowed.

        Body is truncated to ``BODY_TRUNCATE_LIMIT`` chars per spec
        (operator pulls full body from webmail). No password / token
        fields ever appear — this entry's schema is fixed.
        """
        entry: Dict[str, Any] = {
            "received_at": _now_iso(),
            "message_id": parsed.message_id,
            "from": parsed.from_address,
            "to": list(parsed.to),
            "subject": parsed.subject,
            "body_text_truncated_2k": _truncate_body(parsed.body_text),
            "has_html": parsed.has_html,
            "attachments_count": len(parsed.attachments),
            "handled_status": handled_status,
            "spoofing_check_skipped": spoofing_check_skipped,
            "imap_uid": parsed.imap_uid,
        }
        # in_reply_to is needed for context-loader threading. Not
        # part of the bucket spec's enumerated fields but additive
        # + backwards-compat (consumers handle absence).
        entry["in_reply_to"] = None

        if extra:
            entry["extra"] = extra
        if error:
            entry["error"] = error

        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError as exc:
            logger.warning(
                "[kora.email_inbound] log write failed (%s): %r",
                self._log_path,
                exc,
            )

    def _emit_received_event(self, parsed: ParsedIncomingEmail) -> None:
        """Stable structured-log emit for an identified Joshua email.

        ``kora.email_inbound.received`` is the chain-event vocab
        literal; if/when substrate ships the CHECK-constraint
        addition the emit can extend to
        ``IsoKronMCPClient.invoke("kora__append_event", ...)``.
        """
        logger.info(
            "[kora.email_inbound.received] uid=%d from=%s subject=%r "
            "body_len=%d has_html=%s attachments=%d",
            parsed.imap_uid,
            parsed.from_address,
            parsed.subject,
            len(parsed.body_text),
            parsed.has_html,
            len(parsed.attachments),
        )
