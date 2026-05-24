"""Slack-DM handler — KR-FEAT-SLACK-DM ST1.

Called from ``kora_cli/listeners/webhooks.py:_handle_slack`` after
HMAC verification + URL-verification handshake. Owns the Kora-side
DM-processing logic:

  - Identity check: sender must match ``KORA_SLACK_JOSHUA_USER_ID``.
    Non-Joshua messages are dropped silently (don't echo back to a
    third party).
  - Channel-type filter: only ``"im"`` events. Channel messages,
    app_mentions, etc. are filtered.
  - Bot-message filter: events with ``event.bot_id`` set are filtered
    (defense against echo-loops if Kora's own bot is ever in the
    conversation).
  - Subtype filter: only regular messages (no ``event.subtype``);
    drop message_changed / message_deleted / message_replied etc.
  - OperationalStateHolder gating: PAUSED or STOPPED → drop. Don't
    process Joshua's DM during a pause.
  - JSONL append-only persistence at ``<KORA_HOME>/slack_dm_log.jsonl``.
  - ``[kora.slack_dm.received]`` structured-log emit on Joshua DMs
    (chain-event vocab literal flagged for substrate follow-on; same
    pattern as KR-D-DAEMON ST3 webhook dead-letter + KR-MCP-RUNTIME-
    SURFACE ST2 audit log).

# Security posture

The signing secret is consumed by the HMAC verifier in the listener;
this handler NEVER sees it. JSONL entries are bounded to a fixed
allow-list of fields — body content is recorded (Joshua's own
message text is the operator-visible record by design), but no
header values, no signing secret, no bot token, no auth metadata.

A unit test asserts the JSONL does NOT contain the signing-secret
env value after a sequence of events.

# Exception posture

Any uncaught exception inside ``handle_event`` is caught at the
listener-layer wrap (see ``webhooks.py``) and logged to the
dead-letter logger; we return 200 to Slack so it doesn't retry
indefinitely. The handler's own internal failure modes (JSONL write
failure, holder unavailable, etc.) WARN-log + continue — never
crash the request, never block the 200 response.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Env vars.
JOSHUA_USER_ID_ENV = "KORA_SLACK_JOSHUA_USER_ID"
LOG_PATH_ENV = "KORA_SLACK_DM_LOG_PATH"  # test override; defaults to KORA_HOME

# Handled-status enum for the JSONL ``handled_status`` field.
HANDLED_RECEIVED = "received"
HANDLED_FILTERED_NON_JOSHUA = "filtered_non_joshua"
HANDLED_FILTERED_NON_IM = "filtered_non_im"
HANDLED_FILTERED_BOT = "filtered_bot"
HANDLED_FILTERED_SUBTYPE = "filtered_subtype"
HANDLED_DROPPED_PAUSED = "dropped_paused"
HANDLED_DROPPED_STOPPED = "dropped_stopped"
HANDLED_HANDLER_ERROR = "handler_error"


def _resolve_log_path() -> Path:
    """Return the JSONL log path: env override → ``KORA_HOME/slack_dm_log.jsonl``."""
    override = os.environ.get(LOG_PATH_ENV, "").strip()
    if override:
        return Path(override)
    # Lazy import — keeps test paths that monkeypatch LOG_PATH_ENV from
    # needing the full kora_constants resolution chain.
    from kora_constants import get_kora_home

    return get_kora_home() / "slack_dm_log.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_extract(event: Dict[str, Any], *keys: str) -> Optional[Any]:
    """Walk ``event[k1][k2]...`` defensively; return None on any miss."""
    cur: Any = event
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
        if cur is None:
            return None
    return cur


# ---------------------------------------------------------------------------
# Outbound-log free function — KR-PROBE-INVESTIGATION-DATA-COMPLETION
# ---------------------------------------------------------------------------
#
# Extracted from ``SlackDMHandler._append_outbound_log_entry`` so non-
# handler call sites (notably ``probes/wake_consumer.py``) can write
# entries into the same ``slack_dm_log.jsonl`` stream. The handler's
# instance method now delegates here. Schema + None-omit semantics
# are preserved verbatim from the prior in-class implementation —
# JSONL consumers can't tell the entry was written by a non-handler
# caller (intentional: the slack_dm panel doesn't need to branch).


def append_outbound_log_entry(
    *,
    log_path: Path,
    channel_id: str,
    thread_ts: Optional[str],
    text: str,
    slack_message_ts: Optional[str],
    send_status: str,
    failure_reason: Optional[str] = None,
    model_used: Optional[str] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    reasoning_duration_ms: Optional[int] = None,
    reasoning_error: Optional[str] = None,
    caller_actor_kind: Optional[str] = None,
    tools_used: Optional[List[str]] = None,
    cache_creation_input_tokens: Optional[int] = None,
    cache_read_input_tokens: Optional[int] = None,
    short_circuit_category: Optional[str] = None,
    short_circuit_pattern: Optional[str] = None,
    # KR-PROBE-INVESTIGATION-DATA-COMPLETION — optional correlation
    # key for non-DM call sites (probe wake consumer). Pre-existing
    # handler-driven sends leave this ``None`` (omitted from JSONL)
    # so the slack_dm panel keeps parsing legacy entries.
    caller_session_id: Optional[str] = None,
) -> None:
    """Append one outbound-side JSONL entry to ``log_path``.

    Pure I/O helper — no SlackDMHandler instance required. The
    handler's :meth:`SlackDMHandler._append_outbound_log_entry`
    delegates here so both call sites (handler reply path +
    probe wake consumer DM path) write byte-identical rows.

    See :meth:`SlackDMHandler._append_outbound_log_entry` for the
    ST2 reasoning-meta field semantics. ``caller_session_id`` is
    the KR-PROBE-INVESTIGATION-DATA-COMPLETION addition: present
    for probe-driven DMs (``"probe:{probe}:{category}"``) so the
    audit-stream join in CC#2's viewer V2 lights up; absent
    (omitted) for handler-driven DM replies.
    """
    entry: Dict[str, Any] = {
        "sent_at": _now_iso(),
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "text": text,
        "slack_message_ts": slack_message_ts,
        "send_status": send_status,
    }
    if failure_reason:
        entry["failure_reason"] = failure_reason
    if model_used is not None:
        entry["model_used"] = model_used
    if input_tokens is not None:
        entry["input_tokens"] = int(input_tokens)
    if output_tokens is not None:
        entry["output_tokens"] = int(output_tokens)
    if reasoning_duration_ms is not None:
        entry["reasoning_duration_ms"] = int(reasoning_duration_ms)
    if reasoning_error is not None:
        entry["reasoning_error"] = reasoning_error
    if caller_actor_kind is not None:
        entry["caller_actor_kind"] = caller_actor_kind
    if tools_used is not None:
        entry["tools_used"] = list(tools_used)
    if cache_creation_input_tokens is not None:
        entry["cache_creation_input_tokens"] = int(
            cache_creation_input_tokens
        )
    if cache_read_input_tokens is not None:
        entry["cache_read_input_tokens"] = int(cache_read_input_tokens)
    if short_circuit_category is not None:
        entry["short_circuit_category"] = str(short_circuit_category)
    if short_circuit_pattern is not None:
        entry["short_circuit_pattern"] = str(short_circuit_pattern)
    if caller_session_id is not None:
        entry["caller_session_id"] = str(caller_session_id)

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except OSError as exc:
        logger.warning(
            "[kora.slack_dm] outbound log write failed (%s): %r",
            log_path,
            exc,
        )


def resolve_slack_dm_log_path() -> Path:
    """Public accessor for the canonical outbound-log path so
    non-handler call sites can resolve it without monkeypatching
    a private helper. Returns the same Path that
    :class:`SlackDMHandler` would use under the same env state."""
    return _resolve_log_path()


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class SlackDMHandler:
    """Processes a single verified Slack Events payload.

    Stateless across requests — each ``handle_event`` call processes
    one event independently. Persistent state (the JSONL log) is
    file-backed; in-memory state is request-scoped.
    """

    def __init__(
        self,
        log_path: Optional[Path] = None,
        slack_client: Optional[Any] = None,
        reasoning_engine: Optional[Any] = None,
    ) -> None:
        """Construct the handler.

        Args:
          log_path: Override the JSONL log file path. Production
            callers leave this ``None``; tests inject a tmp_path.
          slack_client: KR-FEAT-SLACK-DM ST2 — inject a SlackClient
            for outbound DM replies. Production leaves ``None``;
            the handler lazy-creates a SlackClient on first reply.
          reasoning_engine: KR-FEAT-AI-RESPONSE-LOOP ST2 — inject a
            ReasoningEngine for reply-content generation.
            Production leaves ``None``; the handler resolves
            ``kora_cli.listeners.reasoning_engine_listener.current_reasoning_engine()``
            at reply-time. ``None`` from both injection + accessor
            → canned fallback (handler stays alive; Joshua isn't
            crickets).
        """
        self._log_path = log_path or _resolve_log_path()
        self._slack_client: Optional[Any] = slack_client
        self._reasoning_engine: Optional[Any] = reasoning_engine
        # KR-CHEAP-TRIVIAL-DM-SHORTCIRCUIT — phrasebook cached on
        # the instance after first load. Operator-edits picked up
        # on daemon restart (in-process reload deferred).
        self._phrasebook: Optional[List[Any]] = None

    async def handle_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Process a Slack Events payload.

        Returns the Slack-API-compliant response dict (always
        ``{"ok": True}`` — Slack uses 200 + ok as the acknowledgement;
        the daemon's response code is set in the listener layer).

        Filter order:

          1. PAUSED / STOPPED state → drop, no further processing.
          2. ``event.type`` must be ``message`` AND no subtype.
          3. ``event.channel_type`` must be ``im``.
          4. ``event.bot_id`` must be absent.
          5. ``event.user`` must match ``KORA_SLACK_JOSHUA_USER_ID``.

        Each filter writes a JSONL entry with the appropriate
        ``handled_status`` then returns ``{"ok": True}``.

        Exceptions during filter / log / emit are caught + WARN-logged
        + still return ``{"ok": True}`` — never let internal failures
        cause Slack to retry.
        """
        try:
            return await self._handle_event_inner(payload)
        except Exception as exc:
            # Last-resort guard. Log + return ok so Slack doesn't retry.
            logger.warning(
                "[kora.slack_dm] handler raised %r — returning ok to Slack",
                exc,
            )
            try:
                self._append_log_entry(
                    payload, HANDLED_HANDLER_ERROR, error=repr(exc)
                )
            except Exception as inner:
                logger.warning(
                    "[kora.slack_dm] failed to log handler error: %r", inner
                )
            return {"ok": True}

    async def _handle_event_inner(
        self, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        # Filter 1: OperationalStateHolder gating.
        gate_status = self._check_state_gate()
        if gate_status is not None:
            self._append_log_entry(payload, gate_status)
            logger.info(
                "[kora.slack_dm] %s — message dropped",
                gate_status,
            )
            return {"ok": True}

        event_type = _safe_extract(payload, "event", "type")
        subtype = _safe_extract(payload, "event", "subtype")
        channel_type = _safe_extract(payload, "event", "channel_type")
        bot_id = _safe_extract(payload, "event", "bot_id")
        user_id = _safe_extract(payload, "event", "user")

        # Filter 2: only regular messages (no subtype).
        if event_type != "message" or subtype:
            self._append_log_entry(
                payload,
                HANDLED_FILTERED_SUBTYPE,
                extra={"event_type": event_type, "subtype": subtype},
            )
            return {"ok": True}

        # Filter 3: only IM channel-type.
        if channel_type != "im":
            self._append_log_entry(
                payload,
                HANDLED_FILTERED_NON_IM,
                extra={"channel_type": channel_type},
            )
            return {"ok": True}

        # Filter 4: bot messages.
        if bot_id:
            self._append_log_entry(
                payload,
                HANDLED_FILTERED_BOT,
                extra={"bot_id": bot_id},
            )
            return {"ok": True}

        # Filter 5: identity — must be Joshua.
        expected_joshua = os.environ.get(JOSHUA_USER_ID_ENV, "").strip()
        if not expected_joshua:
            # Misconfigured — fail-CLOSED. Without the Joshua ID set,
            # we can't verify the sender, so drop everything.
            logger.warning(
                "[kora.slack_dm] %s unset — all messages dropped (fail-CLOSED)",
                JOSHUA_USER_ID_ENV,
            )
            self._append_log_entry(
                payload,
                HANDLED_FILTERED_NON_JOSHUA,
                extra={"reason": "joshua_id_env_unset"},
            )
            return {"ok": True}
        if user_id != expected_joshua:
            self._append_log_entry(
                payload,
                HANDLED_FILTERED_NON_JOSHUA,
                extra={"actual_user_id": user_id},
            )
            return {"ok": True}

        # All filters passed — Joshua DM received.
        self._append_log_entry(payload, HANDLED_RECEIVED)
        self._emit_received_event(payload)
        # ST2 — outbound echo reply. Failures DO NOT propagate; we
        # always return ok-to-Slack for the inbound, then log the
        # reply outcome separately into the outbound JSONL.
        await self._send_echo_reply(payload)
        return {"ok": True}

    # ------------------------------------------------------------------
    # Filters / helpers
    # ------------------------------------------------------------------

    def _check_state_gate(self) -> Optional[str]:
        """Return a handled_status if the operational state should
        drop this message; otherwise None."""
        try:
            from agent.operational_state import PrimaryState
            from agent.operational_state_holder import get_holder
        except Exception:
            # If the operational-state module can't even be imported,
            # we're in an unusual test path. Don't gate; let the rest
            # of the filters apply.
            return None

        holder = get_holder()
        if holder is None:
            # No holder initialized → no gating. The handler is
            # running outside the daemon (or in a partial-init test);
            # let processing proceed.
            return None

        # holder.current is a @property — caught in KR-MCP-RUNTIME-SURFACE
        # ST1 K-DG corrections.
        state = holder.current
        ps = state.primary_state

        if ps is PrimaryState.PAUSED:
            return HANDLED_DROPPED_PAUSED
        if ps is PrimaryState.STOPPED:
            return HANDLED_DROPPED_STOPPED
        return None

    def _append_log_entry(
        self,
        payload: Dict[str, Any],
        handled_status: str,
        *,
        extra: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        """Append one JSONL entry. Best-effort: any write failure is
        WARN-logged + swallowed."""
        entry: Dict[str, Any] = {
            "received_at": _now_iso(),
            "channel_id": _safe_extract(payload, "event", "channel") or "",
            "thread_ts": _safe_extract(payload, "event", "thread_ts"),
            "user_id": _safe_extract(payload, "event", "user") or "",
            "text": _safe_extract(payload, "event", "text") or "",
            "event_ts": _safe_extract(payload, "event", "ts") or "",
            "handled_status": handled_status,
        }
        if extra:
            entry["extra"] = extra
        if error:
            entry["error"] = error

        try:
            # Ensure parent dir exists (KORA_HOME may need to be created
            # in test envs). Best-effort; failure path logged.
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError as exc:
            logger.warning(
                "[kora.slack_dm] log write failed (%s): %r",
                self._log_path,
                exc,
            )

    def _emit_received_event(self, payload: Dict[str, Any]) -> None:
        """Stable structured-log emit for an identified Joshua DM.

        ``kora.slack_dm.received`` is the intended chain-event vocab
        literal; if/when substrate ships the CHECK-constraint
        addition, this can extend to also call
        ``IsoKronMCPClient.invoke("kora__append_event", ...)``.
        For now structured log is the audit seam.
        """
        logger.info(
            "[kora.slack_dm.received] channel=%s user=%s ts=%s text_len=%d",
            _safe_extract(payload, "event", "channel") or "",
            _safe_extract(payload, "event", "user") or "",
            _safe_extract(payload, "event", "ts") or "",
            len(_safe_extract(payload, "event", "text") or ""),
        )

    # ------------------------------------------------------------------
    # Outbound reply — reasoning-engine driven (KR-FEAT-AI-RESPONSE-LOOP ST2)
    # ------------------------------------------------------------------

    # Canned fallback text per PM ruling. Sent when the reasoning
    # engine is unavailable OR returned an error. NOT a re-echo —
    # Joshua needs to know reasoning didn't work, but not be hit
    # with a dump of his own message.
    _CANNED_FALLBACK_TEXT = (
        "Kora is currently unable to respond; operator notified."
    )

    async def _send_echo_reply(self, payload: Dict[str, Any]) -> None:
        """Reply to a verified Joshua DM.

        Method name retained for diff-minimization with KR-FEAT-
        SLACK-DM ST2 (#122); body swapped from echo construction
        to reasoning-engine call per KR-FEAT-AI-RESPONSE-LOOP ST2.

        Flow:

          1. Resolve reasoning engine (injection → daemon singleton
             → None). If None: canned fallback + outbound entry +
             early return.
          2. Build conversation context from JSONL (last 10 turns
             same thread).
          3. Call ``engine.respond(message, context)``. Result
             error-set → canned fallback + record reasoning_error
             in outbound entry. Error-unset → use result.text +
             record reasoning metadata in outbound entry.
          4. Send via SlackClient with retry / dead-letter shape
             from KR-FEAT-SLACK-DM ST2.
          5. After successful response (NOT canned), call cost-
             ladder ``record_inference()`` to bill the tokens.

        Failure modes (each writes one outbound JSONL entry +
        structured log; never crashes the inbound handler):

          - SlackClient unavailable → failed outbound entry,
            ``failure_reason="slack_client_not_configured"``
          - SlackTransportError / SlackAPIError → failed outbound
            entry, stable failure-reason taxonomy from ST2
          - Reasoning engine unavailable → canned reply sent,
            outbound ``reasoning_error="engine_unavailable"``
          - Reasoning engine returned error → canned reply sent,
            outbound ``reasoning_error=<error code>``
        """
        from datetime import datetime, timezone

        channel_id = _safe_extract(payload, "event", "channel") or ""
        original_text = _safe_extract(payload, "event", "text") or ""
        # Per spec: thread under originating DM via event.thread_ts
        # (already in-thread) or event.ts (new thread).
        thread_ts = _safe_extract(payload, "event", "thread_ts") or _safe_extract(
            payload, "event", "ts"
        )

        # ---- Short-circuit attempt (KR-CHEAP-TRIVIAL-DM-SHORTCIRCUIT) ----
        # Pre-filter for routine status queries — answers from the
        # pre-warmed snapshot at zero LLM cost. Returns None on no
        # phrasebook match OR when the snapshot can't satisfy the
        # template; either path falls through to the reasoning engine
        # unchanged.
        short_circuit_reply = self._try_short_circuit(original_text)
        if short_circuit_reply is not None:
            reply_text = short_circuit_reply.reply_text
            reasoning_meta = self._short_circuit_reasoning_meta(
                short_circuit_reply
            )
        else:
            reply_text, reasoning_meta = await self._resolve_via_engine(
                payload=payload,
                channel_id=channel_id,
                thread_ts=thread_ts,
                original_text=original_text,
            )

        # ---- Slack outbound ----
        client = self._get_or_create_slack_client()
        if client is None:
            self._append_outbound_log_entry(
                channel_id=channel_id,
                thread_ts=thread_ts,
                text=reply_text,
                slack_message_ts=None,
                send_status="failed",
                failure_reason="slack_client_not_configured",
                **reasoning_meta,
            )
            self._emit_reply_failed_event(
                channel_id=channel_id,
                reason="slack_client_not_configured",
            )
            return

        try:
            response = await client.post_dm(
                channel_id=channel_id,
                text=reply_text,
                thread_ts=thread_ts,
            )
        except Exception as exc:
            reason = self._reply_failure_reason(exc)
            self._append_outbound_log_entry(
                channel_id=channel_id,
                thread_ts=thread_ts,
                text=reply_text,
                slack_message_ts=None,
                send_status="failed",
                failure_reason=reason,
                **reasoning_meta,
            )
            self._emit_reply_failed_event(
                channel_id=channel_id, reason=reason
            )
            return

        # Success.
        message_ts = (
            response.get("ts") if isinstance(response, dict) else None
        )
        self._append_outbound_log_entry(
            channel_id=channel_id,
            thread_ts=thread_ts,
            text=reply_text,
            slack_message_ts=str(message_ts) if message_ts else None,
            send_status="ok",
            **reasoning_meta,
        )

        # ---- Cost-ladder write (only on successful, non-canned reply) ----
        # Bill the tokens against the $200/mo Agent SDK pool. Skip
        # if the reply was canned (no real inference happened).
        # Short-circuit hits hit this path too but the cost-ladder
        # bails internally (all four token buckets are 0).
        if reasoning_meta["reasoning_error"] is None:
            self._record_inference_to_cost_ladder(reasoning_meta)
        return

    async def _resolve_via_engine(
        self,
        *,
        payload: Dict[str, Any],
        channel_id: str,
        thread_ts: Optional[str],
        original_text: str,
    ) -> tuple[str, Dict[str, Any]]:
        """The original (pre-short-circuit) engine resolution path.

        Extracted into its own method so the short-circuit branch
        in ``handle_event`` is the only call site that touches the
        engine; non-matching messages flow through here unchanged.
        Returns ``(reply_text, reasoning_meta)``.
        """
        engine = self._resolve_reasoning_engine()
        reasoning_meta: Dict[str, Any] = {
            "model_used": None,
            "input_tokens": None,
            "output_tokens": None,
            "reasoning_duration_ms": None,
            "reasoning_error": None,
            # KR-FEAT-AGENTIC-REASONING ST2 — names of reasoning-side
            # tools Kora actually invoked during this response.
            # Empty list when she didn't call any tools (flat
            # completion path); recorded in the outbound JSONL so
            # the REASONING-PANEL can surface "this response used
            # N tools" without parsing structured logs.
            "tools_used": None,
            # KR-CHEAP-PROMPT-CACHING — cache-token totals. Default
            # None (engine didn't run / errored) → 0 when present.
            # Handler bills cache_creation at ~1.25x base and
            # cache_read at ~0.1x base via CanonicalUsage in
            # _record_inference_to_cost_ladder.
            "cache_creation_input_tokens": None,
            "cache_read_input_tokens": None,
        }

        if engine is None:
            # Daemon misconfigured OR running outside-coordinator
            # test path. Send canned fallback so Joshua isn't met
            # with silence; record the reason for operator triage.
            reply_text = self._CANNED_FALLBACK_TEXT
            reasoning_meta["reasoning_error"] = "engine_unavailable"
            logger.warning(
                "[kora.slack_dm.reasoning_skipped] reason=engine_unavailable "
                "channel=%s — sending canned fallback",
                channel_id,
            )
        else:
            reply_text, reasoning_meta = await self._call_reasoning_engine(
                engine=engine,
                payload=payload,
                channel_id=channel_id,
                thread_ts=thread_ts,
                original_text=original_text,
            )

        return reply_text, reasoning_meta

    # ------------------------------------------------------------------
    # Short-circuit (KR-CHEAP-TRIVIAL-DM-SHORTCIRCUIT)
    # ------------------------------------------------------------------

    def _try_short_circuit(self, text: str) -> Optional[Any]:
        """Try to answer the DM from the snapshot via the phrasebook.

        Returns a :class:`ShortCircuitMatch` on success or ``None``
        on no-match / render-fall-through. Caller treats ``None`` as
        "proceed with normal engine resolution."

        Phrasebook is cached on the handler instance after first
        load. An operator can edit
        ``${KORA_HOME}/phrasebook/slack_dm.yml`` and restart the
        daemon to pick up changes (in-process reload is out of
        scope for v1).

        Any unexpected exception here is logged + swallowed — a
        broken phrasebook MUST NOT break DM handling. The caller
        falls through to engine resolution as if no phrasebook
        existed.
        """
        try:
            from kora_cli.short_circuit import (
                load_phrasebook,
                try_short_circuit as _try,
            )
            from kora_cli.snapshot.state_snapshot import read_snapshot
        except Exception as exc:
            logger.warning(
                "[kora.slack_dm.short_circuit] module import failed: %r "
                "— falling through to engine",
                exc,
            )
            return None

        try:
            if self._phrasebook is None:
                self._phrasebook = load_phrasebook()
            snapshot = read_snapshot()
            return _try(text, self._phrasebook, snapshot)
        except Exception as exc:
            logger.warning(
                "[kora.slack_dm.short_circuit] try failed: %r — "
                "falling through to engine",
                exc,
            )
            return None

    @staticmethod
    def _short_circuit_reasoning_meta(match: Any) -> Dict[str, Any]:
        """Build a sentinel ``reasoning_meta`` for a short-circuit
        hit. Keys match the engine-driven shape so downstream code
        (the outbound logger, the cost-ladder write) treats the
        two paths uniformly. ``model_used="short_circuit"`` is the
        telemetry discriminator CC#1's KR-CHEAP-COST-TELEMETRY
        will join on for zero-cost-route classification.

        The two extra keys (``short_circuit_category`` +
        ``short_circuit_pattern``) flow through ``**reasoning_meta``
        into ``_append_outbound_log_entry`` which now accepts them.
        """
        return {
            "model_used": "short_circuit",
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_duration_ms": 0,
            "reasoning_error": None,
            "tools_used": [],
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "short_circuit_category": match.entry.category,
            "short_circuit_pattern": match.entry.pattern.pattern,
        }

    # ------------------------------------------------------------------
    # Reasoning engine helpers
    # ------------------------------------------------------------------

    def _resolve_reasoning_engine(self) -> Optional[Any]:
        """Return injected engine, else daemon-singleton, else None."""
        if self._reasoning_engine is not None:
            return self._reasoning_engine
        try:
            from kora_cli.listeners.reasoning_engine_listener import (
                current_reasoning_engine,
            )
        except Exception:
            # Listener module didn't import — daemon not active.
            return None
        return current_reasoning_engine()

    async def _call_reasoning_engine(
        self,
        *,
        engine: Any,
        payload: Dict[str, Any],
        channel_id: str,
        thread_ts: Optional[str],
        original_text: str,
    ) -> tuple[str, Dict[str, Any]]:
        """Call engine.respond + project result into reply_text +
        reasoning_meta dict.

        Returns ``(reply_text, reasoning_meta)``. On error the
        reply_text is the canned fallback; meta carries the error
        code so the outbound JSONL records it.
        """
        from datetime import datetime, timezone

        # Lazy import — keeps non-reasoning test paths fast +
        # avoids forcing the anthropic SDK import at module-load.
        from kora_cli.reasoning.context_loader import (
            load_slack_dm_context,
        )
        from kora_cli.reasoning.engine import IncomingMessage

        try:
            context = load_slack_dm_context(
                channel_id=channel_id, thread_ts=thread_ts
            )
        except Exception as exc:
            logger.warning(
                "[kora.slack_dm.reasoning_skipped] context-load failed: %r "
                "channel=%s — using empty context",
                exc,
                channel_id,
            )
            from kora_cli.reasoning.engine import ConversationContext

            context = ConversationContext()

        message = IncomingMessage(
            text=original_text,
            source="slack_dm",
            received_at=datetime.now(timezone.utc),
            metadata={
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "user_id": _safe_extract(payload, "event", "user"),
                "event_ts": _safe_extract(payload, "event", "ts"),
            },
        )

        try:
            result = await engine.respond(message, context)
        except Exception as exc:
            # An engine that itself raises (not just ResponseResult.error)
            # is a runtime bug — caught defensively so the handler
            # stays alive.
            logger.warning(
                "[kora.slack_dm.reasoning_skipped] engine.respond raised %r "
                "channel=%s — canned fallback",
                exc,
                channel_id,
            )
            return (
                self._CANNED_FALLBACK_TEXT,
                {
                    "model_used": None,
                    "input_tokens": None,
                    "output_tokens": None,
                    "reasoning_duration_ms": None,
                    "reasoning_error": f"engine_exception:{type(exc).__name__}",
                    "tools_used": None,
                    "cache_creation_input_tokens": None,
                    "cache_read_input_tokens": None,
                },
            )

        meta = {
            "model_used": result.model_used or None,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "reasoning_duration_ms": result.reasoning_duration_ms,
            "reasoning_error": result.error,
            # KR-FEAT-AGENTIC-REASONING ST2 — pass the engine's
            # tools_used list through to the outbound JSONL.
            # Semantic distinction in JSONL key presence:
            #   - key absent: engine refused / errored / bypassed
            #     (no tools could have been called)
            #   - key present == []: engine completed reasoning +
            #     chose to use zero tools
            #   - key present with names: engine called these tools
            # When result.error is set the engine refused before
            # invoking any tool — same as "engine bypassed" from
            # the tools_used audit perspective, so we pass None to
            # the outbound builder + the key is omitted.
            "tools_used": (
                list(result.tools_used)
                if result.error is None
                else None
            ),
            # KR-CHEAP-PROMPT-CACHING — cache-token accumulators.
            # Pass through whatever the engine accumulated; 0 when
            # no caching engaged (uncached call OR pre-cache state).
            "cache_creation_input_tokens": result.cache_creation_input_tokens,
            "cache_read_input_tokens": result.cache_read_input_tokens,
        }

        if result.error is not None:
            # Engine refused (paused / cost-halted / SDK failure).
            # Send canned text so Joshua sees something; record
            # the error code so operator can triage.
            logger.warning(
                "[kora.slack_dm.reasoning_failed] error=%s channel=%s",
                result.error,
                channel_id,
            )
            return (self._CANNED_FALLBACK_TEXT, meta)

        # Success — engine produced a real response. Defensive
        # check: empty text from a successful call shouldn't
        # happen but if it does, fall back to canned so Joshua
        # doesn't see a blank message.
        if not result.text.strip():
            logger.warning(
                "[kora.slack_dm.reasoning_failed] empty text on success "
                "model=%s channel=%s — canned fallback",
                result.model_used,
                channel_id,
            )
            meta["reasoning_error"] = "empty_response_text"
            return (self._CANNED_FALLBACK_TEXT, meta)

        return (result.text, meta)

    @staticmethod
    def _record_inference_to_cost_ladder(
        reasoning_meta: Dict[str, Any],
    ) -> None:
        """Bill the reply's tokens to the cost-ladder ($200/mo pool).

        Fail-soft: holder uninitialized → skip (test path / partial
        daemon boot). record_inference itself is fail-soft per
        ``agent.cost_state_holder``'s docstring (pricing-lookup miss
        accumulates 0).
        """
        try:
            from agent.cost_state_holder import get_cost_holder
            from agent.usage_pricing import CanonicalUsage
        except Exception as exc:
            logger.warning(
                "[kora.slack_dm.cost_ladder_skipped] import failed: %r",
                exc,
            )
            return

        holder = get_cost_holder()
        if holder is None:
            return

        model_name = reasoning_meta.get("model_used")
        input_tokens = reasoning_meta.get("input_tokens") or 0
        output_tokens = reasoning_meta.get("output_tokens") or 0
        # KR-CHEAP-PROMPT-CACHING — bill cache_creation + cache_read
        # at their respective rates. CanonicalUsage(.cache_write_tokens
        # → ~1.25x base) + (.cache_read_tokens → ~0.1x base) per the
        # PricingEntry table in agent/usage_pricing.py. When the
        # engine didn't cache (None or 0), CanonicalUsage's defaults
        # keep this a no-op for those fields.
        cache_creation_tokens = (
            reasoning_meta.get("cache_creation_input_tokens") or 0
        )
        cache_read_tokens = (
            reasoning_meta.get("cache_read_input_tokens") or 0
        )
        # Bail only when EVERY token bucket is 0 — a pure-cache-read
        # call (input_tokens=0 but cache_read_tokens>0) still bills.
        if not model_name or (
            input_tokens == 0
            and output_tokens == 0
            and cache_creation_tokens == 0
            and cache_read_tokens == 0
        ):
            return

        try:
            holder.record_inference(
                CanonicalUsage(
                    input_tokens=int(input_tokens),
                    output_tokens=int(output_tokens),
                    cache_write_tokens=int(cache_creation_tokens),
                    cache_read_tokens=int(cache_read_tokens),
                ),
                model_name=str(model_name),
                provider="anthropic",
                # KR-CHEAP-COST-TELEMETRY — tag this Kora reply-bill
                # under the slack_dm route. First iteration of a
                # tool-use loop is attributed to the originating
                # route (here slack_dm); iteration 2+ would attribute
                # to ``tool_loop_iteration`` once the reasoning
                # engine surfaces that signal (deferred follow-on).
                route="slack_dm",
            )
        except Exception as exc:
            logger.warning(
                "[kora.slack_dm.cost_ladder_skipped] record_inference "
                "raised %r — continuing",
                exc,
            )

    def _get_or_create_slack_client(self) -> Optional[Any]:
        """Get the SlackClient — daemon-coordinator-managed first;
        lazy-create as fallback.

        Resolution order:

          1. ``self._slack_client`` (test injection or prior cache)
          2. ``current_slack_client()`` from the daemon listener
             (KR-MCP-SEND-TOOLS) — present once the daemon boots
             with ``slack_client`` listener registered
          3. Lazy-construct a fresh SlackClient (legacy path —
             keeps standalone-handler tests passing without daemon
             listeners running)

        The lazy-construct fallback preserves the pre-KR-MCP-SEND-
        TOOLS contract: a handler instantiated outside the daemon
        (test fixtures, ad-hoc scripts) still acquires a client.
        Production daemon paths get the shared listener instance.

        Returns ``None`` if every path fails (KORA_SLACK_BOT_TOKEN
        unset).
        """
        if self._slack_client is not None:
            return self._slack_client

        # Listener-managed singleton (KR-MCP-SEND-TOOLS).
        try:
            from kora_cli.listeners.slack_client_listener import (
                current_slack_client,
            )

            shared = current_slack_client()
            if shared is not None:
                self._slack_client = shared
                return self._slack_client
        except Exception as exc:
            # Import error / accessor blow-up — fall through to
            # lazy-construct. Don't swallow silently; log so the
            # operator can correlate.
            logger.debug(
                "[kora.slack_dm] current_slack_client lookup failed: %r — "
                "falling back to lazy construct",
                exc,
            )

        # Lazy-construct fallback (legacy / standalone-handler path).
        try:
            from kora_cli.clients.slack_client import SlackClient

            self._slack_client = SlackClient()
            return self._slack_client
        except Exception as exc:
            # SlackClientNotConfigured is the expected failure when
            # KORA_SLACK_BOT_TOKEN is unset. Log once + cache None
            # so subsequent inbound events don't re-attempt.
            logger.warning(
                "[kora.slack_dm] SlackClient unavailable: %r — "
                "outbound replies disabled",
                exc,
            )
            return None

    def _append_outbound_log_entry(
        self,
        *,
        channel_id: str,
        thread_ts: Optional[str],
        text: str,
        slack_message_ts: Optional[str],
        send_status: str,
        failure_reason: Optional[str] = None,
        # KR-FEAT-AI-RESPONSE-LOOP ST2 — reasoning metadata. All
        # optional + backwards-compatible; pre-ST2 outbound entries
        # don't have these fields and consumers must handle absence
        # (same JSONL file accumulates both shapes).
        model_used: Optional[str] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        reasoning_duration_ms: Optional[int] = None,
        reasoning_error: Optional[str] = None,
        # KR-MCP-SEND-TOOLS — when a send is driven by an MCP tool
        # call, the caller's actor_kind appears here for audit
        # attribution. None (omitted) on handler-driven sends.
        caller_actor_kind: Optional[str] = None,
        # KR-FEAT-AGENTIC-REASONING ST2 — names of reasoning-side
        # tools Kora invoked during this response. Empty list when
        # she didn't use tools (flat completion path). None
        # (omitted from JSONL) on canned-fallback / non-reasoning
        # paths so the field's presence distinguishes "engine ran"
        # from "engine bypassed."
        tools_used: Optional[List[str]] = None,
        # KR-CHEAP-PROMPT-CACHING — cache-token totals from the
        # engine. None (omitted from JSONL) on non-reasoning paths;
        # explicit 0 means the engine ran but no cache engaged
        # (older SDK / uncached call). Persisted alongside
        # input_tokens/output_tokens so the reasoning panel can
        # show cache-hit rate per call without grepping logs.
        cache_creation_input_tokens: Optional[int] = None,
        cache_read_input_tokens: Optional[int] = None,
        # KR-CHEAP-TRIVIAL-DM-SHORTCIRCUIT — when the DM was
        # answered from the snapshot via a phrasebook match (no
        # LLM call), these carry the phrasebook entry's category +
        # source-regex pattern. Both None on engine-driven paths.
        # CC#1's KR-CHEAP-COST-TELEMETRY will join on
        # ``model_used == "short_circuit"`` to classify zero-cost
        # route hits; the category field lets the reasoning panel
        # break down which query shapes are getting short-circuited.
        short_circuit_category: Optional[str] = None,
        short_circuit_pattern: Optional[str] = None,
    ) -> None:
        """Outbound-side JSONL entry. Distinct schema from inbound
        entries (``sent_at`` instead of ``received_at``) so operator
        log-analysis can branch on key presence.

        ST2 extended fields (all optional, all None on canned-
        fallback / non-reasoning paths so historical entries stay
        readable):

          - ``model_used``: e.g. ``"claude-opus-4-7"``
          - ``input_tokens`` / ``output_tokens``: from SDK usage
          - ``reasoning_duration_ms``: engine-side wall-clock
          - ``reasoning_error``: stable error code from
            ``ResponseResult.error`` (``cost_ladder_halted`` /
            ``sdk_5xx`` / ``engine_unavailable`` / etc.) — None on
            successful reasoning calls

        ``caller_actor_kind`` (KR-MCP-SEND-TOOLS): when a send is
        driven by an MCP tool call, the caller's actor_kind appears
        here for audit attribution. ``None`` (omitted) for
        handler-driven sends (echo replies + reasoning-engine
        replies from CC#3's KR-FEAT-AI-RESPONSE-LOOP). Backwards-
        compatible — consumers handle absence; existing entries
        without the field keep parsing.
        """
        # KR-PROBE-INVESTIGATION-DATA-COMPLETION — delegates to the
        # free function so handler-driven sends + probe-wake-driven
        # sends write byte-identical JSONL rows.
        append_outbound_log_entry(
            log_path=self._log_path,
            channel_id=channel_id,
            thread_ts=thread_ts,
            text=text,
            slack_message_ts=slack_message_ts,
            send_status=send_status,
            failure_reason=failure_reason,
            model_used=model_used,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_duration_ms=reasoning_duration_ms,
            reasoning_error=reasoning_error,
            caller_actor_kind=caller_actor_kind,
            tools_used=tools_used,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            short_circuit_category=short_circuit_category,
            short_circuit_pattern=short_circuit_pattern,
        )

    def _emit_reply_failed_event(
        self, *, channel_id: str, reason: str
    ) -> None:
        """Audit per reply-failure — KR-AUDIT-JSONL-SINK dual-write.

        Preserves the existing ``[kora.slack_dm.reply_failed]``
        structured-log line VERBATIM (operator grep workflows
        unchanged) + writes a JSONL row to ``kora_audit_log.jsonl``
        for AGENT-ACTIVITY-PANEL / WEBHOOK-EVENTS-PANEL consumption.

        ``channel_id`` is included verbatim (Slack channel IDs are
        not sensitive). ``reason`` is a stable machine code
        (``slack_client_not_configured`` / ``transport:<status>`` /
        ``slack_api:<error>``) — no operator data.
        """
        # Existing structured-log line — preserved VERBATIM.
        logger.warning(
            "[kora.slack_dm.reply_failed] channel=%s reason=%s",
            channel_id,
            reason,
        )

        # KR-AUDIT-JSONL-SINK — JSONL bridge to panels.
        from kora_cli.audit import emit_audit

        emit_audit(
            seam="slack_dm.reply_failed",
            details={
                "channel_id": channel_id,
                "reason": reason,
            },
            source="slack_dm",
        )

    @staticmethod
    def _reply_failure_reason(exc: BaseException) -> str:
        """Map an exception to a stable JSONL ``failure_reason`` code.

        Pure helper — no imports of SlackClient module needed inline
        (the type-checks happen via attribute presence so a slimmed
        SlackClient won't break this map).
        """
        if isinstance(exc, ImportError):
            return "slack_client_import_error"
        slack_error = getattr(exc, "slack_error", None)
        if slack_error:
            return f"slack_api:{slack_error}"
        last_status = getattr(exc, "last_status", None)
        if last_status is not None:
            return f"transport:{last_status}"
        return f"transport:{type(exc).__name__}"
