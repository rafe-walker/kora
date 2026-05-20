"""KR-2 ST4 — KoraSessionContext assembler."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Optional

import pytest

from plugins.memory.isokron.session_context import (
    DEFAULT_RECENT_EVENT_LIMIT,
    DEFAULT_SCRATCHPAD_LIMIT,
    KoraSessionContext,
    assemble_session_context,
)


# ---------------------------------------------------------------------------
# Combined fake — serves Role Charter / scratchpad / events / constitution
# all from one connection.
# ---------------------------------------------------------------------------


class _FakeTxn:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConnection:
    """Fake conn that routes queries based on SQL content keywords."""

    def __init__(
        self,
        *,
        role_charter_row: Optional[dict[str, Any]] = None,
        own_scratchpad_rows: Optional[list[dict[str, Any]]] = None,
        cross_agent_rows: Optional[list[dict[str, Any]]] = None,
        recent_event_rows: Optional[list[dict[str, Any]]] = None,
        constitution_row: Optional[dict[str, Any]] = None,
    ):
        self.role_charter_row = role_charter_row
        self.own_scratchpad_rows = own_scratchpad_rows or []
        self.cross_agent_rows = cross_agent_rows or []
        self.recent_event_rows = recent_event_rows or []
        self.constitution_row = constitution_row
        self.calls: list[tuple] = []

    async def execute(self, sql: str, *args):
        self.calls.append(("execute", sql[:60], args))
        return "OK"

    async def fetchrow(self, sql: str, *args):
        self.calls.append(("fetchrow", sql[:60], args))
        if "kora_role_charter" in sql:
            return self.role_charter_row
        if "workspace_constitution_revisions" in sql:
            return self.constitution_row
        return None

    async def fetch(self, sql: str, *args):
        self.calls.append(("fetch", sql[:60], args))
        if "ar.actor_kind = 'kora'" in sql:
            return self.own_scratchpad_rows
        if "ar.actor_kind != 'kora'" in sql:
            return self.cross_agent_rows
        if "LIKE 'kora.%'" in sql:
            return self.recent_event_rows
        return []

    def transaction(self):
        return _FakeTxn(self)


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

_CONTENT_MD = "# Charter v1.0"
_CONTENT_HASH = hashlib.sha256(_CONTENT_MD.encode("utf-8")).hexdigest()
_JSONB = {
    "schema_version": 1,
    "charter_version": "1.0",
    "sections": {
        "identity": "Kora is bounded.",
        "authority_can_do": ["a"],
        "authority_cannot_do": ["b"],
        "override_preconditions": ["c"],
        "escalation_triggers": ["d"],
        "per_session_discipline": ["e"],
        "audit_attribution": "f",
        "charter_modification": "g",
        "effective_date_clause": "h",
    },
}


def _charter_row():
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "workspace_id": WORKSPACE_ID,
        "schema_version": 1,
        "content_md": _CONTENT_MD,
        "content_jsonb": _JSONB,
        "content_hash": _CONTENT_HASH,
        "created_at": "2026-05-20T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# Assembler
# ---------------------------------------------------------------------------


def test_assemble_session_context_fans_out_six_reads():
    """All six reads fire (one fetchrow + four fetches + one fetchrow for constitution)."""
    conn = _FakeConnection(
        role_charter_row=_charter_row(),
        own_scratchpad_rows=[],
        cross_agent_rows=[],
        recent_event_rows=[],
        constitution_row={
            "revision_id": "22222222-2222-2222-2222-222222222222",
            "rules_hash": b"\xab\xcd" * 16,
        },
    )
    pool = _FakePool(conn)
    ctx = asyncio.run(assemble_session_context(WORKSPACE_ID, pool))
    assert isinstance(ctx, KoraSessionContext)
    assert ctx.workspace_id == WORKSPACE_ID
    assert ctx.assembled_at  # ISO-8601 non-empty
    assert ctx.role_charter.charter_version == "1.0"
    assert ctx.capability_matrix_row.actor_kind == "kora"
    assert ctx.own_scratchpad == ()
    assert ctx.cross_agent_scratchpad == ()
    assert ctx.recent_chain_events == ()
    assert ctx.active_constitution_revision_id == "22222222-2222-2222-2222-222222222222"
    assert ctx.active_constitution_rules_hash == "abcd" * 16


def test_assemble_session_context_constitution_absent_returns_none_pair():
    """Fresh workspace (no Constitution revisions) → both fields are None."""
    conn = _FakeConnection(role_charter_row=_charter_row())
    pool = _FakePool(conn)
    ctx = asyncio.run(assemble_session_context(WORKSPACE_ID, pool))
    assert ctx.active_constitution_revision_id is None
    assert ctx.active_constitution_rules_hash is None


def test_assemble_session_context_default_limits():
    """Default scratchpad limit 100; default recent-event limit 50."""
    assert DEFAULT_SCRATCHPAD_LIMIT == 100
    assert DEFAULT_RECENT_EVENT_LIMIT == 50
