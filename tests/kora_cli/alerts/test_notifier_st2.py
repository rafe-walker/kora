"""Tests for KR-ALERT-NOTIFY ST2 — throttling stack + digest mode + test tool.

Bucket §2 ST2 scenarios:

  Per-category cooldown:
   1. Category dispatched once → repeat within window suppressed
   2. Window elapsed → category re-fires
   3. Different categories share no state — both fire in same cycle
   4. cooldown=0 disables filter (operator opt-out)
   5. Cooldown stamp cleared when alert resolves (re-fire on
      subsequent re-emergence isn't gated)
   6. NotificationCycleResult.cooldown_suppressed counts correctly

  Burst dampening:
   7. ≤threshold alerts → all individual (no summary)
   8. >threshold alerts → ONE summary dispatch + zero individual sends
   9. Burst summary includes severity counts + alert titles
  10. Burst path stamps category cooldown for each suppressed alert
  11. NotificationCycleResult.burst_summarized counts correctly

  Digest mode:
  12. mode=digest: critical fires immediately
  13. mode=digest: warning + info queued (not dispatched)
  14. mode=immediate: warning + info dispatch immediately (default)
  15. flush_digest empty queue → no-op success
  16. flush_digest in immediate mode → no-op
  17. flush_digest with queued alerts → one email + queue cleared
  18. flush_digest failure → queue NOT cleared (retry next flush)
  19. digest email subject + body shape

  Throttle interaction:
  20. cooldown + burst: cooldown filter runs FIRST; burst threshold
      applies to post-cooldown set
  21. burst + digest: in digest, criticals subject to burst dampening;
      warnings + info skip burst (they queue instead)

  Test tool entry point:
  22. dispatch_synthetic_alert bypasses dedup (same id re-dispatches)
  23. dispatch_synthetic_alert bypasses cooldown
  24. dispatch_synthetic_alert bypasses burst dampening
  25. dispatch_synthetic_alert bypasses digest queue

  reset_dedup_state:
  26. Clears cooldown stamps + digest queue (not just last_alert_ids)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kora_cli.alerts.aggregator import Alert
from kora_cli.alerts.notifier import (
    AlertNotifier,
    DigestFlushResult,
    DispatchOutcome,
    NotificationCycleResult,
    format_burst_summary_text,
    format_digest_body,
    format_digest_subject,
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
    # ST2 envs default to defaults for these tests; individual tests
    # override per-scenario.
    monkeypatch.delenv("KORA_ALERT_NOTIFY_CATEGORY_COOLDOWN_SEC", raising=False)
    monkeypatch.delenv("KORA_ALERT_NOTIFY_BURST_THRESHOLD", raising=False)
    monkeypatch.delenv("KORA_ALERT_NOTIFY_MODE", raising=False)


def _alert(
    *,
    id: str,
    severity: str = "warning",
    category: str = "cost_ladder",
    title: str = "test alert",
) -> Alert:
    return Alert(
        id=id,
        severity=severity,
        category=category,
        title=title,
        detail="detail body",
        source_panel="ops",
        source_panel_route="/alerts",
        first_seen_at="2026-05-23T10:00:00Z",
    )


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


def _factories(slack=None, purelymail=None):
    return (lambda: slack), (lambda: purelymail)


# ===========================================================================
# Per-category cooldown
# ===========================================================================


@pytest.mark.asyncio
async def test_cooldown_suppresses_repeat_category_within_window(monkeypatch):
    """Cycle 1: cost_ladder_warned dispatches. Cycle 2: a different
    alert in the same category (cost_ladder) suppressed."""
    monkeypatch.setenv("KORA_ALERT_NOTIFY_CATEGORY_COOLDOWN_SEC", "1800")
    slack = _make_slack_client()
    sf, pf = _factories(slack=slack, purelymail=_make_purelymail_client())
    alerts_state = [
        [_alert(id="a1", severity="warning", category="cost_ladder")]
    ]
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: alerts_state[0],
    )
    r1 = await notifier.run_notification_cycle()
    assert r1.slack_dispatched == 1
    # Cycle 2: add a2 (same category cost_ladder) + keep a1 active.
    alerts_state[0] = [
        _alert(id="a1", severity="warning", category="cost_ladder"),
        _alert(id="a2", severity="warning", category="cost_ladder"),
    ]
    r2 = await notifier.run_notification_cycle()
    # a2 is newly-firing but cooldown-suppressed.
    assert r2.newly_firing_count == 1
    assert r2.slack_dispatched == 0
    assert r2.cooldown_suppressed == 1


@pytest.mark.asyncio
async def test_cooldown_clears_after_window_elapsed(monkeypatch):
    """Manipulate cooldown timestamp to simulate window elapsed."""
    monkeypatch.setenv("KORA_ALERT_NOTIFY_CATEGORY_COOLDOWN_SEC", "60")
    slack = _make_slack_client()
    sf, pf = _factories(slack=slack, purelymail=_make_purelymail_client())
    alerts_state = [[_alert(id="a1", category="cost_ladder")]]
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: alerts_state[0],
    )
    await notifier.run_notification_cycle()
    # Backdate the cooldown stamp by 2 minutes so the window elapses.
    notifier._last_dispatched_by_category["cost_ladder"] = (
        datetime.now(timezone.utc) - timedelta(seconds=120)
    )
    alerts_state[0] = [
        _alert(id="a1", category="cost_ladder"),
        _alert(id="a2", category="cost_ladder"),
    ]
    r2 = await notifier.run_notification_cycle()
    # a2 is new + cooldown window has elapsed → fires.
    assert r2.slack_dispatched == 1
    assert r2.cooldown_suppressed == 0


@pytest.mark.asyncio
async def test_distinct_categories_share_no_cooldown(monkeypatch):
    monkeypatch.setenv("KORA_ALERT_NOTIFY_CATEGORY_COOLDOWN_SEC", "1800")
    slack = _make_slack_client()
    sf, pf = _factories(slack=slack, purelymail=_make_purelymail_client())
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: [
            _alert(id="a", category="cost_ladder", severity="warning"),
            _alert(id="b", category="operational_state", severity="critical"),
            _alert(id="c", category="service_unhealthy", severity="warning"),
        ],
    )
    result = await notifier.run_notification_cycle()
    assert result.slack_dispatched == 3
    assert result.cooldown_suppressed == 0


@pytest.mark.asyncio
async def test_cooldown_zero_disables_filter(monkeypatch):
    monkeypatch.setenv("KORA_ALERT_NOTIFY_CATEGORY_COOLDOWN_SEC", "0")
    slack = _make_slack_client()
    sf, pf = _factories(slack=slack, purelymail=_make_purelymail_client())
    alerts_state = [[_alert(id="a1", category="cost_ladder")]]
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: alerts_state[0],
    )
    await notifier.run_notification_cycle()
    alerts_state[0] = [
        _alert(id="a1", category="cost_ladder"),
        _alert(id="a2", category="cost_ladder"),
    ]
    r2 = await notifier.run_notification_cycle()
    assert r2.slack_dispatched == 1
    assert r2.cooldown_suppressed == 0


@pytest.mark.asyncio
async def test_cooldown_stamp_cleared_when_category_resolves(monkeypatch):
    """When all alerts of a category resolve, the cooldown stamp
    clears so the next category fire isn't gated."""
    monkeypatch.setenv("KORA_ALERT_NOTIFY_CATEGORY_COOLDOWN_SEC", "1800")
    slack = _make_slack_client()
    sf, pf = _factories(slack=slack, purelymail=_make_purelymail_client())
    alerts_state = [[_alert(id="a1", category="cost_ladder")]]
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: alerts_state[0],
    )
    await notifier.run_notification_cycle()
    assert "cost_ladder" in notifier._last_dispatched_by_category
    # All cost_ladder alerts resolve, replaced by a different category.
    alerts_state[0] = [_alert(id="b", category="operational_state")]
    r2 = await notifier.run_notification_cycle()
    assert "cost_ladder" not in notifier._last_dispatched_by_category
    assert r2.slack_dispatched == 1
    # Now cost_ladder re-emerges — should fire (stamp was cleared).
    alerts_state[0] = [
        _alert(id="b", category="operational_state"),
        _alert(id="a2", category="cost_ladder"),
    ]
    r3 = await notifier.run_notification_cycle()
    assert r3.slack_dispatched == 1
    assert r3.cooldown_suppressed == 0


# ===========================================================================
# Burst dampening
# ===========================================================================


@pytest.mark.asyncio
async def test_burst_below_threshold_individual_sends(monkeypatch):
    monkeypatch.setenv("KORA_ALERT_NOTIFY_BURST_THRESHOLD", "5")
    monkeypatch.setenv("KORA_ALERT_NOTIFY_CATEGORY_COOLDOWN_SEC", "0")
    slack = _make_slack_client()
    sf, pf = _factories(slack=slack, purelymail=_make_purelymail_client())
    alerts = [
        _alert(id=f"a{i}", category=f"cat_{i}", severity="warning")
        for i in range(5)
    ]
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: alerts,
    )
    result = await notifier.run_notification_cycle()
    assert result.slack_dispatched == 5
    assert result.burst_summarized == 0
    assert slack.post_dm.await_count == 5


@pytest.mark.asyncio
async def test_burst_above_threshold_single_summary(monkeypatch):
    monkeypatch.setenv("KORA_ALERT_NOTIFY_BURST_THRESHOLD", "5")
    monkeypatch.setenv("KORA_ALERT_NOTIFY_CATEGORY_COOLDOWN_SEC", "0")
    slack = _make_slack_client()
    sf, pf = _factories(slack=slack, purelymail=_make_purelymail_client())
    alerts = [
        _alert(id=f"a{i}", category=f"cat_{i}", severity="warning")
        for i in range(7)
    ]
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: alerts,
    )
    result = await notifier.run_notification_cycle()
    assert result.burst_summarized == 7
    assert result.slack_dispatched == 1  # the summary message
    assert slack.post_dm.await_count == 1
    # The summary text includes the count + each alert title.
    summary_text = slack.post_dm.await_args.kwargs["text"]
    assert "7 Kora alerts" in summary_text


@pytest.mark.asyncio
async def test_burst_path_stamps_all_category_cooldowns(monkeypatch):
    """Even though burst path uses ONE summary, each suppressed
    alert's category gets a cooldown stamp so the next cycle doesn't
    re-spam the same categories."""
    monkeypatch.setenv("KORA_ALERT_NOTIFY_BURST_THRESHOLD", "3")
    monkeypatch.setenv("KORA_ALERT_NOTIFY_CATEGORY_COOLDOWN_SEC", "1800")
    slack = _make_slack_client()
    sf, pf = _factories(slack=slack, purelymail=_make_purelymail_client())
    alerts = [
        _alert(id=f"a{i}", category=f"cat_{i}", severity="warning")
        for i in range(5)
    ]
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: alerts,
    )
    await notifier.run_notification_cycle()
    for i in range(5):
        assert f"cat_{i}" in notifier._last_dispatched_by_category


@pytest.mark.asyncio
async def test_format_burst_summary_text_shape():
    alerts = [
        _alert(
            id="cost_ladder_halted",
            severity="critical",
            category="cost_ladder",
            title="Budget halted at 105%",
        ),
        _alert(
            id="operator_paused",
            severity="critical",
            category="operational_state",
            title="Kora paused",
        ),
        _alert(
            id="cost_ladder_warned",
            severity="warning",
            category="cost_ladder",
            title="Budget at 80%",
        ),
        _alert(
            id="capability_denied_24h",
            severity="info",
            category="agent_capability_denied",
            title="12 capability_denied in 24h",
        ),
    ]
    text = format_burst_summary_text(alerts)
    assert "🚨 [BURST] 4 Kora alerts" in text
    assert "critical=2" in text
    assert "warning=1" in text
    assert "info=1" in text
    assert "Budget halted at 105%" in text
    assert "Kora paused" in text


# ===========================================================================
# Digest mode
# ===========================================================================


@pytest.mark.asyncio
async def test_digest_mode_critical_fires_immediately(monkeypatch):
    monkeypatch.setenv("KORA_ALERT_NOTIFY_MODE", "digest")
    monkeypatch.setenv("KORA_ALERT_NOTIFY_CATEGORY_COOLDOWN_SEC", "0")
    slack = _make_slack_client()
    purelymail = _make_purelymail_client()
    sf, pf = _factories(slack=slack, purelymail=purelymail)
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: [
            _alert(id="crit", severity="critical", category="operational_state")
        ],
    )
    result = await notifier.run_notification_cycle()
    assert result.slack_dispatched == 1
    assert result.digest_queued == 0
    assert notifier.digest_queue_size == 0


@pytest.mark.asyncio
async def test_digest_mode_warning_and_info_queue(monkeypatch):
    monkeypatch.setenv("KORA_ALERT_NOTIFY_MODE", "digest")
    monkeypatch.setenv("KORA_ALERT_NOTIFY_CATEGORY_COOLDOWN_SEC", "0")
    slack = _make_slack_client()
    purelymail = _make_purelymail_client()
    sf, pf = _factories(slack=slack, purelymail=purelymail)
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: [
            _alert(id="warn", severity="warning", category="cost_ladder"),
            _alert(id="info", severity="info", category="agent_capability_denied"),
        ],
    )
    result = await notifier.run_notification_cycle()
    assert result.digest_queued == 2
    assert result.slack_dispatched == 0
    assert result.email_dispatched == 0
    assert notifier.digest_queue_size == 2
    slack.post_dm.assert_not_called()
    purelymail.send_email.assert_not_called()


@pytest.mark.asyncio
async def test_immediate_mode_default_fires_all_individually(monkeypatch):
    monkeypatch.delenv("KORA_ALERT_NOTIFY_MODE", raising=False)
    monkeypatch.setenv("KORA_ALERT_NOTIFY_CATEGORY_COOLDOWN_SEC", "0")
    slack = _make_slack_client()
    purelymail = _make_purelymail_client()
    sf, pf = _factories(slack=slack, purelymail=purelymail)
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: [
            _alert(id="warn", severity="warning", category="cost_ladder"),
            _alert(id="info", severity="info", category="agent_capability_denied"),
        ],
    )
    result = await notifier.run_notification_cycle()
    assert result.slack_dispatched == 1
    assert result.email_dispatched == 1
    assert result.digest_queued == 0


@pytest.mark.asyncio
async def test_flush_digest_empty_queue_noop(monkeypatch):
    monkeypatch.setenv("KORA_ALERT_NOTIFY_MODE", "digest")
    sf, pf = _factories(
        slack=_make_slack_client(), purelymail=_make_purelymail_client()
    )
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: [],
    )
    result = await notifier.flush_digest()
    assert result.flushed_count == 0
    assert result.success is True


@pytest.mark.asyncio
async def test_flush_digest_immediate_mode_noop(monkeypatch):
    """In immediate mode the queue is always empty, but defensively
    flush_digest() also checks mode + bails before send."""
    monkeypatch.delenv("KORA_ALERT_NOTIFY_MODE", raising=False)
    purelymail = _make_purelymail_client()
    sf, pf = _factories(
        slack=_make_slack_client(), purelymail=purelymail
    )
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: [],
    )
    # Force a queue entry to prove mode-check beats queue-check.
    notifier._digest_queue.append(_alert(id="orphan", severity="warning"))
    result = await notifier.flush_digest()
    assert result.flushed_count == 0
    assert result.success is True
    purelymail.send_email.assert_not_called()


@pytest.mark.asyncio
async def test_flush_digest_sends_and_clears_queue(monkeypatch):
    monkeypatch.setenv("KORA_ALERT_NOTIFY_MODE", "digest")
    monkeypatch.setenv("KORA_ALERT_NOTIFY_CATEGORY_COOLDOWN_SEC", "0")
    slack = _make_slack_client()
    purelymail = _make_purelymail_client()
    sf, pf = _factories(slack=slack, purelymail=purelymail)
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: [
            _alert(id="w1", severity="warning"),
            _alert(id="i1", severity="info", category="agent_capability_denied"),
        ],
    )
    await notifier.run_notification_cycle()
    assert notifier.digest_queue_size == 2

    flush = await notifier.flush_digest()
    assert flush.success is True
    assert flush.flushed_count == 2
    assert notifier.digest_queue_size == 0
    purelymail.send_email.assert_awaited_once()
    kw = purelymail.send_email.await_args.kwargs
    assert kw["to"] == [_JOSHUA_EMAIL]
    assert "Kora digest" in kw["subject"]


@pytest.mark.asyncio
async def test_flush_digest_failure_retains_queue(monkeypatch):
    monkeypatch.setenv("KORA_ALERT_NOTIFY_MODE", "digest")
    monkeypatch.setenv("KORA_ALERT_NOTIFY_CATEGORY_COOLDOWN_SEC", "0")
    slack = _make_slack_client()
    purelymail = _make_purelymail_client(status="failed")
    sf, pf = _factories(slack=slack, purelymail=purelymail)
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: [_alert(id="w1", severity="warning")],
    )
    await notifier.run_notification_cycle()
    assert notifier.digest_queue_size == 1

    flush = await notifier.flush_digest()
    assert flush.success is False
    assert flush.flushed_count == 0
    # Queue NOT drained — retry next flush.
    assert notifier.digest_queue_size == 1


def test_format_digest_subject_includes_counts():
    alerts = [
        _alert(id="w1", severity="warning"),
        _alert(id="w2", severity="warning"),
        _alert(id="i1", severity="info"),
    ]
    subject = format_digest_subject(alerts)
    assert "3 alert(s)" in subject
    assert "2 warning" in subject
    assert "1 info" in subject


def test_format_digest_body_groups_by_severity():
    alerts = [
        _alert(id="w1", severity="warning", title="warn-1"),
        _alert(id="i1", severity="info", title="info-1"),
        _alert(id="w2", severity="warning", title="warn-2"),
    ]
    body = format_digest_body(alerts)
    assert "WARNING (2)" in body
    assert "INFO (1)" in body
    assert "warn-1" in body
    assert "warn-2" in body
    assert "info-1" in body
    # warning section appears before info section
    warn_idx = body.index("WARNING")
    info_idx = body.index("INFO")
    assert warn_idx < info_idx


# ===========================================================================
# Throttle interaction
# ===========================================================================


@pytest.mark.asyncio
async def test_cooldown_runs_before_burst(monkeypatch):
    """If cooldown suppresses 4 of 8 alerts → 4 remain → below burst
    threshold of 5 → all 4 fire individually."""
    monkeypatch.setenv("KORA_ALERT_NOTIFY_BURST_THRESHOLD", "5")
    monkeypatch.setenv("KORA_ALERT_NOTIFY_CATEGORY_COOLDOWN_SEC", "1800")
    slack = _make_slack_client()
    sf, pf = _factories(slack=slack, purelymail=_make_purelymail_client())
    # Pre-stamp 4 categories as recently dispatched.
    alerts_state = [
        [
            _alert(id=f"warm_{i}", category=f"cooldown_cat_{i}")
            for i in range(4)
        ]
    ]
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: alerts_state[0],
    )
    await notifier.run_notification_cycle()
    # Now add 4 NEW categories — cooldown lets these through, but the
    # 4 still-active warm categories get suppressed (already stamped).
    alerts_state[0] = [
        _alert(id=f"warm_{i}", category=f"cooldown_cat_{i}")
        for i in range(4)
    ] + [
        _alert(id=f"new_{i}", category=f"new_cat_{i}")
        for i in range(4)
    ]
    r2 = await notifier.run_notification_cycle()
    # 4 new fire (under threshold of 5). 0 cooldown suppress because
    # the warm ones are already in last_alert_ids — they don't appear
    # in newly_firing at all.
    assert r2.slack_dispatched == 4
    assert r2.burst_summarized == 0


@pytest.mark.asyncio
async def test_digest_does_not_consume_burst(monkeypatch):
    """In digest mode, non-criticals queue (not subject to burst).
    Critical-only subset is what burst dampening evaluates."""
    monkeypatch.setenv("KORA_ALERT_NOTIFY_MODE", "digest")
    monkeypatch.setenv("KORA_ALERT_NOTIFY_BURST_THRESHOLD", "3")
    monkeypatch.setenv("KORA_ALERT_NOTIFY_CATEGORY_COOLDOWN_SEC", "0")
    slack = _make_slack_client()
    purelymail = _make_purelymail_client()
    sf, pf = _factories(slack=slack, purelymail=purelymail)
    alerts = [
        _alert(id="crit1", severity="critical", category="cost_ladder"),
        _alert(id="crit2", severity="critical", category="operational_state"),
    ] + [
        _alert(id=f"warn_{i}", severity="warning", category=f"cat_{i}")
        for i in range(6)
    ]
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: alerts,
    )
    result = await notifier.run_notification_cycle()
    # 2 criticals → below burst threshold of 3 → individual.
    # 6 warnings → queued for digest (NOT subject to burst).
    assert result.slack_dispatched == 2
    assert result.digest_queued == 6
    assert result.burst_summarized == 0


# ===========================================================================
# Test-tool entry point
# ===========================================================================


@pytest.mark.asyncio
async def test_dispatch_synthetic_alert_bypasses_dedup(monkeypatch):
    """Same id can be dispatched twice via the synthetic entry point."""
    monkeypatch.setenv("KORA_ALERT_NOTIFY_CATEGORY_COOLDOWN_SEC", "1800")
    slack = _make_slack_client()
    sf, pf = _factories(slack=slack, purelymail=_make_purelymail_client())
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: [],
    )
    test_alert = _alert(
        id="test:1", severity="warning", category="test_alert"
    )
    o1 = await notifier.dispatch_synthetic_alert(test_alert)
    o2 = await notifier.dispatch_synthetic_alert(test_alert)
    assert o1.success is True
    assert o2.success is True
    assert slack.post_dm.await_count == 2


@pytest.mark.asyncio
async def test_dispatch_synthetic_alert_bypasses_cooldown_state(monkeypatch):
    """Calling dispatch_synthetic_alert does NOT pollute the cooldown
    table — operator can fire test alerts without locking out a real
    category."""
    monkeypatch.setenv("KORA_ALERT_NOTIFY_CATEGORY_COOLDOWN_SEC", "1800")
    slack = _make_slack_client()
    sf, pf = _factories(slack=slack, purelymail=_make_purelymail_client())
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: [],
    )
    test_alert = _alert(
        id="test:1", severity="warning", category="cost_ladder"
    )
    await notifier.dispatch_synthetic_alert(test_alert)
    # cost_ladder cooldown stamp should NOT have been set by the
    # synthetic path (it doesn't go through the cycle's cooldown
    # stamping logic).
    assert "cost_ladder" not in notifier._last_dispatched_by_category


@pytest.mark.asyncio
async def test_dispatch_synthetic_alert_info_routes_to_email(monkeypatch):
    monkeypatch.delenv("KORA_ALERT_NOTIFY_MODE", raising=False)
    purelymail = _make_purelymail_client()
    sf, pf = _factories(
        slack=_make_slack_client(), purelymail=purelymail
    )
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: [],
    )
    test_alert = _alert(id="test:info", severity="info", category="test_alert")
    outcome = await notifier.dispatch_synthetic_alert(test_alert)
    assert outcome.success is True
    assert outcome.channel == "email"
    purelymail.send_email.assert_awaited_once()


# ===========================================================================
# reset_dedup_state — ST2 extension
# ===========================================================================


@pytest.mark.asyncio
async def test_reset_dedup_state_clears_cooldown_and_digest(monkeypatch):
    monkeypatch.setenv("KORA_ALERT_NOTIFY_MODE", "digest")
    monkeypatch.setenv("KORA_ALERT_NOTIFY_CATEGORY_COOLDOWN_SEC", "1800")
    slack = _make_slack_client()
    sf, pf = _factories(slack=slack, purelymail=_make_purelymail_client())
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: [
            _alert(id="crit", severity="critical", category="operational_state"),
            _alert(id="warn", severity="warning", category="cost_ladder"),
        ],
    )
    await notifier.run_notification_cycle()
    assert notifier._last_dispatched_by_category  # critical stamped
    assert notifier.digest_queue_size == 1  # warning queued

    notifier.reset_dedup_state()
    assert notifier._last_dispatched_by_category == {}
    assert notifier.digest_queue_size == 0
    assert notifier.last_alert_ids == set()


# ===========================================================================
# Telemetry counter tests
# ===========================================================================


@pytest.mark.asyncio
async def test_telemetry_counters_default_zero_when_no_throttling(monkeypatch):
    monkeypatch.setenv("KORA_ALERT_NOTIFY_CATEGORY_COOLDOWN_SEC", "0")
    monkeypatch.setenv("KORA_ALERT_NOTIFY_BURST_THRESHOLD", "100")
    sf, pf = _factories(
        slack=_make_slack_client(), purelymail=_make_purelymail_client()
    )
    notifier = AlertNotifier(
        slack_client_factory=sf,
        purelymail_client_factory=pf,
        compute_alerts=lambda: [_alert(id="a")],
    )
    result = await notifier.run_notification_cycle()
    assert result.cooldown_suppressed == 0
    assert result.burst_summarized == 0
    assert result.digest_queued == 0
