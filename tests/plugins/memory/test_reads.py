"""KR-2 ST2 — IsoKron read paths.

Tests the three async read functions + cache + system_prompt_block
assembler. No live Postgres — all queries are issued against a hand-
rolled fake pool / connection that records call order for RLS-GUC
ordering assertions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, List, Optional

import pytest

from plugins.memory.isokron.cache import TTLCache
from plugins.memory.isokron.capability_matrix_mirror import (
    ACTOR_CAPABILITY_MATRIX_KORA_COLUMN,
)
from plugins.memory.isokron.models import (
    KoraCapabilityRow,
    NoActiveRoleCharterError,
    PolicyRegistryEntry,
    RoleCharter,
    RoleCharterIntegrityError,
    RoleCharterSections,
)
from plugins.memory.isokron.provider import (
    RULE_6_HONEST_LABEL,
    SYSTEM_PROMPT_POLICY_PATHS,
    _assemble_system_prompt_block,
)
from plugins.memory.isokron.reads import (
    EXPECTED_POLICY_REGISTRY_ROW_COUNT,
    compute_role_charter_content_hash,
    read_active_role_charter,
    read_kora_capability_row,
    read_kora_policy_registry,
)


# ---------------------------------------------------------------------------
# Fake asyncpg pool + connection (records call order for ordering asserts)
# ---------------------------------------------------------------------------


class _FakeTransactionCtx:
    """Async context manager mimicking ``asyncpg.Connection.transaction()``.

    Records its lifecycle on the parent connection so tests can assert
    transaction boundaries.
    """

    def __init__(self, conn: "_FakeConnection"):
        self._conn = conn

    async def __aenter__(self):
        self._conn.calls.append(("transaction.enter",))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._conn.calls.append(("transaction.exit",))
        return False


class _FakeConnection:
    """Records every execute / fetchrow / fetch call in order."""

    def __init__(
        self,
        *,
        fetchrow_result: Optional[dict[str, Any]] = None,
        fetch_result: Optional[List[dict[str, Any]]] = None,
    ):
        self.fetchrow_result = fetchrow_result
        self.fetch_result = fetch_result or []
        self.calls: list[tuple] = []

    async def execute(self, sql: str, *args):
        self.calls.append(("execute", sql, args))
        return "OK"

    async def fetchrow(self, sql: str, *args):
        self.calls.append(("fetchrow", sql, args))
        return self.fetchrow_result

    async def fetch(self, sql: str, *args):
        self.calls.append(("fetch", sql, args))
        return self.fetch_result

    def transaction(self):
        return _FakeTransactionCtx(self)


class _FakeAcquireCtx:
    def __init__(self, conn: _FakeConnection):
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

_VALID_CHARTER_MD = "# Kora Role Charter v1.0\n\nIdentity is bounded."

_VALID_CHARTER_HASH = hashlib.sha256(
    _VALID_CHARTER_MD.encode("utf-8")
).hexdigest()

_VALID_CHARTER_JSONB = {
    "schema_version": 1,
    "charter_version": "1.0",
    "sections": {
        "identity": "Kora is a bounded operator-tier-plus AI.",
        "authority_can_do": [
            "Propose policy changes",
            "Write to agent scratchpad",
            "Author Operations declarations",
        ],
        "authority_cannot_do": [
            "Override security or policy verdicts",
            "Modify condensation drafts",
            "Approve own policy proposals",
        ],
        "override_preconditions": [
            "6 AND-gated firewall preconditions per Plan 04 §Step 2",
        ],
        "escalation_triggers": [
            "Novel non-routine class-1 decisions",
        ],
        "per_session_discipline": [
            "Pre-fetch Role Charter on session start",
        ],
        "audit_attribution": "All Kora actions emit kora.* chain events.",
        "charter_modification": "Operator-direct only.",
        "effective_date_clause": "Effective 2026-05-20.",
    },
}


def _valid_charter_row(*, override_hash: Optional[str] = None) -> dict[str, Any]:
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "workspace_id": WORKSPACE_ID,
        "schema_version": 1,
        "content_md": _VALID_CHARTER_MD,
        "content_jsonb": _VALID_CHARTER_JSONB,
        "content_hash": override_hash or _VALID_CHARTER_HASH,
        "created_at": "2026-05-20T00:00:00Z",
    }


def _seed_policy_rows(count: int = 31) -> List[dict[str, Any]]:
    """Generate ``count`` policy rows that match the 31 canonical paths.

    For the first 31 entries we use the literal canonical paths from
    migration 0078 so coverage tests against the registry work. Beyond
    31 we append synthetic paths.
    """
    canonical = [
        ("policy.kora_disabled", False),
        ("policy.kora_context_assembler_token_ceiling.opus", 180000),
        ("policy.kora_context_assembler_token_ceiling.sonnet", 150000),
        ("policy.kora_context_assembler_token_ceiling.haiku", 80000),
        ("policy.kora_rate_limit_per_minute", 60),
        ("policy.kora_max_policy_proposals_per_24h", 10),
        ("policy.kora_max_nonsecurity_overrides_per_24h", 5),
        ("policy.kora_rationale_novelty_window_days", 30),
        ("policy.kora_recommendation_confidence_threshold_push", 0.80),
        ("policy.kora_push_dismissal_rate_threshold", 0.30),
        ("policy.kora_push_calibration_data_window_days", 30),
        ("policy.kora_push_calibration_min_n_pull_interactions", 200),
        ("policy.kora_push_mode_enabled", False),
        ("policy.kora_output_size_cap_pull", 500),
        ("policy.kora_output_size_cap_push", 200),
        ("policy.kora_output_size_cap_ambient", 100),
        ("policy.allow_multiple_hnao_per_workspace", False),
        ("policy.kora_ambient_mode_enabled", False),
        ("policy.kora_voice_provider", "elevenlabs"),
        ("policy.kora_voice_cache_retention_after_subscription_lapse", "pending_rights_verification"),
        ("policy.kora_self_improvement_loop_enabled", False),
        ("policy.kora_5whys_human_review_required", True),
        ("policy.kora_per_session_cost_warning_threshold_usd", 5.00),
        ("policy.kora_per_session_cost_hard_cap_usd", 20.00),
        ("policy.kora_run_skill_max_recursion_depth", 1),
        ("policy.kora_cross_workspace_writes_allowed", False),
        ("policy.kora_hnao_observation_timeout_seconds", 300),
        ("policy.kora_hnao_backlog_ceiling_per_node", 10),
        ("policy.kora_kill_switch_in_flight_grace_seconds", 0),
        ("policy.kora_kill_switch_outstanding_tickets_action", "reassign_to_higher_tier"),
        ("policy.actor_scratchpad_writes_per_minute", 30),
    ]
    rows = [
        {"policy_path": path, "policy_value": value} for path, value in canonical[:count]
    ]
    # If count > 31, pad with synthetic entries
    for i in range(31, count):
        rows.append(
            {
                "policy_path": f"policy.kora_synthetic_test_extra_{i}",
                "policy_value": True,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Role Charter read
# ---------------------------------------------------------------------------


def test_read_active_role_charter_returns_typed_shape():
    """Happy path: query returns a row, integrity check passes, shape parsed."""
    conn = _FakeConnection(fetchrow_result=_valid_charter_row())
    pool = _FakePool(conn)
    charter = asyncio.run(read_active_role_charter(WORKSPACE_ID, pool))
    assert isinstance(charter, RoleCharter)
    assert charter.workspace_id == WORKSPACE_ID
    assert charter.charter_version == "1.0"
    assert charter.content_hash == _VALID_CHARTER_HASH
    assert charter.sections.identity.startswith("Kora is a bounded")
    assert len(charter.sections.authority_can_do) == 3
    assert len(charter.sections.authority_cannot_do) == 3
    # SQL was issued with bound workspace_id.
    method, _sql, args = conn.calls[0]
    assert method == "fetchrow"
    assert args == (WORKSPACE_ID,)


def test_read_active_role_charter_integrity_error_on_hash_mismatch():
    """Recomputed SHA-256 ≠ stored content_hash raises RoleCharterIntegrityError."""
    bad_row = _valid_charter_row(override_hash="deadbeef" * 8)
    conn = _FakeConnection(fetchrow_result=bad_row)
    pool = _FakePool(conn)
    with pytest.raises(RoleCharterIntegrityError) as excinfo:
        asyncio.run(read_active_role_charter(WORKSPACE_ID, pool))
    assert excinfo.value.expected_hash == "deadbeef" * 8
    assert excinfo.value.actual_hash == _VALID_CHARTER_HASH


def test_read_active_role_charter_raises_when_zero_rows():
    """No active row → NoActiveRoleCharterError carrying workspace_id."""
    conn = _FakeConnection(fetchrow_result=None)
    pool = _FakePool(conn)
    with pytest.raises(NoActiveRoleCharterError) as excinfo:
        asyncio.run(read_active_role_charter(WORKSPACE_ID, pool))
    assert excinfo.value.workspace_id == WORKSPACE_ID


def test_read_active_role_charter_null_content_treated_as_integrity_error():
    """An unpopulated-shell row (content_md = NULL) raises IntegrityError.

    Mirrors the TS reader's defensive check: a partially-migrated dev
    DB throws a clean error rather than silently returning garbage.
    """
    null_row = _valid_charter_row()
    null_row["content_md"] = None
    null_row["content_hash"] = None
    conn = _FakeConnection(fetchrow_result=null_row)
    pool = _FakePool(conn)
    with pytest.raises(RoleCharterIntegrityError):
        asyncio.run(read_active_role_charter(WORKSPACE_ID, pool))


def test_read_active_role_charter_handles_jsonb_as_string():
    """If asyncpg's JSONB codec isn't registered, value comes as JSON str.

    The reader parses defensively so either form works — protects
    against config drift in how the pool's codec is wired.
    """
    row = _valid_charter_row()
    row["content_jsonb"] = json.dumps(_VALID_CHARTER_JSONB)
    conn = _FakeConnection(fetchrow_result=row)
    pool = _FakePool(conn)
    charter = asyncio.run(read_active_role_charter(WORKSPACE_ID, pool))
    assert charter.charter_version == "1.0"


# ---------------------------------------------------------------------------
# Policy registry read
# ---------------------------------------------------------------------------


def test_read_kora_policy_registry_31_rows_healthy_no_warning(caplog):
    """31-row workspace returns all entries and does NOT log a warning."""
    conn = _FakeConnection(fetch_result=_seed_policy_rows(31))
    pool = _FakePool(conn)
    with caplog.at_level(logging.WARNING, logger="isokron_client.reads"):
        entries = asyncio.run(read_kora_policy_registry(WORKSPACE_ID, pool))
    assert len(entries) == EXPECTED_POLICY_REGISTRY_ROW_COUNT == 31
    assert all(isinstance(e, PolicyRegistryEntry) for e in entries)
    assert all(e.workspace_id == WORKSPACE_ID for e in entries)
    # No drift warning emitted.
    drift_warnings = [
        r for r in caplog.records if "row count drift" in r.getMessage()
    ]
    assert drift_warnings == []


def test_read_kora_policy_registry_drift_warns_does_not_fail(caplog):
    """30 rows (1 short) warns but still returns the rows it has."""
    conn = _FakeConnection(fetch_result=_seed_policy_rows(30))
    pool = _FakePool(conn)
    with caplog.at_level(logging.WARNING, logger="isokron_client.reads"):
        entries = asyncio.run(read_kora_policy_registry(WORKSPACE_ID, pool))
    assert len(entries) == 30
    drift_warnings = [
        r for r in caplog.records if "row count drift" in r.getMessage()
    ]
    assert len(drift_warnings) == 1
    assert "expected 31" in drift_warnings[0].getMessage()
    assert "got 30" in drift_warnings[0].getMessage()


def test_read_kora_policy_registry_sets_rls_guc_before_select():
    """The set_config(...) for app.current_workspace_id happens BEFORE the SELECT.

    Without the GUC set, the RLS policy on kora_policy_registry returns
    zero rows. The order asserted here is the only correct path.
    """
    conn = _FakeConnection(fetch_result=_seed_policy_rows(31))
    pool = _FakePool(conn)
    asyncio.run(read_kora_policy_registry(WORKSPACE_ID, pool))
    # Expected call sequence:
    #   1. transaction.enter
    #   2. execute  ← set_config(...)
    #   3. fetch    ← SELECT
    #   4. transaction.exit
    methods = [c[0] for c in conn.calls]
    assert methods == [
        "transaction.enter",
        "execute",
        "fetch",
        "transaction.exit",
    ], f"unexpected call sequence: {conn.calls}"
    # set_config call carries the workspace_id as $1.
    _, exec_sql, exec_args = conn.calls[1]
    assert "set_config" in exec_sql
    assert "app.current_workspace_id" in exec_sql
    assert exec_args == (WORKSPACE_ID,)


def test_read_kora_policy_registry_passes_codec_decoded_values_through():
    """Policy values come pre-decoded by asyncpg's JSONB codec — pass-through.

    A Python ``str`` policy_value (e.g. ``"elevenlabs"``) is a valid
    scalar; the reader must NOT json.loads() it. Decoding is the
    pool's responsibility (JSONB codec registered in connection.py).
    """
    rows = [
        {"policy_path": "policy.kora_disabled", "policy_value": False},
        {"policy_path": "policy.kora_rate_limit_per_minute", "policy_value": 60},
        {"policy_path": "policy.kora_voice_provider", "policy_value": "elevenlabs"},
        {"policy_path": "policy.kora_synthetic_object", "policy_value": {"k": 1}},
    ]
    conn = _FakeConnection(fetch_result=rows)
    pool = _FakePool(conn)
    entries = asyncio.run(read_kora_policy_registry(WORKSPACE_ID, pool))
    assert entries[0].policy_value is False
    assert entries[1].policy_value == 60
    assert entries[2].policy_value == "elevenlabs"
    assert entries[3].policy_value == {"k": 1}


# ---------------------------------------------------------------------------
# Capability matrix (C2 mirror)
# ---------------------------------------------------------------------------


def test_capability_mirror_loads_with_expected_shape():
    """The mirror is a non-empty dict with the right keys + boolean values."""
    assert isinstance(ACTOR_CAPABILITY_MATRIX_KORA_COLUMN, dict)
    # 25 SEA + 30 KORA_BROADER = 55 entries (Sea v1.5 cap_sea_assign_ticket +
    # K-13 + Sea_Ticket claim cycle + Kronicle author/edit additions).
    assert len(ACTOR_CAPABILITY_MATRIX_KORA_COLUMN) == 55
    # Values are booleans (not strings, not ints).
    assert all(
        isinstance(v, bool) for v in ACTOR_CAPABILITY_MATRIX_KORA_COLUMN.values()
    )
    # All keys are cap_* strings.
    assert all(k.startswith("cap_") for k in ACTOR_CAPABILITY_MATRIX_KORA_COLUMN)


def test_read_kora_capability_row_returns_typed_kora_row():
    """The async wrapper returns a populated KoraCapabilityRow."""
    row = asyncio.run(read_kora_capability_row())
    assert isinstance(row, KoraCapabilityRow)
    assert row.actor_kind == "kora"
    # Kora has 28 granted caps (4 sea + 24 kora-broader) of 55 total.
    # The 6 K-13/Sea-claim/Kronicle additions are all Kora=true plus the
    # new SEA cap_sea_assign_ticket is Kora=true.
    assert len(row.granted) == 28
    assert len(row.denied) == 27
    assert len(row.granted) + len(row.denied) == 55


def test_kora_capability_row_has_lookup_is_fail_closed():
    """``has()`` returns True for granted, False for denied + unknown."""
    row = asyncio.run(read_kora_capability_row())
    # Known-granted (Plan 04 §Step 1 — Kora primary writer)
    assert row.has("cap_write_agent_scratchpad") is True
    # Known-denied (Cell C — operator-only safety override)
    assert row.has("cap_override_security_or_policy_verdict") is False
    # Unknown capability — fail-closed False, not KeyError.
    assert row.has("cap_does_not_exist_anywhere") is False


# ---------------------------------------------------------------------------
# TTL cache
# ---------------------------------------------------------------------------


def test_ttl_cache_hits_within_ttl_and_expires_after():
    """First put hits within TTL; advance the clock past TTL → miss."""
    clock_state = [0.0]

    def clock() -> float:
        return clock_state[0]

    cache: TTLCache[str] = TTLCache(ttl_seconds=60.0, clock=clock)
    cache.put("ws-1", "charter-v1")
    # t=0 — cached
    assert cache.get("ws-1") == "charter-v1"
    # t=59 — still cached
    clock_state[0] = 59.0
    assert cache.get("ws-1") == "charter-v1"
    # t=60 — expired (>= TTL)
    clock_state[0] = 60.0
    assert cache.get("ws-1") is None
    # Eviction-on-read drops the entry.
    assert len(cache) == 0


def test_ttl_cache_invalidate_drops_entry():
    cache: TTLCache[int] = TTLCache(ttl_seconds=60.0)
    cache.put("k", 42)
    assert cache.get("k") == 42
    cache.invalidate("k")
    assert cache.get("k") is None


# ---------------------------------------------------------------------------
# system_prompt_block assembler
# ---------------------------------------------------------------------------


def _synthetic_charter() -> RoleCharter:
    return RoleCharter(
        id="11111111-1111-1111-1111-111111111111",
        workspace_id=WORKSPACE_ID,
        schema_version=1,
        charter_version="1.0",
        content_md=_VALID_CHARTER_MD,
        content_hash=_VALID_CHARTER_HASH,
        created_at="2026-05-20T00:00:00Z",
        sections=RoleCharterSections(
            identity="Kora is bounded.",
            authority_can_do=("Propose policy", "Write scratchpad"),
            authority_cannot_do=("Override security verdicts",),
            override_preconditions=("6-precondition firewall",),
            escalation_triggers=("Novel class-1 decisions",),
            per_session_discipline=("Prefetch on session start",),
            audit_attribution="kora.* chain events",
            charter_modification="operator-direct",
            effective_date_clause="2026-05-20",
        ),
    )


def _synthetic_policies() -> List[PolicyRegistryEntry]:
    return [
        PolicyRegistryEntry(WORKSPACE_ID, "policy.kora_disabled", False),
        PolicyRegistryEntry(
            WORKSPACE_ID, "policy.kora_max_nonsecurity_overrides_per_24h", 5
        ),
        PolicyRegistryEntry(WORKSPACE_ID, "policy.kora_output_size_cap_pull", 500),
        PolicyRegistryEntry(WORKSPACE_ID, "policy.kora_rate_limit_per_minute", 60),
        PolicyRegistryEntry(
            WORKSPACE_ID,
            "policy.kora_context_assembler_token_ceiling.opus",
            180000,
        ),
    ]


def _synthetic_capabilities() -> KoraCapabilityRow:
    return asyncio.run(read_kora_capability_row())


def test_system_prompt_block_assembles_all_required_sections():
    """The assembler produces non-empty text with each spec-required section."""
    block = _assemble_system_prompt_block(
        _synthetic_charter(),
        _synthetic_policies(),
        _synthetic_capabilities(),
        [],  # no recent events for this base-shape test
    )
    assert block  # non-empty
    # §1 Identity
    assert "§1 Identity" in block
    assert "Kora is bounded." in block
    # §2 CAN
    assert "§2 You CAN:" in block
    assert "Propose policy" in block
    assert "Write scratchpad" in block
    # §3 CANNOT
    assert "§3 You CANNOT:" in block
    assert "Override security verdicts" in block
    # §4 active policy values
    assert "§4 Active policy values" in block
    assert "policy.kora_disabled = false" in block
    assert "policy.kora_max_nonsecurity_overrides_per_24h = 5" in block
    # §5 granted capabilities (Kora has 22 granted of 48)
    assert "§5 Granted capabilities" in block
    assert "cap_write_agent_scratchpad" in block
    # §6 recent activity — present even with empty list
    assert "§6 Recent kora.* activity" in block
    # Granted caps are sorted — never references operator-only caps as granted
    assert "cap_override_security_or_policy_verdict" not in block.split(
        "§5 Granted capabilities"
    )[1].split("\n\n")[0]


def test_system_prompt_block_contains_rule_6_label_verbatim():
    """Rule-6 honest-label appears verbatim — operators grep for this string."""
    block = _assemble_system_prompt_block(
        _synthetic_charter(),
        _synthetic_policies(),
        _synthetic_capabilities(),
        [],
    )
    assert RULE_6_HONEST_LABEL in block
    # And it's at the bottom (last line of the block).
    assert block.rstrip().endswith(RULE_6_HONEST_LABEL)


def test_system_prompt_block_marks_missing_policy_entries():
    """Missing policy_paths render as <not seeded> rather than silently dropped."""
    block = _assemble_system_prompt_block(
        _synthetic_charter(),
        [],  # empty registry — every entry shows <not seeded>
        _synthetic_capabilities(),
        [],
    )
    for path in SYSTEM_PROMPT_POLICY_PATHS:
        assert f"{path} = <not seeded>" in block


# ---------------------------------------------------------------------------
# Provider end-to-end: on_turn_start populates all three caches
# ---------------------------------------------------------------------------


def test_compute_role_charter_content_hash_matches_postgres_encoding():
    """SHA-256 hex digest of content_md byte-matches Postgres ``digest('sha256','hex')``."""
    text = "any role charter body"
    expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert compute_role_charter_content_hash(text) == expected
