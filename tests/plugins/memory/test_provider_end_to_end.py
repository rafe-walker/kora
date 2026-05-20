"""KR-2 ST4 — End-to-end provider lifecycle test.

Walks a synthesized session through the full IsoKronMemoryProvider
lifecycle, mocking only the connection layer (asyncpg pool + the
dedicated IO loop's ``submit_and_wait``). Verifies that no
``NotImplementedError`` surfaces anywhere and that integrity checks
all pass.

Lifecycle covered:

    initialize(session_id)
    on_turn_start(turn=1, message)          # warms 7 reads
    system_prompt_block()                    # uses warm cache
    sync_turn(user, "...cap_X...")           # attempts scratchpad write
    on_memory_write("add", "memory", "...")  # mirrors built-in writes
    on_delegation(task, result, child_id)    # emits + mirrors
    on_session_end([...])                    # emits kora.session.ended
    shutdown()
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, List, Optional

import pytest


# ---------------------------------------------------------------------------
# Synthetic substrate fixtures
# ---------------------------------------------------------------------------


WORKSPACE_ID = "org_e2e_workspace"

_CONTENT_MD = "# Kora Role Charter v1.0\n\nKora is bounded operator-tier-plus."
_CONTENT_HASH = hashlib.sha256(_CONTENT_MD.encode("utf-8")).hexdigest()

_JSONB = {
    "schema_version": 1,
    "charter_version": "1.0",
    "sections": {
        "identity": "Kora is bounded.",
        "authority_can_do": ["Propose policy", "Write scratchpad"],
        "authority_cannot_do": ["Override security verdicts"],
        "override_preconditions": ["6-precondition firewall"],
        "escalation_triggers": ["Novel class-1 decisions"],
        "per_session_discipline": ["Pre-fetch on session start"],
        "audit_attribution": "kora.* chain events",
        "charter_modification": "operator-direct",
        "effective_date_clause": "2026-05-20",
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


def _policy_rows(n: int = 31) -> list[dict[str, Any]]:
    return [
        {"policy_path": f"policy.kora_synth_{i}", "policy_value": True}
        for i in range(n)
    ]


def _event_rows() -> list[dict[str, Any]]:
    return [
        {
            "event_id": "ee1",
            "event_type": "kora.recommendation.issued",
            "occurred_at": "2026-05-20T12:00:00Z",
            "payload_text": '{"item":"watch_brief"}',
        },
        {
            "event_id": "ee2",
            "event_type": "kora.handoff.to_claude_pm",
            "occurred_at": "2026-05-20T12:05:00Z",
            "payload_text": '{"child":"sess-x"}',
        },
    ]


# ---------------------------------------------------------------------------
# Fake connection — routes each SQL by content keyword
# ---------------------------------------------------------------------------


class _FakeTxn:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConnection:
    def __init__(self):
        self.calls: list[tuple] = []

    async def execute(self, sql: str, *args):
        self.calls.append(("execute", sql[:50], args))
        return "OK"

    async def fetchrow(self, sql: str, *args):
        self.calls.append(("fetchrow", sql[:50], args))
        if "kora_role_charter" in sql:
            return _charter_row()
        if "workspace_constitution_revisions" in sql:
            return {
                "revision_id": "33333333-3333-3333-3333-333333333333",
                "rules_hash": b"\xfe\xed" * 16,
            }
        return None

    async def fetch(self, sql: str, *args):
        self.calls.append(("fetch", sql[:50], args))
        if "kora_policy_registry" in sql:
            return _policy_rows(31)
        if "ar.actor_kind = 'kora'" in sql:
            return []
        if "ar.actor_kind != 'kora'" in sql:
            return []
        if "LIKE 'kora.%'" in sql:
            return _event_rows()
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


class _FakeProviderConnection:
    """Drop-in for IsoKronConnection in the E2E test."""

    def __init__(self):
        self._conn = _FakeConnection()
        self._pool = _FakePool(self._conn)
        self.submitted: list = []
        self.closed = False

    def get_pg_pool(self):
        return self._pool

    def submit_and_wait(self, coro, *, timeout: float = 10.0):
        self.submitted.append(coro)
        # Run the coroutine on a one-off loop so deferred-write
        # errors propagate exactly as they would in production.
        return asyncio.run(coro)

    def close(self):
        self.closed = True

    # No-op shims so provider.shutdown() works without the real
    # connection lifecycle.
    @property
    def is_started(self):
        return not self.closed

    def start(self):  # pragma: no cover — initialize calls but we don't need it
        pass


# ---------------------------------------------------------------------------
# E2E test
# ---------------------------------------------------------------------------


def test_provider_end_to_end_full_lifecycle(caplog):
    """Walks every ABC method without surfacing any NotImplementedError."""
    from plugins.memory.isokron.provider import IsoKronMemoryProvider

    provider = IsoKronMemoryProvider(
        config={
            "isokron_dsn": "postgres://kora:secret@localhost:5432/isokron",
            "mcp_endpoint": "stdio://node ./sea-mcp-server.js",
            "default_workspace_id": WORKSPACE_ID,
        }
    )
    fake_conn = _FakeProviderConnection()
    setattr(provider, "_connection", fake_conn)

    with caplog.at_level(logging.DEBUG, logger="plugins.memory.isokron"):
        # 1. Initialize — stash session_id; IO loop is already "started"
        # via fake connection's start().
        provider.initialize(session_id="e2e-001", platform="cli")
        assert provider._initialized is True

        # 2. on_turn_start — warms 7 caches via the gather.
        provider.on_turn_start(turn_number=1, message="user opens session")

        # All caches warm post-prefetch.
        assert WORKSPACE_ID in provider._charter_cache
        assert WORKSPACE_ID in provider._policy_cache
        assert WORKSPACE_ID in provider._capability_cache
        assert WORKSPACE_ID in provider._own_scratchpad_cache
        assert WORKSPACE_ID in provider._cross_agent_scratchpad_cache
        assert WORKSPACE_ID in provider._events_cache
        assert WORKSPACE_ID in provider._constitution_cache

        # 3. system_prompt_block — uses warm cache; non-empty; all sections.
        block = provider.system_prompt_block()
        assert block
        assert "§1 Identity" in block
        assert "§2 You CAN:" in block
        assert "§3 You CANNOT:" in block
        assert "§4 Active policy values" in block
        assert "§5 Granted capabilities" in block
        assert "§6 Recent kora.* activity" in block
        assert "kora.recommendation.issued" in block
        assert "kora.handoff.to_claude_pm" in block
        # Rule-6 honest-label verbatim:
        assert "This identity block was assembled by IsoKronMemoryProvider" in block

        # 4. session_context — typed shape returned from warm caches.
        ctx = provider.session_context()
        assert ctx is not None
        assert ctx.workspace_id == WORKSPACE_ID
        assert ctx.active_constitution_revision_id == "33333333-3333-3333-3333-333333333333"
        # rules_hash hex-encoded.
        assert ctx.active_constitution_rules_hash == "feed" * 16
        assert len(ctx.recent_chain_events) == 2

        # 5. sync_turn — Kora-action token in assistant → attempts write
        # → catches the deferred-error → logs.
        provider.sync_turn(
            "what should I work on?",
            "ok, calling cap_propose_policy_change now",
            session_id="e2e-001",
        )

        # 6. on_memory_write — mirrors to scratchpad.
        provider.on_memory_write(
            "add", "user", "Joshua likes morning coffee."
        )

        # 7. on_delegation — emits + scratchpad mirror (both deferred but
        # both attempted).
        provider.on_delegation(
            "compile a watch brief",
            "watch brief item: x",
            child_session_id="claude_pm-sess-001",
        )

        # 8. on_pre_compress — returns empty per design.
        assert provider.on_pre_compress([{"role": "user", "content": "x"}]) == ""

        # 9. on_session_switch — soft (no reset) keeps caches.
        provider.on_session_switch("e2e-002", reset=False)
        assert WORKSPACE_ID in provider._charter_cache

        # 10. on_session_end — emits kora.session.ended (deferred).
        provider.on_session_end([{"role": "user", "content": "bye"}])

        # 11. on_session_switch with reset=True — flushes all caches.
        provider.on_session_switch("e2e-003", reset=True)
        assert WORKSPACE_ID not in provider._charter_cache
        assert WORKSPACE_ID not in provider._events_cache

    # No NotImplementedError surfaced anywhere through the full lifecycle.
    # Deferred-write WARNINGs were logged but caught.
    deferred = [r for r in caplog.records if "skipped" in r.getMessage()]
    # Expect at least: sync_turn (1) + on_memory_write (1) + on_delegation
    # scratchpad (1) + on_delegation emit (1) + on_session_end emit (1) = 5
    assert len(deferred) >= 5

    provider.shutdown()
    assert provider._initialized is False


def test_provider_session_context_returns_none_when_cache_cold():
    """Before on_turn_start warms the caches, session_context returns None."""
    from plugins.memory.isokron.provider import IsoKronMemoryProvider

    provider = IsoKronMemoryProvider(
        config={
            "isokron_dsn": "postgres://x@y/z",
            "mcp_endpoint": "stdio://x",
            "default_workspace_id": WORKSPACE_ID,
        }
    )
    # No connection set; no prefetch run.
    assert provider.session_context() is None
