"""Probe wake-event CONSUMER — KR-PROBE-WAKE-CONSUMER.

Closes the operator-value loop from PR #163's emission side:

  probe runner → probe.wake_requested audit row (PR #163)
      ↓
  [audit-log tail listener picks up event]   ← this module
      ↓
  [per-(probe, issue_category) inline debounce]
      ↓
  [reasoning engine .respond() with probe-investigation context]
      ↓
  [record_inference(route="probe_investigation")]
      ↓
  [outbound Slack DM to Joshua with investigation summary]

# Read-only contract preserved

This module is a READ-side consumer of the audit JSONL written by
the probe runner. It does NOT mutate probe state, run fix
attempts, or touch the snapshot. **Autofix execution is queued as
KR-PROBE-AUTOFIX-EXECUTION** — strictly out of scope here.

# Debounce policy (v1, inline)

Per spec §2(a): map ``(probe_name, issue_category) → datetime`` of
last dispatched investigation. Default 10 min window via
``KORA_PROBE_DEBOUNCE_SECONDS``. Critical wakes can optionally
bypass via ``KORA_PROBE_DEBOUNCE_BYPASS_CRITICAL=true`` (default
false — even critical debounces, operator opt-in).

Sophisticated debounce (consecutive-failure buffering, hysteresis,
backoff) is queued as KR-PROBE-DEBOUNCE if v1's flat-window
approach generates false-positives in production.

# Fail-soft

Every external dependency is fail-soft:

  * Engine None / engine raise → fallback DM with verbatim issue + reason
  * Slack client None → log warning + record telemetry; no DM sent
  * Cost-ladder write fail → log + continue (DM still sent)
  * Telemetry record fail → log + continue (DM still sent)

The listener that drives this consumer must NOT crash on any
single event — failures are recorded + cycle continues.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env vars + defaults
# ---------------------------------------------------------------------------


DEBOUNCE_SECONDS_ENV = "KORA_PROBE_DEBOUNCE_SECONDS"
DEFAULT_DEBOUNCE_SECONDS = 600  # 10 min per spec §2(a)

BYPASS_CRITICAL_ENV = "KORA_PROBE_DEBOUNCE_BYPASS_CRITICAL"
JOSHUA_SLACK_USER_ID_ENV = "KORA_SLACK_JOSHUA_USER_ID"  # reused from PR #149

# KR-PROBE-DEBOUNCE — consecutive-failure buffering (upgrade from
# PR #166's flat-window debounce per CC#1's #163 follow-on tracker).
# Default 2: a probe must fire wake_requested twice within the
# debounce window before the consumer dispatches an investigation.
# Operator-tunable; setting to 1 preserves pre-upgrade behavior.
CONSECUTIVE_REQUIRED_ENV = "KORA_PROBE_DEBOUNCE_CONSECUTIVE_REQUIRED"
DEFAULT_CONSECUTIVE_REQUIRED = 2


def _read_debounce_seconds() -> int:
    raw = os.environ.get(DEBOUNCE_SECONDS_ENV, "").strip()
    if not raw:
        return DEFAULT_DEBOUNCE_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "[kora.probe_wake_consumer] %s=%r is not numeric; using "
            "default %ds",
            DEBOUNCE_SECONDS_ENV,
            raw,
            DEFAULT_DEBOUNCE_SECONDS,
        )
        return DEFAULT_DEBOUNCE_SECONDS
    if value < 0:
        logger.warning(
            "[kora.probe_wake_consumer] %s=%d must be ≥ 0; using "
            "default %ds",
            DEBOUNCE_SECONDS_ENV,
            value,
            DEFAULT_DEBOUNCE_SECONDS,
        )
        return DEFAULT_DEBOUNCE_SECONDS
    return value


def _read_consecutive_required() -> int:
    """Read ``KORA_PROBE_DEBOUNCE_CONSECUTIVE_REQUIRED`` with fail-soft
    parsing. Defaults to :data:`DEFAULT_CONSECUTIVE_REQUIRED` on
    malformed values. ``1`` disables the buffering and restores the
    PR #166 flat-window behavior."""
    raw = os.environ.get(CONSECUTIVE_REQUIRED_ENV, "").strip()
    if not raw:
        return DEFAULT_CONSECUTIVE_REQUIRED
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "[kora.probe_wake_consumer] %s=%r is not numeric; using "
            "default %d",
            CONSECUTIVE_REQUIRED_ENV,
            raw,
            DEFAULT_CONSECUTIVE_REQUIRED,
        )
        return DEFAULT_CONSECUTIVE_REQUIRED
    if value < 1:
        logger.warning(
            "[kora.probe_wake_consumer] %s=%d must be ≥ 1; using "
            "default %d",
            CONSECUTIVE_REQUIRED_ENV,
            value,
            DEFAULT_CONSECUTIVE_REQUIRED,
        )
        return DEFAULT_CONSECUTIVE_REQUIRED
    return value


def _read_bypass_critical() -> bool:
    raw = os.environ.get(BYPASS_CRITICAL_ENV, "").strip().lower()
    return raw in {"true", "1", "yes", "on"}


# ---------------------------------------------------------------------------
# Outcome (telemetry + test surface)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WakeConsumeOutcome:
    """One per-event outcome. Bundled for tests + listener telemetry."""

    probe: str
    category: str
    severity: str
    dispatched: bool  # False when debounced or no engine
    reasoning_invoked: bool  # False when engine None / debounced
    dm_sent: bool  # False when Slack client unavailable
    debounce_skipped: bool = False
    # KR-PROBE-DEBOUNCE consecutive-failure upgrade. True when an
    # event was held in the consecutive-failure buffer (didn't yet
    # meet ``KORA_PROBE_DEBOUNCE_CONSECUTIVE_REQUIRED``) instead of
    # being dispatched. Distinct from ``debounce_skipped`` (which
    # remains the flat-window post-dispatch skip) so the audit
    # / listener telemetry can tell single-tick-flake holds apart
    # from "already-dispatched recently" skips.
    buffered_skipped: bool = False
    buffered_consecutive_count: int = 0
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# ProbeWakeConsumer
# ---------------------------------------------------------------------------


SlackClientFactory = Callable[[], Optional[Any]]
ReasoningEngineFactory = Callable[[], Optional[Any]]


class ProbeWakeConsumer:
    """Per-event handler for probe.wake_requested audit rows.

    Stateful only via the debounce map. The listener owns the
    instance + calls :meth:`consume_wake_event` per fresh audit row.

    Lazy factories for the reasoning engine + Slack client mirror
    the AlertNotifier pattern (PR #149) — both may be unavailable
    when the listener boots, but become available mid-runtime.
    """

    def __init__(
        self,
        *,
        reasoning_engine_factory: ReasoningEngineFactory,
        slack_client_factory: SlackClientFactory,
        operator_channel_id_resolver: Callable[[], str] = (
            lambda: os.environ.get(JOSHUA_SLACK_USER_ID_ENV, "").strip()
        ),
    ) -> None:
        self._reasoning_engine_factory = reasoning_engine_factory
        self._slack_client_factory = slack_client_factory
        self._operator_channel_id_resolver = operator_channel_id_resolver
        self._debounce_lock = threading.RLock()
        # (probe_name, issue_category) → datetime of last dispatched
        self._last_dispatched: Dict[Tuple[str, str], datetime] = {}
        # KR-PROBE-DEBOUNCE consecutive-failure buffer state.
        # (probe, category) → (consecutive_count, first_seen_at).
        # Reset when the sliding window elapses (proxy for "the
        # probe went healthy then unhealthy again — the burst was
        # transient, restart the counter").
        self._consecutive_buffer: Dict[
            Tuple[str, str], Tuple[int, datetime]
        ] = {}

    @property
    def debounce_map_size(self) -> int:
        """Read-only view for tests + telemetry."""
        return len(self._last_dispatched)

    def reset_debounce_state(self) -> None:
        """Clear the in-memory debounce map + consecutive-failure
        buffer. Listener shutdown calls this so subsequent listener
        start sees a clean slate. Mirrors :meth:`AlertNotifier.reset_dedup_state`
        (PR #149)."""
        with self._debounce_lock:
            self._last_dispatched = {}
            self._consecutive_buffer = {}

    @property
    def consecutive_buffer_size(self) -> int:
        """Read-only view for tests + telemetry."""
        return len(self._consecutive_buffer)

    # ------------------------------------------------------------------
    # Debounce
    # ------------------------------------------------------------------

    def _is_debounced(
        self, probe: str, category: str, severity: str
    ) -> bool:
        """Return True iff this (probe, category) was dispatched
        within the debounce window AND we're not bypassing for
        critical."""
        if severity == "critical" and _read_bypass_critical():
            return False
        window = _read_debounce_seconds()
        if window <= 0:
            return False
        with self._debounce_lock:
            last = self._last_dispatched.get((probe, category))
        if last is None:
            return False
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        return elapsed < window

    def _mark_dispatched(self, probe: str, category: str) -> None:
        with self._debounce_lock:
            self._last_dispatched[(probe, category)] = datetime.now(
                timezone.utc
            )
            # Clear the consecutive buffer once we've dispatched —
            # the post-dispatch flat-window debounce takes over.
            self._consecutive_buffer.pop((probe, category), None)

    # ------------------------------------------------------------------
    # Consecutive-failure buffer (KR-PROBE-DEBOUNCE upgrade)
    # ------------------------------------------------------------------

    def _record_failure_and_check_threshold(
        self, probe: str, category: str, severity: str
    ) -> Tuple[bool, int]:
        """Update the consecutive-failure buffer for one (probe,
        category) event.

        Returns ``(threshold_met, count_after_update)``:
          * ``threshold_met`` is True when this event brings the
            buffer up to the configured required count → caller
            should dispatch.
          * ``count_after_update`` is the post-update buffered
            count, surfaced in the outcome for tests / telemetry.

        Window semantics: the buffer entry expires after the
        existing :data:`DEBOUNCE_SECONDS_ENV` window elapsed since
        ``first_seen_at`` — re-using the existing debounce window
        keeps the operator-tunable surface minimal. A fresh event
        AFTER expiry restarts the count at 1 (proxy for "probe
        went healthy then unhealthy again").

        Critical-severity bypass: when ``severity == "critical"``
        AND ``KORA_PROBE_DEBOUNCE_BYPASS_CRITICAL`` is truthy, the
        threshold is implicitly 1 (caller never reaches this
        method; see :meth:`_should_dispatch_now`).
        """
        required = _read_consecutive_required()
        window = _read_debounce_seconds()
        now = datetime.now(timezone.utc)
        with self._debounce_lock:
            entry = self._consecutive_buffer.get((probe, category))
            if entry is None:
                self._consecutive_buffer[(probe, category)] = (1, now)
                return (required <= 1, 1)
            count, first_seen = entry
            # Expired window → restart count.
            if window > 0 and (now - first_seen).total_seconds() >= window:
                self._consecutive_buffer[(probe, category)] = (1, now)
                return (required <= 1, 1)
            new_count = count + 1
            self._consecutive_buffer[(probe, category)] = (new_count, first_seen)
            return (new_count >= required, new_count)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def consume_wake_event(
        self, event_details: Dict[str, Any]
    ) -> WakeConsumeOutcome:
        """Process one probe.wake_requested event.

        ``event_details`` is the ``details`` dict from the audit
        JSONL row (NOT the full AuditEntry). Per PR #163's emission
        shape: ``probe`` / ``severity`` / ``category`` / ``title`` /
        ``detail`` / ``snapshot_details`` / ``envelope_enabled`` /
        ``envelope_fix_name``.

        Fail-soft contract: every path either dispatches OR returns
        a structured outcome explaining why it didn't. Never
        raises.
        """
        probe = str(event_details.get("probe") or "unknown")
        category = str(event_details.get("category") or "unknown")
        severity = str(event_details.get("severity") or "warning")

        if self._is_debounced(probe, category, severity):
            logger.debug(
                "[kora.probe_wake_consumer] debounced probe=%s "
                "category=%s severity=%s",
                probe,
                category,
                severity,
            )
            return WakeConsumeOutcome(
                probe=probe,
                category=category,
                severity=severity,
                dispatched=False,
                reasoning_invoked=False,
                dm_sent=False,
                debounce_skipped=True,
            )

        # KR-PROBE-DEBOUNCE consecutive-failure buffering. Critical
        # wakes can optionally bypass via BYPASS_CRITICAL_ENV (same
        # opt-in env as the flat-window bypass — operator already
        # tunes one knob for "trust critical urgency").
        bypass_critical = (
            severity == "critical" and _read_bypass_critical()
        )
        if not bypass_critical:
            threshold_met, buffered_count = (
                self._record_failure_and_check_threshold(
                    probe, category, severity
                )
            )
            if not threshold_met:
                logger.debug(
                    "[kora.probe_wake_consumer] buffered probe=%s "
                    "category=%s severity=%s count=%d required=%d "
                    "— waiting for consecutive failure",
                    probe,
                    category,
                    severity,
                    buffered_count,
                    _read_consecutive_required(),
                )
                return WakeConsumeOutcome(
                    probe=probe,
                    category=category,
                    severity=severity,
                    dispatched=False,
                    reasoning_invoked=False,
                    dm_sent=False,
                    buffered_skipped=True,
                    buffered_consecutive_count=buffered_count,
                )

        # KR-PROBE-INVESTIGATION-DATA-COMPLETION — wall-clock start
        # so the investigation_completed audit can carry
        # investigation_duration_ms. Also the lower bound for the
        # audit-stream lookup that decides autofix_attempted (we
        # only count autofix rows emitted DURING this investigation).
        investigation_started_at = datetime.now(timezone.utc)
        investigation_started_monotonic = time.monotonic()
        caller_session_id = f"probe:{probe}:{category}"

        # Resolve engine + invoke reasoning. Failures fall through to
        # fallback-DM path.
        reasoning_text: str
        reasoning_invoked: bool = False
        reasoning_error: Optional[str] = None
        reasoning_result: Optional[Any] = None
        engine = self._reasoning_engine_factory()
        if engine is None:
            reasoning_text = format_fallback_text(
                event_details, reason="engine_unavailable"
            )
            reasoning_error = "engine_unavailable"
            logger.warning(
                "[kora.probe_wake_consumer] reasoning engine "
                "unavailable; sending fallback DM probe=%s",
                probe,
            )
        else:
            try:
                (
                    invocation_text,
                    invocation_error,
                    invocation_result,
                ) = await self._invoke_reasoning(
                    engine=engine, event_details=event_details
                )
            except Exception as exc:
                # respond() itself raised — distinct from a
                # ResponseResult.error set return. Wrap with the
                # type name so the panel can grep + categorize.
                reasoning_text = format_fallback_text(
                    event_details,
                    reason=f"engine_exception:{type(exc).__name__}",
                )
                reasoning_error = f"engine_exception:{type(exc).__name__}"
                logger.warning(
                    "[kora.probe_wake_consumer] engine.respond raised "
                    "%r probe=%s — sending fallback DM",
                    exc,
                    probe,
                )
            else:
                reasoning_result = invocation_result
                if invocation_error is None:
                    reasoning_text = invocation_text
                    reasoning_invoked = True
                else:
                    # Engine ran but returned a ResponseResult with
                    # .error set (cost_ladder_halted / paused /
                    # sdk_5xx etc) OR returned empty text. Surface
                    # the error code verbatim so the panel /
                    # operator can correlate to the engine's
                    # standard error vocabulary.
                    reasoning_text = format_fallback_text(
                        event_details, reason=invocation_error
                    )
                    reasoning_error = invocation_error
                    logger.warning(
                        "[kora.probe_wake_consumer] engine returned "
                        "error=%s probe=%s — sending fallback DM",
                        invocation_error,
                        probe,
                    )

        # Stamp dispatched BEFORE attempting DM so a flapping Slack
        # client can't trigger duplicate investigations in the next
        # cycle. Same shape as AlertNotifier's "alert enters
        # last_alert_ids regardless of dispatch success" contract.
        self._mark_dispatched(probe, category)

        # KR-PROBE-INVESTIGATION-DATA-COMPLETION — send the DM
        # through the routed helper so the slack_dm_log entry gets
        # the caller_session_id alongside the reasoning meta. dm_status
        # mirrors the panel-readable code emitted in the audit row.
        dm_outcome = await self._send_operator_dm_routed(
            probe=probe,
            severity=severity,
            text=reasoning_text,
            caller_session_id=caller_session_id,
            reasoning_result=reasoning_result,
            reasoning_error=reasoning_error,
            investigation_started_monotonic=investigation_started_monotonic,
        )
        dm_sent = dm_outcome["dm_sent"]
        dm_status = dm_outcome["dm_status"]

        # Investigation-completed audit. Always emitted (success +
        # fallback paths alike) so the viewer can render one row
        # per dispatched investigation.
        self._emit_investigation_completed(
            probe=probe,
            category=category,
            severity=severity,
            caller_session_id=caller_session_id,
            reasoning_result=reasoning_result,
            reasoning_error=reasoning_error,
            reasoning_text_for_dm=reasoning_text,
            dm_status=dm_status,
            investigation_started_at=investigation_started_at,
            investigation_started_monotonic=investigation_started_monotonic,
        )

        return WakeConsumeOutcome(
            probe=probe,
            category=category,
            severity=severity,
            dispatched=True,
            reasoning_invoked=reasoning_invoked,
            dm_sent=dm_sent,
            debounce_skipped=False,
            error=reasoning_error,
        )

    # ------------------------------------------------------------------
    # Reasoning invocation
    # ------------------------------------------------------------------

    async def _invoke_reasoning(
        self, *, engine: Any, event_details: Dict[str, Any]
    ) -> Tuple[str, Optional[str], Optional[Any]]:
        """Build the IncomingMessage + ConversationContext, call
        engine.respond, return ``(text, error_code, result)``.

        ``error_code`` is ``None`` on success (text is the engine's
        response). On engine-returned-error (ResponseResult.error
        set), ``error_code`` is the engine's stable error code
        verbatim ("cost_ladder_halted" / "sdk_5xx" / etc) + ``text``
        is empty. On empty-text-success, ``error_code`` is
        ``"empty_response_text"``.

        ``result`` is the raw ResponseResult (KR-PROBE-INVESTIGATION-
        DATA-COMPLETION addition) so the caller can extract
        model_used / token counts for the
        ``probe.investigation_completed`` audit row + the
        slack_dm_log outbound entry. ``None`` only when reasoning
        couldn't run at all (engine path, not handled here — the
        caller short-circuits in the outer ``consume_wake_event``).

        Engine ``respond()`` raising is left to the caller — this
        method only catches the engine's structured error paths.
        Telemetry ``route="probe_investigation"`` fires inside the
        engine (the engine's own record_inference call site reads
        from message.source).
        """
        from kora_cli.reasoning.engine import (
            ConversationContext,
            IncomingMessage,
        )

        message = IncomingMessage(
            text=format_investigation_prompt(event_details),
            source="probe_investigation",  # MessageSource Literal extended
            received_at=datetime.now(timezone.utc),
            metadata={
                "probe_name": event_details.get("probe") or "unknown",
                "issue_category": event_details.get("category") or "unknown",
                "severity": event_details.get("severity") or "warning",
                "envelope_enabled": bool(
                    event_details.get("envelope_enabled", False)
                ),
                "envelope_fix_name": str(
                    event_details.get("envelope_fix_name") or "(none)"
                ),
            },
        )
        context = ConversationContext(
            recent_messages=[],
            current_operational_state="unknown",
            current_cost_ladder_rung="unknown",
        )
        result = await engine.respond(message, context)
        engine_error = getattr(result, "error", None)
        if engine_error is not None:
            return ("", str(engine_error), result)
        text = getattr(result, "text", "") or ""
        if not text.strip():
            return ("", "empty_response_text", result)
        return (text, None, result)

    # ------------------------------------------------------------------
    # Outbound DM
    # ------------------------------------------------------------------

    async def _send_operator_dm_routed(
        self,
        *,
        probe: str,
        severity: str,
        text: str,
        caller_session_id: str,
        reasoning_result: Optional[Any],
        reasoning_error: Optional[str],
        investigation_started_monotonic: float,
    ) -> Dict[str, Any]:
        """KR-PROBE-INVESTIGATION-DATA-COMPLETION — DM + outbound
        log routing.

        Closes the V1NotesBanner gap from PR #171: this method now
        writes the same JSONL row to ``slack_dm_log.jsonl`` that a
        handler-driven reply would, with the probe's
        ``caller_session_id`` so CC#2's viewer can join the audit
        streams.

        Returns ``{"dm_sent": bool, "dm_status": str}``.
        ``dm_status`` is one of:

          * ``"sent"`` — post_dm + outbound log both succeeded.
          * ``"failed_send"`` — post_dm raised OR
            slack_client / channel_id is None. Outbound log entry
            still written (with ``send_status="failed"``) so the
            viewer can render the failure row.
          * ``"engine_unavailable_fallback"`` — reasoning never
            ran (engine None) but the fallback DM was sent
            successfully. Distinct from ``"sent"`` so the operator
            can identify cases where the cheap-cron alert text was
            surfaced verbatim instead of investigated.
          * ``"engine_unavailable_failed_send"`` — fallback path
            AND the DM send failed.
        """
        client = self._slack_client_factory()
        channel_id = self._operator_channel_id_resolver()
        is_fallback = reasoning_error is not None

        if client is None or not channel_id:
            if client is None:
                logger.warning(
                    "[kora.probe_wake_consumer] slack_client_unavailable; "
                    "DM not sent probe=%s",
                    probe,
                )
            else:
                logger.warning(
                    "[kora.probe_wake_consumer] %s unset; DM not sent "
                    "probe=%s",
                    JOSHUA_SLACK_USER_ID_ENV,
                    probe,
                )
            # Best-effort outbound log even when client is unwired
            # so the viewer can show "we tried but never had a
            # transport." channel_id may be ``""`` here — emit
            # anyway so the absence shows up in the panel.
            self._append_outbound_log(
                channel_id=channel_id or "",
                text=text,
                slack_message_ts=None,
                send_status="failed",
                failure_reason=(
                    "slack_client_unavailable"
                    if client is None
                    else "channel_id_unset"
                ),
                caller_session_id=caller_session_id,
                reasoning_result=reasoning_result,
                reasoning_error=reasoning_error,
                investigation_started_monotonic=investigation_started_monotonic,
            )
            return {
                "dm_sent": False,
                "dm_status": (
                    "engine_unavailable_failed_send"
                    if is_fallback
                    else "failed_send"
                ),
            }

        dm_text = format_operator_dm(
            probe=probe, severity=severity, reasoning_text=text
        )
        post_response: Optional[Dict[str, Any]] = None
        send_exception: Optional[BaseException] = None
        try:
            post_response = await client.post_dm(
                channel_id=channel_id, text=dm_text
            )
        except Exception as exc:
            send_exception = exc
            logger.warning(
                "[kora.probe_wake_consumer] post_dm raised %r probe=%s",
                exc,
                probe,
            )

        slack_message_ts: Optional[str] = None
        if isinstance(post_response, dict):
            ts_raw = post_response.get("ts")
            if isinstance(ts_raw, str):
                slack_message_ts = ts_raw

        if send_exception is not None:
            self._append_outbound_log(
                channel_id=channel_id,
                text=dm_text,
                slack_message_ts=None,
                send_status="failed",
                failure_reason=f"post_dm_raised:{type(send_exception).__name__}",
                caller_session_id=caller_session_id,
                reasoning_result=reasoning_result,
                reasoning_error=reasoning_error,
                investigation_started_monotonic=investigation_started_monotonic,
            )
            return {
                "dm_sent": False,
                "dm_status": (
                    "engine_unavailable_failed_send"
                    if is_fallback
                    else "failed_send"
                ),
            }

        self._append_outbound_log(
            channel_id=channel_id,
            text=dm_text,
            slack_message_ts=slack_message_ts,
            send_status="ok",
            failure_reason=None,
            caller_session_id=caller_session_id,
            reasoning_result=reasoning_result,
            reasoning_error=reasoning_error,
            investigation_started_monotonic=investigation_started_monotonic,
        )
        return {
            "dm_sent": True,
            "dm_status": (
                "engine_unavailable_fallback" if is_fallback else "sent"
            ),
        }

    def _append_outbound_log(
        self,
        *,
        channel_id: str,
        text: str,
        slack_message_ts: Optional[str],
        send_status: str,
        failure_reason: Optional[str],
        caller_session_id: str,
        reasoning_result: Optional[Any],
        reasoning_error: Optional[str],
        investigation_started_monotonic: float,
    ) -> None:
        """Best-effort wrapper around the extracted free function
        from slack_dm_handler. Fail-soft (the underlying helper
        already swallows OSError; this wrapper additionally guards
        against the import or path-resolution path raising)."""
        try:
            from kora_cli.handlers.slack_dm_handler import (
                append_outbound_log_entry,
                resolve_slack_dm_log_path,
            )
        except Exception as exc:
            logger.warning(
                "[kora.probe_wake_consumer] outbound log import "
                "failed: %r — slack_dm_log entry skipped",
                exc,
            )
            return

        duration_ms = int(
            (time.monotonic() - investigation_started_monotonic) * 1000
        )
        meta = _reasoning_meta_from_result(reasoning_result)
        try:
            append_outbound_log_entry(
                log_path=resolve_slack_dm_log_path(),
                channel_id=channel_id,
                thread_ts=None,
                text=text,
                slack_message_ts=slack_message_ts,
                send_status=send_status,
                failure_reason=failure_reason,
                model_used=meta.get("model_used"),
                input_tokens=meta.get("input_tokens"),
                output_tokens=meta.get("output_tokens"),
                reasoning_duration_ms=duration_ms,
                reasoning_error=reasoning_error,
                cache_creation_input_tokens=meta.get(
                    "cache_creation_input_tokens"
                ),
                cache_read_input_tokens=meta.get("cache_read_input_tokens"),
                caller_session_id=caller_session_id,
            )
        except Exception as exc:
            logger.warning(
                "[kora.probe_wake_consumer] outbound log write raised "
                "%r — investigation continues",
                exc,
            )

    def _emit_investigation_completed(
        self,
        *,
        probe: str,
        category: str,
        severity: str,
        caller_session_id: str,
        reasoning_result: Optional[Any],
        reasoning_error: Optional[str],
        reasoning_text_for_dm: str,
        dm_status: str,
        investigation_started_at: datetime,
        investigation_started_monotonic: float,
    ) -> None:
        """KR-PROBE-INVESTIGATION-DATA-COMPLETION — emit the
        per-investigation summary audit row. Closes the three
        V1NotesBanner gaps from PR #171.

        Best-effort: any failure here logs + is swallowed; the
        investigation outcome is unaffected.
        """
        try:
            from kora_cli.audit.jsonl_sink import emit_audit
        except Exception as exc:
            logger.warning(
                "[kora.probe_wake_consumer] audit import failed: %r "
                "— investigation_completed row skipped",
                exc,
            )
            return

        meta = _reasoning_meta_from_result(reasoning_result)
        cost_usd = _compute_total_cost_usd(meta)
        duration_ms = int(
            (time.monotonic() - investigation_started_monotonic) * 1000
        )
        autofix_attempted = _autofix_attempted_during(
            caller_session_id=caller_session_id,
            since=investigation_started_at,
        )

        details: Dict[str, Any] = {
            "probe": probe,
            "issue_category": category,
            "severity": severity,
            "model_used": meta.get("model_used"),
            "input_tokens": meta.get("input_tokens"),
            "output_tokens": meta.get("output_tokens"),
            "cache_creation_input_tokens": meta.get(
                "cache_creation_input_tokens"
            ),
            "cache_read_input_tokens": meta.get("cache_read_input_tokens"),
            "total_cost_usd": cost_usd,
            "investigation_duration_ms": duration_ms,
            "investigation_summary_text": reasoning_text_for_dm,
            "dm_status": dm_status,
            "autofix_attempted": autofix_attempted,
        }
        if reasoning_error is not None:
            details["reasoning_error"] = reasoning_error

        try:
            emit_audit(
                "probe.investigation_completed",
                details,
                caller_session_id=caller_session_id,
                source="reasoning",
            )
        except Exception as exc:
            logger.warning(
                "[kora.probe_wake_consumer] emit_audit raised %r — "
                "investigation_completed row skipped",
                exc,
            )


# ---------------------------------------------------------------------------
# Formatters (pure functions — easy to test in isolation)
# ---------------------------------------------------------------------------


_SEVERITY_EMOJI = {
    "critical": "🚨",
    "warning": "⚠️",
    "info": "ℹ️",
}


def format_investigation_prompt(event_details: Dict[str, Any]) -> str:
    """Build the prompt the reasoning engine receives.

    Structured around what the engine needs: name of the probe, the
    issue category + severity, the raw probe details, envelope
    posture (diagnose-only vs autofix-enabled), and an instruction
    to keep the response operator-friendly + Slack-DM-sized.
    """
    probe = event_details.get("probe") or "unknown"
    category = event_details.get("category") or "unknown"
    severity = event_details.get("severity") or "warning"
    title = event_details.get("title") or "(no title)"
    detail = event_details.get("detail") or "(no detail)"
    snapshot_details = event_details.get("snapshot_details") or {}
    envelope_enabled = bool(event_details.get("envelope_enabled", False))
    envelope_fix_name = (
        str(event_details.get("envelope_fix_name") or "(none)")
    )

    envelope_line = (
        f"Available fix-attempt envelope: {envelope_fix_name} (ENABLED)"
        if envelope_enabled and envelope_fix_name != "(none)"
        else "Available fix-attempt envelope: none (diagnose-only mode)"
    )

    lines = [
        f"Probe alert: {probe} → {category} (severity: {severity})",
        "",
        f"Title: {title}",
        f"Detail: {detail}",
        "",
        f"Snapshot details: {snapshot_details}",
        "",
        envelope_line,
        "",
        "Investigate the alert and propose the next action(s). The",
        "response is sent verbatim to the operator as a Slack DM —",
        "keep it concise (2-4 sentences for diagnosis + 1 line for",
        "recommended next step). Use plain text; no markdown headers.",
    ]
    return "\n".join(lines)


def format_operator_dm(
    *, probe: str, severity: str, reasoning_text: str
) -> str:
    """Build the Slack DM body the operator receives.

    Header line carries the alert emoji + probe name; reasoning
    text is the body verbatim. No cost footer in v1 — the cockpit
    telemetry panel surfaces per-call cost via the
    probe_investigation route.
    """
    emoji = _SEVERITY_EMOJI.get(severity, "🔔")
    return f"{emoji} Probe alert · {probe}\n{reasoning_text}"


def format_fallback_text(
    event_details: Dict[str, Any], *, reason: str
) -> str:
    """When reasoning fails, send the issue details verbatim + the
    failure reason. Operator still gets actionable signal — the
    issue itself is visible (it came from the cheap-cron path)
    even when reasoning can't run."""
    probe = event_details.get("probe") or "unknown"
    severity = event_details.get("severity") or "warning"
    title = event_details.get("title") or "(no title)"
    detail = event_details.get("detail") or "(no detail)"
    return (
        f"{probe} ({severity}): {title}\n"
        f"\n"
        f"{detail}\n"
        f"\n"
        f"I was unable to investigate — engine returned: {reason}"
    )


# ---------------------------------------------------------------------------
# Per-investigation helpers — KR-PROBE-INVESTIGATION-DATA-COMPLETION
# ---------------------------------------------------------------------------


def _reasoning_meta_from_result(result: Optional[Any]) -> Dict[str, Any]:
    """Project a ResponseResult into the 5-key meta dict the
    outbound log + investigation_completed audit share.

    Tolerant of partial attribute presence (test doubles may use
    MagicMock without setting every field). ``result is None``
    yields a dict of all-None values so the caller can pass them
    through to the audit / log without branching.
    """
    if result is None:
        return {
            "model_used": None,
            "input_tokens": None,
            "output_tokens": None,
            "cache_creation_input_tokens": None,
            "cache_read_input_tokens": None,
        }
    return {
        "model_used": getattr(result, "model_used", None) or None,
        "input_tokens": getattr(result, "input_tokens", None),
        "output_tokens": getattr(result, "output_tokens", None),
        "cache_creation_input_tokens": getattr(
            result, "cache_creation_input_tokens", None
        ),
        "cache_read_input_tokens": getattr(
            result, "cache_read_input_tokens", None
        ),
    }


def _compute_total_cost_usd(meta: Dict[str, Any]) -> Optional[float]:
    """Compute the investigation's total cost in USD by calling the
    canonical :func:`agent.usage_pricing.estimate_usage_cost`.

    Returns ``None`` when:
      * model_used is missing (engine didn't run / canned fallback)
      * pricing lookup returns ``status="unknown"`` (model has no
        registered pricing — e.g. a custom OpenRouter slug)
      * pricing lookup returns ``status="included"`` (subscription
        route — cost is bundled in the operator's flat fee; the
        audit row encodes this as ``0.0`` so the panel can render
        "(included)" without a separate null-vs-zero check)

    We chose ``estimate_usage_cost`` over a ``cost_telemetry``
    snapshot query (the alternative the spec mentioned) for two
    reasons:
      1. The telemetry snapshot is aggregated across all calls in
         a window — there's no per-investigation row to fetch.
         Picking "the most recent matching call" is racy under
         concurrent investigations.
      2. ``estimate_usage_cost`` is the SAME calculation
         ``agent.cost_state_holder.record_inference`` runs to bill
         the cost-ladder. Reusing it keeps the audit row in
         lockstep with the holder's accounting (operator can
         reconcile audit-sum-by-day against the daily-spend rung
         without rounding drift).
    """
    model = meta.get("model_used")
    if not model:
        return None
    try:
        from agent.usage_pricing import CanonicalUsage, estimate_usage_cost
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "[kora.probe_wake_consumer] usage_pricing import failed: %r",
            exc,
        )
        return None

    def _int_or_zero(key: str) -> int:
        value = meta.get(key)
        if isinstance(value, int):
            return value
        return 0

    usage = CanonicalUsage(
        input_tokens=_int_or_zero("input_tokens"),
        output_tokens=_int_or_zero("output_tokens"),
        cache_read_tokens=_int_or_zero("cache_read_input_tokens"),
        cache_write_tokens=_int_or_zero("cache_creation_input_tokens"),
    )
    try:
        result = estimate_usage_cost(str(model), usage)
    except Exception as exc:
        logger.warning(
            "[kora.probe_wake_consumer] estimate_usage_cost raised %r "
            "model=%s — total_cost_usd recorded as None",
            exc,
            model,
        )
        return None
    if result.status == "unknown":
        return None
    if result.amount_usd is None:
        return None
    # Decimal → float at the audit boundary. JSON can't carry
    # Decimal natively and the cost panel reads as float anyway.
    return float(result.amount_usd)


def _autofix_attempted_during(
    *, caller_session_id: str, since: datetime
) -> bool:
    """Back-reference: did ``tool.probe_autofix_attempted`` fire
    with the same ``caller_session_id`` since the investigation
    started?

    Reads via :func:`kora_cli.audit.jsonl_reader.read_audit_entries`
    filtered by seam + time window, then matches the
    ``caller_session_id`` field. Fail-soft on any reader error
    (returns False — operator triage still gets the rest of the
    row).

    Cost: the window is per-investigation (seconds, typically
    sub-second); reading + filtering the audit JSONL within that
    window is O(N_recent_rows) which is bounded by the probe-wake
    debounce + autofix-cap cadence. No race risk vs. concurrent
    autofix emits since we only look at rows already on disk.
    """
    try:
        from kora_cli.audit.jsonl_reader import read_audit_entries
    except Exception:
        return False
    try:
        entries = read_audit_entries(
            seam="tool.probe_autofix_attempted",
            since=since,
        )
    except Exception as exc:
        logger.warning(
            "[kora.probe_wake_consumer] autofix back-reference read "
            "raised %r — autofix_attempted recorded as False",
            exc,
        )
        return False
    return any(
        getattr(e, "caller_session_id", None) == caller_session_id
        for e in entries
    )
