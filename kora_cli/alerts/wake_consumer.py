"""Alert wake-event CONSUMER — KR-ALERT-INVESTIGATION-WAKE-CONSUMER.

Activates the ``alert_investigation`` cost-telemetry route literal
end-to-end. Parallels :mod:`kora_cli.probes.wake_consumer` (#166)
for alert events: tails the ``notification.dispatched`` audit seam
(#149 KR-ALERT-NOTIFY) → invokes reasoning with alert context →
DMs operator with the investigation result → emits the new
``alert.investigation_completed`` audit seam.

# Wake trigger

The alert notifier already emits ``notification.dispatched`` rows
per alert dispatched (#149); per-alert rows carry ``alert_id`` /
``severity`` / ``category`` / ``channel`` / ``status`` (success/
fail). Burst-summary + digest rows use synthetic alert_ids
(``burst:N`` / ``digest:N``) — those are skipped (they're
aggregates, not single alerts worth investigating).

# 4-stream join precedent (matches probe wake consumer)

  1. ``notification.dispatched`` — alert emitted (existing)
  2. ``alert.investigation_completed`` — investigation done (THIS PR)
  3. ``slack_dm_log.jsonl`` entry — DM with investigation summary
  4. (future) ``tool.alert_autoresolve_attempted`` — when alert
     envelope auto-actions get built; reserved seam, not emitted v1

CC#2 follow-on (KR-FE-ALERT-INVESTIGATIONS-VIEWER) joins the
4 streams via ``caller_session_id = "alert:{category}:{severity}"``.

# Debounce policy

Inline: per (category, severity) → datetime of last dispatched
investigation. Default 10 min window via
``KORA_ALERT_WAKE_DEBOUNCE_SECONDS``. Critical alerts can
optionally bypass via ``KORA_ALERT_WAKE_DEBOUNCE_BYPASS_CRITICAL``
(default false — fail-closed; even critical wakes debounce
unless operator opts in).

# Fail-soft

Every external dependency is fail-soft:
  * Engine None / engine raises → fallback DM with verbatim alert + reason
  * Slack client None / channel_id unset → log warning + outbound-log entry
  * Telemetry / audit record failures → log + continue (DM still sent)

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


DEBOUNCE_SECONDS_ENV = "KORA_ALERT_WAKE_DEBOUNCE_SECONDS"
DEFAULT_DEBOUNCE_SECONDS = 600  # 10 min; matches probe wake consumer default

BYPASS_CRITICAL_ENV = "KORA_ALERT_WAKE_DEBOUNCE_BYPASS_CRITICAL"
JOSHUA_SLACK_USER_ID_ENV = "KORA_SLACK_JOSHUA_USER_ID"


# Per-channel filter for the wake trigger. Per-alert rows ride
# either "slack" or "email"; the burst-summary + digest emits use
# synthetic "burst:N" / "digest:N" alert_ids on the same seam and
# are skipped at the consumer level.
_PER_ALERT_CHANNEL_VALUES = frozenset({"slack", "email"})


def _read_debounce_seconds() -> int:
    raw = os.environ.get(DEBOUNCE_SECONDS_ENV, "").strip()
    if not raw:
        return DEFAULT_DEBOUNCE_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "[kora.alert_wake_consumer] %s=%r is not numeric; using "
            "default %ds",
            DEBOUNCE_SECONDS_ENV,
            raw,
            DEFAULT_DEBOUNCE_SECONDS,
        )
        return DEFAULT_DEBOUNCE_SECONDS
    if value < 0:
        return DEFAULT_DEBOUNCE_SECONDS
    return value


def _read_bypass_critical() -> bool:
    raw = os.environ.get(BYPASS_CRITICAL_ENV, "").strip().lower()
    return raw in {"true", "1", "yes", "on"}


# ---------------------------------------------------------------------------
# Outcome (telemetry + test surface)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AlertWakeOutcome:
    """One per-event outcome. Bundled for tests + listener telemetry."""

    alert_id: str
    category: str
    severity: str
    dispatched: bool
    reasoning_invoked: bool
    dm_sent: bool
    debounce_skipped: bool = False
    # ``filtered_skipped`` distinguishes "not a per-alert row"
    # (burst / digest) from "debounced or dispatched". Lets the
    # listener telemetry break out aggregate-vs-actual.
    filtered_skipped: bool = False
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# AlertWakeConsumer
# ---------------------------------------------------------------------------


SlackClientFactory = Callable[[], Optional[Any]]
ReasoningEngineFactory = Callable[[], Optional[Any]]


class AlertWakeConsumer:
    """Per-event handler for ``notification.dispatched`` rows.

    Stateful only via the debounce map (per (category, severity);
    NOT per alert_id because most categories include the source-rule
    id in the alert_id and operator wants one investigation per
    distinct category-x-severity combination, not per identical
    re-fire).
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
        self._last_dispatched: Dict[Tuple[str, str], datetime] = {}

    @property
    def debounce_map_size(self) -> int:
        return len(self._last_dispatched)

    def reset_debounce_state(self) -> None:
        """Clear the in-memory debounce map. Listener shutdown calls
        this so subsequent listener start sees a clean slate."""
        with self._debounce_lock:
            self._last_dispatched = {}

    # ------------------------------------------------------------------
    # Debounce
    # ------------------------------------------------------------------

    def _is_debounced(self, category: str, severity: str) -> bool:
        if severity == "critical" and _read_bypass_critical():
            return False
        window = _read_debounce_seconds()
        if window <= 0:
            return False
        with self._debounce_lock:
            last = self._last_dispatched.get((category, severity))
        if last is None:
            return False
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        return elapsed < window

    def _mark_dispatched(self, category: str, severity: str) -> None:
        with self._debounce_lock:
            self._last_dispatched[(category, severity)] = datetime.now(
                timezone.utc
            )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def consume_alert_event(
        self, event_details: Dict[str, Any]
    ) -> AlertWakeOutcome:
        """Process one ``notification.dispatched`` event.

        ``event_details`` is the ``details`` dict from the audit row
        per AlertNotifier's shape: ``channel`` / ``alert_id`` /
        ``severity`` / ``category`` / ``status`` (+ ``error`` on
        failed status).

        Fail-soft contract: every path either dispatches OR returns
        a structured outcome explaining why it didn't. Never raises.
        """
        alert_id = str(event_details.get("alert_id") or "")
        category = str(event_details.get("category") or "unknown")
        severity = str(event_details.get("severity") or "warning")
        channel = str(event_details.get("channel") or "")
        status = str(event_details.get("status") or "")

        # Filter aggregate emits (burst_summary / digest_email) +
        # rows for failed dispatches. Only investigate alerts that
        # actually reached the operator's surface.
        if channel not in _PER_ALERT_CHANNEL_VALUES:
            return AlertWakeOutcome(
                alert_id=alert_id,
                category=category,
                severity=severity,
                dispatched=False,
                reasoning_invoked=False,
                dm_sent=False,
                filtered_skipped=True,
            )
        if status != "ok":
            return AlertWakeOutcome(
                alert_id=alert_id,
                category=category,
                severity=severity,
                dispatched=False,
                reasoning_invoked=False,
                dm_sent=False,
                filtered_skipped=True,
            )

        if self._is_debounced(category, severity):
            logger.debug(
                "[kora.alert_wake_consumer] debounced category=%s "
                "severity=%s",
                category,
                severity,
            )
            return AlertWakeOutcome(
                alert_id=alert_id,
                category=category,
                severity=severity,
                dispatched=False,
                reasoning_invoked=False,
                dm_sent=False,
                debounce_skipped=True,
            )

        investigation_started_at = datetime.now(timezone.utc)
        investigation_started_monotonic = time.monotonic()
        caller_session_id = f"alert:{category}:{severity}"

        reasoning_text: str
        reasoning_invoked = False
        reasoning_error: Optional[str] = None
        reasoning_result: Optional[Any] = None
        engine = self._reasoning_engine_factory()
        if engine is None:
            reasoning_text = format_fallback_text(
                event_details, reason="engine_unavailable"
            )
            reasoning_error = "engine_unavailable"
            logger.warning(
                "[kora.alert_wake_consumer] reasoning engine unavailable; "
                "sending fallback DM category=%s",
                category,
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
                reasoning_text = format_fallback_text(
                    event_details,
                    reason=f"engine_exception:{type(exc).__name__}",
                )
                reasoning_error = f"engine_exception:{type(exc).__name__}"
                logger.warning(
                    "[kora.alert_wake_consumer] engine.respond raised "
                    "%r category=%s — sending fallback DM",
                    exc,
                    category,
                )
            else:
                reasoning_result = invocation_result
                if invocation_error is None:
                    reasoning_text = invocation_text
                    reasoning_invoked = True
                else:
                    reasoning_text = format_fallback_text(
                        event_details, reason=invocation_error
                    )
                    reasoning_error = invocation_error
                    logger.warning(
                        "[kora.alert_wake_consumer] engine returned "
                        "error=%s category=%s — sending fallback DM",
                        invocation_error,
                        category,
                    )

        # Stamp dispatched BEFORE attempting DM so a flapping Slack
        # client can't trigger duplicate investigations in the next
        # cycle (probe wake consumer precedent).
        self._mark_dispatched(category, severity)

        dm_outcome = await self._send_operator_dm_routed(
            category=category,
            severity=severity,
            text=reasoning_text,
            caller_session_id=caller_session_id,
            reasoning_result=reasoning_result,
            reasoning_error=reasoning_error,
            investigation_started_monotonic=investigation_started_monotonic,
        )
        dm_sent = dm_outcome["dm_sent"]
        dm_status = dm_outcome["dm_status"]

        self._emit_investigation_completed(
            alert_id=alert_id,
            category=category,
            severity=severity,
            caller_session_id=caller_session_id,
            reasoning_result=reasoning_result,
            reasoning_error=reasoning_error,
            reasoning_text_for_dm=reasoning_text,
            dm_status=dm_status,
            investigation_started_monotonic=investigation_started_monotonic,
        )

        return AlertWakeOutcome(
            alert_id=alert_id,
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
        """Build the IncomingMessage with ``source="alert_investigation"``,
        call engine.respond. Telemetry route attribution fires inside
        the engine — :func:`_record_call_to_telemetry` maps the
        source to ``ROUTE_ALERT_INVESTIGATION`` (per #190's wire).
        """
        from kora_cli.reasoning.engine import (
            ConversationContext,
            IncomingMessage,
        )

        message = IncomingMessage(
            text=format_investigation_prompt(event_details),
            source="alert_investigation",
            received_at=datetime.now(timezone.utc),
            metadata={
                "alert_id": event_details.get("alert_id") or "unknown",
                "category": event_details.get("category") or "unknown",
                "severity": event_details.get("severity") or "warning",
                "channel": event_details.get("channel") or "unknown",
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
        category: str,
        severity: str,
        text: str,
        caller_session_id: str,
        reasoning_result: Optional[Any],
        reasoning_error: Optional[str],
        investigation_started_monotonic: float,
    ) -> Dict[str, Any]:
        """Mirror of probe wake consumer's _send_operator_dm_routed.

        Returns ``{"dm_sent": bool, "dm_status": str}`` with the
        same vocabulary as probe wake consumer so CC#2's viewers
        can share the dm_status enum.
        """
        client = self._slack_client_factory()
        channel_id = self._operator_channel_id_resolver()
        is_fallback = reasoning_error is not None

        if client is None or not channel_id:
            if client is None:
                logger.warning(
                    "[kora.alert_wake_consumer] slack_client_unavailable; "
                    "DM not sent category=%s",
                    category,
                )
            else:
                logger.warning(
                    "[kora.alert_wake_consumer] %s unset; DM not sent "
                    "category=%s",
                    JOSHUA_SLACK_USER_ID_ENV,
                    category,
                )
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
            category=category, severity=severity, reasoning_text=text
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
                "[kora.alert_wake_consumer] post_dm raised %r "
                "category=%s",
                exc,
                category,
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
        try:
            from kora_cli.handlers.slack_dm_handler import (
                append_outbound_log_entry,
                resolve_slack_dm_log_path,
            )
        except Exception as exc:
            logger.warning(
                "[kora.alert_wake_consumer] outbound log import failed: "
                "%r — slack_dm_log entry skipped",
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
                "[kora.alert_wake_consumer] outbound log write raised "
                "%r — investigation continues",
                exc,
            )

    def _emit_investigation_completed(
        self,
        *,
        alert_id: str,
        category: str,
        severity: str,
        caller_session_id: str,
        reasoning_result: Optional[Any],
        reasoning_error: Optional[str],
        reasoning_text_for_dm: str,
        dm_status: str,
        investigation_started_monotonic: float,
    ) -> None:
        try:
            from kora_cli.audit.jsonl_sink import emit_audit
        except Exception as exc:
            logger.warning(
                "[kora.alert_wake_consumer] audit import failed: %r "
                "— investigation_completed row skipped",
                exc,
            )
            return

        meta = _reasoning_meta_from_result(reasoning_result)
        cost_usd = _compute_total_cost_usd(meta)
        duration_ms = int(
            (time.monotonic() - investigation_started_monotonic) * 1000
        )

        details: Dict[str, Any] = {
            "alert_id": alert_id,
            "category": category,
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
            # Reserved for the future alert-envelope autoaction
            # parallel to probe autofix. v1 always false; the seam
            # field is in the payload now so consumers don't have
            # to branch on presence later.
            "autoaction_attempted": False,
        }
        if reasoning_error is not None:
            details["reasoning_error"] = reasoning_error

        try:
            emit_audit(
                "alert.investigation_completed",
                details,
                caller_session_id=caller_session_id,
                source="reasoning",
            )
        except Exception as exc:
            logger.warning(
                "[kora.alert_wake_consumer] emit_audit raised %r — "
                "alert.investigation_completed row skipped",
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

    Mirrors the probe wake consumer's structure: alert identity +
    severity + category, then an instruction to keep the response
    operator-friendly + Slack-DM-sized. Alert events DON'T carry
    title/detail in the audit row (the AlertNotifier audit shape
    is alert_id/severity/category/channel/status only) so the
    prompt asks the engine to reason from the categorical signal
    alone, augmented by whatever the engine pulls via tools.
    """
    alert_id = event_details.get("alert_id") or "unknown"
    category = event_details.get("category") or "unknown"
    severity = event_details.get("severity") or "warning"
    channel = event_details.get("channel") or "unknown"

    lines = [
        f"Alert dispatched: {category} (severity: {severity})",
        f"Alert id: {alert_id}",
        f"Dispatched via: {channel}",
        "",
        "Investigate what triggered this alert and propose the next",
        "action(s). The response is sent verbatim to the operator as",
        "a Slack DM — keep it concise (2-4 sentences for diagnosis +",
        "1 line for recommended next step). Use plain text; no",
        "markdown headers.",
    ]
    return "\n".join(lines)


def format_operator_dm(
    *, category: str, severity: str, reasoning_text: str
) -> str:
    """Slack DM body the operator receives. Header carries the
    severity emoji + category; body is reasoning text verbatim."""
    emoji = _SEVERITY_EMOJI.get(severity, "🔔")
    return f"{emoji} Alert · {category}\n{reasoning_text}"


def format_fallback_text(
    event_details: Dict[str, Any], *, reason: str
) -> str:
    """When reasoning fails, send the alert details verbatim + a
    clear "review and act manually" footer. Operator still gets
    actionable signal — the alert itself (category + severity +
    alert_id) is visible even when reasoning can't run.

    KR-CC1-POLISH (#198): mirrors the probe wake consumer's
    fallback shape (#184) — header line with alert identity, then
    a footer line that surfaces (a) the engine's failure reason
    and (b) explicit "act manually" guidance so the operator
    isn't left wondering whether Kora is going to retry.
    """
    category = event_details.get("category") or "unknown"
    severity = event_details.get("severity") or "warning"
    alert_id = event_details.get("alert_id") or "unknown"
    channel = event_details.get("channel") or "unknown"
    return (
        f"{category} ({severity}): alert id {alert_id} "
        f"(via {channel})\n"
        f"\n"
        f"Kora is unavailable to investigate this alert "
        f"(engine returned: {reason}). Review the alerts panel "
        f"and act manually — Kora will not retry this "
        f"investigation."
    )


# ---------------------------------------------------------------------------
# Per-investigation helpers — shape mirrors probe wake consumer's
# _reasoning_meta_from_result + _compute_total_cost_usd so a future
# refactor can extract them into a shared module without divergence.
# ---------------------------------------------------------------------------


def _reasoning_meta_from_result(result: Optional[Any]) -> Dict[str, Any]:
    """Project a ResponseResult into the 5-key meta dict shared
    between outbound log + investigation_completed audit."""
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
    """Compute the investigation's total cost via the canonical
    pricing helper. Returns ``None`` on unknown-model / pricing-
    miss paths — consumers render "—" in those cells."""
    model = meta.get("model_used")
    if not model:
        return None
    try:
        from agent.usage_pricing import CanonicalUsage, estimate_usage_cost
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "[kora.alert_wake_consumer] usage_pricing import failed: %r",
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
            "[kora.alert_wake_consumer] estimate_usage_cost raised %r "
            "model=%s — total_cost_usd recorded as None",
            exc,
            model,
        )
        return None
    if result.status == "unknown":
        return None
    if result.amount_usd is None:
        return None
    return float(result.amount_usd)
