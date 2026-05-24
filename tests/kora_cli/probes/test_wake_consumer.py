"""Tests for KR-PROBE-WAKE-CONSUMER — wake consumer.

Scenarios:

  Formatters (pure functions):
   1. format_investigation_prompt: includes probe + category + severity
      + title + detail + snapshot details + envelope posture
   2. format_investigation_prompt: envelope_enabled=False → "diagnose-only"
   3. format_investigation_prompt: envelope_enabled=True + non-none fix → ENABLED
   4. format_operator_dm: critical → 🚨; warning → ⚠️; info → ℹ️
   5. format_fallback_text: includes probe + severity + title + detail + reason

  Debounce:
   6. Single wake → dispatched + reasoning invoked + DM sent
   7. Two wakes for same (probe, category) within window → 2nd debounced
   8. Two wakes for same probe + DIFFERENT category → both fire
   9. Window elapsed → re-fires
  10. Bypass-critical env truthy + critical severity → no debounce
  11. Bypass-critical truthy but warning severity → still debounced
  12. KORA_PROBE_DEBOUNCE_SECONDS=0 → no debounce (operator-disable)

  Engine paths:
  13. Engine None → fallback DM with reason="engine_unavailable"
  14. Engine raises → fallback DM with reason="engine_exception:<type>"
  15. Engine returns error → fallback DM
  16. Engine returns empty text → fallback DM
  17. Engine returns text → that text sent as DM body

  Slack paths:
  18. Slack client None → outcome.dm_sent=False (no crash)
  19. Operator channel ID env unset → outcome.dm_sent=False
  20. Slack post_dm raises → outcome.dm_sent=False

  Telemetry route (engine-side):
  21. IncomingMessage.source = "probe_investigation"
  22. Metadata includes probe_name + issue_category + severity +
      envelope_enabled + envelope_fix_name

  Dedup state:
  23. _mark_dispatched stamps timestamp atomically
  24. reset_debounce_state clears the map
  25. debounce_map_size reflects current state
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kora_cli.probes.wake_consumer import (
    BYPASS_CRITICAL_ENV,
    DEBOUNCE_SECONDS_ENV,
    JOSHUA_SLACK_USER_ID_ENV,
    ProbeWakeConsumer,
    WakeConsumeOutcome,
    format_fallback_text,
    format_investigation_prompt,
    format_operator_dm,
)


_JOSHUA_USER_ID = "U01JOSHUA"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.setenv(JOSHUA_SLACK_USER_ID_ENV, _JOSHUA_USER_ID)
    monkeypatch.delenv(DEBOUNCE_SECONDS_ENV, raising=False)
    monkeypatch.delenv(BYPASS_CRITICAL_ENV, raising=False)


def _make_event(
    *,
    probe: str = "fly",
    category: str = "service_unhealthy",
    severity: str = "critical",
    title: str = "Fly app(s) unreachable: HTTP 401",
    detail: str = "Fly probe reported status=unhealthy.",
    envelope_enabled: bool = False,
    envelope_fix_name: str = "restart_unhealthy_machine",
    snapshot_details: dict | None = None,
) -> dict:
    return {
        "probe": probe,
        "category": category,
        "severity": severity,
        "title": title,
        "detail": detail,
        "envelope_enabled": envelope_enabled,
        "envelope_fix_name": envelope_fix_name,
        "snapshot_details": snapshot_details or {"apps_running": 0},
    }


def _make_engine(*, text: str = "Investigation: ...", error=None):
    engine = MagicMock()
    result = MagicMock()
    result.text = text
    result.error = error
    engine.respond = AsyncMock(return_value=result)
    return engine


def _make_slack():
    client = MagicMock()
    client.post_dm = AsyncMock(return_value={"ts": "1.0"})
    return client


def _make_consumer(*, engine=None, slack=None):
    return ProbeWakeConsumer(
        reasoning_engine_factory=lambda: engine,
        slack_client_factory=lambda: slack,
    )


# ===========================================================================
# Formatters
# ===========================================================================


def test_format_investigation_prompt_includes_all_fields():
    text = format_investigation_prompt(_make_event())
    assert "fly" in text
    assert "service_unhealthy" in text
    assert "critical" in text
    assert "Fly app(s) unreachable" in text
    assert "apps_running" in text
    assert "diagnose-only" in text  # envelope_enabled=False


def test_format_investigation_prompt_envelope_enabled():
    text = format_investigation_prompt(
        _make_event(
            envelope_enabled=True,
            envelope_fix_name="restart_unhealthy_machine",
        )
    )
    assert "restart_unhealthy_machine (ENABLED)" in text
    assert "diagnose-only" not in text


def test_format_investigation_prompt_envelope_none_even_when_enabled():
    """An envelope_enabled=True with fix_name="(none)" still surfaces
    as diagnose-only — defensive against drift."""
    text = format_investigation_prompt(
        _make_event(envelope_enabled=True, envelope_fix_name="(none)")
    )
    assert "diagnose-only" in text


def test_format_operator_dm_critical_emoji():
    text = format_operator_dm(
        probe="fly", severity="critical", reasoning_text="bar"
    )
    assert text.startswith("🚨")
    assert "fly" in text
    assert "bar" in text


def test_format_operator_dm_warning_emoji():
    assert format_operator_dm(
        probe="sentry", severity="warning", reasoning_text=""
    ).startswith("⚠️")


def test_format_operator_dm_info_emoji():
    assert format_operator_dm(
        probe="doppler", severity="info", reasoning_text=""
    ).startswith("ℹ️")


def test_format_fallback_text_shape():
    text = format_fallback_text(
        _make_event(severity="warning", title="X", detail="Y"),
        reason="engine_unavailable",
    )
    assert "fly" in text
    assert "warning" in text
    assert "X" in text
    assert "Y" in text
    assert "engine_unavailable" in text


# ===========================================================================
# Debounce
# ===========================================================================


@pytest.mark.asyncio
async def test_single_wake_dispatched_reasoning_dm_sent():
    engine = _make_engine(text="Probe is degraded, suggest restart.")
    slack = _make_slack()
    consumer = _make_consumer(engine=engine, slack=slack)
    outcome = await consumer.consume_wake_event(_make_event())
    assert outcome.dispatched is True
    assert outcome.reasoning_invoked is True
    assert outcome.dm_sent is True
    assert outcome.debounce_skipped is False
    engine.respond.assert_awaited_once()
    slack.post_dm.assert_awaited_once()
    kw = slack.post_dm.await_args.kwargs
    assert kw["channel_id"] == _JOSHUA_USER_ID
    assert "Probe is degraded" in kw["text"]


@pytest.mark.asyncio
async def test_second_wake_same_pair_debounced():
    engine = _make_engine()
    slack = _make_slack()
    consumer = _make_consumer(engine=engine, slack=slack)
    await consumer.consume_wake_event(_make_event())
    outcome2 = await consumer.consume_wake_event(_make_event())
    assert outcome2.debounce_skipped is True
    assert outcome2.dispatched is False
    assert outcome2.reasoning_invoked is False
    assert outcome2.dm_sent is False
    # Engine + slack only called for the FIRST wake.
    assert engine.respond.await_count == 1
    assert slack.post_dm.await_count == 1


@pytest.mark.asyncio
async def test_different_categories_same_probe_independent():
    engine = _make_engine()
    slack = _make_slack()
    consumer = _make_consumer(engine=engine, slack=slack)
    await consumer.consume_wake_event(
        _make_event(category="service_unhealthy")
    )
    o2 = await consumer.consume_wake_event(
        _make_event(category="cost_ladder")
    )
    assert o2.dispatched is True
    assert o2.debounce_skipped is False
    assert engine.respond.await_count == 2


@pytest.mark.asyncio
async def test_debounce_window_elapsed_refires():
    engine = _make_engine()
    slack = _make_slack()
    consumer = _make_consumer(engine=engine, slack=slack)
    await consumer.consume_wake_event(_make_event())
    # Backdate the stamp to outside the default window.
    consumer._last_dispatched[("fly", "service_unhealthy")] = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    )
    o2 = await consumer.consume_wake_event(_make_event())
    assert o2.dispatched is True
    assert o2.debounce_skipped is False


@pytest.mark.asyncio
async def test_bypass_critical_truthy_critical_no_debounce(monkeypatch):
    monkeypatch.setenv(BYPASS_CRITICAL_ENV, "true")
    engine = _make_engine()
    slack = _make_slack()
    consumer = _make_consumer(engine=engine, slack=slack)
    await consumer.consume_wake_event(_make_event(severity="critical"))
    o2 = await consumer.consume_wake_event(_make_event(severity="critical"))
    assert o2.dispatched is True
    assert o2.debounce_skipped is False


@pytest.mark.asyncio
async def test_bypass_critical_truthy_warning_still_debounced(monkeypatch):
    """Bypass only applies to critical; warnings still debounce."""
    monkeypatch.setenv(BYPASS_CRITICAL_ENV, "true")
    engine = _make_engine()
    slack = _make_slack()
    consumer = _make_consumer(engine=engine, slack=slack)
    await consumer.consume_wake_event(_make_event(severity="warning"))
    o2 = await consumer.consume_wake_event(_make_event(severity="warning"))
    assert o2.debounce_skipped is True


@pytest.mark.asyncio
async def test_debounce_zero_disables_all(monkeypatch):
    """Operator opt-out: KORA_PROBE_DEBOUNCE_SECONDS=0 → no
    debouncing at all."""
    monkeypatch.setenv(DEBOUNCE_SECONDS_ENV, "0")
    engine = _make_engine()
    slack = _make_slack()
    consumer = _make_consumer(engine=engine, slack=slack)
    await consumer.consume_wake_event(_make_event())
    o2 = await consumer.consume_wake_event(_make_event())
    assert o2.dispatched is True
    assert o2.debounce_skipped is False


# ===========================================================================
# Engine paths
# ===========================================================================


@pytest.mark.asyncio
async def test_engine_none_sends_fallback_dm():
    slack = _make_slack()
    consumer = _make_consumer(engine=None, slack=slack)
    outcome = await consumer.consume_wake_event(_make_event())
    assert outcome.dispatched is True
    assert outcome.reasoning_invoked is False
    assert outcome.error == "engine_unavailable"
    assert outcome.dm_sent is True
    dm_text = slack.post_dm.await_args.kwargs["text"]
    assert "engine_unavailable" in dm_text


@pytest.mark.asyncio
async def test_engine_raises_sends_fallback_dm():
    engine = MagicMock()
    engine.respond = AsyncMock(side_effect=RuntimeError("engine boom"))
    slack = _make_slack()
    consumer = _make_consumer(engine=engine, slack=slack)
    outcome = await consumer.consume_wake_event(_make_event())
    assert outcome.reasoning_invoked is False
    assert outcome.error == "engine_exception:RuntimeError"
    assert outcome.dm_sent is True
    dm_text = slack.post_dm.await_args.kwargs["text"]
    assert "RuntimeError" in dm_text


@pytest.mark.asyncio
async def test_engine_returns_error_sends_fallback_dm():
    engine = _make_engine(text="", error="cost_ladder_halted")
    slack = _make_slack()
    consumer = _make_consumer(engine=engine, slack=slack)
    outcome = await consumer.consume_wake_event(_make_event())
    assert outcome.reasoning_invoked is False
    assert outcome.error is not None
    assert "cost_ladder_halted" in outcome.error


@pytest.mark.asyncio
async def test_engine_empty_text_sends_fallback_dm():
    engine = _make_engine(text="   ", error=None)
    slack = _make_slack()
    consumer = _make_consumer(engine=engine, slack=slack)
    outcome = await consumer.consume_wake_event(_make_event())
    assert outcome.reasoning_invoked is False
    assert outcome.error is not None
    assert "empty_response_text" in outcome.error


@pytest.mark.asyncio
async def test_engine_text_becomes_dm_body():
    engine = _make_engine(text="Investigation result: looks like a 401.")
    slack = _make_slack()
    consumer = _make_consumer(engine=engine, slack=slack)
    await consumer.consume_wake_event(_make_event())
    dm_text = slack.post_dm.await_args.kwargs["text"]
    assert "Investigation result: looks like a 401." in dm_text


# ===========================================================================
# Slack paths
# ===========================================================================


@pytest.mark.asyncio
async def test_slack_client_none_no_dm():
    engine = _make_engine()
    consumer = _make_consumer(engine=engine, slack=None)
    outcome = await consumer.consume_wake_event(_make_event())
    assert outcome.dispatched is True
    assert outcome.reasoning_invoked is True
    assert outcome.dm_sent is False


@pytest.mark.asyncio
async def test_operator_channel_id_unset_no_dm(monkeypatch):
    monkeypatch.delenv(JOSHUA_SLACK_USER_ID_ENV, raising=False)
    engine = _make_engine()
    slack = _make_slack()
    consumer = _make_consumer(engine=engine, slack=slack)
    outcome = await consumer.consume_wake_event(_make_event())
    assert outcome.dm_sent is False
    slack.post_dm.assert_not_awaited()


@pytest.mark.asyncio
async def test_slack_post_dm_raises_no_crash():
    engine = _make_engine()
    slack = _make_slack()
    slack.post_dm = AsyncMock(side_effect=RuntimeError("slack 429"))
    consumer = _make_consumer(engine=engine, slack=slack)
    outcome = await consumer.consume_wake_event(_make_event())
    assert outcome.dm_sent is False
    # Debounce stamp still set so flapping slack doesn't cause
    # duplicate investigations next cycle.
    assert consumer.debounce_map_size == 1


# ===========================================================================
# Engine input shape (route="probe_investigation")
# ===========================================================================


@pytest.mark.asyncio
async def test_incoming_message_source_is_probe_investigation():
    engine = _make_engine()
    slack = _make_slack()
    consumer = _make_consumer(engine=engine, slack=slack)
    await consumer.consume_wake_event(_make_event())
    message_arg = engine.respond.await_args.args[0]
    assert message_arg.source == "probe_investigation"


@pytest.mark.asyncio
async def test_incoming_message_metadata_includes_probe_context():
    engine = _make_engine()
    slack = _make_slack()
    consumer = _make_consumer(engine=engine, slack=slack)
    await consumer.consume_wake_event(
        _make_event(
            probe="vercel",
            category="service_unhealthy",
            severity="warning",
            envelope_enabled=False,
            envelope_fix_name="(none)",
        )
    )
    metadata = engine.respond.await_args.args[0].metadata
    assert metadata["probe_name"] == "vercel"
    assert metadata["issue_category"] == "service_unhealthy"
    assert metadata["severity"] == "warning"
    assert metadata["envelope_enabled"] is False
    assert metadata["envelope_fix_name"] == "(none)"


# ===========================================================================
# Dedup state
# ===========================================================================


@pytest.mark.asyncio
async def test_dedup_state_advanced_after_dispatch():
    engine = _make_engine()
    slack = _make_slack()
    consumer = _make_consumer(engine=engine, slack=slack)
    assert consumer.debounce_map_size == 0
    await consumer.consume_wake_event(_make_event(probe="fly"))
    await consumer.consume_wake_event(
        _make_event(probe="vercel", category="service_unhealthy")
    )
    assert consumer.debounce_map_size == 2


@pytest.mark.asyncio
async def test_reset_debounce_state_clears():
    engine = _make_engine()
    slack = _make_slack()
    consumer = _make_consumer(engine=engine, slack=slack)
    await consumer.consume_wake_event(_make_event())
    assert consumer.debounce_map_size == 1
    consumer.reset_debounce_state()
    assert consumer.debounce_map_size == 0
