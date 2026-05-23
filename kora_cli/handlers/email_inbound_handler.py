"""Email-inbound handler — KR-FEAT-EMAIL-INBOUND-IMAP ST2.

Called from :func:`kora_cli.listeners.email_inbound_imap_listener.run_poll_cycle`
once per :class:`ParsedIncomingEmail` returned by the IMAP poll.
Mirrors :class:`SlackDMHandler`'s shape: filter precedence, JSONL
append, structured-log emit on identified Joshua mail, optional
reasoning-driven outbound reply.

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

# AUTO_REPLY (KORA_EMAIL_AUTO_REPLY)

Default OFF. When ``true`` / ``1`` / ``yes``, an identified Joshua
email triggers:

  1. Load conversation context from the inbound + outbound JSONL
     filtered to the same In-Reply-To chain.
  2. Build :class:`IncomingMessage` (source ``"email"``).
  3. Call ``current_reasoning_engine().respond(message, context)``.
  4. Send the result via the daemon-singleton ``PurelymailClient``
     with the inbound's ``Message-ID`` threaded as ``In-Reply-To``
     and subject prefixed ``Re:``.
  5. Engine unavailable / engine error → canned fallback text +
     same send path.

The reply is NEVER attempted on filtered messages — only on
``handled_status == "received"`` with ``AUTO_REPLY`` enabled. A
send failure does NOT change the inbound's handled_status; we
still mark the inbound SEEN (operator triages via the outbound
JSONL's failed entry).

# Security contract

  - IMAP password / SMTP password / bot tokens NEVER in JSONL
    (test pinned across diverse failure modes).
  - Body text logged truncated to 2KB — operator pulls full body
    from Purelymail webmail when needed.
  - Spoofing-check absence is recorded as a flag, not a silent
    bypass.

# Exception posture

Any uncaught exception during ``handle_event`` returns
``HandlerResult(status=handler_error, should_mark_seen=False, ...)``
so the listener keeps the IMAP message UNSEEN for next-poll
retry. A separate JSONL entry records the failure for operator
triage.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from kora_cli.clients.purelymail_types import ParsedIncomingEmail

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env vars + defaults
# ---------------------------------------------------------------------------


SENDER_ALLOWLIST_ENV = "KORA_EMAIL_SENDER_ALLOWLIST"
KORA_ADDRESS_ENV = "KORA_EMAIL_KORA_ADDRESS"
JOSHUA_ADDRESS_ENV = "KORA_EMAIL_JOSHUA_ADDRESS"
AUTO_REPLY_ENV = "KORA_EMAIL_AUTO_REPLY"
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

# Canned reply when AUTO_REPLY engaged but the reasoning engine
# can't produce a response. Mirrors SlackDMHandler._CANNED_FALLBACK_TEXT.
CANNED_FALLBACK_TEXT = (
    "Kora is currently unable to respond by email; operator notified."
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
      should_reply: Convenience flag — true iff ``AUTO_REPLY`` is
        enabled AND status is ``received``. The listener itself
        doesn't act on this (the handler drives the send inline);
        the field is surfaced so tests + operator triage can read
        the decision.
    """

    status: str
    should_mark_seen: bool
    should_reply: bool


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


def _auto_reply_enabled() -> bool:
    raw = os.environ.get(AUTO_REPLY_ENV, "").strip().lower()
    return raw in {"true", "1", "yes", "on"}


# ---------------------------------------------------------------------------
# Reasoning-meta helpers (KR-EMAIL-OUTBOUND-REASONING-META)
# ---------------------------------------------------------------------------


def _empty_reasoning_meta(
    *, reasoning_error: Optional[str] = None
) -> Dict[str, Any]:
    """Build a meta dict for paths where the engine didn't run (or
    couldn't be reached). All SDK-side fields are ``None``; only
    ``reasoning_error`` carries a stable code if supplied."""
    return {
        "model_used": None,
        "input_tokens": None,
        "output_tokens": None,
        "reasoning_duration_ms": None,
        "reasoning_error": reasoning_error,
    }


def _reasoning_meta_from_result(result: Any) -> Dict[str, Any]:
    """Project a ResponseResult into the 5-key meta dict.

    Tolerates partial / missing attributes via ``getattr`` so a
    test-double minimal MagicMock still produces a well-shaped
    meta. ``result.error`` is included verbatim — caller decides
    whether to surface it (success path) or override it
    (empty-text path).
    """
    return {
        "model_used": getattr(result, "model_used", None) or None,
        "input_tokens": getattr(result, "input_tokens", None),
        "output_tokens": getattr(result, "output_tokens", None),
        "reasoning_duration_ms": getattr(result, "reasoning_duration_ms", None),
        "reasoning_error": getattr(result, "error", None),
    }


def _email_caller_session_id(message_id: str) -> str:
    """Deterministic correlation key for the audit ↔ outbound JSONL
    xref. Must match the reasoning engine's own derivation in
    :func:`kora_cli.reasoning.anthropic_engine._derive_caller_session_id`
    for the ``email`` source: ``f"email:{message_id}"`` (with
    ``"unknown"`` fallback when the id is empty)."""
    return f"email:{message_id or 'unknown'}"


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class EmailInboundHandler:
    """Process one :class:`ParsedIncomingEmail` through the 5-step filter.

    Stateless across calls — the daemon constructs one instance at
    listener wire-up and reuses it. Per-message state is purely
    request-scoped; persistent state is the JSONL log on disk.
    """

    def __init__(
        self,
        log_path: Optional[Path] = None,
        purelymail_client: Optional[Any] = None,
        reasoning_engine: Optional[Any] = None,
    ) -> None:
        """Construct the handler.

        Args:
          log_path: Override the JSONL log path; tests inject tmp_path.
          purelymail_client: Override the outbound :class:`PurelymailClient`;
            production leaves ``None`` and the handler resolves
            :func:`current_purelymail_client` at reply-time.
          reasoning_engine: Override the reasoning engine; production
            leaves ``None`` and the handler resolves
            :func:`current_reasoning_engine`.
        """
        self._log_path = log_path or _resolve_log_path()
        self._purelymail_client = purelymail_client
        self._reasoning_engine = reasoning_engine

    async def handle_event(
        self, parsed: ParsedIncomingEmail
    ) -> HandlerResult:
        """Run the 5-step filter; write JSONL + optionally reply.

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
                should_reply=False,
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
                should_reply=False,
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
                should_reply=False,
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
                should_reply=False,
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
                should_reply=False,
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
                should_reply=False,
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
                should_reply=False,
            )

        # All filters passed — Joshua mail received.
        self._append_log_entry(
            parsed,
            HANDLED_RECEIVED,
            spoofing_check_skipped=spoofing_skipped,
        )
        self._emit_received_event(parsed)

        # AUTO_REPLY (opt-in, default OFF).
        auto_reply = _auto_reply_enabled()
        if auto_reply:
            try:
                await self._send_auto_reply(parsed)
            except Exception as exc:
                # AUTO_REPLY failures don't change the inbound result —
                # the outbound JSONL records the send failure; we still
                # mark the inbound SEEN so we don't re-process.
                logger.warning(
                    "[kora.email_inbound] AUTO_REPLY raised %r for uid=%d "
                    "— inbound stays handled_status=received",
                    exc,
                    parsed.imap_uid,
                )

        return HandlerResult(
            status=HANDLED_RECEIVED,
            should_mark_seen=True,
            should_reply=auto_reply,
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

    # ------------------------------------------------------------------
    # AUTO_REPLY (env-gated)
    # ------------------------------------------------------------------

    async def _send_auto_reply(self, parsed: ParsedIncomingEmail) -> None:
        """Reasoning-driven reply path. Build context → call engine →
        send via PurelymailClient.

        Engine unavailable / engine error → canned fallback text +
        send anyway. Send failures DO NOT propagate beyond a WARN
        log — outbound JSONL captures the SendResult separately.

        KR-EMAIL-OUTBOUND-REASONING-META: ``_call_reasoning_engine``
        now returns the full reasoning-meta dict (model_used /
        tokens / duration / error) alongside the reply text; the
        meta + a deterministic ``caller_session_id`` get threaded
        through to ``client.send_email`` so the outbound JSONL row
        carries the same correlation key the reasoning audit emit
        already uses. The reasoning-panel email-xref consumes both
        sides via this key.
        """
        engine = self._resolve_reasoning_engine()
        reply_text, reasoning_meta = await self._build_reply_and_meta(
            engine=engine, parsed=parsed
        )

        client = self._resolve_purelymail_client()
        if client is None:
            logger.warning(
                "[kora.email_inbound.reply_failed] reason=purelymail_unavailable "
                "uid=%d reasoning_error=%s",
                parsed.imap_uid,
                reasoning_meta.get("reasoning_error"),
            )
            return

        from_addr = _env_address(KORA_ADDRESS_ENV)
        if from_addr is None:
            # We already gated on this in filter 3 — but defense in
            # depth in case env was unset between the inbound check
            # + this outbound build.
            logger.warning(
                "[kora.email_inbound.reply_failed] reason=kora_address_env_unset "
                "uid=%d",
                parsed.imap_uid,
            )
            return

        subject = parsed.subject or "(no subject)"
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        # Deterministic correlation key — must match the engine's own
        # _derive_caller_session_id for ``email`` source
        # (anthropic_engine.py:869-871: ``f"email:{message_id}"``).
        # Keeping both sides on the same literal string lets the
        # KR-REASONING-PANEL-EMAIL-XREF bucket join audit ↔ outbound
        # JSONL rows by a single field.
        caller_session_id = _email_caller_session_id(parsed.message_id)

        try:
            await client.send_email(
                from_addr=from_addr,
                to=[parsed.from_address],
                subject=subject,
                body_text=reply_text,
                in_reply_to=parsed.message_id,
                caller_session_id=caller_session_id,
                **reasoning_meta,
            )
        except Exception as exc:
            logger.warning(
                "[kora.email_inbound.reply_failed] uid=%d send raised %r — "
                "outbound JSONL records the SendResult",
                parsed.imap_uid,
                exc,
            )

    async def _build_reply_and_meta(
        self, *, engine: Optional[Any], parsed: ParsedIncomingEmail
    ) -> tuple[str, Dict[str, Any]]:
        """Resolve reply text + reasoning-meta dict.

        Three paths:
          - Engine unavailable: canned text + meta with
            ``reasoning_error="engine_unavailable"`` and all other
            fields ``None``.
          - Engine call: delegate to :meth:`_call_reasoning_engine`,
            which returns the full meta dict (model_used / tokens /
            duration / error or fallback-error).
        """
        if engine is None:
            logger.warning(
                "[kora.email_inbound.reasoning_skipped] reason=engine_unavailable "
                "uid=%d",
                parsed.imap_uid,
            )
            return (
                CANNED_FALLBACK_TEXT,
                _empty_reasoning_meta(reasoning_error="engine_unavailable"),
            )
        return await self._call_reasoning_engine(
            engine=engine, parsed=parsed
        )

    def _resolve_reasoning_engine(self) -> Optional[Any]:
        if self._reasoning_engine is not None:
            return self._reasoning_engine
        try:
            from kora_cli.listeners.reasoning_engine_listener import (
                current_reasoning_engine,
            )
        except Exception:
            return None
        return current_reasoning_engine()

    def _resolve_purelymail_client(self) -> Optional[Any]:
        if self._purelymail_client is not None:
            return self._purelymail_client
        try:
            from kora_cli.listeners.purelymail_client_listener import (
                current_purelymail_client,
            )
        except Exception:
            return None
        return current_purelymail_client()

    async def _call_reasoning_engine(
        self, *, engine: Any, parsed: ParsedIncomingEmail
    ) -> tuple[str, Dict[str, Any]]:
        """Call engine.respond + return ``(reply_text, reasoning_meta)``.

        ``reasoning_meta`` is a dict with the 5 fields PurelymailClient's
        outbound log mirrors from the slack_dm post-#131 shape:

          - ``model_used`` (str | None)
          - ``input_tokens`` (int | None)
          - ``output_tokens`` (int | None)
          - ``reasoning_duration_ms`` (int | None)
          - ``reasoning_error`` (str | None)

        On engine error / exception / empty-text → reply_text is
        the canned fallback + meta carries the error code; the
        SDK-side fields are best-effort (populated from result when
        available, otherwise None).
        """
        from kora_cli.reasoning.context_loader import load_email_context
        from kora_cli.reasoning.engine import (
            ConversationContext,
            IncomingMessage,
        )

        try:
            context = load_email_context(
                message_id=parsed.message_id,
                in_reply_to=None,
            )
        except Exception as exc:
            logger.warning(
                "[kora.email_inbound.reasoning_skipped] context-load "
                "failed uid=%d: %r — using empty context",
                parsed.imap_uid,
                exc,
            )
            context = ConversationContext()

        message = IncomingMessage(
            text=parsed.body_text,
            source="email",
            received_at=parsed.received_at,
            metadata={
                "from": parsed.from_address,
                "subject": parsed.subject,
                "message_id": parsed.message_id,
                "in_reply_to": parsed.message_id,
            },
        )

        try:
            result = await engine.respond(message, context)
        except Exception as exc:
            logger.warning(
                "[kora.email_inbound.reasoning_skipped] engine.respond "
                "raised %r uid=%d — canned fallback",
                exc,
                parsed.imap_uid,
            )
            return (
                CANNED_FALLBACK_TEXT,
                _empty_reasoning_meta(
                    reasoning_error=f"engine_exception:{type(exc).__name__}",
                ),
            )

        meta = _reasoning_meta_from_result(result)

        if result.error is not None:
            logger.warning(
                "[kora.email_inbound.reasoning_failed] error=%s uid=%d",
                result.error,
                parsed.imap_uid,
            )
            return (CANNED_FALLBACK_TEXT, meta)

        if not (result.text or "").strip():
            logger.warning(
                "[kora.email_inbound.reasoning_failed] empty text on "
                "success uid=%d — canned fallback",
                parsed.imap_uid,
            )
            # Preserve SDK-side meta from the result (model + tokens
            # ran, just produced empty text) but mark the error code
            # so the panel can distinguish from a happy-path send.
            meta["reasoning_error"] = "empty_response_text"
            return (CANNED_FALLBACK_TEXT, meta)

        return (result.text, meta)
