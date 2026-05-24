"""Integration tests for short-circuit wiring in SlackDMHandler.

Covers spec §2(c) acceptance:
  - Short-circuit match: engine NEVER called; post_dm sent with the
    rendered template; outbound JSONL carries
    model_used="short_circuit" + short_circuit_category +
    short_circuit_pattern
  - Short-circuit match BUT snapshot unavailable → engine path
    drives the reply (original behavior preserved)
  - Short-circuit match BUT field is "unknown" → engine path
    drives the reply
  - No phrasebook match → engine called as normal; no regression
  - Telemetry signal flows into reasoning_meta dict shape
  - Cost-ladder write is a no-op for short-circuit hits (all four
    token buckets are 0; the existing record_inference guard bails)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from kora_cli.handlers.slack_dm_handler import (
    JOSHUA_USER_ID_ENV,
    SlackDMHandler,
)


JOSHUA_ID = "UJOSHUA01"


def _make_payload(
    *,
    user: str = JOSHUA_ID,
    channel: str = "D01CHAN01",
    text: str = "hello",
    ts: str = "1700000000.001",
    thread_ts: str | None = None,
) -> Dict[str, Any]:
    event = {
        "type": "message",
        "user": user,
        "channel": channel,
        "channel_type": "im",
        "text": text,
        "ts": ts,
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    return {"type": "event_callback", "event": event}


def _read_outbound(log_path: Path) -> List[Dict[str, Any]]:
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("sent_at") is not None
    ]


def _make_snapshot() -> Dict[str, Any]:
    """Canonical fresh snapshot for tests."""
    return {
        "schema_version": 1,
        "computed_at": "2099-01-01T00:00:00+00:00",  # always-fresh stub
        "operational_state": {
            "primary": "ready",
            "paused": False,
            "pause_reason": None,
        },
        "alerts": {
            "active_count": 3,
            "by_severity": {"critical": 1, "warning": 1, "info": 1},
            "by_category": {},
        },
        "cost_ladder": {
            "current_tier": "normal",
            "monthly_budget_pct_used": 42.5,
            "model_default": "unknown",
        },
        "service_health": {
            "supabase": "healthy",
            "fly": "healthy",
            "vercel": "healthy",
            "sentry": "healthy",
            "doppler": "healthy",
        },
    }


@pytest.fixture
def log_path(tmp_path) -> Path:
    return tmp_path / "slack_dm_log.jsonl"


@pytest.fixture(autouse=True)
def _joshua_env(monkeypatch):
    monkeypatch.setenv(JOSHUA_USER_ID_ENV, JOSHUA_ID)


@pytest.fixture(autouse=True)
def _reset_holder(monkeypatch):
    from agent import operational_state_holder as h_mod

    monkeypatch.setattr(h_mod, "_HOLDER", None)


@pytest.fixture
def mock_client():
    class _C:
        def __init__(self):
            self.post_dm = AsyncMock(
                return_value={"ok": True, "ts": "1700000001.999"}
            )

    return _C()


@pytest.fixture
def fake_engine():
    """An engine spy that records every call. Tests assert
    `await_count == 0` for short-circuit hits."""
    engine = MagicMock()
    engine.respond = AsyncMock(
        return_value=MagicMock(
            text="engine reply",
            model_used="claude-opus-4-7",
            input_tokens=100,
            output_tokens=20,
            reasoning_duration_ms=500,
            error=None,
            tools_used=[],
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
    )
    return engine


@pytest.fixture
def with_fresh_snapshot(monkeypatch):
    """Patch read_snapshot to return a canonical fresh snapshot.

    The handler's _try_short_circuit imports read_snapshot lazily
    from kora_cli.snapshot.state_snapshot; patch there."""
    monkeypatch.setattr(
        "kora_cli.snapshot.state_snapshot.read_snapshot",
        lambda: _make_snapshot(),
    )


@pytest.fixture
def with_no_snapshot(monkeypatch):
    monkeypatch.setattr(
        "kora_cli.snapshot.state_snapshot.read_snapshot", lambda: None
    )


# ---------------------------------------------------------------------------
# Short-circuit happy path — engine bypassed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_short_circuit_match_bypasses_engine(
    log_path, mock_client, fake_engine, with_fresh_snapshot
):
    handler = SlackDMHandler(
        log_path=log_path,
        slack_client=mock_client,
        reasoning_engine=fake_engine,
    )
    await handler.handle_event(_make_payload(text="status"))

    # Engine NEVER called.
    assert fake_engine.respond.await_count == 0

    # post_dm received the rendered template.
    assert mock_client.post_dm.await_count == 1
    sent_text = mock_client.post_dm.await_args.kwargs["text"]
    assert "Operational state: ready" in sent_text
    assert "3 active alert(s)" in sent_text


@pytest.mark.asyncio
async def test_short_circuit_outbound_jsonl_has_telemetry_keys(
    log_path, mock_client, fake_engine, with_fresh_snapshot
):
    handler = SlackDMHandler(
        log_path=log_path,
        slack_client=mock_client,
        reasoning_engine=fake_engine,
    )
    await handler.handle_event(_make_payload(text="any alerts?"))

    entries = _read_outbound(log_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["send_status"] == "ok"
    # Telemetry discriminator + provenance.
    assert entry["model_used"] == "short_circuit"
    assert entry["short_circuit_category"] == "alert_query"
    assert entry["short_circuit_pattern"].startswith("^(any alerts")
    # Zero-cost markers.
    assert entry["input_tokens"] == 0
    assert entry["output_tokens"] == 0
    assert entry["cache_creation_input_tokens"] == 0
    assert entry["cache_read_input_tokens"] == 0
    # Engine-only field — should reflect "engine ran zero tools" or
    # be present as empty list (the short-circuit sentinel meta
    # sets tools_used=[]).
    assert entry["tools_used"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,expected_category",
    [
        ("hey", "greeting"),
        ("thanks", "thanks"),
        ("ok", "ack"),
        ("status", "status_query"),
        ("any alerts?", "alert_query"),
        ("health", "health_query"),
        ("paused?", "pause_check"),
    ],
)
async def test_short_circuit_categories_route_correctly(
    text,
    expected_category,
    log_path,
    mock_client,
    fake_engine,
    with_fresh_snapshot,
):
    """End-to-end routing check for every category in the bundled
    default that doesn't need fields beyond the fresh snapshot."""
    handler = SlackDMHandler(
        log_path=log_path,
        slack_client=mock_client,
        reasoning_engine=fake_engine,
    )
    await handler.handle_event(_make_payload(text=text))
    assert fake_engine.respond.await_count == 0
    entries = _read_outbound(log_path)
    assert entries[-1]["short_circuit_category"] == expected_category


# ---------------------------------------------------------------------------
# Fall-through paths — engine takes over
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_phrasebook_match_falls_to_engine(
    log_path, mock_client, fake_engine, with_fresh_snapshot
):
    """Free-form question Kora has no canned shape for → engine
    runs as before. No telemetry contamination."""
    handler = SlackDMHandler(
        log_path=log_path,
        slack_client=mock_client,
        reasoning_engine=fake_engine,
    )
    await handler.handle_event(
        _make_payload(text="walk me through the migration plan")
    )
    assert fake_engine.respond.await_count == 1
    entries = _read_outbound(log_path)
    assert entries[-1]["model_used"] == "claude-opus-4-7"
    # Short-circuit keys MUST be absent on engine-driven entries.
    assert "short_circuit_category" not in entries[-1]
    assert "short_circuit_pattern" not in entries[-1]


@pytest.mark.asyncio
async def test_match_but_no_snapshot_falls_to_engine(
    log_path, mock_client, fake_engine, with_no_snapshot
):
    """Phrasebook matched 'status' but read_snapshot returns None
    → fall through; engine runs."""
    handler = SlackDMHandler(
        log_path=log_path,
        slack_client=mock_client,
        reasoning_engine=fake_engine,
    )
    await handler.handle_event(_make_payload(text="status"))
    assert fake_engine.respond.await_count == 1
    entries = _read_outbound(log_path)
    assert entries[-1]["model_used"] == "claude-opus-4-7"
    assert "short_circuit_category" not in entries[-1]


@pytest.mark.asyncio
async def test_match_but_degraded_field_falls_to_engine(
    log_path, mock_client, fake_engine, monkeypatch
):
    """Phrasebook matched 'burn' but the field is 'unknown' →
    don't ship 'Your burn is unknown%'; engine takes over."""
    degraded = _make_snapshot()
    degraded["cost_ladder"]["monthly_budget_pct_used"] = "unknown"
    monkeypatch.setattr(
        "kora_cli.snapshot.state_snapshot.read_snapshot",
        lambda: degraded,
    )
    handler = SlackDMHandler(
        log_path=log_path,
        slack_client=mock_client,
        reasoning_engine=fake_engine,
    )
    await handler.handle_event(_make_payload(text="burn?"))
    assert fake_engine.respond.await_count == 1


# ---------------------------------------------------------------------------
# Safety — broken phrasebook doesn't break DM handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phrasebook_load_failure_does_not_break_handler(
    log_path, mock_client, fake_engine, monkeypatch, with_fresh_snapshot
):
    """If load_phrasebook somehow raises, _try_short_circuit catches
    + logs + falls through; the engine path drives the reply."""
    monkeypatch.setattr(
        "kora_cli.short_circuit.dm_phrasebook.load_phrasebook",
        lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("phrasebook broken")
        ),
    )
    # Re-import to ensure the patched name is what the handler imports.
    monkeypatch.setattr(
        "kora_cli.short_circuit.load_phrasebook",
        lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("phrasebook broken")
        ),
    )
    handler = SlackDMHandler(
        log_path=log_path,
        slack_client=mock_client,
        reasoning_engine=fake_engine,
    )
    # 'status' would normally match → but with broken phrasebook
    # we fall through to engine.
    await handler.handle_event(_make_payload(text="status"))
    assert fake_engine.respond.await_count == 1


# ---------------------------------------------------------------------------
# Telemetry — model_used="short_circuit" is the join key for CC#1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_short_circuit_model_used_is_join_key(
    log_path, mock_client, fake_engine, with_fresh_snapshot
):
    """CC#1's KR-CHEAP-COST-TELEMETRY will GROUP BY model_used. The
    literal 'short_circuit' string is the contract — don't rename
    without coordinating with CC#1."""
    handler = SlackDMHandler(
        log_path=log_path,
        slack_client=mock_client,
        reasoning_engine=fake_engine,
    )
    await handler.handle_event(_make_payload(text="health"))
    entries = _read_outbound(log_path)
    assert entries[-1]["model_used"] == "short_circuit"


# ---------------------------------------------------------------------------
# Cost-ladder safety — short-circuit hits do NOT burn $200/mo pool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_short_circuit_does_not_call_record_inference(
    log_path, mock_client, fake_engine, monkeypatch, with_fresh_snapshot
):
    """All four token buckets are 0 → record_inference's guard bails.
    Test it more directly: spy on the cost holder."""
    fake_holder = MagicMock()
    fake_holder.record_inference = MagicMock()
    monkeypatch.setattr(
        "agent.cost_state_holder.get_cost_holder", lambda: fake_holder
    )

    handler = SlackDMHandler(
        log_path=log_path,
        slack_client=mock_client,
        reasoning_engine=fake_engine,
    )
    await handler.handle_event(_make_payload(text="thanks"))
    # Engine bypassed; cost-ladder NOT charged.
    assert fake_engine.respond.await_count == 0
    fake_holder.record_inference.assert_not_called()
