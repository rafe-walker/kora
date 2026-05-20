"""KR-2 ST4 — Chain event emit + recent events read."""

from __future__ import annotations

import asyncio
from typing import Any, List, Optional

import pytest

from plugins.memory.isokron.events import (
    ChainEventEmitNotAvailableError,
    DEFAULT_RECENT_EVENT_LIMIT,
    RECENT_EVENT_PAYLOAD_TRUNCATE_LENGTH,
    RecentChainEvent,
    SELECT_RECENT_KORA_CHAIN_EVENTS_SQL,
    emit_kora_event,
    read_recent_kora_events,
)


# ---------------------------------------------------------------------------
# Fake pool / connection
# ---------------------------------------------------------------------------


class _FakeConnection:
    def __init__(self, rows: Optional[List[dict[str, Any]]] = None):
        self.rows = rows or []
        self.calls: list[tuple] = []

    async def fetch(self, sql: str, *args):
        self.calls.append(("fetch", sql, args))
        return self.rows


class _FakeAcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquireCtx(self._conn)


WORKSPACE_ID = "org_test_workspace_001"


def _event_row(
    *,
    event_id: str,
    event_type: str = "kora.recommendation.issued",
    occurred_at: Any = "2026-05-20T12:00:00Z",
    payload_text: str = '{"foo":"bar"}',
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "payload_text": payload_text,
    }


# ---------------------------------------------------------------------------
# Read recent events
# ---------------------------------------------------------------------------


def test_read_recent_kora_events_returns_typed_entries():
    rows = [
        _event_row(event_id="a", event_type="kora.recommendation.issued"),
        _event_row(event_id="b", event_type="kora.escalation.requested"),
    ]
    conn = _FakeConnection(rows)
    pool = _FakePool(conn)
    events = asyncio.run(read_recent_kora_events(WORKSPACE_ID, pool))
    assert len(events) == 2
    assert all(isinstance(e, RecentChainEvent) for e in events)
    assert events[0].event_type == "kora.recommendation.issued"
    assert events[1].event_type == "kora.escalation.requested"


def test_read_recent_kora_events_binds_workspace_id_and_default_limit():
    """SQL is bound with (workspace_id, limit); default limit is 50."""
    conn = _FakeConnection([])
    pool = _FakePool(conn)
    asyncio.run(read_recent_kora_events(WORKSPACE_ID, pool))
    _, _sql, args = conn.calls[0]
    assert args == (WORKSPACE_ID, 50)
    assert DEFAULT_RECENT_EVENT_LIMIT == 50


def test_read_recent_kora_events_passes_custom_limit():
    conn = _FakeConnection([])
    pool = _FakePool(conn)
    asyncio.run(read_recent_kora_events(WORKSPACE_ID, pool, limit=10))
    _, _sql, args = conn.calls[0]
    assert args == (WORKSPACE_ID, 10)


def test_read_recent_kora_events_sql_joins_tenant_on_clerk_org_id():
    """The SQL must JOIN hivex_foundation.tenant on clerk_org_id.

    event_log is the substrate's one genuine tenant_id-UUID-keyed table;
    the JOIN translates Kora's workspace_id (Clerk TEXT) into tenant_id.
    Missing the JOIN would either fail (no FK match) or silently
    return zero rows.
    """
    sql = SELECT_RECENT_KORA_CHAIN_EVENTS_SQL
    assert "JOIN hivex_foundation.tenant" in sql
    assert "t.clerk_org_id = $1" in sql
    assert "el.event_type LIKE 'kora.%'" in sql
    assert "ORDER BY el.occurred_at DESC" in sql


def test_read_recent_kora_events_truncates_payload_at_300_chars():
    long_payload = "x" * 500
    rows = [_event_row(event_id="a", payload_text=long_payload)]
    conn = _FakeConnection(rows)
    pool = _FakePool(conn)
    events = asyncio.run(read_recent_kora_events(WORKSPACE_ID, pool))
    assert len(events[0].payload_summary) == RECENT_EVENT_PAYLOAD_TRUNCATE_LENGTH + 1
    assert events[0].payload_summary.endswith("…")
    # First 300 chars are the original text.
    assert events[0].payload_summary[:300] == "x" * 300


def test_read_recent_kora_events_short_payload_passes_through_unchanged():
    rows = [_event_row(event_id="a", payload_text="short")]
    conn = _FakeConnection(rows)
    pool = _FakePool(conn)
    events = asyncio.run(read_recent_kora_events(WORKSPACE_ID, pool))
    assert events[0].payload_summary == "short"


def test_read_recent_kora_events_empty_payload_handled():
    rows = [_event_row(event_id="a", payload_text="")]
    conn = _FakeConnection(rows)
    pool = _FakePool(conn)
    events = asyncio.run(read_recent_kora_events(WORKSPACE_ID, pool))
    assert events[0].payload_summary == ""


def test_read_recent_kora_events_handles_datetime_occurred_at():
    """If asyncpg returns occurred_at as datetime, it gets ISO-encoded."""
    from datetime import datetime, timezone

    rows = [
        _event_row(event_id="a", occurred_at=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)),
    ]
    conn = _FakeConnection(rows)
    pool = _FakePool(conn)
    events = asyncio.run(read_recent_kora_events(WORKSPACE_ID, pool))
    assert "2026-05-20T12:00:00" in events[0].occurred_at


# ---------------------------------------------------------------------------
# Deferred emit
# ---------------------------------------------------------------------------


def test_emit_kora_event_raises_deferred_write_error():
    """Until the Sea MCP tool ships, emit raises ChainEventEmitNotAvailableError."""

    async def _run():
        await emit_kora_event(
            workspace_id=WORKSPACE_ID,
            event_type="kora.session.ended",
            payload={"turn_count": 5},
        )

    with pytest.raises(ChainEventEmitNotAvailableError) as excinfo:
        asyncio.run(_run())
    msg = str(excinfo.value)
    assert "[kora.isokron.todo]" in msg
    assert "D-kr2-st4-no-chain-emit-mcp-tool" in msg
    assert "BUILD_DEVIATIONS" in msg
