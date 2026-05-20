"""Read paths against the IsoKron substrate (KR-2 ST2).

Three async read functions backed by the asyncpg pool on
``IsoKronConnection``:

1. :func:`read_active_role_charter` — fetches the active row from
   ``public.kora_role_charter`` and verifies its SHA-256 integrity
   hash. Mirrors the TS-side ``readActiveKoraRoleCharter`` in
   ``packages/sb1-substrate-shapes/src/kora-role-charter.ts``.

2. :func:`read_kora_policy_registry` — fetches all 31 canonical
   ``kora_policy_registry`` rows for a workspace. RLS-aware: sets
   ``app.current_workspace_id`` via ``set_config(...)`` inside a
   transaction, then runs the SELECT.

3. :func:`read_kora_capability_row` — returns the Kora row of
   ``ACTOR_CAPABILITY_MATRIX``. KR-2 ST2 ships this as a Python mirror
   (Approach C2; see ``capability_matrix_mirror.py``); KR-N will swap
   to the ``kora__read_kora_capability_row`` Sea MCP tool when K-7
   lands.

The 60s TTL caches live on the provider (per-session); these
functions are cache-unaware. The provider's ``initialize`` /
``on_turn_start`` orchestrate cache + concurrency.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any, Sequence

from .capability_matrix_mirror import read_kora_capability_row as _read_capability_row_mirror
from .models import (
    KoraCapabilityRow,
    NoActiveRoleCharterError,
    PolicyRegistryEntry,
    RoleCharter,
    RoleCharterIntegrityError,
    RoleCharterSections,
)

if TYPE_CHECKING:  # pragma: no cover — import-time decoupling
    import asyncpg  # noqa: F401

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical SQL — verbatim from PM-provided ST2 spec / TS reader.
# ---------------------------------------------------------------------------

SELECT_ACTIVE_KORA_ROLE_CHARTER_SQL = """
    SELECT
      id::text          AS id,
      workspace_id      AS workspace_id,
      schema_version    AS schema_version,
      content_md        AS content_md,
      content_jsonb     AS content_jsonb,
      content_hash      AS content_hash,
      created_at        AS created_at
    FROM public.kora_role_charter
    WHERE workspace_id = $1
      AND superseded_at IS NULL
    LIMIT 1
"""

SELECT_KORA_POLICY_REGISTRY_SQL = """
    SELECT policy_path, policy_value
    FROM kora_policy_registry
    WHERE workspace_id = $1
    ORDER BY policy_path
"""

EXPECTED_POLICY_REGISTRY_ROW_COUNT = 31
"""Sanity invariant from migration 0078_kora_policy_registry.sql.

Warn-on-drift, do not fail — operator may have intentionally pruned
or extended the registry. The provider logs at WARNING when the row
count differs; downstream code must not assume exactly 31.
"""


# ---------------------------------------------------------------------------
# Hash helper
# ---------------------------------------------------------------------------


def compute_role_charter_content_hash(content_md: str) -> str:
    """SHA-256 hex digest of ``content_md`` — matches Postgres-side encoding.

    Postgres-side: ``encode(digest(content_md, 'sha256'), 'hex')``.
    UTF-8 byte semantics on both sides; the file at
    ``config/kora/role_charter_v1.md`` produces the same hex when fed
    to ``shasum -a 256``.
    """
    return hashlib.sha256(content_md.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Role Charter read
# ---------------------------------------------------------------------------


def _parse_object_jsonb(value: Any) -> Any:
    """Decode a JSONB column that is ALWAYS an object/array (never scalar).

    The connection wrapper registers the JSONB codec so asyncpg hands
    back ``dict`` / ``list`` directly. If a caller plumbs in a raw
    JSON string (codec not registered, or a fake pool in tests), we
    decode it. Use only for fields known to be objects/arrays —
    callers MUST NOT use this for scalar JSONB fields where a Python
    ``str`` value is a legitimate decoded scalar.
    """
    if isinstance(value, str):
        return json.loads(value)
    return value


def _assemble_role_charter(row: dict[str, Any]) -> RoleCharter:
    """Translate a raw Postgres row to ``RoleCharter`` + verify integrity.

    Raises:
        RoleCharterIntegrityError — content_md/content_hash NULL or
        SHA-256 mismatch.
    """
    content_md = row.get("content_md")
    content_hash = row.get("content_hash")
    workspace_id = row["workspace_id"]

    if content_md is None or content_hash is None:
        raise RoleCharterIntegrityError(
            workspace_id=workspace_id,
            expected_hash=content_hash or "<null>",
            actual_hash="<content_md or content_hash is NULL — body never populated>",
        )

    recomputed = compute_role_charter_content_hash(content_md)
    if recomputed != content_hash:
        raise RoleCharterIntegrityError(
            workspace_id=workspace_id,
            expected_hash=content_hash,
            actual_hash=recomputed,
        )

    jsonb = _parse_object_jsonb(row["content_jsonb"])
    sections_raw = jsonb["sections"]

    sections = RoleCharterSections(
        identity=sections_raw["identity"],
        authority_can_do=tuple(sections_raw["authority_can_do"]),
        authority_cannot_do=tuple(sections_raw["authority_cannot_do"]),
        override_preconditions=tuple(sections_raw["override_preconditions"]),
        escalation_triggers=tuple(sections_raw["escalation_triggers"]),
        per_session_discipline=tuple(sections_raw["per_session_discipline"]),
        audit_attribution=sections_raw["audit_attribution"],
        charter_modification=sections_raw["charter_modification"],
        effective_date_clause=sections_raw["effective_date_clause"],
    )

    created_at = row["created_at"]
    if hasattr(created_at, "isoformat"):
        created_at_iso = created_at.isoformat()
    else:
        created_at_iso = str(created_at)

    return RoleCharter(
        id=row["id"],
        workspace_id=workspace_id,
        schema_version=row["schema_version"],
        charter_version=jsonb["charter_version"],
        content_md=content_md,
        content_hash=content_hash,
        created_at=created_at_iso,
        sections=sections,
    )


async def read_active_role_charter(
    workspace_id: str,
    pool: Any,  # asyncpg.Pool — kept Any to avoid import-time coupling
) -> RoleCharter:
    """Fetch the active Role Charter row + verify SHA-256 integrity.

    Raises:
        NoActiveRoleCharterError — no row matches
            (workspace_id, superseded_at IS NULL).
        RoleCharterIntegrityError — recomputed hash ≠ stored hash, OR
            content_md / content_hash is NULL (= unpopulated shell).
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            SELECT_ACTIVE_KORA_ROLE_CHARTER_SQL, workspace_id
        )
    if row is None:
        raise NoActiveRoleCharterError(workspace_id=workspace_id)
    return _assemble_role_charter(dict(row))


# ---------------------------------------------------------------------------
# Policy registry read
# ---------------------------------------------------------------------------


async def read_kora_policy_registry(
    workspace_id: str,
    pool: Any,
) -> list[PolicyRegistryEntry]:
    """Fetch all ``kora_policy_registry`` rows for a workspace.

    RLS-aware: ``kora_policy_registry`` has a USING-only policy keyed
    off ``current_setting('app.current_workspace_id', true)``. We open
    a transaction, set the GUC via ``set_config(name, value, local)``,
    then SELECT. Without the GUC, the query returns 0 rows silently —
    which the 31-row sanity check would catch but with a misleading
    warning. Setting the GUC is the only correct path.

    The 31-row sanity invariant is logged at WARNING when violated but
    does NOT raise; operators may intentionally extend or prune.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            # SET LOCAL is scoped to the transaction; set_config(...,true)
            # is the parameterizable form (workspace_id is bound, not
            # interpolated — avoids any injection surface even though
            # workspace_id is a TEXT primary key).
            await conn.execute(
                "SELECT set_config('app.current_workspace_id', $1, true)",
                workspace_id,
            )
            rows = await conn.fetch(SELECT_KORA_POLICY_REGISTRY_SQL, workspace_id)

    if len(rows) != EXPECTED_POLICY_REGISTRY_ROW_COUNT:
        logger.warning(
            "[kora.isokron] kora_policy_registry row count drift for "
            "workspace %s: expected %d (migration 0078 canonical seed), "
            "got %d. Operator may have intentionally pruned or extended; "
            "downstream code must not assume exactly %d.",
            workspace_id,
            EXPECTED_POLICY_REGISTRY_ROW_COUNT,
            len(rows),
            EXPECTED_POLICY_REGISTRY_ROW_COUNT,
        )

    return [
        PolicyRegistryEntry(
            workspace_id=workspace_id,
            policy_path=row["policy_path"],
            # asyncpg's JSONB codec (registered on the pool) already
            # decodes scalars to Python types. policy_value can be
            # bool / int / float / str / dict — pass through, do NOT
            # json.loads(): a Python str like "elevenlabs" is a valid
            # decoded scalar, not a JSON literal.
            policy_value=row["policy_value"],
        )
        for row in rows
    ]


def policies_as_mapping(
    entries: Sequence[PolicyRegistryEntry],
) -> dict[str, Any]:
    """Flatten a list of registry entries to ``policy_path -> policy_value``.

    Convenience for callers that want O(1) lookup (e.g. the
    ``system_prompt_block`` assembler picking the 3-5 load-bearing
    policy values).
    """
    return {entry.policy_path: entry.policy_value for entry in entries}


# ---------------------------------------------------------------------------
# Capability matrix read (C2 mirror)
# ---------------------------------------------------------------------------


async def read_kora_capability_row(pool: Any = None) -> KoraCapabilityRow:
    """Return the Kora row of ``ACTOR_CAPABILITY_MATRIX``.

    C2 interim: served from the in-process Python mirror at
    ``plugins/memory/isokron/capability_matrix_mirror.py``. No network
    IO; ``pool`` is accepted for signature symmetry with the other
    reads (and for the future K-7 swap to an MCP call where the pool
    parameter becomes an MCP client handle).

    See BUILD_DEVIATIONS entry ``D-kr2-st2-capability-matrix-mirror``
    and the parity test at
    ``tests/plugins/memory/test_capability_matrix_parity.py``.
    """
    del pool  # unused in C2; reserved for K-7 swap
    return _read_capability_row_mirror()
