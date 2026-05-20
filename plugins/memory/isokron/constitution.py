"""Active Constitution revision read (KR-2 ST4).

Reads the workspace's currently-active Constitution revision from
``kronicle.workspace_constitution_revisions`` (foundation/0083).

**Schema gotcha** verified against the migration on substrate main:

- The table has NO ``superseded_at`` column. Earlier bucket-prompt
  drafts (and PM dispatches) referenced ``WHERE superseded_at IS NULL``
  — that would fail with "column does not exist". The actual "active"
  semantic is ``ORDER BY revision_number DESC LIMIT 1``, riding the
  ``idx_constitution_revisions_workspace_current`` index.
- ``rules_hash`` is ``BYTEA`` (raw bytes), not pre-hex-encoded. We
  hex-encode here to match the TS-side
  ``constitutionVersionHashFromRulesHash`` helper so K-3 (TS
  context-assembler) and KR-2 (Python provider) emit the same
  canonical hex form for K-6's Constitution pre-screen middleware.
- RLS is enabled keyed off ``current_setting('app.current_workspace_id')``
  — same pattern as ``kora_policy_registry`` and ``agent_scratchpad_entries``.
  The reader uses the GUC-in-transaction pattern from ST2/ST3.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ActiveConstitutionRevision:
    """Active Constitution revision for a workspace.

    Mirrors TS-side ``ActiveConstitutionRevision`` from
    ``packages/sea-mcp-server/src/kora/context-assembler/index.ts:386``.

    ``revision_id`` is the UUID PK; ``rules_hash`` is hex-encoded so
    it's canonically comparable to the same hash computed elsewhere
    in the stack (K-6 Constitution pre-screen middleware, TS-side
    ``constitutionVersionHashFromRulesHash``).
    """

    revision_id: str
    rules_hash: str  # hex-encoded


SELECT_ACTIVE_CONSTITUTION_REVISION_SQL = """
    SELECT
      revision_id::text  AS revision_id,
      rules_hash         AS rules_hash
    FROM kronicle.workspace_constitution_revisions
    WHERE workspace_id = $1
    ORDER BY revision_number DESC
    LIMIT 1
"""


def _hex_encode_rules_hash(value: Any) -> str:
    """Normalize ``rules_hash`` from BYTEA / memoryview / str to hex.

    asyncpg returns BYTEA as ``bytes``; mocks in tests may return
    a hex string already (pass-through). ``memoryview`` is the
    asyncpg-bytea-buffer subtype on some versions.
    """
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, memoryview):
        return bytes(value).hex()
    if isinstance(value, str):
        return value
    raise TypeError(
        f"[kora.isokron] rules_hash arrived as unexpected type "
        f"{type(value).__name__}; expected bytes / memoryview / str."
    )


async def read_active_constitution_revision(
    workspace_id: str,
    pool: Any,
) -> Optional[ActiveConstitutionRevision]:
    """Return the active Constitution revision, or ``None`` for fresh workspaces.

    Fresh-workspace bootstrap state: a workspace that has not yet
    authored any Constitution revisions has zero rows in this table.
    Returning ``None`` (vs raising) matches the TS-side behavior;
    callers either fall back to the platform-default Constitution
    (PLAT-* rules in ``config/kronicle/default_constitution.yaml``) or
    skip Constitution-dependent pre-screening for that turn.

    The RLS policy keys off ``app.current_workspace_id`` — same GUC
    pattern as ``kora_policy_registry`` and ``agent_scratchpad_entries``.
    Without the GUC set, the query returns zero rows silently;
    set it in a transaction before the SELECT.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.current_workspace_id', $1, true)",
                workspace_id,
            )
            row = await conn.fetchrow(
                SELECT_ACTIVE_CONSTITUTION_REVISION_SQL, workspace_id
            )
    if row is None:
        return None
    return ActiveConstitutionRevision(
        revision_id=row["revision_id"],
        rules_hash=_hex_encode_rules_hash(row["rules_hash"]),
    )
