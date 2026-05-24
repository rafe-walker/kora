"""KoraSessionContext shape + assembler (KR-2 ST4).

Python mirror of TS-side ``KoraSessionContext`` at
``packages/sea-mcp-server/src/kora/context-assembler/types.ts:130``.
The session context is the single source of truth Kora reads at
session start; the six load-bearing fields are:

1. ``role_charter``                     — K-1 ST4 reader (ST2 here)
2. ``capability_matrix_row``            — Plan 04 ACTOR_CAPABILITY_MATRIX (ST2)
3. ``own_scratchpad``                   — Plan 02 own entries (ST3)
4. ``cross_agent_scratchpad``           — Plan 02 cross-agent entries (ST3)
5. ``recent_chain_events``              — event_log filtered to ``kora.*`` (ST4)
6. ``active_constitution_revision_id`` + ``active_constitution_rules_hash``
                                        — Constitution revision (ST4)

Plus two identity fields:
- ``workspace_id``
- ``assembled_at`` — ISO-8601 timestamp captured at end of assembly

The assembler fans out the six reads via ``asyncio.gather`` so cold
start hits one round-trip latency. Constitution-revision-absent
(fresh workspace) is non-fatal; ``active_constitution_revision_id`` /
``active_constitution_rules_hash`` are ``None`` in that case.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from .constitution import read_active_constitution_revision
from .events import RecentChainEvent, read_recent_kora_events
from .models import KoraCapabilityRow, RoleCharter
from .reads import read_active_role_charter, read_kora_capability_row
from .scratchpad import (
    ScratchpadEntry,
    read_cross_agent_scratchpad,
    read_own_scratchpad,
)


@dataclass(frozen=True, slots=True)
class KoraSessionContext:
    """Six load-bearing reads + two identity fields, assembled once per turn."""

    workspace_id: str
    assembled_at: str  # ISO-8601

    role_charter: RoleCharter
    capability_matrix_row: KoraCapabilityRow
    own_scratchpad: tuple[ScratchpadEntry, ...]
    cross_agent_scratchpad: tuple[ScratchpadEntry, ...]
    recent_chain_events: tuple[RecentChainEvent, ...]

    # Constitution revision — None for fresh workspaces (no rows in
    # kronicle.workspace_constitution_revisions yet).
    active_constitution_revision_id: Optional[str]
    active_constitution_rules_hash: Optional[str]


DEFAULT_SCRATCHPAD_LIMIT = 100
DEFAULT_RECENT_EVENT_LIMIT = 50


async def assemble_session_context(
    workspace_id: str,
    pool: Any,
    *,
    scratchpad_limit: int = DEFAULT_SCRATCHPAD_LIMIT,
    recent_event_limit: int = DEFAULT_RECENT_EVENT_LIMIT,
) -> KoraSessionContext:
    """Fan out the six reads in parallel via ``asyncio.gather``.

    Integrity errors from the Role Charter (SHA-256 mismatch / NULL
    body) propagate — fail-closed per ST2 contract. Scratchpad
    BLAKE3 drift warns but doesn't propagate. Constitution-revision-
    absent is non-fatal (returns None pair).
    """
    (
        role_charter,
        capability_matrix_row,
        own,
        cross,
        recent_events,
        constitution,
    ) = await asyncio.gather(
        read_active_role_charter(workspace_id, pool),
        read_kora_capability_row(pool),
        read_own_scratchpad(workspace_id, pool, limit=scratchpad_limit),
        read_cross_agent_scratchpad(workspace_id, pool, limit=scratchpad_limit),
        read_recent_kora_events(workspace_id, pool, limit=recent_event_limit),
        read_active_constitution_revision(workspace_id, pool),
    )

    return KoraSessionContext(
        workspace_id=workspace_id,
        assembled_at=datetime.now(timezone.utc).isoformat(),
        role_charter=role_charter,
        capability_matrix_row=capability_matrix_row,
        own_scratchpad=tuple(own),
        cross_agent_scratchpad=tuple(cross),
        recent_chain_events=tuple(recent_events),
        active_constitution_revision_id=(
            constitution.revision_id if constitution is not None else None
        ),
        active_constitution_rules_hash=(
            constitution.rules_hash if constitution is not None else None
        ),
    )
