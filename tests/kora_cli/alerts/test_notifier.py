"""Tests for the KR-ALERT-NOTIFY ST1 push-notifier.

Bucket §2 scenarios:

  Formatting helpers (pure functions):
   1. Slack DM text — emoji + severity uppercase + title + detail +
      source + relative-time line
   2. Email subject — `[Kora alert] {severity}: {title}` format
   3. Email body — detail + source + first_seen + optional cockpit URL
   4. Relative time formatting under various deltas

  Channel routing:
   5. critical → Slack DM
   6. warning → Slack DM
   7. info → email
   8. Unknown severity defaults to Slack DM (defensive)

  Dedup semantics:
   9. First cycle: all active alerts fire as newly-firing
  10. Repeat cycle: same alert IDs no re-dispatch
  11. Newly-firing alert mid-stream: only the new one dispatches
  12. Resolved alert: no dispatch on resolution + cleared from set
  13. Mixed: new + still-firing + resolved → only new dispatches

  Per-cycle failure isolation:
  14. SlackClient unavailable → dispatch fails + alert STILL enters
      dedup set (no spam on retry)
  15. PurelymailClient unavailable → same
  16. Joshua env unset (slack or email) → dispatch fails, alert
      enters dedup set
  17. Slack post_dm raises → audit failed + outcome.success=False
  18. SMTP send returns failed SendResult → outcome.success=False
  19. compute_active_alerts raises → empty result (cycle no-op)
  20. emit_audit raises → cycle still returns clean result

  Audit emit:
  21. Successful dispatch emits notification.dispatched with status=ok
  22. Failed dispatch emits notification.dispatched with status=failed
      + error code
  23. Audit details include alert_id + severity + category + channel

  Reset:
  24. reset_dedup_state clears last_alert_ids

  Cycle telemetry:
  25. NotificationCycleResult counts reconcile: slack + email +
      errors == newly_firing_count
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kora_cli.alerts.aggregator import Alert
from kora_cli.alerts.notifier import (
    AlertNotifier,
    DispatchOutcome,
    NotificationCycleResult,
    format_email_body,
    format_email_subject,
    format_slack_dm_text,
    _format_relative_time,
)


_JOSHUA_USER_ID = "U01JOSHUA"
_JOSHUA_EMAIL = "joshua@stormhavenenterprises.com"
_KORA_EMAIL = "kora@stormhavenenterprises.com"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.setenv("KORA_SLACK_JOSHUA_USER_ID", _JOSHUA_USER_ID)
    monkeypatch.setenv("KORA_EMAIL_JOSHUA_ADDRESS", _JOSHUA_EMAIL)
    monkeypatch.setenv("KORA_EMAIL_KORA_ADDRESS", _KORA_EMAIL)
    monkeypatch.delenv("KORA_COCKPIT_URL", raising=False)
    # ST2 throttling envs — disabled by default so ST1-shape tests
    # get the pre-ST2 behavior. ST2-specific tests override these.
    monkeypatch.setenv("KORA_ALERT_NOTIFY_CATEGORY_COOLDOWN_SEC", "0")
    monkeypatch.setenv("KORA_ALERT_NOTIFY_BURST_THRESHOLD", "100")
    monkeypatch.delenv("KORA_ALERT_NOTIFY_MODE", raising=False)


def _make_alert(
    *,
    id: str = "cost_ladder_warned",
    severity: str = "warning",
    category: str = "cost_ladder",
    title: str = "Budget at 80% of monthly cap",
    detail: str = "Cost ladder at warn_75 — reasoning still runs.",
    source_panel: str = "cost",
    source_panel_route: str = "/cost-state",
    first_seen_at: str = "2026-05-23T10:00:00Z",
) -> Alert:
    return Alert(
        id=id,
        severity=severity,
        category=category,
        title=title,
        detail=detail,
        source_panel=source_panel,
        source_panel_route=source_panel_route,
        first_seen_at=first_seen_at,
    )


def _make_clients_factories(*, slack_client=None, purelymail_client=None):
    """Build the two factory callables AlertNotifier expects."""
    return (lambda: slack_client), (lambda: purelymail_client)


def _make_slack_client():
    client = MagicMock()
    client.post_dm = AsyncMock(return_value={"ts": "1716480000.000001"})
    return client


def _make_purelymail_client(*, status: str = "ok"):
    client = MagicMock()
    result = MagicMock()
    result.status = status
    result.smtp_code = 250 if status == "ok" else 550
    client.send_email = AsyncMock(return_value=result)
    return client


# ===========================================================================
# Formatting helpers
# ===========================================================================


def test_format_slack_dm_text_includes_emoji_and_severity():
    alert = _make_alert(severity="critical", title="Halted", detail="Budget halted.")
    text = format_slack_dm_text(
        alert, now=datetime(2026, 5, 23, 10, 5, 0, tzinfo=timezone.utc)
    )
    assert "🔴" in text
    assert "[CRITICAL]" in text
    assert "Halted" in text
    assert "Budget halted." in text
    assert "/cost-state" in text
    assert "5m ago" in text


def test_format_slack_dm_text_warning_emoji():
    alert = _make_alert(severity="warning")
    text = format_slack_dm_text(alert)
    assert "🟡" in text
    assert "[WARNING]" in text


def test_format_email_subject_includes_severity_and_title():
    alert = _make_alert(severity="info", title="capability denied surge")
    assert format_email_subject(alert) == (
        "[Kora alert] info: capability denied surge"
    )


def test_format_email_body_omits_cockpit_url_when_unset(monkeypatch):
    monkeypatch.delenv("KORA_COCKPIT_URL", raising=False)
    alert = _make_alert(detail="see panel", source_panel_route="/alerts")
    body = format_email_body(alert)
    assert "see panel" in body
    assert "/alerts" in body
    assert "Cockpit URL" not in body


def test_format_email_body_includes_cockpit_url_when_set(monkeypatch):
    monkeypatch.setenv("KORA_COCKPIT_URL", "https://kora.example/cockpit")
    body = format_email_body(_make_alert())
    assert "https://kora.example/cockpit" in body


def test_format_relative_time_under_minute():
    now = datetime(2026, 5, 23, 12, 0, 30, tzinfo=timezone.utc)
    assert _format_relative_time("2026-05-23T12:00:00Z", now=now) == "30s ago"


def test_format_relative_time_minutes():
    now = datetime(2026, 5, 23, 12, 5, 0, tzinfo=timezone.utc)
    assert _format_relative_time("2026-05-23T12:00:00Z", now=now) == "5m ago"


def test_format_relative_time_hours():
    now = datetime(2026, 5, 23, 14, 0, 0, tzinfo=timezone.utc)
    assert _format_relative_time("2026-05-23T12:00:00Z", now=now) == "2h ago"


def test_format_relative_time_days():
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    assert _format_relative_time("2026-05-23T12:00:00Z", now=now) == "2d ago"


def test_format_relative_time_malformed_returns_raw():
    assert _format_relative_time("not-a-timestamp") == "not-a-timestamp"


def test_format_relative_time_empty_returns_unknown():
    assert _format_relative_time("") == "(unknown)"


# ===========================================================================
# Channel routing
# ===========================================================================


@pytest.mark.asyncio
async def test_critical_routes_to_slack():
    slack = _make_slack_client()
    purelymail = _make_purelymail_client()
    sf, pf = _make_clients_factories(
        slack_client=slack, purelymail_client=purelymail
    )
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: [_make_alert(severity="critical")],
    )
    result = await notifier.run_notification_cycle()
    assert result.slack_dispatched == 1
    assert result.email_dispatched == 0
    slack.post_dm.assert_awaited_once()
    purelymail.send_email.assert_not_called()


@pytest.mark.asyncio
async def test_warning_routes_to_slack():
    slack = _make_slack_client()
    purelymail = _make_purelymail_client()
    sf, pf = _make_clients_factories(
        slack_client=slack, purelymail_client=purelymail
    )
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: [_make_alert(severity="warning")],
    )
    result = await notifier.run_notification_cycle()
    assert result.slack_dispatched == 1
    slack.post_dm.assert_awaited_once()


@pytest.mark.asyncio
async def test_info_routes_to_email():
    slack = _make_slack_client()
    purelymail = _make_purelymail_client()
    sf, pf = _make_clients_factories(
        slack_client=slack, purelymail_client=purelymail
    )
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: [_make_alert(severity="info")],
    )
    result = await notifier.run_notification_cycle()
    assert result.email_dispatched == 1
    assert result.slack_dispatched == 0
    purelymail.send_email.assert_awaited_once()
    slack.post_dm.assert_not_called()


# ===========================================================================
# Dedup semantics
# ===========================================================================


@pytest.mark.asyncio
async def test_first_cycle_fires_all_active_alerts():
    """PM Q3 default: empty last_alert_ids on first cycle means
    everything currently active gets notified."""
    slack = _make_slack_client()
    purelymail = _make_purelymail_client()
    sf, pf = _make_clients_factories(
        slack_client=slack, purelymail_client=purelymail
    )
    alerts = [
        _make_alert(id="cost_ladder_warned", severity="warning"),
        _make_alert(id="operator_paused", severity="critical"),
    ]
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: alerts,
    )
    result = await notifier.run_notification_cycle()
    assert result.newly_firing_count == 2
    assert result.slack_dispatched == 2


@pytest.mark.asyncio
async def test_repeat_cycle_no_redispatch_for_still_firing():
    slack = _make_slack_client()
    purelymail = _make_purelymail_client()
    sf, pf = _make_clients_factories(
        slack_client=slack, purelymail_client=purelymail
    )
    alerts = [_make_alert(id="cost_ladder_warned", severity="warning")]
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: alerts,
    )
    # First cycle fires.
    r1 = await notifier.run_notification_cycle()
    assert r1.slack_dispatched == 1
    # Second cycle (same alert active): no re-dispatch.
    r2 = await notifier.run_notification_cycle()
    assert r2.newly_firing_count == 0
    assert r2.slack_dispatched == 0
    assert slack.post_dm.await_count == 1


@pytest.mark.asyncio
async def test_new_alert_mid_stream_only_dispatches_new_one():
    slack = _make_slack_client()
    purelymail = _make_purelymail_client()
    sf, pf = _make_clients_factories(
        slack_client=slack, purelymail_client=purelymail
    )
    alerts_state = [[_make_alert(id="a", severity="warning")]]

    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: alerts_state[0],
    )
    await notifier.run_notification_cycle()
    # Add a second alert.
    alerts_state[0] = [
        _make_alert(id="a", severity="warning"),
        _make_alert(id="b", severity="critical"),
    ]
    r2 = await notifier.run_notification_cycle()
    assert r2.newly_firing_count == 1
    assert r2.slack_dispatched == 1
    # Total slack calls = 1 (cycle 1) + 1 (cycle 2's new alert) = 2
    assert slack.post_dm.await_count == 2


@pytest.mark.asyncio
async def test_resolved_alert_clears_from_dedup_set():
    slack = _make_slack_client()
    purelymail = _make_purelymail_client()
    sf, pf = _make_clients_factories(
        slack_client=slack, purelymail_client=purelymail
    )
    alerts_state = [[_make_alert(id="x", severity="warning")]]
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: alerts_state[0],
    )
    await notifier.run_notification_cycle()
    assert "x" in notifier.last_alert_ids
    # Resolve.
    alerts_state[0] = []
    r2 = await notifier.run_notification_cycle()
    assert r2.newly_resolved_count == 1
    assert "x" not in notifier.last_alert_ids


@pytest.mark.asyncio
async def test_mixed_new_still_firing_resolved():
    slack = _make_slack_client()
    purelymail = _make_purelymail_client()
    sf, pf = _make_clients_factories(
        slack_client=slack, purelymail_client=purelymail
    )
    alerts_state = [
        [
            _make_alert(id="a", severity="warning"),
            _make_alert(id="b", severity="warning"),
        ]
    ]
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: alerts_state[0],
    )
    # Cycle 1: a + b new
    await notifier.run_notification_cycle()
    # Cycle 2: a still firing, b resolved, c new
    alerts_state[0] = [
        _make_alert(id="a", severity="warning"),
        _make_alert(id="c", severity="critical"),
    ]
    r2 = await notifier.run_notification_cycle()
    assert r2.newly_firing_count == 1  # only c
    assert r2.newly_resolved_count == 1  # b
    assert r2.slack_dispatched == 1


# ===========================================================================
# Failure isolation
# ===========================================================================


@pytest.mark.asyncio
async def test_slack_client_unavailable_no_redispatch():
    sf, pf = _make_clients_factories(
        slack_client=None, purelymail_client=_make_purelymail_client()
    )
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: [_make_alert(severity="critical", id="boom")],
    )
    r1 = await notifier.run_notification_cycle()
    assert r1.dispatch_errors == 1
    assert "boom" in notifier.last_alert_ids
    # Second cycle: alert still active, but no re-dispatch (spam protection).
    r2 = await notifier.run_notification_cycle()
    assert r2.newly_firing_count == 0
    assert r2.dispatch_errors == 0


@pytest.mark.asyncio
async def test_purelymail_client_unavailable():
    sf, pf = _make_clients_factories(
        slack_client=_make_slack_client(), purelymail_client=None
    )
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: [_make_alert(severity="info")],
    )
    result = await notifier.run_notification_cycle()
    assert result.dispatch_errors == 1
    assert result.email_dispatched == 0


@pytest.mark.asyncio
async def test_joshua_slack_id_unset(monkeypatch):
    monkeypatch.delenv("KORA_SLACK_JOSHUA_USER_ID", raising=False)
    sf, pf = _make_clients_factories(
        slack_client=_make_slack_client(),
        purelymail_client=_make_purelymail_client(),
    )
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: [_make_alert(severity="critical", id="x")],
    )
    result = await notifier.run_notification_cycle()
    assert result.dispatch_errors == 1
    assert "x" in notifier.last_alert_ids


@pytest.mark.asyncio
async def test_joshua_email_unset(monkeypatch):
    monkeypatch.delenv("KORA_EMAIL_JOSHUA_ADDRESS", raising=False)
    sf, pf = _make_clients_factories(
        slack_client=_make_slack_client(),
        purelymail_client=_make_purelymail_client(),
    )
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: [_make_alert(severity="info")],
    )
    result = await notifier.run_notification_cycle()
    assert result.dispatch_errors == 1


@pytest.mark.asyncio
async def test_slack_post_dm_raises():
    slack = _make_slack_client()
    slack.post_dm.side_effect = RuntimeError("slack_429")
    sf, pf = _make_clients_factories(
        slack_client=slack, purelymail_client=_make_purelymail_client()
    )
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: [_make_alert(severity="critical")],
    )
    result = await notifier.run_notification_cycle()
    assert result.dispatch_errors == 1
    assert len(result.outcomes) == 1
    assert result.outcomes[0].success is False
    assert "RuntimeError" in (result.outcomes[0].error or "")


@pytest.mark.asyncio
async def test_smtp_failed_sendresult_marks_failure():
    purelymail = _make_purelymail_client(status="failed")
    sf, pf = _make_clients_factories(
        slack_client=_make_slack_client(), purelymail_client=purelymail
    )
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: [_make_alert(severity="info")],
    )
    result = await notifier.run_notification_cycle()
    assert result.dispatch_errors == 1
    assert result.outcomes[0].success is False


@pytest.mark.asyncio
async def test_compute_active_alerts_raises_returns_empty_result():
    sf, pf = _make_clients_factories(
        slack_client=_make_slack_client(),
        purelymail_client=_make_purelymail_client(),
    )

    def boom():
        raise RuntimeError("aggregator dead")

    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=boom,
    )
    result = await notifier.run_notification_cycle()
    assert result == NotificationCycleResult(
        active_count=0,
        newly_firing_count=0,
        newly_resolved_count=0,
        slack_dispatched=0,
        email_dispatched=0,
        dispatch_errors=0,
        outcomes=[],
    )


@pytest.mark.asyncio
async def test_emit_audit_raises_doesnt_break_cycle():
    sf, pf = _make_clients_factories(
        slack_client=_make_slack_client(),
        purelymail_client=_make_purelymail_client(),
    )
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: [_make_alert(severity="critical")],
    )
    with patch(
        "kora_cli.audit.emit_audit", side_effect=RuntimeError("audit dead")
    ):
        result = await notifier.run_notification_cycle()
    # Cycle still succeeded (slack post_dm ran before audit emit).
    assert result.slack_dispatched == 1


# ===========================================================================
# Audit emit shape
# ===========================================================================


@pytest.mark.asyncio
async def test_audit_records_success_dispatch():
    sf, pf = _make_clients_factories(
        slack_client=_make_slack_client(),
        purelymail_client=_make_purelymail_client(),
    )
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: [
            _make_alert(
                id="cost_ladder_warned",
                severity="warning",
                category="cost_ladder",
            )
        ],
    )
    with patch("kora_cli.audit.emit_audit") as mock_emit:
        await notifier.run_notification_cycle()
    mock_emit.assert_called_once()
    call_kwargs = mock_emit.call_args.kwargs
    assert call_kwargs["seam"] == "notification.dispatched"
    details = call_kwargs["details"]
    assert details["channel"] == "slack_dm"
    assert details["alert_id"] == "cost_ladder_warned"
    assert details["severity"] == "warning"
    assert details["category"] == "cost_ladder"
    assert details["status"] == "ok"
    assert "error" not in details  # success path — error omitted


@pytest.mark.asyncio
async def test_audit_records_failure_dispatch_with_error_code():
    slack = _make_slack_client()
    slack.post_dm.side_effect = RuntimeError("slack_429")
    sf, pf = _make_clients_factories(
        slack_client=slack, purelymail_client=_make_purelymail_client()
    )
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: [_make_alert(severity="critical")],
    )
    with patch("kora_cli.audit.emit_audit") as mock_emit:
        await notifier.run_notification_cycle()
    details = mock_emit.call_args.kwargs["details"]
    assert details["status"] == "failed"
    assert details["error"] == "RuntimeError"


# ===========================================================================
# Reset + telemetry
# ===========================================================================


@pytest.mark.asyncio
async def test_reset_dedup_state_clears_set():
    sf, pf = _make_clients_factories(
        slack_client=_make_slack_client(),
        purelymail_client=_make_purelymail_client(),
    )
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: [_make_alert(severity="warning", id="x")],
    )
    await notifier.run_notification_cycle()
    assert "x" in notifier.last_alert_ids
    notifier.reset_dedup_state()
    assert notifier.last_alert_ids == set()


@pytest.mark.asyncio
async def test_cycle_telemetry_counts_reconcile():
    alerts = [
        _make_alert(id=f"a{i}", severity="warning") for i in range(3)
    ] + [_make_alert(id="info1", severity="info")]
    sf, pf = _make_clients_factories(
        slack_client=_make_slack_client(),
        purelymail_client=_make_purelymail_client(),
    )
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: alerts,
    )
    result = await notifier.run_notification_cycle()
    assert result.newly_firing_count == 4
    assert (
        result.slack_dispatched
        + result.email_dispatched
        + result.dispatch_errors
        == result.newly_firing_count
    )
    assert result.slack_dispatched == 3
    assert result.email_dispatched == 1


@pytest.mark.asyncio
async def test_severity_sort_order_in_dispatch_outcomes():
    """Cycle should dispatch critical-first, then warning, then info,
    then by id. Outcomes recorded in the same order."""
    alerts = [
        _make_alert(id="info1", severity="info"),
        _make_alert(id="warn1", severity="warning"),
        _make_alert(id="crit1", severity="critical"),
    ]
    sf, pf = _make_clients_factories(
        slack_client=_make_slack_client(),
        purelymail_client=_make_purelymail_client(),
    )
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: alerts,
    )
    result = await notifier.run_notification_cycle()
    ids = [o.alert_id for o in result.outcomes]
    assert ids == ["crit1", "warn1", "info1"]


# ===========================================================================
# DispatchOutcome shape
# ===========================================================================


def test_dispatch_outcome_is_frozen():
    o = DispatchOutcome(
        alert_id="x", severity="warning", channel="slack_dm", success=True
    )
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        o.success = False  # type: ignore
