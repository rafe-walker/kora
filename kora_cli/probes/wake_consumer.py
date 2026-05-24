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

    @property
    def debounce_map_size(self) -> int:
        """Read-only view for tests + telemetry."""
        return len(self._last_dispatched)

    def reset_debounce_state(self) -> None:
        """Clear the in-memory debounce map. Listener shutdown calls
        this so subsequent listener start sees a clean slate.
        Mirrors :meth:`AlertNotifier.reset_dedup_state` (PR #149)."""
        with self._debounce_lock:
            self._last_dispatched = {}

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

        # Resolve engine + invoke reasoning. Failures fall through to
        # fallback-DM path.
        reasoning_text: str
        reasoning_invoked: bool = False
        reasoning_error: Optional[str] = None
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
                invocation_text, invocation_error = await self._invoke_reasoning(
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

        dm_sent = await self._send_operator_dm(
            probe=probe,
            severity=severity,
            text=reasoning_text,
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
    ) -> Tuple[str, Optional[str]]:
        """Build the IncomingMessage + ConversationContext, call
        engine.respond, return ``(text, error_code)``.

        ``error_code`` is ``None`` on success (text is the engine's
        response). On engine-returned-error (ResponseResult.error
        set), ``error_code`` is the engine's stable error code
        verbatim ("cost_ladder_halted" / "sdk_5xx" / etc) + ``text``
        is empty. On empty-text-success, ``error_code`` is
        ``"empty_response_text"``.

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
            return ("", str(engine_error))
        text = getattr(result, "text", "") or ""
        if not text.strip():
            return ("", "empty_response_text")
        return (text, None)

    # ------------------------------------------------------------------
    # Outbound DM
    # ------------------------------------------------------------------

    async def _send_operator_dm(
        self, *, probe: str, severity: str, text: str
    ) -> bool:
        """Send the DM. Returns True on success, False otherwise."""
        client = self._slack_client_factory()
        if client is None:
            logger.warning(
                "[kora.probe_wake_consumer] slack_client_unavailable; "
                "DM not sent probe=%s",
                probe,
            )
            return False
        channel_id = self._operator_channel_id_resolver()
        if not channel_id:
            logger.warning(
                "[kora.probe_wake_consumer] %s unset; DM not sent "
                "probe=%s",
                JOSHUA_SLACK_USER_ID_ENV,
                probe,
            )
            return False

        dm_text = format_operator_dm(
            probe=probe, severity=severity, reasoning_text=text
        )
        try:
            await client.post_dm(channel_id=channel_id, text=dm_text)
        except Exception as exc:
            logger.warning(
                "[kora.probe_wake_consumer] post_dm raised %r probe=%s",
                exc,
                probe,
            )
            return False
        return True


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
