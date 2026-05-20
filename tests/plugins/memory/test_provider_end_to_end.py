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


class _FakeMcpClient:
    """KR-7 + KR-7b era fake MCP client — routes by tool_name.

    Post-KR-7b, initialize() ALSO fetches the capability matrix via
    ``kora__read_kora_capability_row`` — the fake returns a small
    canonical-shape matrix so the populate succeeds + the E2E exercises
    the production path (matrix authoritative). Post-KR-7, chain emits
    via ``kora__append_event`` return mock event_ids.
    """

    def __init__(self):
        self.invoke_calls: list[tuple[str, dict]] = []
        # Track only the append-event calls separately so existing
        # KR-7 assertions on "2 emits" stay stable.
        self.append_event_calls: list[tuple[str, dict]] = []

    async def invoke(self, tool_name: str, args: dict):
        self.invoke_calls.append((tool_name, dict(args)))
        if tool_name == "kora__read_kora_capability_row":
            # Minimal canonical-shape matrix — 3 entries covering the
            # caps the E2E hits via iso_node/iso_link handlers (Kora-
            # granted set). Production fetches the full 49.
            return {
                "capability_matrix": {
                    "cap_write_agent_scratchpad": True,
                    "cap_read_precommit_scratchpad": True,
                    "cap_sea_create": True,
                }
            }
        if tool_name == "kora__append_event":
            self.append_event_calls.append((tool_name, dict(args)))
            return {"event_id": f"evt-mock-{len(self.append_event_calls):03d}"}
        raise AssertionError(
            f"_FakeMcpClient received unexpected tool_name: {tool_name!r}"
        )


class _FakeProviderConnection:
    """Drop-in for IsoKronConnection in the E2E test."""

    def __init__(self):
        self._conn = _FakeConnection()
        self._pool = _FakePool(self._conn)
        self._mcp_client = _FakeMcpClient()
        self.submitted: list = []
        self.closed = False

    def get_pg_pool(self):
        return self._pool

    def get_mcp_client(self):
        return self._mcp_client

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


@pytest.fixture
def restore_capability_matrix():
    """Snapshot + restore the hand-mirrored capability matrix.

    KR-7b's MCP-backed populate mutates the module-level dict in place;
    tests that drive initialize against a fake mcp_client need to
    restore the hand-mirrored fallback so other tests in the same
    worker see a clean state.
    """
    from plugins.memory.isokron.capability_matrix_mirror import (
        ACTOR_CAPABILITY_MATRIX_KORA_COLUMN,
    )

    snapshot = dict(ACTOR_CAPABILITY_MATRIX_KORA_COLUMN)
    yield
    ACTOR_CAPABILITY_MATRIX_KORA_COLUMN.clear()
    ACTOR_CAPABILITY_MATRIX_KORA_COLUMN.update(snapshot)


def test_provider_end_to_end_full_lifecycle(caplog, restore_capability_matrix):
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
    # Post-KR-7 reality:
    #   - Scratchpad-write deferrals stay (D-kr2-st3 still open) — 3 WARNINGs
    #     tagged "scratchpad write skipped" (sync_turn + on_memory_write +
    #     on_delegation).
    #   - Chain emits now succeed via the fake MCP client (KR-7 closed
    #     D-kr2-st4) — 2 INFO logs tagged [kora.chain.emit].
    scratchpad_skipped = [
        r for r in caplog.records if "scratchpad write skipped" in r.getMessage()
    ]
    assert len(scratchpad_skipped) == 3

    chain_emitted = [
        r for r in caplog.records if "[kora.chain.emit]" in r.getMessage()
    ]
    assert len(chain_emitted) == 2  # on_delegation + on_session_end
    assert any(
        "kora.handoff.to_claude_pm" in r.getMessage() for r in chain_emitted
    )
    assert any(
        "kora.session.ended" in r.getMessage() for r in chain_emitted
    )

    # The fake MCP client recorded the two append_event calls with the
    # spec-pinned tool name + arg shape. (KR-7b adds a 3rd invoke at
    # initialize for the capability-matrix fetch — checked separately.)
    fake_client = fake_conn._mcp_client
    assert len(fake_client.append_event_calls) == 2
    for tool_name, args in fake_client.append_event_calls:
        assert tool_name == "kora__append_event"
        assert set(args.keys()) == {"workspace_id", "event_type", "payload"}

    # KR-7b: the capability-matrix fetch fired at initialize, replacing
    # the hand-mirrored fallback with the small 3-entry test matrix.
    cap_fetches = [
        c for c in fake_client.invoke_calls
        if c[0] == "kora__read_kora_capability_row"
    ]
    assert len(cap_fetches) == 1
    from plugins.memory.isokron.capability_matrix_mirror import (
        ACTOR_CAPABILITY_MATRIX_KORA_COLUMN,
    )
    # Authoritative fetch replaced the hand-mirrored 49 entries with
    # the 3-entry test matrix.
    assert set(ACTOR_CAPABILITY_MATRIX_KORA_COLUMN.keys()) == {
        "cap_write_agent_scratchpad",
        "cap_read_precommit_scratchpad",
        "cap_sea_create",
    }

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
