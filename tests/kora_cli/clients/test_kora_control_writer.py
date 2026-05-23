"""Unit tests for KoraControlWriter — KR-MCP-STOP-CONTROL ST2 D2.

Covers:
  - dry_run mode returns predicted shape without invoking substrate
  - missing actor_id → MissingActorIdError
  - invalid (level, kind) → InvalidLevelKindError
  - missing pool → PoolUnavailableError
  - asyncpg.PostgresError → SubstrateRejected with sqlstate preserved
  - asyncio.TimeoutError → KoraControlWriterTimeout
  - successful call: projects substrate row → IssueKoraControlResult
  - timeout constant is 10s
  - current_kora_control_writer() returns None when provider missing
  - current_kora_control_writer() returns writer when provider set
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fixtures — fake asyncpg.PostgresError + connection
# ---------------------------------------------------------------------------


class _FakePostgresError(Exception):
    """Stand-in for asyncpg.PostgresError when asyncpg isn't installed."""

    def __init__(self, sqlstate: str, message: str = "") -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class _FakePool:
    """Async-contextmanager fake — yields a connection that returns rows."""

    def __init__(self, response: Any = None, raise_exc: Any = None) -> None:
        self._response = response
        self._raise_exc = raise_exc
        self.fetchrow_calls: list = []

    def acquire(self):
        class _CM:
            pool = self

            async def __aenter__(_):
                class _Conn:
                    async def fetchrow(_self, sql, *args):
                        _CM.pool.fetchrow_calls.append((sql, args))
                        if _CM.pool._raise_exc is not None:
                            raise _CM.pool._raise_exc
                        return _CM.pool._response

                return _Conn()

            async def __aexit__(_, *_args):
                return None

        return _CM()


class _FakeConnection:
    """Fake IsoKronConnection — direct-await mode (no _submit_async loop)."""

    def __init__(self, pool: Any = None, no_pool: bool = False) -> None:
        self._pool = pool
        self._no_pool = no_pool

    def get_pg_pool(self):
        if self._no_pool:
            return None
        return self._pool

    def _submit_async(self, coro):
        """Return an asyncio.Future awaitable. The writer wraps this
        with asyncio.wrap_future, so we hand back a Future tied to
        the current loop's eager execution of the coroutine."""
        loop = asyncio.get_event_loop()
        future = loop.create_future()

        async def _run():
            try:
                result = await coro
                future.set_result(result)
            except Exception as exc:
                future.set_exception(exc)

        loop.create_task(_run())
        return future


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_timeout_constant_is_10s():
    from kora_cli.clients.kora_control_writer import (
        ISSUE_KORA_CONTROL_TIMEOUT_SECONDS,
    )

    assert ISSUE_KORA_CONTROL_TIMEOUT_SECONDS == 10.0


@pytest.mark.asyncio
async def test_dry_run_returns_predicted_shape_without_substrate():
    from kora_cli.clients.kora_control_writer import (
        IssueKoraControlResult,
        KoraControlWriter,
    )

    conn = _FakeConnection(pool=_FakePool())
    writer = KoraControlWriter(conn)

    result = await writer.issue_command(
        workspace_id="ws_test",
        issuer_session_id="sess_abc",
        issuer_actor_id="11111111-2222-3333-4444-555555555555",
        level=1,
        kind="pause",
        reason="dry-run test",
        dry_run=True,
    )

    assert isinstance(result, IssueKoraControlResult)
    assert result.dry_run is True
    assert result.command_id == "00000000-0000-0000-0000-000000000000"
    assert result.sequence == -1
    assert result.lifecycle_state == "created"
    assert result.superseded_command_ids == []
    # Substrate NOT invoked.
    assert conn._pool.fetchrow_calls == []


@pytest.mark.asyncio
async def test_missing_actor_id_raises():
    from kora_cli.clients.kora_control_writer import (
        KoraControlWriter,
        MissingActorIdError,
    )

    conn = _FakeConnection(pool=_FakePool())
    writer = KoraControlWriter(conn)

    with pytest.raises(MissingActorIdError):
        await writer.issue_command(
            workspace_id="ws_test",
            issuer_session_id="sess_abc",
            issuer_actor_id=None,
            level=1,
            kind="pause",
            reason="x",
        )
    # Pre-validation — substrate not touched.
    assert conn._pool.fetchrow_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "level,kind",
    [
        (1, "drain"),  # wrong kind for L1
        (2, "pause"),  # wrong kind for L2
        (1, "kill"),  # L1 + kill mismatch
        (6, "pause"),  # out of range
        (0, "reset"),  # L0 reset isn't valid stop input
    ],
)
async def test_invalid_level_kind_raises(level, kind):
    from kora_cli.clients.kora_control_writer import (
        InvalidLevelKindError,
        KoraControlWriter,
    )

    conn = _FakeConnection(pool=_FakePool())
    writer = KoraControlWriter(conn)
    with pytest.raises(InvalidLevelKindError):
        await writer.issue_command(
            workspace_id="ws_test",
            issuer_session_id="sess",
            issuer_actor_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            level=level,
            kind=kind,
            reason="x",
        )
    assert conn._pool.fetchrow_calls == []


@pytest.mark.asyncio
async def test_pool_unavailable_raises():
    from kora_cli.clients.kora_control_writer import (
        KoraControlWriter,
        KoraControlWriterError,
    )

    conn = _FakeConnection(no_pool=True)
    writer = KoraControlWriter(conn)
    # PoolUnavailableError IS a KoraControlWriterError subclass; the
    # outer wrapper catches it and re-raises unchanged.
    with pytest.raises(KoraControlWriterError):
        await writer.issue_command(
            workspace_id="ws_test",
            issuer_session_id="sess",
            issuer_actor_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            level=1,
            kind="pause",
            reason="x",
        )


@pytest.mark.asyncio
async def test_successful_call_projects_row():
    from kora_cli.clients.kora_control_writer import KoraControlWriter

    # Substrate-row shape (matches RETURNS TABLE in 0090 SECDEF).
    fake_row = {
        "out_command_id": "11111111-1111-1111-1111-111111111111",
        "out_sequence": 42,
        "out_lifecycle_state": "created",
        "out_superseded_command_ids": [],
        "out_chain_event_id": "22222222-2222-2222-2222-222222222222",
    }
    conn = _FakeConnection(pool=_FakePool(response=fake_row))
    writer = KoraControlWriter(conn)

    result = await writer.issue_command(
        workspace_id="ws_test",
        issuer_session_id="sess_xyz",
        issuer_actor_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        level=2,
        kind="drain",
        reason="ops triage",
    )
    assert result.command_id == "11111111-1111-1111-1111-111111111111"
    assert result.sequence == 42
    assert result.lifecycle_state == "created"
    assert result.chain_event_id == "22222222-2222-2222-2222-222222222222"
    assert result.dry_run is False
    # Substrate invoked exactly once.
    assert len(conn._pool.fetchrow_calls) == 1
    sql, args = conn._pool.fetchrow_calls[0]
    assert "public.issue_kora_control" in sql
    assert args[0] == "ws_test"  # workspace_id
    assert args[1] == "sess_xyz"  # issuer_session_id
    assert args[2] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert args[3] == 2  # level
    assert args[4] == "drain"  # kind
    assert args[5] == "ops triage"  # reason


@pytest.mark.asyncio
async def test_postgres_error_wrapped_as_substrate_rejected(monkeypatch):
    """Patch the lazy ``import asyncpg`` to surface our fake error class."""
    from kora_cli.clients import kora_control_writer as writer_mod
    from kora_cli.clients.kora_control_writer import (
        KoraControlWriter,
        SubstrateRejected,
    )

    # Patch builtins.__import__ so the lazy 'import asyncpg' inside
    # issue_command resolves to a fake module that exposes our
    # _FakePostgresError as PostgresError.
    fake_module = MagicMock()
    fake_module.PostgresError = _FakePostgresError
    import builtins

    real_import = builtins.__import__

    def _patched_import(name, *args, **kwargs):
        if name == "asyncpg":
            return fake_module
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _patched_import)

    err = _FakePostgresError(
        sqlstate="42501",
        message="actor_kind='kora' cannot write kora_control",
    )
    conn = _FakeConnection(pool=_FakePool(raise_exc=err))
    writer = KoraControlWriter(conn)

    with pytest.raises(SubstrateRejected) as excinfo:
        await writer.issue_command(
            workspace_id="ws_test",
            issuer_session_id="sess",
            issuer_actor_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            level=1,
            kind="pause",
            reason="x",
        )
    assert excinfo.value.sqlstate == "42501"
    assert "kora" in excinfo.value.substrate_message.lower()


@pytest.mark.asyncio
async def test_timeout_wraps_as_writer_timeout(monkeypatch):
    """Force the substrate call to exceed the 10s deadline by stubbing
    asyncio.wait_for to raise TimeoutError."""
    from kora_cli.clients.kora_control_writer import (
        KoraControlWriter,
        KoraControlWriterTimeout,
    )

    async def _slow_wait_for(awaitable, timeout):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(
        "kora_cli.clients.kora_control_writer.asyncio.wait_for",
        _slow_wait_for,
    )
    conn = _FakeConnection(pool=_FakePool(response=None))
    writer = KoraControlWriter(conn)
    with pytest.raises(KoraControlWriterTimeout) as excinfo:
        await writer.issue_command(
            workspace_id="ws_test",
            issuer_session_id="sess",
            issuer_actor_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            level=1,
            kind="pause",
            reason="x",
        )
    assert "10.0s" in str(excinfo.value)


# ---------------------------------------------------------------------------
# current_kora_control_writer() accessor
# ---------------------------------------------------------------------------


def test_current_writer_returns_none_when_provider_missing():
    from kora_cli.clients.kora_control_writer import (
        current_kora_control_writer,
    )
    from plugins.memory.isokron import active_provider

    active_provider.clear_active_provider()
    assert current_kora_control_writer() is None


def test_current_writer_returns_writer_when_provider_set():
    from kora_cli.clients.kora_control_writer import (
        KoraControlWriter,
        current_kora_control_writer,
    )
    from plugins.memory.isokron import active_provider

    provider = MagicMock()
    provider._connection = _FakeConnection(pool=_FakePool())
    active_provider.set_active_provider(provider)
    try:
        writer = current_kora_control_writer()
        assert isinstance(writer, KoraControlWriter)
    finally:
        active_provider.clear_active_provider()


def test_current_writer_returns_none_when_connection_missing():
    from kora_cli.clients.kora_control_writer import (
        current_kora_control_writer,
    )
    from plugins.memory.isokron import active_provider

    provider = MagicMock()
    del provider._connection
    active_provider.set_active_provider(provider)
    try:
        # MagicMock _connection has been deleted; getattr returns None.
        assert current_kora_control_writer() is None
    finally:
        active_provider.clear_active_provider()
