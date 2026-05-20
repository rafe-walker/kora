"""KR-2 ST4 — Active Constitution revision read."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest

from plugins.memory.isokron.constitution import (
    ActiveConstitutionRevision,
    SELECT_ACTIVE_CONSTITUTION_REVISION_SQL,
    _hex_encode_rules_hash,
    read_active_constitution_revision,
)


# ---------------------------------------------------------------------------
# Fake pool / conn (records call order for the RLS-GUC ordering check)
# ---------------------------------------------------------------------------


class _FakeTxn:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        self._conn.calls.append(("txn.enter",))
        return self

    async def __aexit__(self, *exc):
        self._conn.calls.append(("txn.exit",))
        return False


class _FakeConnection:
    def __init__(self, fetchrow_result: Optional[dict[str, Any]] = None):
        self.fetchrow_result = fetchrow_result
        self.calls: list[tuple] = []

    async def execute(self, sql: str, *args):
        self.calls.append(("execute", sql, args))
        return "OK"

    async def fetchrow(self, sql: str, *args):
        self.calls.append(("fetchrow", sql, args))
        return self.fetchrow_result

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


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_read_active_constitution_revision_returns_typed_shape():
    row = {
        "revision_id": "11111111-1111-1111-1111-111111111111",
        "rules_hash": b"\xde\xad\xbe\xef" + b"\x00" * 28,  # 32 bytes (BLAKE3/SHA-256 size)
    }
    conn = _FakeConnection(fetchrow_result=row)
    pool = _FakePool(conn)
    rev = asyncio.run(read_active_constitution_revision(WORKSPACE_ID, pool))
    assert isinstance(rev, ActiveConstitutionRevision)
    assert rev.revision_id == "11111111-1111-1111-1111-111111111111"
    assert rev.rules_hash == "deadbeef" + "00" * 28


def test_read_active_constitution_revision_returns_none_for_fresh_workspace():
    """Workspace with no Constitution revisions → returns ``None`` (not raises)."""
    conn = _FakeConnection(fetchrow_result=None)
    pool = _FakePool(conn)
    rev = asyncio.run(read_active_constitution_revision(WORKSPACE_ID, pool))
    assert rev is None


def test_read_active_constitution_revision_sets_rls_guc_before_fetch():
    """Required call sequence: txn.enter → execute(set_config) → fetchrow → txn.exit."""
    conn = _FakeConnection(fetchrow_result=None)
    pool = _FakePool(conn)
    asyncio.run(read_active_constitution_revision(WORKSPACE_ID, pool))
    methods = [c[0] for c in conn.calls]
    assert methods == [
        "txn.enter",
        "execute",
        "fetchrow",
        "txn.exit",
    ], f"call sequence: {conn.calls}"
    _, exec_sql, exec_args = conn.calls[1]
    assert "set_config" in exec_sql
    assert "app.current_workspace_id" in exec_sql
    assert exec_args == (WORKSPACE_ID,)


def test_read_active_constitution_revision_sql_orders_by_revision_number_desc():
    """No superseded_at; active = highest revision_number.

    The TS-side reader caught the K-DG drift (earlier bucket prompt
    referenced WHERE superseded_at IS NULL which would fail). Verify
    the corrected SQL ships here.
    """
    sql = SELECT_ACTIVE_CONSTITUTION_REVISION_SQL
    assert "ORDER BY revision_number DESC" in sql
    assert "LIMIT 1" in sql
    assert "superseded_at" not in sql  # must NOT use the nonexistent column


# ---------------------------------------------------------------------------
# Hex encoder
# ---------------------------------------------------------------------------


def test_hex_encode_accepts_bytes():
    assert _hex_encode_rules_hash(b"\xab\xcd") == "abcd"


def test_hex_encode_accepts_bytearray():
    assert _hex_encode_rules_hash(bytearray(b"\xff\x00")) == "ff00"


def test_hex_encode_accepts_memoryview():
    assert _hex_encode_rules_hash(memoryview(b"\x12\x34")) == "1234"


def test_hex_encode_passes_through_string():
    """Tests with mocks may pass a hex string directly — accept it."""
    assert _hex_encode_rules_hash("already_hex") == "already_hex"


def test_hex_encode_rejects_unknown_type():
    with pytest.raises(TypeError) as excinfo:
        _hex_encode_rules_hash(42)
    assert "rules_hash arrived as unexpected type" in str(excinfo.value)
