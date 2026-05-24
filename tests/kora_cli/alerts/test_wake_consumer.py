"""Tests for kora_cli.alerts.wake_consumer — KR-ALERT-INVESTIGATION-WAKE-CONSUMER.

Covers:
  * filter: aggregate / non-ok rows skipped (filtered_skipped=True)
  * debounce: second event within window skipped (debounce_skipped=True)
  * engine_unavailable → fallback DM sent
  * engine raise → fallback DM sent
  * happy path: reasoning called → DM sent + alert.investigation_completed
    audit emitted with correct shape
  * caller_session_id wired ``alert:{category}:{severity}`` everywhere
  * source attribution on the engine call → route="alert_investigation"
  * slack_dm_log entry written with caller_session_id (the 4-stream join key)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kora_cli.alerts.wake_consumer import (
    BYPASS_CRITICAL_ENV,
    DEBOUNCE_SECONDS_ENV,
    JOSHUA_SLACK_USER_ID_ENV,
    AlertWakeConsumer,
    format_fallback_text,
    format_investigation_prompt,
    format_operator_dm,
)


_JOSHUA_USER_ID = "U01JOSHUA"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setenv(
        "KORA_AUDIT_LOG_PATH", str(tmp_path / "kora_audit_log.jsonl")
    )
    monkeypatch.setenv(
        "KORA_SLACK_DM_LOG_PATH", str(tmp_path / "slack_dm_log.jsonl")
    )
    monkeypatch.setenv(JOSHUA_SLACK_USER_ID_ENV, _JOSHUA_USER_ID)
    monkeypatch.delenv(DEBOUNCE_SECONDS_ENV, raising=False)
    monkeypatch.delenv(BYPASS_CRITICAL_ENV, raising=False)
    return tmp_path


def _make_event(
    *,
    alert_id: str = "cost_warn_75",
    category: str = "cost_ladder",
    severity: str = "warning",
    channel: str = "slack",
    status: str = "ok",
) -> dict:
    return {
        "alert_id": alert_id,
        "category": category,
        "severity": severity,
        "channel": channel,
        "status": status,
    }


def _make_engine(*, text: str = "Investigation: ...", error=None):
    engine = MagicMock()
    result = MagicMock()
    result.text = text
    result.error = error
    result.model_used = "claude-haiku-4-5-20251001"
    result.input_tokens = 120
    result.output_tokens = 40
    result.cache_creation_input_tokens = 0
    result.cache_read_input_tokens = 0
    engine.respond = AsyncMock(return_value=result)
    return engine


def _make_slack():
    client = MagicMock()
    client.post_dm = AsyncMock(return_value={"ts": "1.0"})
    return client


def _make_consumer(*, engine=None, slack=None):
    return AlertWakeConsumer(
        reasoning_engine_factory=lambda: engine,
        slack_client_factory=lambda: slack,
    )


def _read_audit(tmp_path) -> list:
    path = tmp_path / "kora_audit_log.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _read_slack_dm_log(tmp_path) -> list:
    path = tmp_path / "slack_dm_log.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ===========================================================================
# Formatters
# ===========================================================================


def test_format_investigation_prompt_includes_all_fields():
    text = format_investigation_prompt(_make_event())
    assert "cost_ladder" in text
    assert "warning" in text
    assert "cost_warn_75" in text
    assert "slack" in text


def test_format_operator_dm_severity_emoji():
    text = format_operator_dm(
        category="cost_ladder", severity="critical", reasoning_text="bar"
    )
    assert text.startswith("🚨")
    assert "cost_ladder" in text
    assert "bar" in text


def test_format_fallback_text_includes_reason():
    text = format_fallback_text(_make_event(), reason="cost_ladder_halted")
    assert "cost_ladder_halted" in text


# ===========================================================================
# Filter (aggregate / non-ok rows)
# ===========================================================================


@pytest.mark.asyncio
async def test_burst_summary_channel_filtered_skipped():
    """notification.dispatched with channel="slack-burst-summary" or
    a synthetic channel value isn't a per-alert row — skip."""
    consumer = _make_consumer(engine=_make_engine(), slack=_make_slack())
    event = _make_event(channel="burst_summary")  # not in allowlist
    outcome = await consumer.consume_alert_event(event)
    assert outcome.filtered_skipped is True
    assert outcome.dispatched is False


@pytest.mark.asyncio
async def test_failed_dispatch_filtered_skipped():
    consumer = _make_consumer(engine=_make_engine(), slack=_make_slack())
    event = _make_event(status="failed")
    outcome = await consumer.consume_alert_event(event)
    assert outcome.filtered_skipped is True


# ===========================================================================
# Debounce
# ===========================================================================


@pytest.mark.asyncio
async def test_second_dispatch_same_category_severity_debounced():
    consumer = _make_consumer(engine=_make_engine(), slack=_make_slack())
    o1 = await consumer.consume_alert_event(_make_event())
    o2 = await consumer.consume_alert_event(_make_event())
    assert o1.dispatched is True
    assert o2.dispatched is False
    assert o2.debounce_skipped is True


@pytest.mark.asyncio
async def test_different_category_independent():
    consumer = _make_consumer(engine=_make_engine(), slack=_make_slack())
    await consumer.consume_alert_event(
        _make_event(category="cost_ladder")
    )
    out = await consumer.consume_alert_event(
        _make_event(category="service_unhealthy")
    )
    assert out.dispatched is True


@pytest.mark.asyncio
async def test_debounce_zero_disables(monkeypatch):
    monkeypatch.setenv(DEBOUNCE_SECONDS_ENV, "0")
    consumer = _make_consumer(engine=_make_engine(), slack=_make_slack())
    await consumer.consume_alert_event(_make_event())
    out = await consumer.consume_alert_event(_make_event())
    # Without the window, both dispatch.
    assert out.dispatched is True


@pytest.mark.asyncio
async def test_critical_bypass_truthy_skips_debounce(monkeypatch):
    monkeypatch.setenv(BYPASS_CRITICAL_ENV, "true")
    consumer = _make_consumer(engine=_make_engine(), slack=_make_slack())
    await consumer.consume_alert_event(
        _make_event(severity="critical")
    )
    out = await consumer.consume_alert_event(
        _make_event(severity="critical")
    )
    assert out.dispatched is True


# ===========================================================================
# Engine paths
# ===========================================================================


@pytest.mark.asyncio
async def test_engine_unavailable_sends_fallback_dm(tmp_path):
    """engine=None → fallback DM (still sent), no reasoning_invoked."""
    consumer = _make_consumer(engine=None, slack=_make_slack())
    out = await consumer.consume_alert_event(_make_event())
    assert out.dispatched is True
    assert out.reasoning_invoked is False
    assert out.dm_sent is True


@pytest.mark.asyncio
async def test_engine_raise_sends_fallback_dm(tmp_path):
    engine = MagicMock()
    engine.respond = AsyncMock(side_effect=RuntimeError("boom"))
    consumer = _make_consumer(engine=engine, slack=_make_slack())
    out = await consumer.consume_alert_event(_make_event())
    assert out.dispatched is True
    assert out.reasoning_invoked is False
    assert out.dm_sent is True
    assert out.error is not None and out.error.startswith("engine_exception:")


@pytest.mark.asyncio
async def test_engine_returns_error_sends_fallback_dm():
    engine = _make_engine(error="cost_ladder_halted", text="")
    consumer = _make_consumer(engine=engine, slack=_make_slack())
    out = await consumer.consume_alert_event(_make_event())
    assert out.reasoning_invoked is False
    assert out.error == "cost_ladder_halted"
    assert out.dm_sent is True


@pytest.mark.asyncio
async def test_happy_path_dispatches_and_audits(tmp_path):
    """End-to-end happy path: reasoning called → DM sent → both
    alert.investigation_completed AND slack_dm_log entry written
    with the same caller_session_id."""
    engine = _make_engine(text="Cost is at 76% — review burn rate")
    slack = _make_slack()
    consumer = _make_consumer(engine=engine, slack=slack)
    outcome = await consumer.consume_alert_event(_make_event())

    assert outcome.dispatched is True
    assert outcome.reasoning_invoked is True
    assert outcome.dm_sent is True
    slack.post_dm.assert_awaited_once()

    # Stream 2 — alert.investigation_completed audit.
    audit = _read_audit(tmp_path)
    completed = [
        e for e in audit if e["seam"] == "alert.investigation_completed"
    ]
    assert len(completed) == 1
    details = completed[0]["details"]
    assert details["alert_id"] == "cost_warn_75"
    assert details["category"] == "cost_ladder"
    assert details["severity"] == "warning"
    assert details["dm_status"] == "sent"
    assert details["model_used"] == "claude-haiku-4-5-20251001"
    assert details["investigation_summary_text"].startswith("Cost is at 76%")
    assert details["autoaction_attempted"] is False  # v1 hardcoded

    # caller_session_id pattern (alert:{category}:{severity})
    assert (
        completed[0]["caller_session_id"]
        == "alert:cost_ladder:warning"
    )

    # Stream 3 — slack_dm_log entry with the same caller_session_id.
    dm_log = _read_slack_dm_log(tmp_path)
    assert len(dm_log) == 1
    assert dm_log[0]["caller_session_id"] == "alert:cost_ladder:warning"
    assert dm_log[0]["model_used"] == "claude-haiku-4-5-20251001"
    assert dm_log[0]["send_status"] == "ok"


@pytest.mark.asyncio
async def test_incoming_message_source_is_alert_investigation():
    """Engine receives IncomingMessage with source='alert_investigation'
    so the engine's bypass-path telemetry mapping attributes the call
    to ROUTE_ALERT_INVESTIGATION (per #190's wire)."""
    engine = _make_engine()
    consumer = _make_consumer(engine=engine, slack=_make_slack())
    await consumer.consume_alert_event(_make_event())
    engine.respond.assert_awaited_once()
    (message, _context), _ = engine.respond.await_args
    assert message.source == "alert_investigation"
    assert message.metadata["category"] == "cost_ladder"
    assert message.metadata["alert_id"] == "cost_warn_75"


@pytest.mark.asyncio
async def test_slack_client_none_skips_dm_but_still_audits(tmp_path):
    """slack=None → DM not sent, but the alert.investigation_completed
    audit still fires with dm_status reflecting the failure."""
    consumer = _make_consumer(engine=_make_engine(), slack=None)
    out = await consumer.consume_alert_event(_make_event())
    assert out.dm_sent is False
    audit = _read_audit(tmp_path)
    completed = [
        e for e in audit if e["seam"] == "alert.investigation_completed"
    ]
    assert len(completed) == 1
    assert completed[0]["details"]["dm_status"] == "failed_send"


@pytest.mark.asyncio
async def test_reset_debounce_state_clears_map():
    consumer = _make_consumer(engine=_make_engine(), slack=_make_slack())
    await consumer.consume_alert_event(_make_event())
    assert consumer.debounce_map_size == 1
    consumer.reset_debounce_state()
    assert consumer.debounce_map_size == 0


# ===========================================================================
# KR-CC1-POLISH (#198) — fallback DM wording + dm_status assertion
# ===========================================================================


def test_format_fallback_text_includes_review_manually_footer():
    """The fallback footer must include the explicit "review +
    act manually" guidance so the operator isn't left wondering
    whether Kora will retry."""
    text = format_fallback_text(
        _make_event(), reason="engine_unavailable"
    )
    assert "Kora is unavailable to investigate" in text
    assert "Review the alerts panel" in text
    assert "act manually" in text
    assert "Kora will not retry" in text
    # Identity preserved.
    assert "cost_ladder" in text
    assert "cost_warn_75" in text


@pytest.mark.asyncio
async def test_fallback_dm_records_engine_unavailable_dm_status(
    tmp_path,
):
    """Engine None → DM sent successfully (fallback path) →
    investigation_completed audit must carry
    dm_status="engine_unavailable_fallback" verbatim. CC#2's
    KR-FE-ALERT-INVESTIGATIONS-VIEWER renders that enum value."""
    consumer = _make_consumer(engine=None, slack=_make_slack())
    outcome = await consumer.consume_alert_event(_make_event())
    assert outcome.dm_sent is True
    assert outcome.reasoning_invoked is False
    audit = _read_audit(tmp_path)
    completed = [
        r for r in audit if r["seam"] == "alert.investigation_completed"
    ]
    assert len(completed) == 1
    details = completed[0]["details"]
    assert details["dm_status"] == "engine_unavailable_fallback"
    # investigation_summary_text contains the fallback wording,
    # not an empty / placeholder string.
    assert "Kora is unavailable to investigate" in details[
        "investigation_summary_text"
    ]
    assert details["reasoning_error"] == "engine_unavailable"
