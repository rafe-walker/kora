"""Tests for ``kora_cli.reasoning.context_loader`` — ST1.

Covers:
  - Empty / missing file → empty context with state strings
    "unknown" (when holders absent)
  - Filters to (channel_id, thread_ts) — same channel/diff channel,
    matching thread, None-thread matching
  - Skips filtered/dropped/error inbound entries
  - Skips failed outbound entries
  - max_turns slicing keeps last-N
  - Operational + cost state surfaces from holders when initialized
  - Malformed lines logged + skipped
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from kora_cli.reasoning.context_loader import (
    DEFAULT_MAX_TURNS,
    LOG_PATH_ENV,
    load_slack_dm_context,
)


def _make_inbound(
    *,
    channel: str = "D01",
    thread_ts: Any = None,
    text: str = "hi",
    handled_status: str = "received",
    received_at: str = "2026-05-22T10:00:00+00:00",
) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "received_at": received_at,
        "channel_id": channel,
        "thread_ts": thread_ts,
        "user_id": "UJOSHUA",
        "text": text,
        "event_ts": "1700000000.001",
        "handled_status": handled_status,
    }
    return entry


def _make_outbound(
    *,
    channel: str = "D01",
    thread_ts: Any = None,
    text: str = "reply",
    send_status: str = "ok",
    sent_at: str = "2026-05-22T10:00:01+00:00",
) -> Dict[str, Any]:
    return {
        "sent_at": sent_at,
        "channel_id": channel,
        "thread_ts": thread_ts,
        "text": text,
        "slack_message_ts": "1700000001.999",
        "send_status": send_status,
    }


def _write_jsonl(path: Path, entries: List[Dict[str, Any]]) -> Path:
    path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )
    return path


@pytest.fixture(autouse=True)
def _reset_holders(monkeypatch):
    """No holders by default → state strings 'unknown'. Tests that
    want a holder override per-test.

    KR-PER-TENANT-COST-LADDER-FOUNDATION (#202): cost_state_holder
    moved from a singleton ``_HOLDER`` to a per-tenant
    ``_HOLDERS_BY_TENANT`` dict. Use the canonical reset hook
    rather than poking the module-private attribute directly so
    future shape changes don't break this fixture again.
    """
    from agent import operational_state_holder as h_mod
    from agent.cost_state_holder import _reset_cost_holder_for_tests

    monkeypatch.setattr(h_mod, "_HOLDER", None)
    _reset_cost_holder_for_tests()


# ---------------------------------------------------------------------------
# Missing / empty file
# ---------------------------------------------------------------------------


def test_missing_file_returns_empty(tmp_path):
    ctx = load_slack_dm_context(
        channel_id="D01",
        thread_ts=None,
        log_path=tmp_path / "missing.jsonl",
    )
    assert ctx.recent_messages == []
    assert ctx.current_operational_state == "unknown"
    assert ctx.current_cost_ladder_rung == "unknown"


def test_empty_file_returns_empty(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    ctx = load_slack_dm_context(
        channel_id="D01", thread_ts=None, log_path=path
    )
    assert ctx.recent_messages == []


# ---------------------------------------------------------------------------
# Thread / channel filtering
# ---------------------------------------------------------------------------


def test_same_channel_no_thread_matches(tmp_path):
    path = _write_jsonl(
        tmp_path / "log.jsonl",
        [
            _make_inbound(channel="D01", thread_ts=None, text="msg1"),
            _make_outbound(channel="D01", thread_ts=None, text="reply1"),
        ],
    )
    ctx = load_slack_dm_context(
        channel_id="D01", thread_ts=None, log_path=path
    )
    texts = [t.text for t in ctx.recent_messages]
    assert texts == ["msg1", "reply1"]


def test_different_channel_filtered(tmp_path):
    path = _write_jsonl(
        tmp_path / "log.jsonl",
        [
            _make_inbound(channel="D01", text="me"),
            _make_inbound(channel="D02", text="other-channel"),
            _make_outbound(channel="D01", text="ok"),
        ],
    )
    ctx = load_slack_dm_context(
        channel_id="D01", thread_ts=None, log_path=path
    )
    texts = [t.text for t in ctx.recent_messages]
    assert "other-channel" not in texts


def test_thread_ts_matches_exact(tmp_path):
    path = _write_jsonl(
        tmp_path / "log.jsonl",
        [
            _make_inbound(channel="D01", thread_ts="1.001", text="in-thread"),
            _make_inbound(channel="D01", thread_ts="2.002", text="other-thread"),
            _make_inbound(channel="D01", thread_ts=None, text="no-thread"),
        ],
    )
    ctx = load_slack_dm_context(
        channel_id="D01", thread_ts="1.001", log_path=path
    )
    texts = [t.text for t in ctx.recent_messages]
    assert texts == ["in-thread"]


def test_thread_ts_none_does_not_match_threaded_entries(tmp_path):
    path = _write_jsonl(
        tmp_path / "log.jsonl",
        [
            _make_inbound(channel="D01", thread_ts=None, text="no-thread"),
            _make_inbound(channel="D01", thread_ts="1.001", text="threaded"),
        ],
    )
    ctx = load_slack_dm_context(
        channel_id="D01", thread_ts=None, log_path=path
    )
    texts = [t.text for t in ctx.recent_messages]
    assert texts == ["no-thread"]


# ---------------------------------------------------------------------------
# Skip filtered / failed entries
# ---------------------------------------------------------------------------


def test_filtered_inbound_entries_skipped(tmp_path):
    path = _write_jsonl(
        tmp_path / "log.jsonl",
        [
            _make_inbound(channel="D01", text="valid"),
            _make_inbound(
                channel="D01",
                text="not-joshua",
                handled_status="filtered_non_joshua",
            ),
            _make_inbound(
                channel="D01",
                text="paused-drop",
                handled_status="dropped_paused",
            ),
            _make_inbound(
                channel="D01",
                text="handler-err",
                handled_status="handler_error",
            ),
        ],
    )
    ctx = load_slack_dm_context(
        channel_id="D01", thread_ts=None, log_path=path
    )
    texts = [t.text for t in ctx.recent_messages]
    assert texts == ["valid"]


def test_failed_outbound_entries_skipped(tmp_path):
    path = _write_jsonl(
        tmp_path / "log.jsonl",
        [
            _make_outbound(channel="D01", text="successful-reply"),
            _make_outbound(
                channel="D01", text="failed-reply", send_status="failed"
            ),
        ],
    )
    ctx = load_slack_dm_context(
        channel_id="D01", thread_ts=None, log_path=path
    )
    texts = [t.text for t in ctx.recent_messages]
    assert texts == ["successful-reply"]


# ---------------------------------------------------------------------------
# Ordering + max_turns
# ---------------------------------------------------------------------------


def test_turns_returned_oldest_to_newest(tmp_path):
    path = _write_jsonl(
        tmp_path / "log.jsonl",
        [
            _make_inbound(
                channel="D01", text="msg1",
                received_at="2026-05-22T10:00:00+00:00",
            ),
            _make_outbound(
                channel="D01", text="reply1",
                sent_at="2026-05-22T10:00:05+00:00",
            ),
            _make_inbound(
                channel="D01", text="msg2",
                received_at="2026-05-22T10:01:00+00:00",
            ),
        ],
    )
    ctx = load_slack_dm_context(
        channel_id="D01", thread_ts=None, log_path=path
    )
    texts = [t.text for t in ctx.recent_messages]
    assert texts == ["msg1", "reply1", "msg2"]


def test_max_turns_slices_most_recent(tmp_path):
    entries = []
    for i in range(20):
        entries.append(
            _make_inbound(
                channel="D01",
                text=f"msg{i}",
                received_at=f"2026-05-22T10:{i:02d}:00+00:00",
            )
        )
    path = _write_jsonl(tmp_path / "log.jsonl", entries)
    ctx = load_slack_dm_context(
        channel_id="D01", thread_ts=None, max_turns=5, log_path=path
    )
    texts = [t.text for t in ctx.recent_messages]
    assert texts == ["msg15", "msg16", "msg17", "msg18", "msg19"]


def test_default_max_turns_is_10(tmp_path):
    entries = []
    for i in range(15):
        entries.append(
            _make_inbound(
                channel="D01",
                text=f"msg{i}",
                received_at=f"2026-05-22T10:{i:02d}:00+00:00",
            )
        )
    path = _write_jsonl(tmp_path / "log.jsonl", entries)
    ctx = load_slack_dm_context(
        channel_id="D01", thread_ts=None, log_path=path
    )
    assert len(ctx.recent_messages) == DEFAULT_MAX_TURNS


# ---------------------------------------------------------------------------
# Malformed lines
# ---------------------------------------------------------------------------


def test_malformed_json_lines_skipped(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    raw = (
        json.dumps(_make_inbound(channel="D01", text="ok"))
        + "\n{not-valid-json\n"
        + json.dumps(_make_inbound(channel="D01", text="also-ok"))
        + "\n"
    )
    path = tmp_path / "log.jsonl"
    path.write_text(raw, encoding="utf-8")

    ctx = load_slack_dm_context(
        channel_id="D01", thread_ts=None, log_path=path
    )
    texts = [t.text for t in ctx.recent_messages]
    assert texts == ["ok", "also-ok"]
    assert any("malformed" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Operational + cost state surfaces
# ---------------------------------------------------------------------------


def test_operational_state_from_holder(tmp_path, monkeypatch):
    from agent.operational_state import (
        ClaimPermission,
        OperationalState,
        PrimaryState,
    )
    from agent.operational_state_holder import OperationalStateHolder
    from agent import operational_state_holder as h_mod

    monkeypatch.setattr(
        h_mod,
        "_HOLDER",
        OperationalStateHolder(
            OperationalState(primary_state=PrimaryState.PAUSED)
        ),
    )

    path = _write_jsonl(tmp_path / "log.jsonl", [])
    ctx = load_slack_dm_context(
        channel_id="D01", thread_ts=None, log_path=path
    )
    assert ctx.current_operational_state == "paused"


def test_log_path_env_override(tmp_path, monkeypatch):
    p = _write_jsonl(
        tmp_path / "custom.jsonl",
        [_make_inbound(channel="D01", text="from-env-path")],
    )
    monkeypatch.setenv(LOG_PATH_ENV, str(p))
    # No log_path arg → uses env override.
    ctx = load_slack_dm_context(channel_id="D01", thread_ts=None)
    texts = [t.text for t in ctx.recent_messages]
    assert texts == ["from-env-path"]
