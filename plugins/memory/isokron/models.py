"""Typed shapes + exceptions for the IsoKron read paths (KR-2 ST2).

Mirrors the TS-side `@hivex/sb1-substrate-shapes` reader contracts:

- ``RoleCharter`` / ``RoleCharterSections`` mirror the
  ``KoraRoleCharter`` interface in ``packages/sb1-substrate-shapes/src/
  kora-role-charter.ts`` (K-1 ST4).
- ``PolicyRegistryEntry`` mirrors a single row from the
  ``kora_policy_registry`` table (migration 0078).
- ``KoraCapabilityRow`` mirrors the Kora column of
  ``ACTOR_CAPABILITY_MATRIX`` in
  ``packages/sea-mcp-server/src/capability-matrix.ts`` (Plan 04 §0).

Errors are named to match the TS-side classes so log search across
both stacks finds the same incident family.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class IsoKronReadError(RuntimeError):
    """Base for all IsoKron read-path failures."""


class NoActiveRoleCharterError(IsoKronReadError):
    """Raised when no active Role Charter row exists for the workspace.

    Mirrors ``NoActiveKoraRoleCharterError`` on the TS side (K-1 ST4 reader).
    K-1 ST1's partial unique index guarantees ≤1 active row; this error
    indicates 0 rows — typically a workspace seeded without an active
    charter, or an unmigrated dev DB.
    """

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id
        super().__init__(
            f"No active Kora Role Charter row found for workspace {workspace_id}"
        )


class RoleCharterIntegrityError(IsoKronReadError):
    """Raised when recomputed SHA-256 of ``content_md`` ≠ stored ``content_hash``.

    Mirrors ``KoraRoleCharterIntegrityError`` on the TS side. Surfaces a
    tampering or migration-corruption signal. Fail-closed by contract —
    the provider raises rather than degrading.
    """

    def __init__(
        self,
        workspace_id: str,
        expected_hash: str,
        actual_hash: str,
    ):
        self.workspace_id = workspace_id
        self.expected_hash = expected_hash
        self.actual_hash = actual_hash
        super().__init__(
            f"Kora Role Charter integrity violation for workspace {workspace_id}: "
            f"stored content_hash={expected_hash} but recomputed SHA-256={actual_hash}"
        )


# ---------------------------------------------------------------------------
# Role Charter shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoleCharterSections:
    """Parsed sections from ``content_jsonb.sections``.

    Matches the JSONB structure built by migration 0078
    (``packages/db/migrations/0078_kora_role_charter_body.sql``) via
    ``jsonb_build_object``. K-1 ST2 confirmed the runtime shape is
    stable across all populated rows.
    """

    identity: str
    authority_can_do: tuple[str, ...]
    authority_cannot_do: tuple[str, ...]
    override_preconditions: tuple[str, ...]
    escalation_triggers: tuple[str, ...]
    per_session_discipline: tuple[str, ...]
    audit_attribution: str
    charter_modification: str
    effective_date_clause: str


@dataclass(frozen=True, slots=True)
class RoleCharter:
    """Active Role Charter row + parsed sections.

    Returned by ``read_active_role_charter`` after the SHA-256 integrity
    check passes. ``schema_version`` lets future migrations evolve the
    JSONB layout while keeping a single Python shape.
    """

    id: str
    workspace_id: str
    schema_version: int
    charter_version: str
    content_md: str
    content_hash: str
    created_at: str  # ISO-8601 from TIMESTAMPTZ
    sections: RoleCharterSections


# ---------------------------------------------------------------------------
# Policy Registry shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PolicyRegistryEntry:
    """One row from ``kora_policy_registry``.

    ``policy_path`` is the regex-validated dotted name
    (``^policy\\.[a-z_]+(\\.[a-z_]+)*$``); ``policy_value`` is the
    JSONB content — scalar (bool/number/string) OR object — parsed
    into Python types by asyncpg's default JSONB codec.
    """

    workspace_id: str
    policy_path: str
    policy_value: Any  # JSONB — scalar or dict; asyncpg decodes to Python


PolicyRegistry = Mapping[str, Any]
"""Convenience alias: ``policy_path -> policy_value`` for fast lookup."""


# ---------------------------------------------------------------------------
# Capability matrix shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KoraCapabilityRow:
    """The Kora row of ``ACTOR_CAPABILITY_MATRIX``.

    ``granted`` is the set of capabilities where the Kora column is
    ``true``; ``denied`` is the complementary set. Kept as frozensets
    so callers can do O(1) membership checks without copying.

    Source: KR-2 ST2 ships this as a Python mirror of the TS const at
    ``packages/sea-mcp-server/src/capability-matrix.ts`` (Approach C2;
    see ``capability_matrix_mirror.py`` and BUILD_DEVIATIONS entry
    ``D-kr2-st2-capability-matrix-mirror``).
    """

    actor_kind: str  # always "kora" in normal operation
    granted: frozenset[str]
    denied: frozenset[str]

    def has(self, capability: str) -> bool:
        """Fail-closed lookup: unknown capability returns False."""
        return capability in self.granted
