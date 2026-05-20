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
    """[DEPRECATED in KR-8] Raised by the pre-K-8 deferred-write path.

    Kept exported for one release so downstream code or pinned tests
    that still reference the class resolve cleanly. After KR-8 (which
    swapped the defer for a real ``mcp_client.invoke`` call) this class
    is no longer raised by ``write_scratchpad_entry``; substrate-side
    failures surface as :class:`IsoKronMCPInvocationError`.

    BUILD_DEVIATIONS ``D-kr2-st3-no-scratchpad-write-mcp-tool`` is
    Closed in KR-8. Remove this class when KR-N audits show no
    remaining references.
    """

    DEFAULT_MESSAGE = (
        "[kora.isokron.deprecated] ScratchpadWriteNotAvailableError is "
        "obsolete after KR-8 — scratchpad writes now route through "
        "kora__write_agent_scratchpad via IsoKronMCPClient. Substrate-"
        "side failures surface as IsoKronMCPInvocationError. Original "
        "deferred-tag follows for grep stability: "
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
# Write path (KR-8: real Sea MCP call via kora__write_agent_scratchpad)
# ---------------------------------------------------------------------------


async def write_scratchpad_entry(
    *,
    workspace_id: str,
    scratchpad_kind: ScratchpadKind,
    visibility_scope: VisibilityScope,
    content: str,
    mcp_client: Any,
) -> str:
    """Write a scratchpad entry via the ``kora__write_agent_scratchpad`` MCP tool.

    Returns the new ``scratchpad_entry_id`` (UUID-as-text). K-8's
    substrate-side flow (per its PR body): BEGIN → SET LOCAL
    ``app.current_workspace_id`` → emit ``kronicle.agent_scratchpad.created``
    chain event → INSERT into ``kronicle.agent_scratchpad_entries`` →
    COMMIT. Fail-closed on ``actor_kind ≠ 'kora'`` BEFORE any DB write.

    Args:
        workspace_id: Clerk ``org_*`` TEXT.
        scratchpad_kind: ``ScratchpadKind`` enum value.
        visibility_scope: ``VisibilityScope`` enum value.
        content: inline content; BLAKE3 hex hash computed via
            :func:`compute_scratchpad_content_hash`. ``content_uri``
            (XOR alternative) is deferred to a follow-on; KR-8 ships
            the inline-only happy path that matches all existing
            call sites.
        mcp_client: a started :class:`IsoKronMCPClient`. Required.

    Raises:
        ValueError: ``mcp_client`` is ``None`` (defensive — caller
            should have resolved via
            ``IsoKronConnection.get_mcp_client()``).
        IsoKronMCPInvocationError: substrate-side error (CHECK
            violation, actor_kind ≠ 'kora', cap gate failure, content
            XOR violation, etc.).
        RuntimeError: ``kora__write_agent_scratchpad`` response shape
            drifted (missing or non-str ``scratchpad_entry_id``).
    """
    if mcp_client is None:
        raise ValueError(
            "write_scratchpad_entry: mcp_client is required (resolve via "
            "IsoKronConnection.get_mcp_client() before calling)"
        )
    content_hash = compute_scratchpad_content_hash(content)
    result = await mcp_client.invoke(
        "kora__write_agent_scratchpad",
        {
            "workspace_id": workspace_id,
            "scratchpad_kind": scratchpad_kind.value,
            "visibility_scope": visibility_scope.value,
            "content_inline": content,
            "content_hash": content_hash,
        },
    )
    # K-8 contract: tool returns {'scratchpad_entry_id': '<uuid>',
    # 'approved_event_id': '<uuid>'}. Runtime cares about the entry_id;
    # the event_id is in event_log for chain audit.
    entry_id = (
        result.get("scratchpad_entry_id") if isinstance(result, dict) else None
    )
    if not isinstance(entry_id, str):
        raise RuntimeError(
            f"kora__write_agent_scratchpad returned unexpected shape: "
            f"{result!r}; expected "
            f"{{'scratchpad_entry_id': '<uuid>', 'approved_event_id': '<uuid>'}}"
        )
    return entry_id
