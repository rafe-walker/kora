"""Tests for kora_cli.audit.reasoning_xref's email-source xref path.

After CC#1's PR #146 (KR-EMAIL-OUTBOUND-REASONING-META), the email
outbound JSONL writer accepts + records the same 6 reasoning meta
fields as the slack_dm path:

  - model_used, input_tokens, output_tokens,
    reasoning_duration_ms, reasoning_error
  - caller_session_id (matches engine derivation
    ``f"email:{message_id}"`` per anthropic_engine.py:869-871)

This file covers the email-source xref extension to the helper
shipped in PR #143. The slack-first precedence + degradation
paths are tested in test_reasoning_xref.py (unchanged); email
tests focus on email-specific shape + behavior.

Scenarios:
  1. _parse_email_session_id: happy path + unknown fallback + non-email
  2. Email audit with matching outbound (PRIMARY caller_session_id)
     → model/tokens populated; response_text STAYS NULL per CC#1
     #124 design (body not in email outbound JSONL)
  3. PRIMARY caller_session_id match preferred over SECONDARY
     in_reply_to match
  4. SECONDARY in_reply_to fallback works when caller_session_id absent
  5. LAST RESORT timestamp window when both correlation keys absent
  6. Outside ±60s window → graceful degradation
  7. Cost-rung derivation per model on email path
  8. cost_ladder_halted xref supersedes audit status (same as slack)
  9. Slack-first precedence: slack_dm audit prefers slack outbound
     even when email outbound exists in the same window
 10. Empty email outbound log → email-source rows degrade gracefully
 11. Malformed email outbound line tolerated
 12. SECURITY: walk-payload sweep on email-enriched output
 13. Endpoint integration: email path populates fields end-to-end
"""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from kora_cli.audit.jsonl_sink import AUDIT_LOG_FILENAME
from kora_cli.audit.reasoning_xref import (
    _parse_email_session_id,
    load_reasoning_calls_with_xref,
)


_ANTHROPIC_KEY_SHAPE = re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b")
_HEX_SECRET_SHAPE = re.compile(r"\b[0-9a-fA-F]{32,}\b")

# Email RFC-822 message-id shape. Real Purelymail message IDs look
# like ``<random@kora.stormhavenenterprises.com>``; we use a
# representative fixture form throughout.
_INBOUND_MSG_ID = "<inbound-abc-123@joshua.example.com>"
_REPLY_MSG_ID = "<reply-xyz-456@kora.example.com>"
_OTHER_INBOUND_MSG_ID = "<other-inbound-999@joshua.example.com>"


from tests.kora_cli._panel_test_helpers import isolated_kora_home  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Per the #137/#141 fixture-isolation lesson — monkeypatch
    get_kora_home in all 3 module namespaces via the shared helper."""
    return isolated_kora_home(tmp_path, monkeypatch)


def _iso(minutes_ago: int = 5) -> str:
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _email_audit(
    *,
    minutes_ago: int = 5,
    tool_name: str = "get_state",
    tool_status: str = "ok",
    tool_duration_ms: int = 100,
    message_id: str = _INBOUND_MSG_ID,
) -> Dict[str, Any]:
    """Email-source audit entry per the engine's _derive_caller_session_id
    (``f"email:{message_id}"``)."""
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {
        "emitted_at": ts.isoformat(),
        "seam": "reasoning.tool_called",
        "details": {
            "tool_name": tool_name,
            "triggered_by": "email",
            "tool_duration_ms": tool_duration_ms,
            "tool_status": tool_status,
        },
        "caller_session_id": f"email:{message_id}",
        "source": "reasoning",
    }


def _email_outbound(
    *,
    minutes_ago: int = 5,
    in_reply_to: str = _INBOUND_MSG_ID,
    message_id: str = _REPLY_MSG_ID,
    model_used: str | None = "claude-opus-4-7",
    input_tokens: int | None = 612,
    output_tokens: int | None = 184,
    reasoning_duration_ms: int | None = 1500,
    reasoning_error: str | None = None,
    caller_session_id: str | None = None,
    send_status: str = "sent",
    from_addr: str = "kora@kora.example.com",
    to: List[str] | None = None,
    subject: str = "Re: status",
) -> Dict[str, Any]:
    """Email outbound JSONL shape per
    ``kora_cli/clients/purelymail_client.py:_append_outbound_log``
    post-PR #146. Reasoning meta fields appear opt-in when non-None."""
    entry: Dict[str, Any] = {
        "sent_at": _iso(minutes_ago),
        "from": from_addr,
        "to": to or ["joshua@joshua.example.com"],
        "subject": subject,
        "in_reply_to": in_reply_to,
        "send_status": send_status,
        "message_id": message_id,
        "smtp_code": 250,
        "error": None,
        "retry_count": 0,
        "caller_actor_kind": None,
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
    if caller_session_id is not None:
        entry["caller_session_id"] = caller_session_id
    return entry


def _slack_outbound(
    *,
    minutes_ago: int = 5,
    channel_id: str = "D0123456789",
    thread_ts: str = "1779380123.456",
    text: str = "ok",
    model_used: str | None = "claude-sonnet-4-6",
) -> Dict[str, Any]:
    """Helper for the slack-first-precedence test only."""
    entry: Dict[str, Any] = {
        "sent_at": _iso(minutes_ago),
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "text": text,
        "slack_message_ts": f"{minutes_ago}.0",
        "send_status": "ok",
    }
    if model_used is not None:
        entry["model_used"] = model_used
    return entry


def write_audit(env_path: Path, entries: List[Dict[str, Any]]) -> Path:
    log_path = env_path / AUDIT_LOG_FILENAME
    with log_path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return log_path


def write_email_outbound(env_path: Path, entries: List[Dict[str, Any]]) -> Path:
    log_path = env_path / "email_outbound_log.jsonl"
    with log_path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return log_path


def write_slack_outbound(env_path: Path, entries: List[Dict[str, Any]]) -> Path:
    log_path = env_path / "slack_dm_log.jsonl"
    with log_path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return log_path


# ---- 1. _parse_email_session_id -----------------------------------


def test_parse_email_session_id_happy_path():
    assert _parse_email_session_id(f"email:{_INBOUND_MSG_ID}") == _INBOUND_MSG_ID


def test_parse_email_session_id_unknown_fallback_returns_none():
    """The engine's _derive_caller_session_id falls back to
    ``f"email:unknown"`` when the inbound message_id is missing.
    No correlation target → None so xref skips gracefully."""
    assert _parse_email_session_id("email:unknown") is None


def test_parse_email_session_id_other_shapes_return_none():
    assert _parse_email_session_id(None) is None
    assert _parse_email_session_id("") is None
    assert _parse_email_session_id("email:") is None  # empty msg_id
    assert _parse_email_session_id("D0123:1779.456") is None  # slack
    assert _parse_email_session_id("mcp:claude_pm:tool") is None
    assert _parse_email_session_id("unknown") is None


# ---- 2. Successful email xref via PRIMARY caller_session_id -------


def test_email_xref_primary_caller_session_id_match(env):
    """Post-#146 PRIMARY correlation: outbound carries
    caller_session_id matching the engine's derivation."""
    write_audit(env, [_email_audit()])
    write_email_outbound(env, [
        _email_outbound(caller_session_id=f"email:{_INBOUND_MSG_ID}"),
    ])
    calls, _ = load_reasoning_calls_with_xref()
    assert len(calls) == 1
    call = calls[0]
    assert call["model_used"] == "claude-opus-4-7"
    assert call["input_tokens"] == 612
    assert call["output_tokens"] == 184
    assert call["cost_rung_at_call"] == "normal"
    assert call["triggered_by"] == "email"


def test_email_xref_response_text_stays_null_per_design(env):
    """CC#1 PR #124 design decision: body NEVER in email outbound
    JSONL (privacy + size). Even on a successful xref, the
    response_text_truncated_200 field stays null for email-sourced
    rows. Same shape as slack_dm panel's text carve-out + #143's
    message_id carve-out."""
    write_audit(env, [_email_audit()])
    write_email_outbound(env, [
        _email_outbound(caller_session_id=f"email:{_INBOUND_MSG_ID}"),
    ])
    calls, _ = load_reasoning_calls_with_xref()
    assert calls[0]["response_text_truncated_200"] is None, (
        "Email-sourced rows must surface response_text as null "
        "regardless of xref success — the body isn't in the email "
        "outbound JSONL by design (CC#1 PR #124)"
    )


# ---- 3. PRIMARY > SECONDARY precedence -----------------------------


def test_primary_caller_session_id_match_preferred_over_in_reply_to(env):
    """Both correlation keys are present in different outbound
    entries; the PRIMARY caller_session_id match wins. This catches
    a refactor that accidentally reorders the cascade."""
    write_audit(env, [_email_audit()])
    write_email_outbound(env, [
        # In-reply-to match — would win under SECONDARY but should
        # lose to the primary-session-id entry below.
        _email_outbound(
            in_reply_to=_INBOUND_MSG_ID,
            caller_session_id=None,
            model_used="claude-sonnet-4-6",
        ),
        # PRIMARY match — caller_session_id literal equality.
        _email_outbound(
            in_reply_to="<unrelated@nowhere>",
            caller_session_id=f"email:{_INBOUND_MSG_ID}",
            model_used="claude-opus-4-7",
        ),
    ])
    calls, _ = load_reasoning_calls_with_xref()
    assert calls[0]["model_used"] == "claude-opus-4-7", (
        "PRIMARY caller_session_id must beat SECONDARY in_reply_to"
    )


# ---- 4. SECONDARY in_reply_to fallback ----------------------------


def test_secondary_in_reply_to_match_when_caller_session_id_absent(env):
    """For outbound entries written by non-reasoning paths (operator
    scripts, MCP send tool) that omit caller_session_id, the
    in_reply_to chain match still correlates correctly."""
    write_audit(env, [_email_audit()])
    write_email_outbound(env, [
        _email_outbound(
            in_reply_to=_INBOUND_MSG_ID,
            caller_session_id=None,  # missing PRIMARY key
            model_used="claude-sonnet-4-6",
        ),
    ])
    calls, _ = load_reasoning_calls_with_xref()
    assert calls[0]["model_used"] == "claude-sonnet-4-6"


# ---- 5. LAST RESORT timestamp window -----------------------------


def test_last_resort_timestamp_window_when_both_keys_absent(env):
    """Defensive against a writer-side bug that drops BOTH
    caller_session_id AND in_reply_to. Falls back to closest
    sent_at within ±60s."""
    write_audit(env, [_email_audit(minutes_ago=5)])
    write_email_outbound(env, [
        _email_outbound(
            minutes_ago=5,
            in_reply_to="<unrelated@nowhere>",
            caller_session_id=None,
            model_used="claude-haiku-4-5-20251001",
        ),
    ])
    calls, _ = load_reasoning_calls_with_xref()
    # LAST RESORT path; populated from the only outbound entry
    assert calls[0]["model_used"] == "claude-haiku-4-5-20251001"
    assert calls[0]["cost_rung_at_call"] == "downshift_90"


# ---- 6. Outside ±60s window degrades ----------------------------


def test_outbound_outside_60s_window_no_xref(env):
    write_audit(env, [_email_audit(minutes_ago=5)])
    write_email_outbound(env, [
        # 30 min ago — way outside ±60s window. ALSO has neither
        # correlation key, so it should only reach the LAST RESORT
        # path AND get rejected by the time window.
        _email_outbound(
            minutes_ago=30,
            in_reply_to="<unrelated@nowhere>",
            caller_session_id=None,
            model_used="claude-opus-4-7",
        ),
    ])
    calls, _ = load_reasoning_calls_with_xref()
    assert calls[0]["model_used"] is None, (
        "Outbound outside ±60s window must NOT match — graceful "
        "degradation to null fields"
    )


# ---- 7. Cost-rung derivation on email path ----------------------


@pytest.mark.parametrize(
    "model,expected",
    [
        ("claude-opus-4-7", "normal"),
        ("claude-sonnet-4-6", "warn_75"),
        ("claude-haiku-4-5-20251001", "downshift_90"),
        ("unknown-future-model", "unknown"),
    ],
)
def test_cost_rung_derivation_on_email_path(env, model, expected):
    """Same substring-match logic from #143 — verified on the email
    path too so the FE pill-colour stays consistent across sources."""
    write_audit(env, [_email_audit()])
    write_email_outbound(env, [
        _email_outbound(
            caller_session_id=f"email:{_INBOUND_MSG_ID}",
            model_used=model,
        ),
    ])
    calls, _ = load_reasoning_calls_with_xref()
    assert calls[0]["cost_rung_at_call"] == expected


# ---- 8. cost_ladder_halted xref supersedes status --------------


def test_cost_ladder_halted_email_xref_supersedes_audit_status(env):
    """Same supersession logic as slack_dm — verified on email."""
    write_audit(env, [_email_audit(tool_status="ok")])
    write_email_outbound(env, [
        _email_outbound(
            caller_session_id=f"email:{_INBOUND_MSG_ID}",
            model_used=None,
            reasoning_error="cost_ladder_halted",
        ),
    ])
    calls, _ = load_reasoning_calls_with_xref()
    assert calls[0]["status"] == "halted"
    assert calls[0]["error_code"] == "cost_ladder_halted"
    assert calls[0]["cost_rung_at_call"] == "hard_stop_100"


# ---- 9. Slack-first precedence across sources ------------------


def test_slack_first_precedence(env):
    """A slack_dm audit row must xref to slack_dm outbound, not
    email outbound, even when both log files have entries in the
    same time window. The cascade in the helper tries slack first;
    only falls through to email when the slack parse OR match fails."""
    write_audit(env, [
        {
            "emitted_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
            "seam": "reasoning.tool_called",
            "details": {
                "tool_name": "t1",
                "triggered_by": "slack_dm",
                "tool_duration_ms": 100,
                "tool_status": "ok",
            },
            "caller_session_id": "D0123456789:1779380123.456",
            "source": "reasoning",
        },
    ])
    write_slack_outbound(env, [
        _slack_outbound(
            channel_id="D0123456789",
            thread_ts="1779380123.456",
            text="slack-reply",
            model_used="claude-sonnet-4-6",
        ),
    ])
    # Email outbound ALSO has a recent entry; precedence must skip it
    write_email_outbound(env, [
        _email_outbound(
            caller_session_id=f"email:{_INBOUND_MSG_ID}",
            model_used="claude-opus-4-7",
        ),
    ])
    calls, _ = load_reasoning_calls_with_xref()
    assert calls[0]["model_used"] == "claude-sonnet-4-6", (
        "Slack-first precedence: slack_dm audit must NOT cross-"
        "correlate with the email outbound just because both logs "
        "exist"
    )
    # slack_dm carries the body, so response_text DOES populate
    # for slack-sourced rows (carve-out applies only to email)
    assert calls[0]["response_text_truncated_200"] == "slack-reply"


# ---- 10. Empty email outbound log degrades ----------------------


def test_email_audit_no_outbound_log_returns_null_fields(env):
    write_audit(env, [_email_audit()])
    # No email outbound file written
    calls, _ = load_reasoning_calls_with_xref()
    assert len(calls) == 1
    call = calls[0]
    assert call["model_used"] is None
    assert call["input_tokens"] == 0
    assert call["output_tokens"] == 0
    assert call["response_text_truncated_200"] is None
    assert call["cost_rung_at_call"] == "unknown"
    assert call["triggered_by"] == "email"  # carries through from audit


# ---- 11. Malformed email outbound line tolerated ---------------


def test_malformed_email_outbound_line_skipped(env, caplog):
    write_audit(env, [_email_audit()])
    log_path = env / "email_outbound_log.jsonl"
    with log_path.open("w", encoding="utf-8") as f:
        f.write("{NOT VALID JSON{{{\n")
        f.write(json.dumps(_email_outbound(
            caller_session_id=f"email:{_INBOUND_MSG_ID}",
        )) + "\n")

    import logging
    with caplog.at_level(logging.WARNING):
        calls, _ = load_reasoning_calls_with_xref()
    # The valid entry still matches; corrupt line skipped
    assert calls[0]["model_used"] == "claude-opus-4-7"


# ---- 12. SECURITY ---------------------------------------------


def test_no_token_shapes_anywhere_in_email_xref_output(env):
    write_audit(env, [_email_audit()])
    write_email_outbound(env, [
        _email_outbound(
            caller_session_id=f"email:{_INBOUND_MSG_ID}",
        ),
    ])
    calls, _ = load_reasoning_calls_with_xref()
    blob = json.dumps(calls)
    assert _ANTHROPIC_KEY_SHAPE.findall(blob) == []
    assert _HEX_SECRET_SHAPE.findall(blob) == []


# ---- 13. Endpoint integration ---------------------------------


@pytest.mark.asyncio
async def test_endpoint_email_xref_populates_fields_end_to_end(env):
    """Hit the live endpoint with email-shaped audit + outbound;
    confirms the helper change reaches the panel response."""
    write_audit(env, [_email_audit()])
    write_email_outbound(env, [
        _email_outbound(caller_session_id=f"email:{_INBOUND_MSG_ID}"),
    ])

    from kora_cli import web_server

    result = await web_server.list_recent_reasoning()
    assert len(result["calls"]) == 1
    call = result["calls"][0]
    assert call["model_used"] == "claude-opus-4-7"
    assert call["input_tokens"] == 612
    assert call["cost_rung_at_call"] == "normal"
    assert call["response_text_truncated_200"] is None  # email carve-out
    assert result["by_model_24h"].get("claude-opus-4-7") == 1
    assert result["tokens_total_24h"]["input"] == 612


# ---- 14. Different in_reply_to → no SECONDARY match ----------


def test_email_outbound_for_different_inbound_ignored(env):
    """An email outbound replying to a DIFFERENT inbound (not the
    one this audit row originated from) must NOT cross-correlate
    via SECONDARY in_reply_to."""
    write_audit(env, [_email_audit(message_id=_INBOUND_MSG_ID)])
    write_email_outbound(env, [
        _email_outbound(
            in_reply_to=_OTHER_INBOUND_MSG_ID,
            caller_session_id=None,
            model_used="claude-opus-4-7",
            minutes_ago=30,  # also outside ±60s for LAST RESORT
        ),
    ])
    calls, _ = load_reasoning_calls_with_xref()
    assert calls[0]["model_used"] is None
