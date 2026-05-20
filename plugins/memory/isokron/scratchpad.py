"""Scratchpad reads + deferred writes (KR-2 ST3).

Read paths against ``kronicle.agent_scratchpad_entries`` (foundation/0135):

- :func:`read_own_scratchpad` — Kora's own entries (JOIN actor_registry
  on actor_id, filter ``actor_kind = 'kora'``).
- :func:`read_cross_agent_scratchpad` — entries from other actors
  (Critic / Oracle / claude_pm) marked
  ``visibility_scope = 'cross_agent_dereferenceable'``.

Both reads use the same RLS-GUC-in-transaction pattern as the policy
registry: ``set_config('app.current_workspace_id', $1, true)`` then
SELECT. BLAKE3 hash mismatch logs a WARNING (per spec § ST3: "No
fail-closed semantics on integrity mismatch for scratchpad — scratchpad
is mutable working memory; log warning + continue with the entry marked
stale, don't refuse the session").

Write path is DEFERRED — see :class:`ScratchpadWriteNotAvailableError`
and BUILD_DEVIATIONS entry ``D-kr2-st3-no-scratchpad-write-mcp-tool``.
Spec § ST3 fallback ("If the tool doesn't exist substrate-side yet,
BUILD_DEVIATIONS + queue for substrate-team via PM coordination. Do
NOT bypass with direct INSERT.") applies because, as of substrate
main ``a3e77f67``, ``packages/sea-mcp-server/src/tools/`` exposes only
``kora__propose_convention`` / ``kora__read_escalation_queue`` /
``kora__propose_policy_change`` — no ``kora__write_agent_scratchpad``.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Any, Optional

try:  # blake3 is a plugin pip_dependency; the plugin.yaml extra installs it.
    import blake3 as _blake3
except ImportError:  # pragma: no cover — surfaced via is_available()
    _blake3 = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums (mirror kronicle.scratchpad_* enum types from migration 0135)
# ---------------------------------------------------------------------------


class ScratchpadKind(str, enum.Enum):
    """Mirrors ``kronicle.scratchpad_kind`` ENUM from foundation/0135.

    Stable strings — they appear in chain events, audit log payloads,
    and Sea MCP tool params.
    """

    REASONING_TRAIL = "reasoning_trail"
    HYPOTHESIS = "hypothesis"
    DISCARDED_OPTION = "discarded_option"
    OVERRIDE_RATIONALE = "override_rationale"
    SELF_CRITIQUE = "self_critique"
    ROUTE_DECISION = "route_decision"
    COMPACTED_SUMMARY = "compacted_summary"


class VisibilityScope(str, enum.Enum):
    """Mirrors ``kronicle.scratchpad_visibility_scope`` ENUM."""

    AGENT_PRIVATE = "agent_private"
    CROSS_AGENT_DEREFERENCEABLE = "cross_agent_dereferenceable"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ScratchpadWriteNotAvailableError(RuntimeError):
    """The substrate-side write tool isn't deployed yet.

    Raised by :func:`write_scratchpad_entry` until ``kora__write_agent_
    scratchpad`` (or equivalent) ships on the Sea MCP server. Per spec
    § ST3, runtime callers MUST NOT bypass with direct INSERT — that
    skips authorization (``cap_write_agent_scratchpad`` gate), chain
    event emission (``approved_event_id NOT NULL`` requirement), and
    the visibility_scope semantics check.

    Tracked as ``D-kr2-st3-no-scratchpad-write-mcp-tool`` in
    ``BUILD_DEVIATIONS.md``. Closes when the MCP tool lands.
    """

    DEFAULT_MESSAGE = (
        "[kora.isokron.todo] scratchpad writes deferred — Sea MCP server "
        "does not yet expose kora__write_agent_scratchpad (or equivalent). "
        "Tracked in BUILD_DEVIATIONS.md as D-kr2-st3-no-scratchpad-write-"
        "mcp-tool. Spec § ST3: do NOT bypass with direct INSERT — the "
        "MCP tool gates cap_write_agent_scratchpad authorization, emits "
        "the approved_event_id chain event, and validates visibility_scope."
    )

    def __init__(self, message: Optional[str] = None):
        super().__init__(message or self.DEFAULT_MESSAGE)


# ---------------------------------------------------------------------------
# Typed shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScratchpadEntry:
    """One row from ``kronicle.agent_scratchpad_entries`` (JOIN-projected).

    Mirrors the SELECT shape in spec § ST3:

    - ``actor_kind`` + ``actor_label`` come from the
      ``actor_registry`` JOIN (no columns on the scratchpad table
      itself per foundation/0135 — that's a PM-verified schema
      gotcha).
    - ``content_inline`` XOR ``content_uri`` per the CHECK constraint
      ``agent_scratchpad_content_xor``.
    - ``content_hash`` is BLAKE3 hex (per migration comment line 97).
    - ``visibility_scope`` is a string-enum value, not a bool.
    """

    scratchpad_entry_id: str
    actor_kind: str
    actor_label: str
    content_inline: Optional[str]
    content_uri: Optional[str]
    content_hash: str
    visibility_scope: VisibilityScope
    scratchpad_kind: ScratchpadKind
    created_at: str  # ISO-8601

    def is_inline(self) -> bool:
        """True iff the entry stores content inline (vs object-store URI)."""
        return self.content_inline is not None


# ---------------------------------------------------------------------------
# BLAKE3 helper
# ---------------------------------------------------------------------------


def compute_scratchpad_content_hash(content: str) -> str:
    """BLAKE3 hex digest of ``content`` — matches Postgres-side storage.

    Migration 0135 stores ``content_hash TEXT`` as hex-encoded BLAKE3
    (line 97 of the migration). Runtime callers verify by recomputing
    and comparing; mismatches WARN but do not fail (scratchpad is
    mutable working memory, not the Role Charter's identity surface).
    """
    if _blake3 is None:  # pragma: no cover — plugin.yaml lists blake3 as required
        raise RuntimeError(
            "[kora.isokron] blake3 not installed — required for scratchpad "
            "content integrity checks. Run `uv sync --extra isokron`."
        )
    return _blake3.blake3(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Canonical SQL (verbatim from spec § ST3 + foundation/0135 verified)
# ---------------------------------------------------------------------------

SELECT_OWN_SCRATCHPAD_SQL = """
    SELECT
      s.scratchpad_entry_id::text AS scratchpad_entry_id,
      ar.actor_kind                AS actor_kind,
      ar.display_name              AS actor_label,
      s.content_inline             AS content_inline,
      s.content_uri                AS content_uri,
      s.content_hash               AS content_hash,
      s.visibility_scope::text     AS visibility_scope,
      s.scratchpad_kind::text      AS scratchpad_kind,
      s.created_at                 AS created_at
    FROM kronicle.agent_scratchpad_entries s
    JOIN public.actor_registry ar ON ar.actor_id = s.actor_id
    WHERE s.workspace_id = $1
      AND ar.actor_kind = 'kora'
      AND s.status = 'active'
    ORDER BY s.created_at DESC
    LIMIT $2
"""

SELECT_CROSS_AGENT_SCRATCHPAD_SQL = """
    SELECT
      s.scratchpad_entry_id::text AS scratchpad_entry_id,
      ar.actor_kind                AS actor_kind,
      ar.display_name              AS actor_label,
      s.content_inline             AS content_inline,
      s.content_uri                AS content_uri,
      s.content_hash               AS content_hash,
      s.visibility_scope::text     AS visibility_scope,
      s.scratchpad_kind::text      AS scratchpad_kind,
      s.created_at                 AS created_at
    FROM kronicle.agent_scratchpad_entries s
    JOIN public.actor_registry ar ON ar.actor_id = s.actor_id
    WHERE s.workspace_id = $1
      AND ar.actor_kind != 'kora'
      AND s.visibility_scope = 'cross_agent_dereferenceable'
      AND s.status = 'active'
    ORDER BY s.created_at DESC
    LIMIT $2
"""

DEFAULT_SCRATCHPAD_READ_LIMIT = 100
"""Per spec § ST3: default limit 100 for both reads."""


# ---------------------------------------------------------------------------
# Row → ScratchpadEntry assembler
# ---------------------------------------------------------------------------


def _assemble_entry(row: dict[str, Any]) -> ScratchpadEntry:
    """Project a raw DB row to ``ScratchpadEntry``.

    Verifies content_hash via BLAKE3 when ``content_inline`` is set;
    mismatches log WARNING (per spec — scratchpad is mutable). Does
    not verify hashes when ``content_uri`` is set (the URI target is
    an object store; verification is the object-store fetcher's job).
    """
    inline = row.get("content_inline")
    uri = row.get("content_uri")
    stored_hash = row["content_hash"]

    if inline is not None:
        recomputed = compute_scratchpad_content_hash(inline)
        if recomputed != stored_hash:
            logger.warning(
                "[kora.isokron] scratchpad content_hash drift for "
                "entry %s (actor=%s): stored=%s recomputed=%s. "
                "Continuing with the entry per spec § ST3 (scratchpad "
                "is mutable working memory; do not fail-close).",
                row["scratchpad_entry_id"],
                row["actor_kind"],
                stored_hash,
                recomputed,
            )

    created_at = row["created_at"]
    if hasattr(created_at, "isoformat"):
        created_at_iso = created_at.isoformat()
    else:
        created_at_iso = str(created_at)

    return ScratchpadEntry(
        scratchpad_entry_id=row["scratchpad_entry_id"],
        actor_kind=row["actor_kind"],
        actor_label=row["actor_label"],
        content_inline=inline,
        content_uri=uri,
        content_hash=stored_hash,
        visibility_scope=VisibilityScope(row["visibility_scope"]),
        scratchpad_kind=ScratchpadKind(row["scratchpad_kind"]),
        created_at=created_at_iso,
    )


# ---------------------------------------------------------------------------
# Read paths
# ---------------------------------------------------------------------------


async def _run_scratchpad_select(
    sql: str,
    workspace_id: str,
    limit: int,
    pool: Any,
) -> list[ScratchpadEntry]:
    """Shared txn + RLS-GUC + SELECT wiring for both reads.

    Mirrors the policy-registry pattern from KR-2 ST2 (``reads.py``).
    Without the GUC set, RLS returns zero rows silently.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.current_workspace_id', $1, true)",
                workspace_id,
            )
            rows = await conn.fetch(sql, workspace_id, limit)
    return [_assemble_entry(dict(row)) for row in rows]


async def read_own_scratchpad(
    workspace_id: str,
    pool: Any,
    *,
    limit: int = DEFAULT_SCRATCHPAD_READ_LIMIT,
) -> list[ScratchpadEntry]:
    """Read Kora's own scratchpad entries for the workspace.

    Filters via ``actor_registry.actor_kind = 'kora'`` JOIN; only
    ``status='active'`` rows (superseded / invalidated / tombstoned
    are excluded — operators query those via SQL directly).
    """
    return await _run_scratchpad_select(
        SELECT_OWN_SCRATCHPAD_SQL, workspace_id, limit, pool
    )


async def read_cross_agent_scratchpad(
    workspace_id: str,
    pool: Any,
    *,
    limit: int = DEFAULT_SCRATCHPAD_READ_LIMIT,
) -> list[ScratchpadEntry]:
    """Read other actors' scratchpad entries Kora is allowed to dereference.

    Filters to ``actor_kind != 'kora'`` (Critic / Oracle / claude_pm)
    AND ``visibility_scope = 'cross_agent_dereferenceable'`` AND
    ``status = 'active'``.
    """
    return await _run_scratchpad_select(
        SELECT_CROSS_AGENT_SCRATCHPAD_SQL, workspace_id, limit, pool
    )


# ---------------------------------------------------------------------------
# Write path (deferred — see BUILD_DEVIATIONS D-kr2-st3-no-scratchpad-write-mcp-tool)
# ---------------------------------------------------------------------------


async def write_scratchpad_entry(
    *,
    workspace_id: str,
    scratchpad_kind: ScratchpadKind,
    visibility_scope: VisibilityScope,
    content: str,
    mcp_client: Any = None,
) -> str:
    """Append a scratchpad entry via the Sea MCP tool surface.

    Raises ``ScratchpadWriteNotAvailableError`` until the substrate-side
    tool lands. Sync_turn and on_memory_write catch this specific
    exception + log a one-line warning so sessions stay alive.

    Signature is the shape the future MCP-backed implementation will
    keep — caller code wired against this signature won't need to
    change when the tool ships and the body switches to an
    ``mcp_client.invoke('kora__write_agent_scratchpad', ...)`` call.

    Returns the new ``scratchpad_entry_id`` (UUID-as-text) when wired;
    until then, never returns.
    """
    del workspace_id, scratchpad_kind, visibility_scope, content, mcp_client
    raise ScratchpadWriteNotAvailableError()
