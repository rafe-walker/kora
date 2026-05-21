"""Unit tests for ``plugins/memory/isokron/dr_epoch.py`` (KR-P2-M ST2).

Covers:
  - ``read_substrate_epoch`` happy + missing-row paths
  - ``read_kora_known_epoch`` happy (positive) + first-boot (NULL → None)
  - ``write_kora_known_epoch`` happy + idempotent same-value write
  - ``write_kora_known_epoch`` arg validation (non-positive, missing actor_id)
  - ``write_kora_known_epoch`` monotonic violation →
    :exc:`KoraKnownEpochMonotonicViolation`
  - Substrate-side raise propagates (auth failure / role mismatch)
"""

from __future__ import annotations

import pytest

from plugins.memory.isokron.dr_epoch import (
    KoraKnownEpochMonotonicViolation,
    read_kora_known_epoch,
    read_substrate_epoch,
    write_kora_known_epoch,
)


# ---------------------------------------------------------------------------
# Fake asyncpg pool / connection
# ---------------------------------------------------------------------------


class _FakeConnection:
    """Captures execute/fetchrow + returns canned rows or raises."""

    def __init__(
        self,
        *,
        fetchrow_returns=None,
        fetchrow_raises=None,
    ):
        self._fetchrow_returns = fetchrow_returns
        self._fetchrow_raises = fetchrow_raises
        self.fetchrow_calls: list[tuple] = []

    async def fetchrow(self, sql, *args):
        self.fetchrow_calls.append((sql, args))
        if self._fetchrow_raises is not None:
            raise self._fetchrow_raises
        return self._fetchrow_returns


class _FakeAcquireContext:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return None


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquireContext(self._conn)


def _make_pool(*, fetchrow_returns=None, fetchrow_raises=None) -> _FakePool:
    return _FakePool(
        _FakeConnection(
            fetchrow_returns=fetchrow_returns,
            fetchrow_raises=fetchrow_raises,
        )
    )


KORA_ACTOR_UUID = "33333333-3333-3333-3333-333333333333"


# ---------------------------------------------------------------------------
# read_substrate_epoch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_substrate_epoch_returns_positive_int():
    pool = _make_pool(fetchrow_returns={"epoch": 7})
    assert await read_substrate_epoch(pool) == 7


@pytest.mark.asyncio
async def test_read_substrate_epoch_coerces_to_int():
    """asyncpg's BIGINT comes through as Python int, but be defensive
    if the underlying driver ever surfaces it as a str/Decimal."""
    pool = _make_pool(fetchrow_returns={"epoch": "12"})
    assert await read_substrate_epoch(pool) == 12


@pytest.mark.asyncio
async def test_read_substrate_epoch_raises_when_singleton_missing():
    pool = _make_pool(fetchrow_returns=None)
    with pytest.raises(RuntimeError, match="singleton missing"):
        await read_substrate_epoch(pool)


@pytest.mark.asyncio
async def test_read_substrate_epoch_uses_stable_accessor():
    pool = _make_pool(fetchrow_returns={"epoch": 1})
    await read_substrate_epoch(pool)
    sql, args = pool._conn.fetchrow_calls[0]
    assert "public.substrate_epoch()" in sql
    assert args == ()


# ---------------------------------------------------------------------------
# read_kora_known_epoch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_kora_known_epoch_returns_int():
    pool = _make_pool(fetchrow_returns={"epoch": 5})
    assert await read_kora_known_epoch(pool) == 5


@pytest.mark.asyncio
async def test_read_kora_known_epoch_returns_none_on_first_boot():
    """NULL substrate column = Kora has never completed a boot."""
    pool = _make_pool(fetchrow_returns={"epoch": None})
    assert await read_kora_known_epoch(pool) is None


@pytest.mark.asyncio
async def test_read_kora_known_epoch_raises_when_singleton_missing():
    pool = _make_pool(fetchrow_returns=None)
    with pytest.raises(RuntimeError, match="singleton missing"):
        await read_kora_known_epoch(pool)


# ---------------------------------------------------------------------------
# write_kora_known_epoch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_kora_known_epoch_happy_path():
    pool = _make_pool(fetchrow_returns={"written_epoch": 7})
    result = await write_kora_known_epoch(
        pool, observed_epoch=7, kora_actor_id=KORA_ACTOR_UUID
    )
    assert result == 7

    # Verify SECDEF call shape
    sql, args = pool._conn.fetchrow_calls[0]
    assert "public.kora_write_known_epoch" in sql
    assert args == (7, KORA_ACTOR_UUID)


@pytest.mark.asyncio
async def test_write_kora_known_epoch_idempotent_same_value():
    """Writing the same value twice is fine — substrate UPDATEs to the
    same value without raising."""
    pool = _make_pool(fetchrow_returns={"written_epoch": 7})
    first = await write_kora_known_epoch(
        pool, observed_epoch=7, kora_actor_id=KORA_ACTOR_UUID
    )
    second = await write_kora_known_epoch(
        pool, observed_epoch=7, kora_actor_id=KORA_ACTOR_UUID
    )
    assert first == second == 7


@pytest.mark.asyncio
async def test_write_kora_known_epoch_validates_observed_epoch_positive():
    pool = _make_pool(fetchrow_returns={"written_epoch": 1})
    with pytest.raises(ValueError, match="positive int"):
        await write_kora_known_epoch(
            pool, observed_epoch=0, kora_actor_id=KORA_ACTOR_UUID
        )
    with pytest.raises(ValueError, match="positive int"):
        await write_kora_known_epoch(
            pool, observed_epoch=-1, kora_actor_id=KORA_ACTOR_UUID
        )


@pytest.mark.asyncio
async def test_write_kora_known_epoch_validates_observed_epoch_int():
    pool = _make_pool(fetchrow_returns={"written_epoch": 1})
    with pytest.raises(ValueError, match="positive int"):
        await write_kora_known_epoch(
            pool, observed_epoch="5", kora_actor_id=KORA_ACTOR_UUID  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_write_kora_known_epoch_validates_actor_id_present():
    pool = _make_pool(fetchrow_returns={"written_epoch": 1})
    with pytest.raises(ValueError, match="kora_actor_id is required"):
        await write_kora_known_epoch(
            pool, observed_epoch=1, kora_actor_id=""
        )


# ---------------------------------------------------------------------------
# Monotonic violation — KoraKnownEpochMonotonicViolation
# ---------------------------------------------------------------------------


class _FakeMonotonicViolationError(Exception):
    """Stand-in for asyncpg's CheckViolationError carrying SQLSTATE 23514."""

    sqlstate = "23514"

    def __str__(self):
        return (
            "kora_write_known_epoch: monotonic violation — "
            "observed 3 < kora_known_epoch 7. Refusing to roll back."
        )


@pytest.mark.asyncio
async def test_write_kora_known_epoch_wraps_monotonic_violation():
    pool = _make_pool(fetchrow_raises=_FakeMonotonicViolationError())
    with pytest.raises(KoraKnownEpochMonotonicViolation) as exc_info:
        await write_kora_known_epoch(
            pool, observed_epoch=3, kora_actor_id=KORA_ACTOR_UUID
        )
    err = exc_info.value
    assert err.observed == 3
    assert err.current == 7
    assert "DR signal" in str(err)


@pytest.mark.asyncio
async def test_monotonic_violation_chains_original_cause():
    original = _FakeMonotonicViolationError()
    pool = _make_pool(fetchrow_raises=original)
    with pytest.raises(KoraKnownEpochMonotonicViolation) as exc_info:
        await write_kora_known_epoch(
            pool, observed_epoch=3, kora_actor_id=KORA_ACTOR_UUID
        )
    assert exc_info.value.__cause__ is original


# ---------------------------------------------------------------------------
# Non-monotonic substrate errors propagate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_kora_known_epoch_propagates_auth_failures():
    """SECDEF raises ``insufficient_privilege`` (42501) on non-kora
    actor. This should NOT be wrapped as MonotonicViolation; the
    original error propagates."""

    class _FakeAuthError(Exception):
        sqlstate = "42501"

        def __str__(self):
            return "kora_write_known_epoch: actor_id ... is not an active kora actor."

    pool = _make_pool(fetchrow_raises=_FakeAuthError())
    with pytest.raises(_FakeAuthError):
        await write_kora_known_epoch(
            pool, observed_epoch=5, kora_actor_id="not-a-kora-uuid"
        )


@pytest.mark.asyncio
async def test_write_kora_known_epoch_propagates_generic_errors():
    """A transport hiccup (RuntimeError) propagates as-is."""
    pool = _make_pool(fetchrow_raises=RuntimeError("connection dropped"))
    with pytest.raises(RuntimeError, match="connection dropped"):
        await write_kora_known_epoch(
            pool, observed_epoch=5, kora_actor_id=KORA_ACTOR_UUID
        )


# ---------------------------------------------------------------------------
# SECDEF returns no row — defensive contract guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_raises_runtime_error_when_secdef_returns_no_row():
    pool = _make_pool(fetchrow_returns=None)
    with pytest.raises(RuntimeError, match="returned no row"):
        await write_kora_known_epoch(
            pool, observed_epoch=5, kora_actor_id=KORA_ACTOR_UUID
        )
