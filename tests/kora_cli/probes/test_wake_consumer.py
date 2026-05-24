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
    from kora_cli.audit.jsonl_sink import (
        BATCH_SIZE_ENV,
        _reset_batching_for_tests,
    )

    monkeypatch.setenv(JOSHUA_SLACK_USER_ID_ENV, _JOSHUA_USER_ID)
    monkeypatch.delenv(DEBOUNCE_SECONDS_ENV, raising=False)
    monkeypatch.delenv(BYPASS_CRITICAL_ENV, raising=False)
    # KR-PROBE-DEBOUNCE — default-behavior tests in this file predate
    # the consecutive-failure upgrade. Force required=1 here so
    # single-tick scenarios still dispatch; dedicated consecutive-
    # buffering tests set their own env explicitly.
    monkeypatch.setenv("KORA_PROBE_DEBOUNCE_CONSECUTIVE_REQUIRED", "1")
    # KR-CHEAP-AUDIT-BATCHING — these tests read the audit JSONL
    # immediately after the consumer emits + assume sync semantics.
    # Force per-emit writes to keep that contract; dedicated
    # batching tests live in tests/kora_cli/audit/test_jsonl_sink.py.
    monkeypatch.setenv(BATCH_SIZE_ENV, "0")
    _reset_batching_for_tests()
    yield
    _reset_batching_for_tests()


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


# ===========================================================================
# KR-PROBE-INVESTIGATION-DATA-COMPLETION — 3 V1NotesBanner gaps
# ===========================================================================


@pytest.fixture
def _audit_redirect(tmp_path, monkeypatch):
    """Per-test audit + slack-log redirect — both surfaces are
    file-backed singletons that we redirect into tmp_path so the
    new audit-stream + outbound-log assertions are scoped to the
    test."""
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "kora_constants.get_kora_home", lambda: tmp_path, raising=False
    )
    monkeypatch.setenv(
        "KORA_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl")
    )
    monkeypatch.setenv(
        "KORA_SLACK_DM_LOG_PATH",
        str(tmp_path / "slack_dm_log.jsonl"),
    )
    return tmp_path


def _read_audit(tmp_path):
    import json as _json

    path = tmp_path / "audit.jsonl"
    if not path.exists():
        return []
    return [
        _json.loads(line)
        for line in path.read_text().splitlines()
        if line
    ]


def _read_slack_log(tmp_path):
    import json as _json

    path = tmp_path / "slack_dm_log.jsonl"
    if not path.exists():
        return []
    return [
        _json.loads(line)
        for line in path.read_text().splitlines()
        if line
    ]


def _engine_with_tokens(
    *,
    text: str = "investigation summary line",
    model_used: str = "claude-haiku-4-5-20251001",
    input_tokens: int = 1200,
    output_tokens: int = 250,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 800,
    error=None,
):
    """ResponseResult-shaped MagicMock with full token detail so
    the cost + log assertions can verify the meta projection."""
    engine = MagicMock()
    result = MagicMock()
    result.text = text
    result.error = error
    result.model_used = model_used
    result.input_tokens = input_tokens
    result.output_tokens = output_tokens
    result.cache_creation_input_tokens = cache_creation_input_tokens
    result.cache_read_input_tokens = cache_read_input_tokens
    engine.respond = AsyncMock(return_value=result)
    return engine


@pytest.mark.asyncio
async def test_completed_audit_emitted_with_meta_on_happy_path(
    _audit_redirect,
):
    """End-to-end: probe wake → reasoning runs → DM sent →
    BOTH slack_dm_log entry AND probe.investigation_completed
    audit row written, both keyed by the same caller_session_id."""
    engine = _engine_with_tokens()
    slack = _make_slack()
    consumer = _make_consumer(engine=engine, slack=slack)

    outcome = await consumer.consume_wake_event(_make_event())

    assert outcome.dispatched is True
    assert outcome.dm_sent is True
    slack.post_dm.assert_awaited_once()

    # Audit stream: probe.investigation_completed.
    audit = _read_audit(_audit_redirect)
    completed = [
        e for e in audit if e["seam"] == "probe.investigation_completed"
    ]
    assert len(completed) == 1
    row = completed[0]
    assert row["caller_session_id"] == "probe:fly:service_unhealthy"
    assert row["source"] == "reasoning"
    assert row["details"]["probe"] == "fly"
    assert row["details"]["issue_category"] == "service_unhealthy"
    assert row["details"]["severity"] == "critical"
    assert row["details"]["model_used"] == "claude-haiku-4-5-20251001"
    assert row["details"]["input_tokens"] == 1200
    assert row["details"]["output_tokens"] == 250
    assert row["details"]["cache_read_input_tokens"] == 800
    assert row["details"]["dm_status"] == "sent"
    assert row["details"]["autofix_attempted"] is False
    # Reason field is the actual reasoning text VERBATIM (operator-
    # decision-relevant — per #182 precedent, not #179 redaction).
    assert "investigation summary line" in row["details"][
        "investigation_summary_text"
    ]
    assert isinstance(row["details"]["investigation_duration_ms"], int)
    # Cost may be None when the pricing registry doesn't know the
    # model (test env doesn't ship pricing data for haiku-4-5); the
    # FIELD must always be present so the panel can render
    # "(unknown)" without a key-error.
    assert "total_cost_usd" in row["details"]

    # Outbound-log stream: slack_dm_log.jsonl.
    log = _read_slack_log(_audit_redirect)
    assert len(log) == 1
    entry = log[0]
    assert entry["caller_session_id"] == "probe:fly:service_unhealthy"
    assert entry["send_status"] == "ok"
    assert entry["model_used"] == "claude-haiku-4-5-20251001"
    assert entry["input_tokens"] == 1200
    assert entry["output_tokens"] == 250
    assert entry["channel_id"] == _JOSHUA_USER_ID
    # Same correlation key as the audit row → CC#2's viewer can
    # JOIN the two streams cleanly.
    assert (
        entry["caller_session_id"] == row["caller_session_id"]
    )


@pytest.mark.asyncio
async def test_completed_audit_fires_on_engine_unavailable_fallback(
    _audit_redirect,
):
    """Engine None → fallback DM → completed audit STILL fires
    with dm_status=engine_unavailable_fallback."""
    slack = _make_slack()
    consumer = _make_consumer(engine=None, slack=slack)

    outcome = await consumer.consume_wake_event(_make_event())
    assert outcome.dm_sent is True
    assert outcome.error == "engine_unavailable"

    audit = _read_audit(_audit_redirect)
    completed = [
        e for e in audit if e["seam"] == "probe.investigation_completed"
    ]
    assert len(completed) == 1
    details = completed[0]["details"]
    assert details["dm_status"] == "engine_unavailable_fallback"
    assert details["reasoning_error"] == "engine_unavailable"
    # No model — engine never ran.
    assert details["model_used"] is None
    assert details["total_cost_usd"] is None
    # The fallback text IS recorded (it's what the operator saw).
    assert "engine_unavailable" in details["investigation_summary_text"]


@pytest.mark.asyncio
async def test_completed_audit_on_slack_send_failure(
    _audit_redirect,
):
    """post_dm raises → outbound log written with send_status=failed
    + completed audit fires with dm_status=failed_send."""
    engine = _engine_with_tokens()
    slack = MagicMock()
    slack.post_dm = AsyncMock(side_effect=RuntimeError("transport boom"))
    consumer = _make_consumer(engine=engine, slack=slack)

    outcome = await consumer.consume_wake_event(_make_event())
    assert outcome.dm_sent is False

    audit = _read_audit(_audit_redirect)
    completed = [
        e for e in audit if e["seam"] == "probe.investigation_completed"
    ]
    assert len(completed) == 1
    assert completed[0]["details"]["dm_status"] == "failed_send"

    log = _read_slack_log(_audit_redirect)
    assert len(log) == 1
    assert log[0]["send_status"] == "failed"
    assert log[0]["failure_reason"] == "post_dm_raised:RuntimeError"
    # Same key on the failure row so the panel join still works.
    assert log[0]["caller_session_id"] == "probe:fly:service_unhealthy"


@pytest.mark.asyncio
async def test_completed_audit_on_engine_unavailable_failed_send(
    _audit_redirect,
):
    """Engine None AND no slack client → fallback path AND no DM.
    dm_status surfaces the combined failure."""
    consumer = _make_consumer(engine=None, slack=None)

    outcome = await consumer.consume_wake_event(_make_event())
    assert outcome.dm_sent is False

    audit = _read_audit(_audit_redirect)
    completed = [
        e for e in audit if e["seam"] == "probe.investigation_completed"
    ]
    assert len(completed) == 1
    assert (
        completed[0]["details"]["dm_status"]
        == "engine_unavailable_failed_send"
    )

    log = _read_slack_log(_audit_redirect)
    assert len(log) == 1
    assert log[0]["failure_reason"] == "slack_client_unavailable"


@pytest.mark.asyncio
async def test_autofix_attempted_field_picks_up_concurrent_seam(
    _audit_redirect,
):
    """If tool.probe_autofix_attempted fires with the same
    caller_session_id during the investigation, the completed
    audit row's autofix_attempted=True. We simulate the in-flight
    tool invocation by having the mock engine emit the autofix row
    as a side effect of respond() — same temporal shape as a real
    Claude tool call landing mid-respond."""
    from kora_cli.audit.jsonl_sink import emit_audit

    captured_result = MagicMock()
    captured_result.text = "investigation done"
    captured_result.error = None
    captured_result.model_used = "claude-haiku-4-5-20251001"
    captured_result.input_tokens = 100
    captured_result.output_tokens = 50
    captured_result.cache_creation_input_tokens = 0
    captured_result.cache_read_input_tokens = 0

    async def respond_with_autofix_side_effect(*args, **kwargs):
        # Emit the tool.probe_autofix_attempted row "during" the
        # respond() call. This is the temporal order the back-
        # reference relies on (autofix row written AFTER
        # investigation_started_at stamp).
        emit_audit(
            "tool.probe_autofix_attempted",
            {
                "probe": "fly",
                "action": "restart_machine",
                "target_id": "abc123",
                "reason_from_reasoning": "synthetic",
                "status": "attempted",
            },
            caller_session_id="probe:fly:service_unhealthy",
            source="reasoning",
        )
        return captured_result

    engine = MagicMock()
    engine.respond = AsyncMock(side_effect=respond_with_autofix_side_effect)
    slack = _make_slack()
    consumer = _make_consumer(engine=engine, slack=slack)

    await consumer.consume_wake_event(_make_event())

    audit = _read_audit(_audit_redirect)
    completed = [
        e for e in audit if e["seam"] == "probe.investigation_completed"
    ]
    assert len(completed) == 1
    assert completed[0]["details"]["autofix_attempted"] is True


@pytest.mark.asyncio
async def test_autofix_attempted_false_when_no_matching_session(
    _audit_redirect,
):
    """An autofix row from a DIFFERENT investigation must not
    flip autofix_attempted for this one."""
    from kora_cli.audit.jsonl_sink import emit_audit

    async def respond_with_other_session_autofix(*args, **kwargs):
        # Different caller_session_id — should NOT match.
        emit_audit(
            "tool.probe_autofix_attempted",
            {"probe": "vercel", "status": "attempted"},
            caller_session_id="probe:vercel:deploy_failed",
            source="reasoning",
        )
        r = MagicMock()
        r.text = "done"
        r.error = None
        r.model_used = None
        r.input_tokens = 0
        r.output_tokens = 0
        r.cache_creation_input_tokens = 0
        r.cache_read_input_tokens = 0
        return r

    engine = MagicMock()
    engine.respond = AsyncMock(
        side_effect=respond_with_other_session_autofix
    )
    slack = _make_slack()
    consumer = _make_consumer(engine=engine, slack=slack)

    await consumer.consume_wake_event(_make_event())

    audit = _read_audit(_audit_redirect)
    completed = [
        e for e in audit if e["seam"] == "probe.investigation_completed"
    ]
    assert completed[0]["details"]["autofix_attempted"] is False


@pytest.mark.asyncio
async def test_completed_audit_swallows_emit_failure(
    _audit_redirect, monkeypatch
):
    """Any failure in the audit emission path must not affect the
    investigation outcome — best-effort posture."""
    engine = _engine_with_tokens()
    slack = _make_slack()
    consumer = _make_consumer(engine=engine, slack=slack)

    def _emit_boom(*args, **kwargs):
        raise RuntimeError("audit disk full")

    monkeypatch.setattr(
        "kora_cli.audit.jsonl_sink.emit_audit", _emit_boom
    )

    outcome = await consumer.consume_wake_event(_make_event())
    # DM still went out; investigation didn't crash.
    assert outcome.dispatched is True
    assert outcome.dm_sent is True


@pytest.mark.asyncio
async def test_caller_session_id_consistent_across_all_streams(
    _audit_redirect,
):
    """Single fact, single key: probe wake → autofix attempt →
    completed → slack_dm_log. All four streams keyed by
    probe:fly:service_unhealthy for the same investigation."""
    from kora_cli.audit.jsonl_sink import emit_audit

    # Simulated wake row (the bucket spec says this is already
    # emitted by the probe runner per PR #163; we mint one here so
    # the assertion runs end-to-end without the runner).
    emit_audit(
        "probe.wake_requested",
        {"probe": "fly", "category": "service_unhealthy"},
        caller_session_id="probe:fly:service_unhealthy",
        source="cron",
    )
    # And an autofix-attempted row (the second of the 4 streams).
    emit_audit(
        "tool.probe_autofix_attempted",
        {"probe": "fly", "status": "attempted"},
        caller_session_id="probe:fly:service_unhealthy",
        source="reasoning",
    )

    engine = _engine_with_tokens()
    slack = _make_slack()
    consumer = _make_consumer(engine=engine, slack=slack)
    await consumer.consume_wake_event(_make_event())

    audit = _read_audit(_audit_redirect)
    log = _read_slack_log(_audit_redirect)

    keys_by_seam = {e["seam"]: e["caller_session_id"] for e in audit}
    assert (
        keys_by_seam["probe.wake_requested"]
        == "probe:fly:service_unhealthy"
    )
    assert (
        keys_by_seam["tool.probe_autofix_attempted"]
        == "probe:fly:service_unhealthy"
    )
    assert (
        keys_by_seam["probe.investigation_completed"]
        == "probe:fly:service_unhealthy"
    )
    assert log[0]["caller_session_id"] == "probe:fly:service_unhealthy"


# ===========================================================================
# Cost computation helper
# ===========================================================================


def test_compute_total_cost_usd_missing_model_returns_none():
    from kora_cli.probes.wake_consumer import _compute_total_cost_usd

    assert _compute_total_cost_usd({"model_used": None}) is None


def test_compute_total_cost_usd_handles_zero_token_meta():
    """A 0-token completion (canned-fallback shape) shouldn't crash
    + should produce a numeric (possibly zero) cost when model is
    known to the pricing registry — or None when it isn't."""
    from kora_cli.probes.wake_consumer import _compute_total_cost_usd

    out = _compute_total_cost_usd(
        {
            "model_used": "claude-opus-4-7",
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
    )
    # The pricing registry may or may not know this model in the
    # test env. The contract: returns None or a float, never raises.
    assert out is None or isinstance(out, float)


def test_compute_total_cost_usd_swallows_pricing_exception(monkeypatch):
    from kora_cli.probes import wake_consumer

    def _boom(*args, **kwargs):
        raise RuntimeError("pricing registry borked")

    monkeypatch.setattr(
        "agent.usage_pricing.estimate_usage_cost", _boom
    )
    assert (
        wake_consumer._compute_total_cost_usd(
            {
                "model_used": "claude-opus-4-7",
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            }
        )
        is None
    )


# ===========================================================================
# KR-PROBE-DEBOUNCE — consecutive-failure buffering upgrade
# ===========================================================================


@pytest.mark.asyncio
async def test_consecutive_first_failure_buffered_not_dispatched(monkeypatch):
    """With required=2, the first wake_requested event for a
    (probe, category) pair is held in the buffer rather than
    dispatched. Prevents single-tick flakes from waking Kora."""
    monkeypatch.setenv("KORA_PROBE_DEBOUNCE_CONSECUTIVE_REQUIRED", "2")
    # Severity warning so the critical-bypass path doesn't apply.
    consumer = _make_consumer(engine=_make_engine(), slack=_make_slack())
    out = await consumer.consume_wake_event(
        _make_event(severity="warning")
    )
    assert out.dispatched is False
    assert out.buffered_skipped is True
    assert out.buffered_consecutive_count == 1
    assert consumer.consecutive_buffer_size == 1


@pytest.mark.asyncio
async def test_consecutive_second_failure_dispatches(monkeypatch):
    """The second event within the window pushes the buffer to the
    threshold → dispatch fires."""
    monkeypatch.setenv("KORA_PROBE_DEBOUNCE_CONSECUTIVE_REQUIRED", "2")
    engine = _make_engine()
    slack = _make_slack()
    consumer = _make_consumer(engine=engine, slack=slack)
    out1 = await consumer.consume_wake_event(
        _make_event(severity="warning")
    )
    assert out1.dispatched is False
    out2 = await consumer.consume_wake_event(
        _make_event(severity="warning")
    )
    assert out2.dispatched is True
    assert out2.buffered_skipped is False
    # Buffer cleared after dispatch (post-dispatch flat-window
    # debounce takes over).
    assert consumer.consecutive_buffer_size == 0


@pytest.mark.asyncio
async def test_consecutive_critical_bypass_dispatches_first(monkeypatch):
    """Critical-severity wakes with the bypass env truthy skip the
    consecutive-failure buffer + dispatch on first event."""
    monkeypatch.setenv("KORA_PROBE_DEBOUNCE_CONSECUTIVE_REQUIRED", "5")
    monkeypatch.setenv(BYPASS_CRITICAL_ENV, "true")
    consumer = _make_consumer(engine=_make_engine(), slack=_make_slack())
    out = await consumer.consume_wake_event(
        _make_event(severity="critical")
    )
    assert out.dispatched is True


@pytest.mark.asyncio
async def test_consecutive_different_pairs_independent(monkeypatch):
    """Distinct (probe, category) pairs accumulate independently —
    one buffered, the other unrelated."""
    monkeypatch.setenv("KORA_PROBE_DEBOUNCE_CONSECUTIVE_REQUIRED", "2")
    consumer = _make_consumer(engine=_make_engine(), slack=_make_slack())
    await consumer.consume_wake_event(
        _make_event(probe="fly", category="machine_down", severity="warning")
    )
    out = await consumer.consume_wake_event(
        _make_event(probe="vercel", category="deploy_fail", severity="warning")
    )
    assert out.dispatched is False
    assert out.buffered_consecutive_count == 1
    assert consumer.consecutive_buffer_size == 2


@pytest.mark.asyncio
async def test_consecutive_required_one_preserves_legacy_behavior(monkeypatch):
    """required=1 disables buffering — single failure dispatches."""
    monkeypatch.setenv("KORA_PROBE_DEBOUNCE_CONSECUTIVE_REQUIRED", "1")
    consumer = _make_consumer(engine=_make_engine(), slack=_make_slack())
    out = await consumer.consume_wake_event(
        _make_event(severity="warning")
    )
    assert out.dispatched is True


@pytest.mark.asyncio
async def test_consecutive_reset_debounce_state_clears_buffer(monkeypatch):
    """``reset_debounce_state`` clears the consecutive buffer too —
    listener shutdown restart should see a clean slate."""
    monkeypatch.setenv("KORA_PROBE_DEBOUNCE_CONSECUTIVE_REQUIRED", "2")
    consumer = _make_consumer(engine=_make_engine(), slack=_make_slack())
    await consumer.consume_wake_event(_make_event(severity="warning"))
    assert consumer.consecutive_buffer_size == 1
    consumer.reset_debounce_state()
    assert consumer.consecutive_buffer_size == 0
