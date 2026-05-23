"""Tests for kora_cli.audit.reasoning_xref.

Cross-references kora_audit_log.jsonl reasoning rows with
slack_dm_log.jsonl outbound entries to populate model_used /
tokens / cost_rung_at_call / response_text_truncated_200 on
reasoning panel rows.

Scenarios:
  1. Audit-only path (no slack_dm log) → ReasoningCall with null
     model fields (graceful degradation; same shape as PR #141
     pre-xref behavior)
  2. Successful xref via channel_id + thread_ts match → model
     fields populated
  3. Successful xref via timestamp-window fallback → model fields
     populated even when thread_ts doesn't match
  4. Outbound entry outside ±60s window → no xref, graceful
     degradation
  5. Cost-rung derivation per model (opus / sonnet / haiku /
     unknown / hard-stop)
  6. caller_session_id shape for other sources (email/mcp/unknown)
     → no xref attempt; graceful degradation
  7. response_text_truncated_200 capped at 200 chars
  8. Multiple groups within window — each picks its own match
  9. SECURITY: walk-payload sweep (with response_text carve-out)
 10. Malformed slack_dm log line tolerated
 11. Empty slack_dm log → all rows graceful-degrade
 12. cost_ladder_halted xref status supersedes audit status
"""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from kora_cli.audit.jsonl_sink import AUDIT_LOG_FILENAME
from kora_cli.audit.reasoning_xref import (
    _derive_cost_rung,
    _parse_slack_dm_session_id,
    _truncate_response_text,
    load_reasoning_calls_with_xref,
)


_ANTHROPIC_KEY_SHAPE = re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b")
_HEX_SECRET_SHAPE = re.compile(r"\b[0-9a-fA-F]{32,}\b")

_DM_CHANNEL = "D0123456789ABCDEF"
_EVENT_TS = "1779380123.456"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Per #137/#141 fixture lesson — monkeypatch get_kora_home in
    all 3 module namespaces. The reasoning_xref helper uses a local
    kora_constants import in _slack_dm_log_path() so the patch in
    kora_constants is sufficient for the xref reader; the audit
    reader has the same local-import pattern."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setattr("kora_constants.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr("kora_cli.config.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr("kora_cli.web_server.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr(
        "kora_cli.config.get_config_path",
        lambda: tmp_path / "config.yaml",
    )
    monkeypatch.setattr(
        "kora_cli.config.get_env_path", lambda: tmp_path / ".env"
    )
    return tmp_path


def _iso(minutes_ago: int = 5) -> str:
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _audit_entry(
    *,
    seam: str = "reasoning.tool_called",
    minutes_ago: int = 5,
    tool_name: str = "get_state",
    tool_status: str = "ok",
    tool_duration_ms: int = 100,
    caller_session_id: str = f"{_DM_CHANNEL}:{_EVENT_TS}",
    triggered_by: str = "slack_dm",
    source: str = "reasoning",
) -> Dict[str, Any]:
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {
        "emitted_at": ts.isoformat(),
        "seam": seam,
        "details": {
            "tool_name": tool_name,
            "triggered_by": triggered_by,
            "tool_duration_ms": tool_duration_ms,
            "tool_status": tool_status,
        },
        "caller_session_id": caller_session_id,
        "source": source,
    }


def _outbound_entry(
    *,
    minutes_ago: int = 5,
    channel_id: str = _DM_CHANNEL,
    thread_ts: str = _EVENT_TS,
    text: str = "ok",
    model_used: str | None = "claude-opus-4-7",
    input_tokens: int | None = 842,
    output_tokens: int | None = 127,
    reasoning_duration_ms: int | None = 1247,
    reasoning_error: str | None = None,
    send_status: str = "ok",
) -> Dict[str, Any]:
    """Outbound JSONL shape per slack_dm_handler.py:811-833."""
    entry: Dict[str, Any] = {
        "sent_at": _iso(minutes_ago),
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "text": text,
        "slack_message_ts": f"{minutes_ago}.0",
        "send_status": send_status,
    }
    if model_used is not None:
        entry["model_used"] = model_used
    if input_tokens is not None:
        entry["input_tokens"] = input_tokens
    if output_tokens is not None:
        entry["output_tokens"] = output_tokens
    if reasoning_duration_ms is not None:
        entry["reasoning_duration_ms"] = reasoning_duration_ms
    if reasoning_error is not None:
        entry["reasoning_error"] = reasoning_error
    return entry


def write_audit(env_path: Path, entries: List[Dict[str, Any]]) -> Path:
    log_path = env_path / AUDIT_LOG_FILENAME
    with log_path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return log_path


def write_slack_dm(env_path: Path, entries: List[Dict[str, Any]]) -> Path:
    log_path = env_path / "slack_dm_log.jsonl"
    with log_path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return log_path


# ---- _parse_slack_dm_session_id ----------------------------------


def test_parse_slack_dm_session_id_happy_path():
    result = _parse_slack_dm_session_id(f"{_DM_CHANNEL}:{_EVENT_TS}")
    assert result == (_DM_CHANNEL, _EVENT_TS)


def test_parse_session_id_other_sources_return_none():
    """email / mcp / unknown / fallback shapes don't parse — they
    have no slack_dm correlation target."""
    assert _parse_slack_dm_session_id("email:msg-id-123") is None
    assert _parse_slack_dm_session_id("mcp:claude_pm:get_state") is None
    assert _parse_slack_dm_session_id("unknown") is None
    assert _parse_slack_dm_session_id("slack_dm:unknown") is None
    assert _parse_slack_dm_session_id("") is None
    assert _parse_slack_dm_session_id(None) is None


# ---- _derive_cost_rung -------------------------------------------


def test_derive_cost_rung_opus_normal():
    assert _derive_cost_rung("claude-opus-4-7", None) == "normal"
    assert _derive_cost_rung("claude-opus-5-0", None) == "normal", (
        "Substring match so future model revs keep working"
    )


def test_derive_cost_rung_sonnet_warn_75():
    assert _derive_cost_rung("claude-sonnet-4-6", None) == "warn_75"


def test_derive_cost_rung_haiku_downshift_90():
    assert _derive_cost_rung("claude-haiku-4-5-20251001", None) == "downshift_90"


def test_derive_cost_rung_cost_ladder_halted_overrides_model():
    """When reasoning_error is cost_ladder_halted, the rung is
    hard_stop_100 regardless of model_used (engine refused before
    making the SDK call, but the writer may still have a stale
    model_used from a previous turn)."""
    assert _derive_cost_rung("claude-opus-4-7", "cost_ladder_halted") == "hard_stop_100"
    assert _derive_cost_rung(None, "cost_ladder_halted") == "hard_stop_100"


def test_derive_cost_rung_unmapped_model_unknown():
    assert _derive_cost_rung("some-future-model-99", None) == "unknown"
    assert _derive_cost_rung(None, None) == "unknown"
    assert _derive_cost_rung("", None) == "unknown"


# ---- _truncate_response_text -------------------------------------


def test_truncate_response_text_under_cap_passes_through():
    short = "hello world"
    assert _truncate_response_text(short) == short


def test_truncate_response_text_at_cap():
    text = "x" * 200
    assert _truncate_response_text(text) == text


def test_truncate_response_text_over_cap_truncated_with_ellipsis():
    text = "x" * 250
    result = _truncate_response_text(text)
    assert len(result) == 201  # 200 chars + ellipsis
    assert result.endswith("…")


def test_truncate_response_text_none_passes_through():
    assert _truncate_response_text(None) is None


# ---- 1. Audit-only path: graceful degradation -----------------


def test_audit_only_no_slack_dm_log_returns_null_model_fields(env):
    write_audit(env, [_audit_entry()])
    calls, raw_count = load_reasoning_calls_with_xref()
    assert len(calls) == 1
    call = calls[0]
    assert call["model_used"] is None
    assert call["input_tokens"] == 0
    assert call["output_tokens"] == 0
    assert call["response_text_truncated_200"] is None
    assert call["cost_rung_at_call"] == "unknown"
    # Group structure still present
    assert call["tools_used"] == ["get_state"]
    assert call["status"] == "ok"


# ---- 2. Successful xref via thread_ts match -------------------


def test_successful_xref_populates_model_fields(env):
    write_audit(env, [_audit_entry()])
    write_slack_dm(env, [_outbound_entry()])
    calls, _ = load_reasoning_calls_with_xref()
    call = calls[0]
    assert call["model_used"] == "claude-opus-4-7"
    assert call["input_tokens"] == 842
    assert call["output_tokens"] == 127
    assert call["response_text_truncated_200"] == "ok"
    assert call["cost_rung_at_call"] == "normal"


def test_xref_uses_thread_ts_match_when_available(env):
    """thread_ts == event_ts is the natural threading match; even
    when multiple outbound entries are in the channel, the matcher
    picks the one whose thread_ts ties back to this inbound."""
    write_audit(env, [_audit_entry()])
    write_slack_dm(env, [
        # Different thread, would be picked by time but ignored
        # because thread_ts mismatches.
        _outbound_entry(
            minutes_ago=5,
            thread_ts="other-thread.001",
            model_used="claude-sonnet-4-6",
            text="other",
        ),
        # Correct thread match
        _outbound_entry(
            minutes_ago=5,
            thread_ts=_EVENT_TS,
            model_used="claude-opus-4-7",
            text="right",
        ),
    ])
    calls, _ = load_reasoning_calls_with_xref()
    assert calls[0]["model_used"] == "claude-opus-4-7"
    assert calls[0]["response_text_truncated_200"] == "right"


# ---- 3. Timestamp-window fallback -----------------------------


def test_timestamp_window_fallback_when_thread_ts_mismatches(env):
    """If no outbound thread_ts matches event_ts but an outbound
    in the same channel is within ±60s of the audit emitted_at,
    use it (best-effort correlation)."""
    write_audit(env, [_audit_entry(minutes_ago=5)])
    write_slack_dm(env, [
        # No thread_ts match, but in-window by time
        _outbound_entry(
            minutes_ago=5,
            thread_ts="completely-unrelated",
            model_used="claude-sonnet-4-6",
        ),
    ])
    calls, _ = load_reasoning_calls_with_xref()
    assert calls[0]["model_used"] == "claude-sonnet-4-6"


# ---- 4. Outside-window degrades gracefully ---------------------


def test_outbound_outside_60s_window_no_xref(env):
    write_audit(env, [_audit_entry(minutes_ago=5)])
    write_slack_dm(env, [
        # 30 min ago — way outside ±60s window
        _outbound_entry(
            minutes_ago=30,
            thread_ts="unrelated",
            model_used="claude-opus-4-7",
        ),
    ])
    calls, _ = load_reasoning_calls_with_xref()
    assert calls[0]["model_used"] is None, (
        "Outbound outside ±60s window must NOT match — graceful "
        "degradation to null fields"
    )


# ---- 5. Different-channel outbound ignored --------------------


def test_outbound_different_channel_ignored(env):
    write_audit(env, [_audit_entry()])
    write_slack_dm(env, [
        _outbound_entry(channel_id="DOTHERCHANNEL", model_used="claude-opus-4-7"),
    ])
    calls, _ = load_reasoning_calls_with_xref()
    assert calls[0]["model_used"] is None, (
        "Outbound from different channel must NOT cross-correlate"
    )


# ---- 6. Non-slack_dm session IDs degrade -----------------------


def test_email_session_id_skips_xref(env):
    """email-shaped session ids have no slack_dm correlation
    target — KR-REASONING-PANEL-EMAIL-XREF follow-on handles."""
    write_audit(env, [_audit_entry(caller_session_id="email:msg-123")])
    write_slack_dm(env, [_outbound_entry()])
    calls, _ = load_reasoning_calls_with_xref()
    # Group still rendered, fields stay null
    assert len(calls) == 1
    assert calls[0]["model_used"] is None


def test_mcp_session_id_skips_xref(env):
    write_audit(env, [_audit_entry(caller_session_id="mcp:claude_pm:get_state")])
    write_slack_dm(env, [_outbound_entry()])
    calls, _ = load_reasoning_calls_with_xref()
    assert calls[0]["model_used"] is None


# ---- 7. Multiple groups + multiple outbound ---------------------


def test_multiple_groups_each_pick_own_match(env):
    """Two distinct sessions; each xrefs to its own outbound."""
    write_audit(env, [
        _audit_entry(
            caller_session_id=f"{_DM_CHANNEL}:111.111",
            tool_name="t-a",
            minutes_ago=10,
        ),
        _audit_entry(
            caller_session_id=f"{_DM_CHANNEL}:222.222",
            tool_name="t-b",
            minutes_ago=5,
        ),
    ])
    write_slack_dm(env, [
        _outbound_entry(
            minutes_ago=10,
            thread_ts="111.111",
            model_used="claude-opus-4-7",
            text="reply-a",
        ),
        _outbound_entry(
            minutes_ago=5,
            thread_ts="222.222",
            model_used="claude-sonnet-4-6",
            text="reply-b",
        ),
    ])
    calls, _ = load_reasoning_calls_with_xref()
    # Newest first by group started_at
    by_id = {c["id"]: c for c in calls}
    a = next(c for c in calls if c["response_text_truncated_200"] == "reply-a")
    b = next(c for c in calls if c["response_text_truncated_200"] == "reply-b")
    assert a["model_used"] == "claude-opus-4-7"
    assert a["cost_rung_at_call"] == "normal"
    assert b["model_used"] == "claude-sonnet-4-6"
    assert b["cost_rung_at_call"] == "warn_75"


# ---- 8. response_text truncation -----------------------------


def test_long_response_text_truncated_to_200_chars(env):
    long_text = "x" * 500
    write_audit(env, [_audit_entry()])
    write_slack_dm(env, [_outbound_entry(text=long_text)])
    calls, _ = load_reasoning_calls_with_xref()
    text = calls[0]["response_text_truncated_200"]
    assert len(text) == 201  # 200 + ellipsis
    assert text.endswith("…")


# ---- 9. cost_ladder_halted xref supersedes status ------------


def test_cost_ladder_halted_xref_supersedes_audit_status(env):
    """When the xref'd outbound has reasoning_error=cost_ladder_halted,
    the call's status surfaces as halted even if the audit rows
    showed ok (e.g., the engine refused before logging any tool
    calls; alternatively a stale ok row + a halt on a later turn)."""
    write_audit(env, [_audit_entry(tool_status="ok")])
    write_slack_dm(env, [
        _outbound_entry(
            model_used=None,
            reasoning_error="cost_ladder_halted",
            text=None,
        ),
    ])
    calls, _ = load_reasoning_calls_with_xref()
    assert calls[0]["status"] == "halted"
    assert calls[0]["error_code"] == "cost_ladder_halted"
    assert calls[0]["cost_rung_at_call"] == "hard_stop_100"


# ---- 10. Malformed slack_dm log tolerated ---------------------


def test_malformed_slack_dm_line_skipped(env, caplog):
    """Malformed line in slack_dm log → log + skip, other entries
    still parsed (same discipline as audit reader)."""
    write_audit(env, [_audit_entry()])
    log_path = env / "slack_dm_log.jsonl"
    with log_path.open("w", encoding="utf-8") as f:
        f.write("{NOT VALID JSON{{{\n")
        f.write(json.dumps(_outbound_entry()) + "\n")

    import logging
    with caplog.at_level(logging.WARNING):
        calls, _ = load_reasoning_calls_with_xref()
    assert calls[0]["model_used"] == "claude-opus-4-7"


# ---- 11. raw_in_window_count semantics ------------------------


def test_raw_in_window_count_uses_individual_rows_not_groups(env):
    """Per PR #141 rationale: aggregate counts must reflect
    INDIVIDUAL audit rows, not groups, so the headline number
    represents activity volume."""
    write_audit(env, [
        _audit_entry(caller_session_id="s1", tool_name="a"),
        _audit_entry(caller_session_id="s1", tool_name="b"),
        _audit_entry(caller_session_id="s1", tool_name="c"),
        _audit_entry(caller_session_id="s2", tool_name="d"),
    ])
    calls, raw_count = load_reasoning_calls_with_xref()
    assert len(calls) == 2  # 2 groups
    assert raw_count == 4  # 4 individual rows


# ---- 12. SECURITY walk-payload --------------------------------


def test_no_token_shapes_anywhere_in_xref_output(env):
    write_audit(env, [_audit_entry()])
    write_slack_dm(env, [_outbound_entry(text="ok response text")])
    calls, _ = load_reasoning_calls_with_xref()
    blob = json.dumps(calls)
    assert _ANTHROPIC_KEY_SHAPE.findall(blob) == []
    assert _HEX_SECRET_SHAPE.findall(blob) == []


# ---- 13. Endpoint integration --------------------------------


@pytest.mark.asyncio
async def test_endpoint_xref_populates_fields_when_slack_dm_present(env):
    """End-to-end: hit the endpoint with both audit + slack_dm
    fixture files. Verifies the endpoint actually wires through
    the xref helper, not just the helper itself."""
    write_audit(env, [_audit_entry()])
    write_slack_dm(env, [_outbound_entry()])

    from kora_cli import web_server

    result = await web_server.list_recent_reasoning()
    assert len(result["calls"]) == 1
    call = result["calls"][0]
    assert call["model_used"] == "claude-opus-4-7"
    assert call["input_tokens"] == 842
    assert call["cost_rung_at_call"] == "normal"
    # by_model_24h aggregate also reflects xref
    assert result["by_model_24h"].get("claude-opus-4-7") == 1
    assert result["tokens_total_24h"]["input"] == 842


@pytest.mark.asyncio
async def test_endpoint_graceful_degradation_when_slack_dm_missing(env):
    """End-to-end graceful degradation: audit present, slack_dm
    absent → endpoint returns rows with null model fields, no
    crash."""
    write_audit(env, [_audit_entry()])
    # No slack_dm file written

    from kora_cli import web_server

    result = await web_server.list_recent_reasoning()
    assert len(result["calls"]) == 1
    assert result["calls"][0]["model_used"] is None
    assert result["by_model_24h"] == {}
    assert result["tokens_total_24h"] == {"input": 0, "output": 0}
