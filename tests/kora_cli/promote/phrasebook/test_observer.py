"""Tests for kora_cli.promote.phrasebook.observer."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kora_cli.promote.phrasebook.observer import (
    collect_recent_observations,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setenv(
        "KORA_SLACK_DM_LOG_PATH", str(tmp_path / "slack_dm_log.jsonl")
    )
    return tmp_path


def _write_log(tmp_path: Path, entries: list) -> None:
    path = tmp_path / "slack_dm_log.jsonl"
    path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )


def _entry(
    *,
    text: str = "Burn is $42 today.",
    model_used: str = "claude-haiku-4-5-20251001",
    send_status: str = "ok",
    sent_at: datetime = None,
    caller_session_id: str = "D1JOSH:1700000000.1",
    input_tokens: int = 1000,
    output_tokens: int = 50,
) -> dict:
    if sent_at is None:
        sent_at = datetime.now(timezone.utc) - timedelta(hours=1)
    return {
        "sent_at": sent_at.isoformat(),
        "channel_id": "D1JOSH",
        "thread_ts": None,
        "text": text,
        "slack_message_ts": "1.0",
        "send_status": send_status,
        "model_used": model_used,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "caller_session_id": caller_session_id,
    }


@pytest.mark.asyncio
async def test_collect_returns_engine_driven_entries(tmp_path):
    _write_log(tmp_path, [_entry(text="Burn is $42.")])
    out = await collect_recent_observations(
        since=datetime.now(timezone.utc) - timedelta(hours=24),
    )
    assert len(out) == 1
    assert out[0].kora_response == "Burn is $42."
    assert out[0].route == "slack_dm"


@pytest.mark.asyncio
async def test_collect_excludes_short_circuit_hits(tmp_path):
    _write_log(
        tmp_path,
        [
            _entry(text="Yes you're paused."),
            _entry(text="Burn $42", model_used="short_circuit"),
        ],
    )
    out = await collect_recent_observations(
        since=datetime.now(timezone.utc) - timedelta(hours=24),
    )
    assert len(out) == 1
    assert out[0].kora_response == "Yes you're paused."


@pytest.mark.asyncio
async def test_collect_excludes_non_engine_paths(tmp_path):
    """Entries without model_used are canned-fallback / non-
    reasoning → drop them, they don't carry the Q+A signal."""
    entry = _entry()
    entry.pop("model_used")
    _write_log(tmp_path, [entry])
    out = await collect_recent_observations(
        since=datetime.now(timezone.utc) - timedelta(hours=24),
    )
    assert out == []


@pytest.mark.asyncio
async def test_collect_excludes_failed_sends(tmp_path):
    _write_log(tmp_path, [_entry(send_status="failed")])
    out = await collect_recent_observations(
        since=datetime.now(timezone.utc) - timedelta(hours=24),
    )
    assert out == []


@pytest.mark.asyncio
async def test_collect_time_window_excludes_old_entries(tmp_path):
    old = datetime.now(timezone.utc) - timedelta(days=10)
    fresh = datetime.now(timezone.utc) - timedelta(hours=2)
    _write_log(
        tmp_path,
        [_entry(text="old", sent_at=old), _entry(text="fresh", sent_at=fresh)],
    )
    out = await collect_recent_observations(
        since=datetime.now(timezone.utc) - timedelta(days=1),
    )
    assert [o.kora_response for o in out] == ["fresh"]


@pytest.mark.asyncio
async def test_collect_route_filter_drops_other_routes(tmp_path):
    _write_log(
        tmp_path,
        [
            _entry(
                text="probe answer",
                caller_session_id="probe:fly:service_unhealthy",
            ),
            _entry(text="slack answer"),
        ],
    )
    out = await collect_recent_observations(
        since=datetime.now(timezone.utc) - timedelta(hours=24),
        route_filter=["slack_dm"],
    )
    assert [o.route for o in out] == ["slack_dm"]


@pytest.mark.asyncio
async def test_collect_tolerates_malformed_lines(tmp_path):
    log_path = tmp_path / "slack_dm_log.jsonl"
    log_path.write_text(
        json.dumps(_entry(text="valid"))
        + "\nNOT JSON\n{\"sent_at\":\"bad-ts\",\"model_used\":\"x\","
        "\"send_status\":\"ok\",\"text\":\"t\"}\n"
        + json.dumps(_entry(text="valid2"))
        + "\n",
        encoding="utf-8",
    )
    out = await collect_recent_observations(
        since=datetime.now(timezone.utc) - timedelta(hours=24),
    )
    assert sorted(o.kora_response for o in out) == ["valid", "valid2"]


@pytest.mark.asyncio
async def test_collect_missing_log_returns_empty(tmp_path):
    """No file → no observations, no exception."""
    out = await collect_recent_observations(
        since=datetime.now(timezone.utc) - timedelta(hours=24),
    )
    assert out == []


@pytest.mark.asyncio
async def test_collect_sorts_by_timestamp_ascending(tmp_path):
    t1 = datetime.now(timezone.utc) - timedelta(hours=5)
    t2 = datetime.now(timezone.utc) - timedelta(hours=3)
    t3 = datetime.now(timezone.utc) - timedelta(hours=1)
    _write_log(
        tmp_path,
        [
            _entry(text="3rd", sent_at=t3),
            _entry(text="1st", sent_at=t1),
            _entry(text="2nd", sent_at=t2),
        ],
    )
    out = await collect_recent_observations(
        since=datetime.now(timezone.utc) - timedelta(hours=24),
    )
    assert [o.kora_response for o in out] == ["1st", "2nd", "3rd"]
