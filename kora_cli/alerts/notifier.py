"""Alert push-notification — KR-ALERT-NOTIFY ST1 + ST2.

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
  3. ST2 throttling stack applied in order: per-category cooldown
     filter → digest-mode split → burst dampening.
  4. For each dispatchable alert, route to channel by severity.
  5. After the cycle, ``last_alert_ids = active_ids``.

# Channel rules (ST1 PM-default Q2)

  - ``critical`` → Slack DM (immediate, operator action needed)
  - ``warning`` → Slack DM (operator should know soon)
  - ``info`` → email (not action-required)

# Dedup semantics (ST1 PM-default Q3)

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

# ST2 throttling stack

Three independently-controllable mechanisms layered on top of
ST1's set-diff dedup:

## Per-category cooldown
Default 30 min (``KORA_ALERT_NOTIFY_CATEGORY_COOLDOWN_SEC``).
Same alert ``category`` can't dispatch within the cooldown
window. Prevents storms — e.g. 5 service-unhealthy probes
flipping in/out over 30 min produce ONE notification (the
first), not 5. Cooldown is per-category, not per-alert-id, so
distinct alert ids within the same category share the window.

## Burst dampening
Default threshold 5 (``KORA_ALERT_NOTIFY_BURST_THRESHOLD``).
If >threshold newly-firing alerts pass the cooldown filter in
ONE cycle → send a single SUMMARY message instead of N
individual ones. The summary names the alerts + severities;
operator opens cockpit for details. Suppresses individual sends
INCLUDING criticals — spec §2 ST2 is firm on this. Trade-off
covered in the operator runbook: if a single cycle produces 6+
alerts the operator is in a degraded state already; one summary
ping is better than 6 separate buzzes.

## Daily digest mode
``KORA_ALERT_NOTIFY_MODE=digest`` (default ``immediate``).
Criticals still fire immediately via Slack. Warnings + info
get queued in memory; a separate scheduled task
(``alerts.digest_flush``) flushes the queue once per
``KORA_ALERT_NOTIFY_DIGEST_INTERVAL_SEC`` (default 86400 = 24h)
as ONE digest email grouped by severity + category.

The queue is in-memory only — daemon restart loses queued
non-critical alerts. They'll re-fire on the first cycle if
still active (PM Q3 default). Persistence is a future bucket.
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

# ST2 envs
CATEGORY_COOLDOWN_SEC_ENV = "KORA_ALERT_NOTIFY_CATEGORY_COOLDOWN_SEC"
BURST_THRESHOLD_ENV = "KORA_ALERT_NOTIFY_BURST_THRESHOLD"
MODE_ENV = "KORA_ALERT_NOTIFY_MODE"

DEFAULT_CATEGORY_COOLDOWN_SEC = 1800.0  # 30 min
DEFAULT_BURST_THRESHOLD = 5

MODE_IMMEDIATE = "immediate"
MODE_DIGEST = "digest"
_VALID_MODES = {MODE_IMMEDIATE, MODE_DIGEST}


def _read_category_cooldown_sec() -> float:
    raw = os.environ.get(CATEGORY_COOLDOWN_SEC_ENV, "").strip()
    if not raw:
        return DEFAULT_CATEGORY_COOLDOWN_SEC
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "[kora.alert_notifier] %s=%r is not numeric; using default %ss",
            CATEGORY_COOLDOWN_SEC_ENV,
            raw,
            DEFAULT_CATEGORY_COOLDOWN_SEC,
        )
        return DEFAULT_CATEGORY_COOLDOWN_SEC
    if value < 0:
        logger.warning(
            "[kora.alert_notifier] %s=%s must be ≥ 0; using default %ss",
            CATEGORY_COOLDOWN_SEC_ENV,
            value,
            DEFAULT_CATEGORY_COOLDOWN_SEC,
        )
        return DEFAULT_CATEGORY_COOLDOWN_SEC
    return value


def _read_burst_threshold() -> int:
    raw = os.environ.get(BURST_THRESHOLD_ENV, "").strip()
    if not raw:
        return DEFAULT_BURST_THRESHOLD
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "[kora.alert_notifier] %s=%r is not an integer; using "
            "default %d",
            BURST_THRESHOLD_ENV,
            raw,
            DEFAULT_BURST_THRESHOLD,
        )
        return DEFAULT_BURST_THRESHOLD
    if value < 1:
        logger.warning(
            "[kora.alert_notifier] %s=%d must be ≥ 1; using default %d",
            BURST_THRESHOLD_ENV,
            value,
            DEFAULT_BURST_THRESHOLD,
        )
        return DEFAULT_BURST_THRESHOLD
    return value


def _read_mode() -> str:
    raw = os.environ.get(MODE_ENV, "").strip().lower()
    if not raw:
        return MODE_IMMEDIATE
    if raw not in _VALID_MODES:
        logger.warning(
            "[kora.alert_notifier] %s=%r not in %s; defaulting to "
            "immediate",
            MODE_ENV,
            raw,
            sorted(_VALID_MODES),
        )
        return MODE_IMMEDIATE
    return raw


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

    ST2 telemetry fields default to 0 so existing ST1 callers /
    tests reading the older shape stay compatible.
    """

    active_count: int
    newly_firing_count: int
    newly_resolved_count: int
    slack_dispatched: int
    email_dispatched: int
    dispatch_errors: int
    outcomes: List[DispatchOutcome] = field(default_factory=list)
    # ST2 — throttling telemetry
    cooldown_suppressed: int = 0
    burst_summarized: int = 0
    digest_queued: int = 0


@dataclass(frozen=True, slots=True)
class DigestFlushResult:
    """Outcome of one ``flush_digest()`` call.

    Empty queue → all zeros + ``success=True`` (no-op flush).
    Non-empty queue → one email dispatch attempt; success/error
    reflected.
    """

    flushed_count: int
    success: bool
    error: Optional[str] = None


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


def format_burst_summary_text(alerts: List[Alert]) -> str:
    """ST2 burst-dampening summary message. ONE Slack DM in place of
    >threshold individual ones.

    Lists severity counts on the first line; followed by each alert
    on its own line (emoji + severity + title). Operator opens
    cockpit for details.
    """
    by_severity: Dict[str, int] = {"critical": 0, "warning": 0, "info": 0}
    for a in alerts:
        by_severity[a.severity] = by_severity.get(a.severity, 0) + 1
    parts = [
        f"🚨 [BURST] {len(alerts)} Kora alerts in one cycle "
        f"(critical={by_severity.get('critical', 0)} "
        f"warning={by_severity.get('warning', 0)} "
        f"info={by_severity.get('info', 0)})",
        "",
    ]
    for alert in alerts:
        emoji = _SEVERITY_EMOJI.get(alert.severity, "🔵")
        parts.append(
            f"{emoji} [{alert.severity.upper()}] {alert.title}"
        )
    cockpit_url = os.environ.get(COCKPIT_URL_ENV, "").strip()
    if cockpit_url:
        parts.append("")
        parts.append(f"Cockpit: {cockpit_url}")
    return "\n".join(parts)


def format_digest_subject(alerts: List[Alert]) -> str:
    """ST2 digest-mode subject. Single email per flush cycle."""
    by_severity: Dict[str, int] = {"critical": 0, "warning": 0, "info": 0}
    for a in alerts:
        by_severity[a.severity] = by_severity.get(a.severity, 0) + 1
    return (
        f"[Kora digest] {len(alerts)} alert(s): "
        f"{by_severity.get('warning', 0)} warning, "
        f"{by_severity.get('info', 0)} info"
    )


def format_digest_body(alerts: List[Alert]) -> str:
    """ST2 digest-mode body. Groups by severity (warning before
    info; criticals don't queue — they fire immediately). Within
    each severity group, alerts ordered by id for stable output."""
    parts = [
        f"Kora queued {len(alerts)} non-critical alert(s) since the "
        f"last digest flush. Criticals are dispatched immediately and "
        f"do not appear here.",
    ]
    severity_order = ["warning", "info"]
    for severity in severity_order:
        in_group = sorted(
            (a for a in alerts if a.severity == severity), key=lambda a: a.id
        )
        if not in_group:
            continue
        parts.append("")
        parts.append(f"== {severity.upper()} ({len(in_group)}) ==")
        for alert in in_group:
            parts.append(
                f"- {alert.title}\n"
                f"  category={alert.category} "
                f"first_seen={alert.first_seen_at} "
                f"source={alert.source_panel_route}"
            )
    cockpit_url = os.environ.get(COCKPIT_URL_ENV, "").strip()
    if cockpit_url:
        parts.append("")
        parts.append(f"Cockpit URL: {cockpit_url}")
    return "\n".join(parts)


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
        # ST2 — per-category cooldown timestamps (last successful or
        # attempted dispatch by category). Cleared on
        # reset_dedup_state alongside the alert-ids set.
        self._last_dispatched_by_category: Dict[str, datetime] = {}
        # ST2 — digest-mode queue. Non-critical alerts in digest mode
        # accumulate here until the digest_flush periodic task drains
        # them. Empty when mode is immediate.
        self._digest_queue: List[Alert] = []

    @property
    def last_alert_ids(self) -> Set[str]:
        """Read-only view for tests + telemetry."""
        return set(self._last_alert_ids)

    @property
    def digest_queue_size(self) -> int:
        """Number of alerts queued for the next digest flush. Always
        0 in immediate mode (queue stays empty)."""
        return len(self._digest_queue)

    def reset_dedup_state(self) -> None:
        """Clear the in-memory dedup set + ST2 cooldown / digest
        state. Listener shutdown calls this so a subsequent listener
        start sees a clean slate."""
        self._last_alert_ids = set()
        self._last_dispatched_by_category = {}
        self._digest_queue = []

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

        # ST2 throttling stack — applied in order.
        now = datetime.now(timezone.utc)

        # 1. Per-category cooldown filter
        cooldown_sec = _read_category_cooldown_sec()
        dispatchable, cooldown_suppressed = self._apply_cooldown_filter(
            new_fires, now=now, cooldown_sec=cooldown_sec
        )

        # 2. Digest-mode split (criticals always immediate; warning+info
        # enqueue for the digest flush task)
        mode = _read_mode()
        if mode == MODE_DIGEST:
            critical_subset = [
                a for a in dispatchable if a.severity == "critical"
            ]
            non_critical_subset = [
                a for a in dispatchable if a.severity != "critical"
            ]
            self._digest_queue.extend(non_critical_subset)
            digest_queued = len(non_critical_subset)
            dispatchable = critical_subset
        else:
            digest_queued = 0

        # 3. Burst dampening — above threshold, replace individual sends
        # with ONE summary message. Per spec §2 ST2: no severity carve-
        # out; criticals get summarized too. The summary names them all
        # + the runbook documents the trade-off.
        burst_threshold = _read_burst_threshold()
        outcomes: List[DispatchOutcome] = []
        slack_dispatched = 0
        email_dispatched = 0
        dispatch_errors = 0
        burst_summarized = 0
        if len(dispatchable) > burst_threshold:
            burst_summarized = len(dispatchable)
            outcome = await self._dispatch_burst_summary(dispatchable)
            outcomes.append(outcome)
            if outcome.success:
                slack_dispatched += 1
            else:
                dispatch_errors += 1
            # Stamp every category as recently-dispatched even on burst
            # path so individual category cooldowns activate (prevents
            # next-cycle storm of single-category re-fires).
            for alert in dispatchable:
                self._last_dispatched_by_category[alert.category] = now
        else:
            for alert in dispatchable:
                outcome = await self._dispatch_alert(alert)
                outcomes.append(outcome)
                if outcome.success:
                    if outcome.channel == _CHANNEL_SLACK:
                        slack_dispatched += 1
                    elif outcome.channel == _CHANNEL_EMAIL:
                        email_dispatched += 1
                else:
                    dispatch_errors += 1
                # Cooldown timestamp set on attempt — failure paths
                # still stamp so a flapping category doesn't retry
                # per cycle.
                self._last_dispatched_by_category[alert.category] = now

        # Update dedup state AFTER dispatching — even if dispatch failed,
        # the alert ID enters last_alert_ids to prevent re-notify spam
        # on the next cycle. Audit captures the failure for triage.
        self._last_alert_ids = active_ids

        # Clear cooldown stamps for resolved categories so a re-fire
        # after resolution doesn't get gated. The stamp's purpose is
        # "don't re-ping a still-active category"; once the alert is
        # gone the next fire of the same category is a legitimate new
        # event.
        active_categories = {a.category for a in alerts}
        self._last_dispatched_by_category = {
            cat: ts
            for cat, ts in self._last_dispatched_by_category.items()
            if cat in active_categories
        }

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
            cooldown_suppressed=cooldown_suppressed,
            burst_summarized=burst_summarized,
            digest_queued=digest_queued,
        )

    def _apply_cooldown_filter(
        self,
        alerts: List[Alert],
        *,
        now: datetime,
        cooldown_sec: float,
    ) -> tuple[List[Alert], int]:
        """Return ``(dispatchable, suppressed_count)``. Alerts whose
        ``category`` has been dispatched within ``cooldown_sec``
        seconds are dropped from the output. Cooldown=0 disables
        the filter (operator opts out)."""
        if cooldown_sec == 0:
            return list(alerts), 0
        dispatchable: List[Alert] = []
        suppressed = 0
        for alert in alerts:
            last_at = self._last_dispatched_by_category.get(alert.category)
            if last_at is not None:
                elapsed = (now - last_at).total_seconds()
                if elapsed < cooldown_sec:
                    suppressed += 1
                    logger.debug(
                        "[kora.alert_notifier] cooldown suppress "
                        "category=%s alert_id=%s elapsed=%.1fs",
                        alert.category,
                        alert.id,
                        elapsed,
                    )
                    continue
            dispatchable.append(alert)
        return dispatchable, suppressed

    async def _dispatch_burst_summary(
        self, alerts: List[Alert]
    ) -> DispatchOutcome:
        """One Slack DM summarizing all alerts in this burst. Used
        in place of N individual dispatches when newly-firing >
        burst threshold."""
        try:
            await self._send_slack_burst_summary(alerts)
        except Exception as exc:
            err_text = f"{type(exc).__name__}"
            logger.warning(
                "[kora.alert_notifier] burst-summary dispatch failed: %r",
                exc,
            )
            self._emit_audit_burst_summary(
                alerts, status="failed", error=err_text
            )
            return DispatchOutcome(
                alert_id=f"burst:{len(alerts)}",
                severity="critical",
                channel=_CHANNEL_SLACK,
                success=False,
                error=err_text,
            )

        self._emit_audit_burst_summary(alerts, status="ok", error=None)
        return DispatchOutcome(
            alert_id=f"burst:{len(alerts)}",
            severity="critical",
            channel=_CHANNEL_SLACK,
            success=True,
        )

    async def _send_slack_burst_summary(self, alerts: List[Alert]) -> None:
        """Send the burst-summary message via Slack."""
        client = self._slack_client_factory()
        if client is None:
            raise RuntimeError("slack_client_unavailable")
        joshua_user_id = os.environ.get(
            JOSHUA_SLACK_USER_ID_ENV, ""
        ).strip()
        if not joshua_user_id:
            raise RuntimeError("joshua_slack_user_id_unset")
        text = format_burst_summary_text(alerts)
        await client.post_dm(channel_id=joshua_user_id, text=text)

    def _emit_audit_burst_summary(
        self,
        alerts: List[Alert],
        *,
        status: str,
        error: Optional[str],
    ) -> None:
        """One audit row per burst-summary dispatch attempt."""
        try:
            from kora_cli.audit import emit_audit
        except Exception as exc:
            logger.warning(
                "[kora.alert_notifier] audit import failed: %r", exc
            )
            return
        details: Dict[str, Any] = {
            "channel": _CHANNEL_SLACK,
            "alert_id": f"burst:{len(alerts)}",
            "severity": "burst",
            "category": "burst_summary",
            "status": status,
            "burst_count": len(alerts),
            "burst_alert_ids": sorted(a.id for a in alerts),
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
                "[kora.alert_notifier] emit_audit (burst) raised %r — "
                "continuing",
                exc,
            )

    # ------------------------------------------------------------------
    # Digest flush (ST2)
    # ------------------------------------------------------------------

    async def flush_digest(self) -> DigestFlushResult:
        """Drain the digest queue into one email + send.

        No-op cases (return ``DigestFlushResult(0, True, None)``):
          - Queue empty (mode is immediate OR digest cycle had no
            queueable alerts)
          - Mode is immediate (queue would be empty anyway, but
            check defensively)

        Success path: format → send → clear queue → return result
        with ``flushed_count`` reflecting what was sent.

        Failure path: send raises → return ``success=False`` with
        error code; **queue is NOT cleared** so the next flush
        attempt has another chance. Audit emits one
        ``notification.dispatched`` row regardless of outcome.
        """
        mode = _read_mode()
        if mode != MODE_DIGEST or not self._digest_queue:
            return DigestFlushResult(flushed_count=0, success=True)

        queued = list(self._digest_queue)
        try:
            await self._send_email_digest(queued)
        except Exception as exc:
            err_text = f"{type(exc).__name__}"
            logger.warning(
                "[kora.alert_notifier] digest flush failed: %r — queue "
                "retained for next flush attempt",
                exc,
            )
            self._emit_audit_digest(
                queued, status="failed", error=err_text
            )
            return DigestFlushResult(
                flushed_count=0, success=False, error=err_text
            )

        flushed = len(queued)
        self._digest_queue = []
        self._emit_audit_digest(queued, status="ok", error=None)
        logger.info(
            "[kora.alert_notifier] digest flushed %d alert(s)", flushed
        )
        return DigestFlushResult(flushed_count=flushed, success=True)

    async def _send_email_digest(self, alerts: List[Alert]) -> None:
        """Format + send the digest as one email."""
        client = self._purelymail_client_factory()
        if client is None:
            raise RuntimeError("purelymail_client_unavailable")
        joshua_email = os.environ.get(JOSHUA_EMAIL_ADDRESS_ENV, "").strip()
        if not joshua_email:
            raise RuntimeError("joshua_email_address_unset")
        from_addr = os.environ.get(KORA_EMAIL_FROM_ADDRESS_ENV, "").strip()
        if not from_addr:
            raise RuntimeError("kora_email_from_address_unset")

        subject = format_digest_subject(alerts)
        body_text = format_digest_body(alerts)
        result = await client.send_email(
            from_addr=from_addr,
            to=[joshua_email],
            subject=subject,
            body_text=body_text,
        )
        if getattr(result, "status", None) == "failed":
            raise RuntimeError(
                f"smtp_send_failed:{getattr(result, 'smtp_code', None)}"
            )

    def _emit_audit_digest(
        self,
        alerts: List[Alert],
        *,
        status: str,
        error: Optional[str],
    ) -> None:
        """One audit row per digest-flush attempt."""
        try:
            from kora_cli.audit import emit_audit
        except Exception as exc:
            logger.warning(
                "[kora.alert_notifier] audit import failed: %r", exc
            )
            return
        details: Dict[str, Any] = {
            "channel": _CHANNEL_EMAIL,
            "alert_id": f"digest:{len(alerts)}",
            "severity": "digest",
            "category": "digest_email",
            "status": status,
            "digest_count": len(alerts),
            "digest_alert_ids": sorted(a.id for a in alerts),
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
                "[kora.alert_notifier] emit_audit (digest) raised %r — "
                "continuing",
                exc,
            )

    # ------------------------------------------------------------------
    # Test-tool entry point (KR-ALERT-NOTIFY ST2 §4 Q4)
    # ------------------------------------------------------------------

    async def dispatch_synthetic_alert(self, alert: Alert) -> DispatchOutcome:
        """Bypass dedup + cooldown + burst + digest. Used by the
        ``kora__send_test_alert`` MCP tool to verify channels are
        configured. NOT for production code paths."""
        return await self._dispatch_alert(alert)

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
