"""Unit tests for ``plugins/memory/isokron/kora_control_reader.py`` (KR-P2-J ST1).

Covers:
  - ``KoraControlCommand`` immutability + field shape
  - ``get_active_command`` — happy path, missing workspace_id, missing pool,
    correct GUC name set inside the transaction
  - ``mark_*`` methods — correct ``target_state`` arg to ``transition_kora_control``
  - ``mark_failed`` — reason is logged at WARN; not propagated through SECDEF
  - Defensive error paths — unresolved workspace_id, missing pool, invalid
    target_state

Fakes mirror ``tests/plugins/memory/test_events.py`` shape: a tiny
``_FakeConnection`` + ``_FakePool`` capture the SQL + args sent through
asyncpg without standing up a real database.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

import pytest

from plugins.memory.isokron.kora_control_reader import (
    CALL_TRANSITION_KORA_CONTROL_SQL,
    KORA_CONTROL_WORKSPACE_GUC,
    KoraControlCommand,
    KoraControlReader,
    SELECT_ACTIVE_KORA_CONTROL_COMMAND_SQL,
    _row_to_command,
)


# ---------------------------------------------------------------------------
# Fakes — minimal asyncpg pool/connection/transaction
# ---------------------------------------------------------------------------


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None


class _FakeConnection:
    """Captures execute() + fetchrow() calls; returns canned rows."""

    def __init__(
        self,
        select_row: Optional[dict] = None,
        transition_row: Optional[dict] = None,
    ):
        self._select_row = select_row
        self._transition_row = transition_row
        self.execute_calls: list[tuple] = []   # (sql, args)
        self.fetchrow_calls: list[tuple] = []  # (sql, args)

    def transaction(self):
        return _FakeTransaction()

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, args))
        return None

    async def fetchrow(self, sql, *args):
        self.fetchrow_calls.append((sql, args))
        if "transition_kora_control" in sql:
            return self._transition_row
        return self._select_row


class _FakeAcquireContext:
    def __init__(self, conn: _FakeConnection):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return None


class _FakePool:
    def __init__(self, conn: _FakeConnection):
        self._conn = conn

    def acquire(self):
        return _FakeAcquireContext(self._conn)


def _make_provider(
    *,
    workspace_id: Optional[str] = "ws-1",
    pool: Optional[_FakePool] = None,
    workspace_id_raises: bool = False,
) -> SimpleNamespace:
    """Provider with the surface ``KoraControlReader`` consults."""
    def _resolve():
        if workspace_id_raises:
            raise RuntimeError("workspace not configured")
        return workspace_id

    if pool is None:
        return SimpleNamespace(
            _resolve_workspace_id=_resolve,
            _connection=None,
        )
    return SimpleNamespace(
        _resolve_workspace_id=_resolve,
        _connection=SimpleNamespace(get_pg_pool=lambda: pool),
    )


def _sample_select_row() -> dict:
    """A canonical kora_control row dict shaped like asyncpg.Record."""
    now = datetime(2026, 5, 21, 12, 0, 0, tzinfo=timezone.utc)
    return {
        "command_id": "11111111-1111-1111-1111-111111111111",
        "workspace_id": "ws-1",
        "issuer_session_id": "cockpit-session-7",
        "issuer_actor_id": "22222222-2222-2222-2222-222222222222",
        "issuer_actor_kind": "operator",
        "level": 2,
        "kind": "stop",
        "reason": "operator-paused due to drift",
        "target_session": None,
        "sequence": 17,
        "lifecycle_state": "visible_to_runtime",
        "created_at": now,
        "visible_to_runtime_at": now,
        "expires_at": None,
        "observed_at": now,
        "acknowledged_at": None,
        "enforced_at": None,
    }


def _sample_transition_row() -> dict:
    return {
        "command_id": "11111111-1111-1111-1111-111111111111",
        "lifecycle_state": "acknowledged",
        "transitioned": True,
        "chain_event_id": None,
    }


KORA_ACTOR_UUID = "33333333-3333-3333-3333-333333333333"


# ---------------------------------------------------------------------------
# KoraControlCommand value-class shape
# ---------------------------------------------------------------------------


def test_kora_control_command_is_frozen():
    cmd = _row_to_command(_sample_select_row())
    with pytest.raises(dataclasses.FrozenInstanceError):
        cmd.lifecycle_state = "enforced"  # type: ignore[misc]


def test_row_to_command_preserves_all_fields():
    row = _sample_select_row()
    cmd = _row_to_command(row)
    assert cmd.command_id == row["command_id"]
    assert cmd.workspace_id == row["workspace_id"]
    assert cmd.issuer_session_id == row["issuer_session_id"]
    assert cmd.issuer_actor_id == row["issuer_actor_id"]
    assert cmd.issuer_actor_kind == row["issuer_actor_kind"]
    assert cmd.level == row["level"]
    assert cmd.kind == row["kind"]
    assert cmd.reason == row["reason"]
    assert cmd.target_session is None
    assert cmd.sequence == row["sequence"]
    assert cmd.lifecycle_state == row["lifecycle_state"]
    assert cmd.created_at == row["created_at"]
    assert cmd.visible_to_runtime_at == row["visible_to_runtime_at"]
    assert cmd.expires_at is None
    assert cmd.observed_at == row["observed_at"]
    assert cmd.acknowledged_at is None
    assert cmd.enforced_at is None


# ---------------------------------------------------------------------------
# get_active_command — happy path + GUC + transaction wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_active_command_returns_row_on_happy_path():
    conn = _FakeConnection(select_row=_sample_select_row())
    pool = _FakePool(conn)
    provider = _make_provider(pool=pool)
    reader = KoraControlReader(provider, KORA_ACTOR_UUID)

    cmd = await reader.get_active_command()
    assert cmd is not None
    assert cmd.command_id == "11111111-1111-1111-1111-111111111111"
    assert cmd.level == 2
    assert cmd.kind == "stop"


@pytest.mark.asyncio
async def test_get_active_command_sets_correct_GUC_inside_transaction():
    """Substrate's kora_control RLS reads ``current_setting('app.workspace_id')``.

    The reader must set THAT exact GUC name (not the kronicle-tier
    ``app.current_workspace_id``) before the SELECT, or RLS returns
    zero rows silently.
    """
    conn = _FakeConnection(select_row=_sample_select_row())
    pool = _FakePool(conn)
    provider = _make_provider(workspace_id="ws-7", pool=pool)
    reader = KoraControlReader(provider, KORA_ACTOR_UUID)

    await reader.get_active_command()

    assert len(conn.execute_calls) == 1
    sql, args = conn.execute_calls[0]
    assert sql == "SELECT set_config($1, $2, true)"
    assert args == (KORA_CONTROL_WORKSPACE_GUC, "ws-7")
    assert KORA_CONTROL_WORKSPACE_GUC == "app.workspace_id"  # not current_*


@pytest.mark.asyncio
async def test_get_active_command_uses_the_active_command_select_sql():
    """Make sure the SELECT statement matches the published constant
    (regression guard against accidental query drift)."""
    conn = _FakeConnection(select_row=_sample_select_row())
    pool = _FakePool(conn)
    reader = KoraControlReader(_make_provider(pool=pool), KORA_ACTOR_UUID)

    await reader.get_active_command()

    assert len(conn.fetchrow_calls) == 1
    sql, args = conn.fetchrow_calls[0]
    assert sql == SELECT_ACTIVE_KORA_CONTROL_COMMAND_SQL
    assert args == ()


@pytest.mark.asyncio
async def test_get_active_command_returns_none_when_no_row():
    conn = _FakeConnection(select_row=None)
    pool = _FakePool(conn)
    reader = KoraControlReader(_make_provider(pool=pool), KORA_ACTOR_UUID)
    assert await reader.get_active_command() is None


@pytest.mark.asyncio
async def test_get_active_command_returns_none_when_workspace_id_unresolved():
    conn = _FakeConnection(select_row=_sample_select_row())
    pool = _FakePool(conn)
    reader = KoraControlReader(
        _make_provider(workspace_id=None, pool=pool), KORA_ACTOR_UUID
    )
    assert await reader.get_active_command() is None


@pytest.mark.asyncio
async def test_get_active_command_returns_none_when_workspace_resolve_raises():
    """Defensive: a provider whose ``_resolve_workspace_id`` raises is
    treated as 'no workspace context' rather than propagating."""
    conn = _FakeConnection(select_row=_sample_select_row())
    pool = _FakePool(conn)
    reader = KoraControlReader(
        _make_provider(pool=pool, workspace_id_raises=True), KORA_ACTOR_UUID
    )
    assert await reader.get_active_command() is None


@pytest.mark.asyncio
async def test_get_active_command_returns_none_when_pool_unavailable():
    reader = KoraControlReader(_make_provider(pool=None), KORA_ACTOR_UUID)
    assert await reader.get_active_command() is None


@pytest.mark.asyncio
async def test_get_active_command_ignores_actor_id_argument():
    """``actor_id`` is informational — kora_control is workspace-scoped,
    so passing a different actor_id does NOT change the SELECT shape."""
    conn = _FakeConnection(select_row=_sample_select_row())
    pool = _FakePool(conn)
    reader = KoraControlReader(_make_provider(pool=pool), KORA_ACTOR_UUID)

    await reader.get_active_command(actor_id="some-other-actor")

    assert len(conn.fetchrow_calls) == 1
    sql, args = conn.fetchrow_calls[0]
    assert sql == SELECT_ACTIVE_KORA_CONTROL_COMMAND_SQL
    assert args == ()  # no actor_id in the parameterized query


# ---------------------------------------------------------------------------
# mark_* — correct target_state passed to transition_kora_control
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mark_method,expected_target_state",
    [
        ("mark_observed", "visible_to_runtime"),
        ("mark_acknowledged", "acknowledged"),
        ("mark_enforcing", "enforcing"),
        ("mark_enforced", "enforced"),
    ],
)
async def test_mark_methods_call_transition_with_correct_target_state(
    mark_method, expected_target_state
):
    conn = _FakeConnection(transition_row=_sample_transition_row())
    pool = _FakePool(conn)
    reader = KoraControlReader(_make_provider(pool=pool), KORA_ACTOR_UUID)

    await getattr(reader, mark_method)(
        "11111111-1111-1111-1111-111111111111"
    )

    assert len(conn.fetchrow_calls) == 1
    sql, args = conn.fetchrow_calls[0]
    assert sql == CALL_TRANSITION_KORA_CONTROL_SQL
    workspace_id, command_id, runtime_actor_id, target_state = args
    assert workspace_id == "ws-1"
    assert command_id == "11111111-1111-1111-1111-111111111111"
    assert runtime_actor_id == KORA_ACTOR_UUID
    assert target_state == expected_target_state


@pytest.mark.asyncio
async def test_mark_failed_calls_transition_with_failed_target_state():
    conn = _FakeConnection(transition_row=_sample_transition_row())
    pool = _FakePool(conn)
    reader = KoraControlReader(_make_provider(pool=pool), KORA_ACTOR_UUID)

    await reader.mark_failed(
        "11111111-1111-1111-1111-111111111111",
        reason="enforcement raised RuntimeError",
    )

    assert len(conn.fetchrow_calls) == 1
    _sql, args = conn.fetchrow_calls[0]
    target_state = args[-1]
    assert target_state == "failed"


@pytest.mark.asyncio
async def test_mark_failed_logs_reason_at_warn_locally(caplog):
    """Reason is local-log only — substrate SECDEF does not accept it.

    Operator triage correlates the local WARN with the substrate-emitted
    ``kora_control.failed`` chain event by command_id + timestamp.
    """
    conn = _FakeConnection(transition_row=_sample_transition_row())
    pool = _FakePool(conn)
    reader = KoraControlReader(_make_provider(pool=pool), KORA_ACTOR_UUID)

    with caplog.at_level(
        logging.WARNING,
        logger="isokron_client.kora_control_reader",
    ):
        await reader.mark_failed(
            "command-id-xyz", reason="enforcement raised TimeoutError"
        )

    assert any(
        "[kora.control.failed]" in rec.getMessage()
        and "command-id-xyz" in rec.getMessage()
        and "enforcement raised TimeoutError" in rec.getMessage()
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# _call_transition error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_transition_raises_on_invalid_target_state():
    reader = KoraControlReader(_make_provider(), KORA_ACTOR_UUID)
    with pytest.raises(ValueError, match="not a runtime-driven"):
        await reader._call_transition(
            "command-id", "superseded"  # not in _RUNTIME_TARGET_STATES
        )


@pytest.mark.asyncio
async def test_call_transition_raises_when_workspace_id_unresolved():
    reader = KoraControlReader(
        _make_provider(workspace_id=None), KORA_ACTOR_UUID
    )
    with pytest.raises(RuntimeError, match="workspace_id is unresolved"):
        await reader._call_transition("command-id", "enforced")


@pytest.mark.asyncio
async def test_call_transition_raises_when_pool_unavailable():
    reader = KoraControlReader(_make_provider(pool=None), KORA_ACTOR_UUID)
    with pytest.raises(RuntimeError, match="asyncpg pool unavailable"):
        await reader._call_transition("command-id", "enforced")


# ---------------------------------------------------------------------------
# SQL surface — load-bearing literals stay stable
# ---------------------------------------------------------------------------


def test_select_sql_filters_by_non_terminal_lifecycle_and_stop_kind():
    """The SELECT excludes 'reset' kind + filters non-terminal lifecycle."""
    sql = SELECT_ACTIVE_KORA_CONTROL_COMMAND_SQL
    # Non-terminal states present
    for state in ("created", "visible_to_runtime", "acknowledged", "enforcing"):
        assert f"'{state}'" in sql
    # Terminal states absent
    for state in ("enforced", "superseded", "expired", "failed", "escalated"):
        assert f"IN ('{state}'" not in sql
    # 'stop' filter present
    assert "kind = 'stop'" in sql
    # Sort + LIMIT correct
    assert "ORDER BY level DESC, sequence ASC" in sql
    assert "LIMIT 1" in sql


def test_call_transition_sql_references_secdef():
    sql = CALL_TRANSITION_KORA_CONTROL_SQL
    assert "public.transition_kora_control" in sql
    # Four positional args: workspace_id, command_id, runtime_actor_id, target_state
    assert "$1::text" in sql
    assert "$2::uuid" in sql
    assert "$3::uuid" in sql
    assert "$4::text" in sql


def test_workspace_guc_name_matches_substrate_rls():
    """Substrate ``0089_kora_control_table.sql`` line 201:
       USING (workspace_id = current_setting('app.workspace_id', true))
    Reader must set THAT GUC, not kronicle-tier ``app.current_workspace_id``.
    """
    assert KORA_CONTROL_WORKSPACE_GUC == "app.workspace_id"
