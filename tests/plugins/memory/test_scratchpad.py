"""KR-2 ST3 — Scratchpad reads + deferred-write surface.

Test plan (per spec § ST3 § 5, scaled to deferred-write reality):

- Read path shapes (own + cross-agent) against a fake pool that records
  call ordering for the RLS-GUC-before-SELECT assertion.
- BLAKE3 integrity mismatch logs WARNING but does NOT raise (spec §
  ST3: scratchpad is mutable, do not fail-close).
- Deferred write raises ``ScratchpadWriteNotAvailableError`` carrying
  the BUILD_DEVIATIONS tag (D-kr2-st3-no-scratchpad-write-mcp-tool).
- ``sync_turn`` heuristic detects cap_* tokens; attempts write; catches
  the deferred-write error; logs; leaves the session alive.
- ``on_memory_write`` mirrors built-in memory writes with the same
  catch-and-continue pattern.
- Cache invalidation fires on attempted write (success or deferred).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, List, Optional

import blake3
import pytest

from plugins.memory.isokron.scratchpad import (
    DEFAULT_SCRATCHPAD_READ_LIMIT,
    ScratchpadEntry,
    ScratchpadKind,
    ScratchpadWriteNotAvailableError,
    VisibilityScope,
    compute_scratchpad_content_hash,
    read_cross_agent_scratchpad,
    read_own_scratchpad,
    write_scratchpad_entry,
)
from plugins.memory.isokron.provider import (
    _KORA_ACTION_PATTERN,
    _looks_like_kora_action,
    _summarize_for_scratchpad,
)


# ---------------------------------------------------------------------------
# Fake asyncpg pool + connection (records call ordering)
# ---------------------------------------------------------------------------


class _FakeTxnCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        self._conn.calls.append(("transaction.enter",))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._conn.calls.append(("transaction.exit",))
        return False


class _FakeConnection:
    def __init__(self, fetch_rows: Optional[List[dict[str, Any]]] = None):
        self.fetch_rows = fetch_rows or []
        self.calls: list[tuple] = []

    async def execute(self, sql: str, *args):
        self.calls.append(("execute", sql, args))
        return "OK"

    async def fetch(self, sql: str, *args):
        self.calls.append(("fetch", sql, args))
        return self.fetch_rows

    def transaction(self):
        return _FakeTxnCtx(self)


class _FakeAcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, conn: _FakeConnection):
        self._conn = conn

    def acquire(self):
        return _FakeAcquireCtx(self._conn)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


WORKSPACE_ID = "org_test_workspace_001"


def _hash(text: str) -> str:
    return blake3.blake3(text.encode("utf-8")).hexdigest()


def _row(
    *,
    entry_id: str,
    actor_kind: str = "kora",
    actor_label: str = "Kora",
    content_inline: Optional[str] = "thinking out loud",
    content_uri: Optional[str] = None,
    content_hash: Optional[str] = None,
    visibility_scope: str = "agent_private",
    scratchpad_kind: str = "reasoning_trail",
) -> dict[str, Any]:
    if content_hash is None and content_inline is not None:
        content_hash = _hash(content_inline)
    elif content_hash is None:
        content_hash = "0" * 64  # placeholder for content_uri rows
    return {
        "scratchpad_entry_id": entry_id,
        "actor_kind": actor_kind,
        "actor_label": actor_label,
        "content_inline": content_inline,
        "content_uri": content_uri,
        "content_hash": content_hash,
        "visibility_scope": visibility_scope,
        "scratchpad_kind": scratchpad_kind,
        "created_at": "2026-05-20T12:00:00Z",
    }


# ---------------------------------------------------------------------------
# Read own scratchpad
# ---------------------------------------------------------------------------


def test_read_own_scratchpad_returns_typed_entries():
    rows = [
        _row(entry_id="a", content_inline="kora reasoning step 1"),
        _row(entry_id="b", content_inline="kora reasoning step 2"),
    ]
    conn = _FakeConnection(fetch_rows=rows)
    pool = _FakePool(conn)
    entries = asyncio.run(read_own_scratchpad(WORKSPACE_ID, pool))
    assert len(entries) == 2
    for entry in entries:
        assert isinstance(entry, ScratchpadEntry)
        assert entry.actor_kind == "kora"
        assert entry.scratchpad_kind == ScratchpadKind.REASONING_TRAIL
        assert entry.visibility_scope == VisibilityScope.AGENT_PRIVATE
        assert entry.is_inline()


def test_read_own_scratchpad_sets_rls_guc_before_select():
    """Required call sequence: txn.enter → set_config → fetch → txn.exit."""
    conn = _FakeConnection(fetch_rows=[])
    pool = _FakePool(conn)
    asyncio.run(read_own_scratchpad(WORKSPACE_ID, pool))
    methods = [c[0] for c in conn.calls]
    assert methods == [
        "transaction.enter",
        "execute",
        "fetch",
        "transaction.exit",
    ], f"call sequence: {conn.calls}"
    _, exec_sql, exec_args = conn.calls[1]
    assert "set_config" in exec_sql
    assert "app.current_workspace_id" in exec_sql
    assert exec_args == (WORKSPACE_ID,)


def test_read_own_scratchpad_binds_workspace_id_and_limit():
    """SQL bind is (workspace_id, limit); default limit is 100."""
    conn = _FakeConnection(fetch_rows=[])
    pool = _FakePool(conn)
    asyncio.run(read_own_scratchpad(WORKSPACE_ID, pool, limit=25))
    _, _sql, args = conn.calls[2]
    assert args == (WORKSPACE_ID, 25)
    # Default limit constant matches spec § ST3.
    assert DEFAULT_SCRATCHPAD_READ_LIMIT == 100


# ---------------------------------------------------------------------------
# Read cross-agent scratchpad
# ---------------------------------------------------------------------------


def test_read_cross_agent_scratchpad_returns_typed_entries():
    rows = [
        _row(
            entry_id="x",
            actor_kind="critic",
            actor_label="Critic",
            content_inline="critic self_critique",
            visibility_scope="cross_agent_dereferenceable",
            scratchpad_kind="self_critique",
        ),
        _row(
            entry_id="y",
            actor_kind="oracle",
            actor_label="Oracle",
            content_inline="oracle hypothesis",
            visibility_scope="cross_agent_dereferenceable",
            scratchpad_kind="hypothesis",
        ),
    ]
    conn = _FakeConnection(fetch_rows=rows)
    pool = _FakePool(conn)
    entries = asyncio.run(read_cross_agent_scratchpad(WORKSPACE_ID, pool))
    assert len(entries) == 2
    assert {e.actor_kind for e in entries} == {"critic", "oracle"}
    assert all(
        e.visibility_scope == VisibilityScope.CROSS_AGENT_DEREFERENCEABLE
        for e in entries
    )


def test_read_cross_agent_scratchpad_sql_excludes_kora_and_filters_scope():
    """The SQL WHERE clause excludes kora and filters cross_agent_dereferenceable."""
    from plugins.memory.isokron.scratchpad import (
        SELECT_CROSS_AGENT_SCRATCHPAD_SQL,
    )

    sql = SELECT_CROSS_AGENT_SCRATCHPAD_SQL
    assert "ar.actor_kind != 'kora'" in sql
    assert "s.visibility_scope = 'cross_agent_dereferenceable'" in sql
    assert "s.status = 'active'" in sql


# ---------------------------------------------------------------------------
# BLAKE3 integrity check (warn-only)
# ---------------------------------------------------------------------------


def test_blake3_hash_mismatch_logs_warning_and_does_not_raise(caplog):
    """Per spec § ST3: scratchpad mismatch is mutable-working-memory drift."""
    bad = _row(entry_id="z", content_inline="real content")
    bad["content_hash"] = "deadbeef" * 8  # intentional drift
    conn = _FakeConnection(fetch_rows=[bad])
    pool = _FakePool(conn)
    with caplog.at_level(logging.WARNING, logger="plugins.memory.isokron.scratchpad"):
        entries = asyncio.run(read_own_scratchpad(WORKSPACE_ID, pool))
    assert len(entries) == 1
    # Warning emitted; no exception raised.
    drift = [r for r in caplog.records if "content_hash drift" in r.getMessage()]
    assert len(drift) == 1
    assert "stored=deadbeef" in drift[0].getMessage()


def test_compute_scratchpad_content_hash_matches_blake3_hexdigest():
    text = "any content"
    expected = blake3.blake3(text.encode("utf-8")).hexdigest()
    assert compute_scratchpad_content_hash(text) == expected


def test_content_uri_entries_skip_integrity_check(caplog):
    """``content_uri``-backed entries don't try to BLAKE3-verify a missing inline."""
    uri_row = _row(
        entry_id="u",
        content_inline=None,
        content_uri="s3://bucket/key.txt",
        content_hash="0" * 64,
    )
    conn = _FakeConnection(fetch_rows=[uri_row])
    pool = _FakePool(conn)
    with caplog.at_level(logging.WARNING, logger="plugins.memory.isokron.scratchpad"):
        entries = asyncio.run(read_own_scratchpad(WORKSPACE_ID, pool))
    assert len(entries) == 1
    assert not entries[0].is_inline()
    assert entries[0].content_uri == "s3://bucket/key.txt"
    # No drift warning — verification only runs for inline content.
    assert not [r for r in caplog.records if "content_hash drift" in r.getMessage()]


# ---------------------------------------------------------------------------
# Deferred write surface
# ---------------------------------------------------------------------------


def test_write_scratchpad_entry_raises_deferred_write_error():
    """Until the MCP tool lands, every write raises ScratchpadWriteNotAvailableError."""

    async def _run():
        await write_scratchpad_entry(
            workspace_id=WORKSPACE_ID,
            scratchpad_kind=ScratchpadKind.REASONING_TRAIL,
            visibility_scope=VisibilityScope.AGENT_PRIVATE,
            content="any content",
        )

    with pytest.raises(ScratchpadWriteNotAvailableError) as excinfo:
        asyncio.run(_run())
    msg = str(excinfo.value)
    assert "[kora.isokron.todo]" in msg
    assert "D-kr2-st3-no-scratchpad-write-mcp-tool" in msg
    assert "BUILD_DEVIATIONS" in msg


# ---------------------------------------------------------------------------
# Kora-action heuristic + summarizer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Calling cap_write_agent_scratchpad now", True),
        ("Using cap_propose_policy_change", True),
        ("cap_class2", True),  # digits in name
        ("the previous capability cap_observe_hnao", True),
        ("just a friendly hello", False),
        ("CAP_UPPERCASE_DOES_NOT_MATCH", False),  # case-sensitive
        ("", False),
        ("captain capability cap-not-snake", False),
    ],
)
def test_looks_like_kora_action_heuristic(text, expected):
    assert _looks_like_kora_action(text) is expected


def test_kora_action_pattern_is_word_bounded():
    """The regex must not match cap_*-prefixed substrings inside other words."""
    assert _KORA_ACTION_PATTERN.search("supercap_sea_create") is None


def test_summarize_for_scratchpad_returns_short_content_unchanged():
    assert _summarize_for_scratchpad("hi") == "hi"


def test_summarize_for_scratchpad_truncates_long_content_with_marker():
    long = "x" * 3000
    summary = _summarize_for_scratchpad(long)
    assert len(summary) <= 2000
    assert summary.endswith("…[truncated by isokron]")


# ---------------------------------------------------------------------------
# Provider-level sync_turn + on_memory_write integration
# ---------------------------------------------------------------------------


class _FakeProviderConnection:
    """Replaces IsoKronConnection in unit tests.

    ``submit_and_wait`` invokes the coroutine via ``asyncio.run`` so
    ``ScratchpadWriteNotAvailableError`` propagates exactly as it
    would from the real dedicated loop.
    """

    def __init__(self):
        self.submitted: list = []

    def get_pg_pool(self):  # pragma: no cover — sync_turn/on_memory_write don't read
        raise AssertionError("pool access not expected in write-path tests")

    def submit_and_wait(self, coro, *, timeout: float = 10.0):
        self.submitted.append(coro)
        return asyncio.run(coro)


def _make_provider_for_writes(
    *, workspace_id: str = WORKSPACE_ID
) -> tuple[Any, _FakeProviderConnection]:
    """Build a provider with a fake connection. Returns (provider, fake) so
    tests can access the fake's recorded calls without fighting the type
    of ``provider._connection`` (declared ``IsoKronConnection | None``)."""
    from plugins.memory.isokron.provider import IsoKronMemoryProvider

    provider = IsoKronMemoryProvider(
        config={
            "isokron_dsn": "postgres://kora:secret@localhost:5432/isokron",
            "mcp_endpoint": "stdio://node ./sea-mcp-server.js",
            "default_workspace_id": workspace_id,
        }
    )
    fake = _FakeProviderConnection()
    # setattr bypasses ty's invalid-assignment on the typed attribute;
    # tests are the one legitimate place to swap the connection for a fake.
    setattr(provider, "_connection", fake)
    return provider, fake


def test_sync_turn_no_kora_action_is_noop(caplog):
    """A turn without cap_* in assistant_content does not attempt a write."""
    provider, fake = _make_provider_for_writes()
    with caplog.at_level(logging.WARNING, logger="plugins.memory.isokron.provider"):
        provider.sync_turn("user msg", "assistant ack", session_id="s")
    assert fake.submitted == []
    assert not [r for r in caplog.records if "scratchpad write" in r.getMessage()]


def test_sync_turn_kora_action_attempts_write_and_catches_deferred_error(caplog):
    """cap_* in assistant_content triggers a write attempt; deferred error is caught."""
    provider, fake = _make_provider_for_writes()
    with caplog.at_level(logging.WARNING, logger="plugins.memory.isokron.provider"):
        provider.sync_turn(
            "user asks something",
            "ok, calling cap_write_agent_scratchpad on this",
            session_id="s",
        )
    # Write attempt was made (submitted to the fake loop).
    assert len(fake.submitted) == 1
    # The error was caught + logged; the turn returned normally.
    drift = [r for r in caplog.records if "scratchpad write skipped" in r.getMessage()]
    assert len(drift) == 1
    assert "D-kr2-st3-no-scratchpad-write-mcp-tool" in drift[0].getMessage()


def test_sync_turn_invalidates_own_cache_on_attempted_write():
    """Cache invalidation runs even when the write deferred-errors out."""
    provider, _fake = _make_provider_for_writes()
    # Seed the cache with something the test can detect as evicted.
    provider._own_scratchpad_cache.put(WORKSPACE_ID, [])
    assert WORKSPACE_ID in provider._own_scratchpad_cache
    provider.sync_turn("u", "assistant says cap_route_intent fired", session_id="s")
    # Cache entry evicted post-attempt.
    assert WORKSPACE_ID not in provider._own_scratchpad_cache


def test_on_memory_write_mirrors_to_scratchpad_with_action_and_target(caplog):
    """on_memory_write embeds (action, target) in the scratchpad summary."""
    provider, fake = _make_provider_for_writes()
    with caplog.at_level(logging.WARNING, logger="plugins.memory.isokron.provider"):
        provider.on_memory_write("add", "user", "Joshua's morning beverage is coffee.")
    assert len(fake.submitted) == 1
    # The deferred-write error surfaces as a one-line warning.
    msgs = [r.getMessage() for r in caplog.records]
    assert any("on_memory_write scratchpad write skipped" in m for m in msgs)


def test_sync_turn_skips_silently_without_workspace_id(caplog):
    """No workspace_id → debug log + no write attempt; turn stays alive."""
    from plugins.memory.isokron.provider import IsoKronMemoryProvider

    provider = IsoKronMemoryProvider(
        config={
            "isokron_dsn": "postgres://x@y/z",
            "mcp_endpoint": "stdio://x",
            # no default_workspace_id
        }
    )
    fake = _FakeProviderConnection()
    setattr(provider, "_connection", fake)
    with caplog.at_level(logging.DEBUG, logger="plugins.memory.isokron.provider"):
        provider.sync_turn("u", "ok calling cap_propose_class2", session_id="s")
    assert fake.submitted == []


# ---------------------------------------------------------------------------
# Provider-level scratchpad reads (via fake pool through the connection)
# ---------------------------------------------------------------------------


class _FakeReadingConnection(_FakeProviderConnection):
    """Provider-connection fake that also exposes a fake pg pool."""

    def __init__(self, conn: _FakeConnection):
        super().__init__()
        self._pool = _FakePool(conn)

    def get_pg_pool(self):
        return self._pool


def test_provider_read_own_scratchpad_caches_results():
    """First call hits the pool, second call within TTL hits the cache."""
    from plugins.memory.isokron.provider import IsoKronMemoryProvider

    rows = [_row(entry_id="a"), _row(entry_id="b")]
    conn = _FakeConnection(fetch_rows=rows)
    provider = IsoKronMemoryProvider(
        config={
            "isokron_dsn": "postgres://x@y/z",
            "mcp_endpoint": "stdio://x",
            "default_workspace_id": WORKSPACE_ID,
        }
    )
    setattr(provider, "_connection", _FakeReadingConnection(conn))

    entries1 = provider.read_own_scratchpad()
    assert len(entries1) == 2
    # Second call → cache hit; no additional fetch.
    entries2 = provider.read_own_scratchpad()
    assert entries2 is entries1  # exact object reference (cached)
    # Only one fetch happened on the pool.
    fetch_count = sum(1 for c in conn.calls if c[0] == "fetch")
    assert fetch_count == 1
