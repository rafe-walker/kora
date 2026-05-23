"""Alert push-notification — KR-ALERT-NOTIFY ST1.

Closes the operator-feedback loop: when a critical/warning alert
fires, Kora pings Joshua via Slack DM; info alerts go to email.
Joshua doesn't have to LOOK at the cockpit.

# Architecture

  1. Periodic task (from the heartbeat scheduler @ 3min default
     cadence) calls :meth:`AlertNotifier.run_notification_cycle`.
  2. That method calls
     :func:`kora_cli.alerts.aggregator.compute_active_alerts` and
     computes set-diff against ``last_alert_ids`` from the
     previous cycle.
  3. For each newly-firing alert, dispatch to the channel matched
     by severity (rules below).
  4. After the cycle, ``last_alert_ids = active_ids`` so a still-
     firing alert doesn't re-notify next cycle.

# Channel rules (PM-default Q2)

  - ``critical`` → Slack DM (immediate, operator action needed)
  - ``warning`` → Slack DM (operator should know soon)
  - ``info`` → email (not action-required; v1 fires immediately,
    digest-shaping deferred to ST2)

# Dedup semantics (PM-default Q3)

``last_alert_ids`` is in-memory ONLY. Daemon restart starts with
an empty set, so all currently-active alerts get notified on the
first cycle as "new" — operator gets re-pinged on every restart.
Persistence across restarts is a future bucket if the re-ping
becomes annoying.

# Failure semantics

Send failures DO NOT cause re-notify on the next cycle. The
alert ID enters ``last_alert_ids`` regardless of send outcome —
this prevents spam if Slack/SMTP are flapping. The audit JSONL
records the failure so the operator can triage via the audit
panel if they were expecting a notification that didn't arrive.

Trade-off: a single transient Slack 429 means the operator
might miss that alert. ST2's per-category cooldown + the
operator runbook addendum cover this gap with a different
mechanism (the alert STILL shows in the cockpit; notification
is a "push" convenience, not a delivery guarantee).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional, Set

from kora_cli.alerts.aggregator import Alert, compute_active_alerts

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Channel routing
# ---------------------------------------------------------------------------


# Severity → channel. Documented in module docstring.
_CHANNEL_SLACK = "slack_dm"
_CHANNEL_EMAIL = "email"

_SEVERITY_TO_CHANNEL = {
    "critical": _CHANNEL_SLACK,
    "warning": _CHANNEL_SLACK,
    "info": _CHANNEL_EMAIL,
}

# Severity → emoji prefix in Slack DM body
_SEVERITY_EMOJI = {
    "critical": "🔴",
    "warning": "🟡",
    "info": "🔵",
}


# ---------------------------------------------------------------------------
# Envs
# ---------------------------------------------------------------------------


JOSHUA_SLACK_USER_ID_ENV = "KORA_SLACK_JOSHUA_USER_ID"
JOSHUA_EMAIL_ADDRESS_ENV = "KORA_EMAIL_JOSHUA_ADDRESS"
KORA_EMAIL_FROM_ADDRESS_ENV = "KORA_EMAIL_KORA_ADDRESS"
COCKPIT_URL_ENV = "KORA_COCKPIT_URL"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """One per dispatch attempt. Bundled into :class:`NotificationCycleResult`."""

    alert_id: str
    severity: str
    channel: str
    success: bool
    error: Optional[str] = None


@dataclass(frozen=True, slots=True)
class NotificationCycleResult:
    """Summary of one ``run_notification_cycle`` call.

    Surfaces cycle telemetry for the audit log + operator triage.
    Empty cycles (no newly-firing alerts) return zeros across the
    board.
    """

    active_count: int
    newly_firing_count: int
    newly_resolved_count: int
    slack_dispatched: int
    email_dispatched: int
    dispatch_errors: int
    outcomes: List[DispatchOutcome] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Formatting helpers (pure functions — easy to test in isolation)
# ---------------------------------------------------------------------------


def _format_relative_time(at_iso: str, *, now: Optional[datetime] = None) -> str:
    """Human-readable relative time. Falls back to the raw ISO on parse failure."""
    if not at_iso:
        return "(unknown)"
    try:
        ts = datetime.fromisoformat(at_iso.replace("Z", "+00:00"))
    except ValueError:
        return at_iso
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    delta = (now - ts).total_seconds()
    if delta < 0:
        return at_iso  # future-dated; surface raw
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def format_slack_dm_text(alert: Alert, *, now: Optional[datetime] = None) -> str:
    """Slack DM body per bucket §2(c)."""
    emoji = _SEVERITY_EMOJI.get(alert.severity, "🔵")
    relative = _format_relative_time(alert.first_seen_at, now=now)
    return (
        f"{emoji} [{alert.severity.upper()}] Kora alert\n"
        f"{alert.title}\n"
        f"\n"
        f"{alert.detail}\n"
        f"\n"
        f"Source: {alert.source_panel_route}\n"
        f"First seen: {relative}"
    )


def format_email_subject(alert: Alert) -> str:
    """Email subject per bucket §2(d)."""
    return f"[Kora alert] {alert.severity}: {alert.title}"


def format_email_body(alert: Alert) -> str:
    """Email body per bucket §2(d). ``KORA_COCKPIT_URL`` env appends
    a cockpit link when configured; otherwise omitted."""
    cockpit_url = os.environ.get(COCKPIT_URL_ENV, "").strip()
    cockpit_block = f"\n\nCockpit URL: {cockpit_url}" if cockpit_url else ""
    return (
        f"{alert.detail}\n"
        f"\n"
        f"Source panel: {alert.source_panel_route}\n"
        f"First seen: {alert.first_seen_at}"
        f"{cockpit_block}"
    )


# ---------------------------------------------------------------------------
# AlertNotifier
# ---------------------------------------------------------------------------


SlackClientFactory = Callable[[], Optional[Any]]
PurelymailClientFactory = Callable[[], Optional[Any]]


class AlertNotifier:
    """Periodic alert push-notifier with set-diff dedup.

    Constructed by the listener with lazy factories for the
    SlackClient + PurelymailClient — they may not be initialized
    yet when the listener boots (fail-soft pattern). The factory
    is called once per cycle so a client that comes up mid-runtime
    starts being usable immediately.
    """

    def __init__(
        self,
        *,
        slack_client_factory: SlackClientFactory,
        purelymail_client_factory: PurelymailClientFactory,
        compute_alerts: Callable[[], List[Alert]] = compute_active_alerts,
    ) -> None:
        self._slack_client_factory = slack_client_factory
        self._purelymail_client_factory = purelymail_client_factory
        self._compute_alerts = compute_alerts
        # Last cycle's active alert ids. Empty at construction →
        # the first cycle treats all current alerts as newly-firing
        # (PM Q3 ruling: fire on first cycle; persistence is future).
        self._last_alert_ids: Set[str] = set()

    @property
    def last_alert_ids(self) -> Set[str]:
        """Read-only view for tests + telemetry."""
        return set(self._last_alert_ids)

    def reset_dedup_state(self) -> None:
        """Clear the in-memory dedup set. Listener shutdown calls
        this so a subsequent listener start sees a clean slate."""
        self._last_alert_ids = set()

    async def run_notification_cycle(self) -> NotificationCycleResult:
        """One cycle: compute active alerts → diff → dispatch new fires.

        Fail-soft: any exception inside the cycle is caught + logged;
        an empty :class:`NotificationCycleResult` is returned so the
        heartbeat scheduler doesn't crash.
        """
        try:
            alerts = list(self._compute_alerts())
        except Exception as exc:
            logger.warning(
                "[kora.alert_notifier] compute_active_alerts raised %r — "
                "skipping cycle",
                exc,
            )
            return NotificationCycleResult(
                active_count=0,
                newly_firing_count=0,
                newly_resolved_count=0,
                slack_dispatched=0,
                email_dispatched=0,
                dispatch_errors=0,
                outcomes=[],
            )

        active_ids = {a.id for a in alerts}
        newly_firing_ids = active_ids - self._last_alert_ids
        newly_resolved_ids = self._last_alert_ids - active_ids

        # Order new-fires by severity (critical first) for predictable
        # dispatch sequence. Reuse the aggregator's sort key shape.
        severity_rank = {"critical": 0, "warning": 1, "info": 2}
        new_fires = [a for a in alerts if a.id in newly_firing_ids]
        new_fires.sort(
            key=lambda a: (severity_rank.get(a.severity, 99), a.id)
        )

        outcomes: List[DispatchOutcome] = []
        slack_dispatched = 0
        email_dispatched = 0
        dispatch_errors = 0
        for alert in new_fires:
            outcome = await self._dispatch_alert(alert)
            outcomes.append(outcome)
            if outcome.success:
                if outcome.channel == _CHANNEL_SLACK:
                    slack_dispatched += 1
                elif outcome.channel == _CHANNEL_EMAIL:
                    email_dispatched += 1
            else:
                dispatch_errors += 1

        # Update dedup state AFTER dispatching — even if dispatch failed,
        # the alert ID enters last_alert_ids to prevent re-notify spam
        # on the next cycle. Audit captures the failure for triage.
        self._last_alert_ids = active_ids

        if newly_resolved_ids:
            logger.info(
                "[kora.alert_notifier] %d alert(s) resolved this cycle: %s",
                len(newly_resolved_ids),
                sorted(newly_resolved_ids),
            )

        return NotificationCycleResult(
            active_count=len(active_ids),
            newly_firing_count=len(newly_firing_ids),
            newly_resolved_count=len(newly_resolved_ids),
            slack_dispatched=slack_dispatched,
            email_dispatched=email_dispatched,
            dispatch_errors=dispatch_errors,
            outcomes=outcomes,
        )

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def _dispatch_alert(self, alert: Alert) -> DispatchOutcome:
        """Route one alert to its channel. Records audit + returns
        outcome regardless of success/failure."""
        channel = _SEVERITY_TO_CHANNEL.get(alert.severity, _CHANNEL_SLACK)
        try:
            if channel == _CHANNEL_SLACK:
                await self._send_slack_dm(alert)
            else:
                await self._send_email(alert)
        except Exception as exc:
            err_text = f"{type(exc).__name__}"
            logger.warning(
                "[kora.alert_notifier] dispatch failed alert=%s channel=%s: %r",
                alert.id,
                channel,
                exc,
            )
            self._emit_audit(alert, channel=channel, status="failed", error=err_text)
            return DispatchOutcome(
                alert_id=alert.id,
                severity=alert.severity,
                channel=channel,
                success=False,
                error=err_text,
            )

        self._emit_audit(alert, channel=channel, status="ok", error=None)
        return DispatchOutcome(
            alert_id=alert.id,
            severity=alert.severity,
            channel=channel,
            success=True,
        )

    async def _send_slack_dm(self, alert: Alert) -> None:
        """Format + send via the live SlackClient. Raises on failure
        (caller catches + records audit)."""
        client = self._slack_client_factory()
        if client is None:
            raise RuntimeError("slack_client_unavailable")

        joshua_user_id = os.environ.get(
            JOSHUA_SLACK_USER_ID_ENV, ""
        ).strip()
        if not joshua_user_id:
            raise RuntimeError("joshua_slack_user_id_unset")

        text = format_slack_dm_text(alert)
        # Slack's chat.postMessage auto-resolves the bot's DM channel
        # for a user-id passed as channel_id — same approach the MCP
        # kora__send_slack_dm tool uses (mcp_tools.py:1119).
        await client.post_dm(channel_id=joshua_user_id, text=text)

    async def _send_email(self, alert: Alert) -> None:
        """Format + send via the live PurelymailClient. Raises on failure."""
        client = self._purelymail_client_factory()
        if client is None:
            raise RuntimeError("purelymail_client_unavailable")

        joshua_email = os.environ.get(JOSHUA_EMAIL_ADDRESS_ENV, "").strip()
        if not joshua_email:
            raise RuntimeError("joshua_email_address_unset")

        from_addr = os.environ.get(KORA_EMAIL_FROM_ADDRESS_ENV, "").strip()
        if not from_addr:
            raise RuntimeError("kora_email_from_address_unset")

        subject = format_email_subject(alert)
        body_text = format_email_body(alert)
        result = await client.send_email(
            from_addr=from_addr,
            to=[joshua_email],
            subject=subject,
            body_text=body_text,
        )
        # The SMTP client returns a SendResult with status="failed" on
        # error rather than raising — surface that as a dispatch
        # failure so the audit log records the SMTP code.
        if getattr(result, "status", None) == "failed":
            raise RuntimeError(
                f"smtp_send_failed:{getattr(result, 'smtp_code', None)}"
            )

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def _emit_audit(
        self,
        alert: Alert,
        *,
        channel: str,
        status: str,
        error: Optional[str],
    ) -> None:
        """Record one dispatch attempt in the audit JSONL.

        Fail-soft: an audit-write error must not propagate (caller
        is already inside dispatch try/except)."""
        try:
            from kora_cli.audit import emit_audit
        except Exception as exc:
            logger.warning(
                "[kora.alert_notifier] audit import failed: %r", exc
            )
            return
        details = {
            "channel": channel,
            "alert_id": alert.id,
            "severity": alert.severity,
            "category": alert.category,
            "status": status,
        }
        if error is not None:
            details["error"] = error
        try:
            emit_audit(
                seam="notification.dispatched",
                details=details,
                source=None,
            )
        except Exception as exc:
            logger.warning(
                "[kora.alert_notifier] emit_audit raised %r — continuing",
                exc,
            )
