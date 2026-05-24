"""RelationLink reads + deferred write (KR-3 ST2).

Substrate-side schema verified against
``packages/db/migrations/0058_relationlink.sql`` on substrate main
``41ddc208``:

- Table: ``relationlink`` (no schema prefix; lives in ``public``).
- PK: ``link_id UUID``.
- Workspace keying: ``workspace_id TEXT REFERENCES workspaces(id)``.
- Endpoints: ``from_entity_kind TEXT``, ``from_entity_id UUID``,
  ``to_entity_kind TEXT``, ``to_entity_id UUID``. Endpoint kinds are
  extensible TEXT (not CHECK'd) — the application-layer per-pair gate
  config validates.
- ``link_type TEXT`` — 21 V1 link types per ADR-0033 (11 sea/idea + 10
  platform-wide); ``same_as`` reserved for v1.5. SQL column is
  un-CHECK'd; vocabulary is informational.
- ``validity_state`` closed enum (``active`` / ``superseded`` /
  ``disputed`` / ``tombstoned``).
- ``created_by_actor_kind`` CHECK is **8 actor_kinds**: operator,
  oracle, critic, claude_pm, hermes, platform_seal, platform_rollback,
  platform_session_expiry. **No ``kora``** — KR-3 ST2 surfaces this as
  one of three blockers gating the write path.
- ``chain_event_id UUID NOT NULL`` — every write requires a chain
  event emit first.
- No RLS on the table; ``workspace_id`` filter in WHERE is the
  application-layer isolation. Direct asyncpg reads without GUC.

Three write-path blockers in current substrate state (all logged in
``BUILD_DEVIATIONS.md`` D-kr3-st2-no-relationlink-write-mcp-tool):

1. ``created_by_actor_kind`` CHECK must extend to include ``'kora'``
   (substrate migration needed; PM coordinates).
2. No Sea MCP write tool exposes the path (substrate-team dispatches
   the MCP tool after the CHECK extension lands).
3. ``chain_event_id NOT NULL`` requires the substrate MCP tool to
   emit + bind in one SECDEF (same shape as
   ``kronicle.compact_scratchpad`` from Plan 02).

Reads (``read_relationlink_for_node``, ``traverse_relationlink``)
work today against current schema.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RelationLinkWriteNotAvailableError(RuntimeError):
    """[DEPRECATED in KR-9] Raised by the pre-K-10 deferred-write path.

    Kept exported for one release so downstream code or pinned tests
    that still reference the class resolve cleanly. After KR-9 (which
    swapped the defer for a real ``mcp_client.invoke`` call) this class
    is no longer raised by ``create_relationlink``; substrate-side
    failures surface as :class:`IsoKronMCPInvocationError`.

    BUILD_DEVIATIONS ``D-kr3-st2-no-relationlink-write-mcp-tool`` is
    Closed in KR-9. Remove this class when KR-N audits show no
    remaining references.
    """

    DEFAULT_MESSAGE = (
        "[kora.isokron.deprecated] RelationLinkWriteNotAvailableError "
        "is obsolete after KR-9 — relationlink writes now route through "
        "kora__create_relationlink via IsoKronMCPClient. Substrate-side "
        "failures surface as IsoKronMCPInvocationError. Original "
        "deferred-tag preserved for grep stability: [kora.isokron.todo] "
        "relationlink writes deferred — three blockers in substrate "
        "main: (1) created_by_actor_kind CHECK lacks 'kora'; (2) no Sea "
        "MCP tool exposes the write; (3) chain_event_id NOT NULL "
        "requires substrate-side emit. Tracked in BUILD_DEVIATIONS.md "
        "as D-kr3-st2-no-relationlink-write-mcp-tool."
    )

    def __init__(self, message: Optional[str] = None):
        super().__init__(message or self.DEFAULT_MESSAGE)


# ---------------------------------------------------------------------------
# Typed shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RelationLinkRow:
    """One ``relationlink`` row projected to the JSON-friendly shape."""

    link_id: str
    workspace_id: str
    from_entity_kind: str
    from_entity_id: str
    to_entity_kind: str
    to_entity_id: str
    link_type: str
    link_weight: Optional[int]
    validity_state: str
    created_at: str  # ISO-8601


@dataclass(frozen=True, slots=True)
class ReachableNode:
    """One node reachable via :func:`traverse_relationlink`.

    ``depth_from_start`` is 1 for direct neighbors, 2 for two hops, etc.
    Tenet 2 (≤2 nesting) — keep the surface flat; callers that want the
    full path can issue a second query or build it themselves.
    """

    entity_id: str
    entity_kind: str
    via_link_type: str
    depth_from_start: int


# ---------------------------------------------------------------------------
# V1 link type vocabulary (verbatim from
# packages/sb1-substrate-shapes/src/relationlink.ts)
# ---------------------------------------------------------------------------

SEA_IDEA_LINK_TYPES: tuple[str, ...] = (
    "parent_of",
    "relates_to",
    "inspired_by",
    "responds_to",
    "conflicts_with",
    "supersedes",
    "blocks",
    "depends_on",
    "condenses_into",
    "branches_from",
    "references",
)

PLATFORM_WIDE_LINK_TYPES: tuple[str, ...] = (
    "derived_from",
    "applies_to",
    "validates",
    "grounds_in",
    "documented_in",
    "implements",
    "duplicates",
    "caused_by",
    "part_of",
    "covers",
)

V1_LINK_TYPES: tuple[str, ...] = SEA_IDEA_LINK_TYPES + PLATFORM_WIDE_LINK_TYPES
"""21 V1 link types (11 sea/idea + 10 platform-wide) per ADR-0033."""

RELATIONLINK_VALIDITY_STATES: tuple[str, ...] = (
    "active",
    "superseded",
    "disputed",
    "tombstoned",
)


# ---------------------------------------------------------------------------
# Read SQL — direct asyncpg, no RLS GUC needed (table has no RLS)
# ---------------------------------------------------------------------------

SELECT_RELATIONLINK_FOR_NODE_SQL = """
    SELECT
      link_id::text             AS link_id,
      workspace_id              AS workspace_id,
      from_entity_kind          AS from_entity_kind,
      from_entity_id::text      AS from_entity_id,
      to_entity_kind            AS to_entity_kind,
      to_entity_id::text        AS to_entity_id,
      link_type                 AS link_type,
      link_weight               AS link_weight,
      validity_state            AS validity_state,
      created_at                AS created_at
    FROM relationlink
    WHERE workspace_id = $1
      AND (from_entity_id = $2 OR to_entity_id = $2)
      AND validity_state = 'active'
    ORDER BY created_at DESC
    LIMIT $3
"""


# Recursive CTE for traversal in one direction. ``$3`` is the array of
# allowed ``link_type`` values; ``$4`` is the max depth. We anchor at
# ``$2`` (the start node) and follow ``to_entity_id`` (outgoing) or
# ``from_entity_id`` (incoming).
_TRAVERSE_OUTGOING_SQL = """
    WITH RECURSIVE walk AS (
      SELECT
        to_entity_id::text   AS entity_id,
        to_entity_kind       AS entity_kind,
        link_type            AS via_link_type,
        1                    AS depth
      FROM relationlink
      WHERE workspace_id = $1
        AND validity_state = 'active'
        AND from_entity_id = $2
        AND link_type = ANY($3::text[])

      UNION ALL

      SELECT
        rl.to_entity_id::text,
        rl.to_entity_kind,
        rl.link_type,
        walk.depth + 1
      FROM walk
      JOIN relationlink rl
        ON rl.workspace_id = $1
       AND rl.validity_state = 'active'
       AND rl.link_type = ANY($3::text[])
       AND rl.from_entity_id::text = walk.entity_id
      WHERE walk.depth < $4
    )
    SELECT DISTINCT entity_id, entity_kind, via_link_type, depth
    FROM walk
"""

_TRAVERSE_INCOMING_SQL = """
    WITH RECURSIVE walk AS (
      SELECT
        from_entity_id::text AS entity_id,
        from_entity_kind     AS entity_kind,
        link_type            AS via_link_type,
        1                    AS depth
      FROM relationlink
      WHERE workspace_id = $1
        AND validity_state = 'active'
        AND to_entity_id = $2
        AND link_type = ANY($3::text[])

      UNION ALL

      SELECT
        rl.from_entity_id::text,
        rl.from_entity_kind,
        rl.link_type,
        walk.depth + 1
      FROM walk
      JOIN relationlink rl
        ON rl.workspace_id = $1
       AND rl.validity_state = 'active'
       AND rl.link_type = ANY($3::text[])
       AND rl.to_entity_id::text = walk.entity_id
      WHERE walk.depth < $4
    )
    SELECT DISTINCT entity_id, entity_kind, via_link_type, depth
    FROM walk
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _project_link_row(row: dict[str, Any]) -> RelationLinkRow:
    return RelationLinkRow(
        link_id=row["link_id"],
        workspace_id=row["workspace_id"],
        from_entity_kind=row["from_entity_kind"],
        from_entity_id=row["from_entity_id"],
        to_entity_kind=row["to_entity_kind"],
        to_entity_id=row["to_entity_id"],
        link_type=row["link_type"],
        link_weight=row["link_weight"],
        validity_state=row["validity_state"],
        created_at=_iso(row["created_at"]),
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 100
MAX_TRAVERSE_DEPTH = 3


async def read_relationlink_for_node(
    workspace_id: str,
    entity_id: str,
    pool: Any,
    *,
    limit: int = DEFAULT_LIST_LIMIT,
) -> list[RelationLinkRow]:
    """Return all active ``relationlink`` rows where ``entity_id`` is
    either source or target.

    Per spec § ST2 § iso_link_list_for_node — useful for "what does
    Kora know about this entity?" queries. Capped at 100 rows.
    """
    capped = max(1, min(MAX_LIST_LIMIT, limit))
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            SELECT_RELATIONLINK_FOR_NODE_SQL, workspace_id, entity_id, capped
        )
    return [_project_link_row(dict(row)) for row in rows]


async def traverse_relationlink(
    workspace_id: str,
    from_entity_id: str,
    link_types: list[str],
    pool: Any,
    *,
    direction: str = "outgoing",
    max_depth: int = 2,
) -> list[ReachableNode]:
    """Walk typed edges from ``from_entity_id`` up to ``max_depth`` hops.

    ``direction`` must be ``'outgoing'``, ``'incoming'``, or ``'both'``.
    For ``'both'`` we issue two queries and merge results — the union
    keeps the implementation simple at the cost of one extra round trip.

    ``link_types`` is the set of ``link_type`` values to follow; the
    recursive CTE restricts each hop to that set so the walk doesn't
    sprawl through unrelated edges.

    Returns deduplicated nodes; ``depth_from_start`` is the minimum
    depth observed when the same node is reachable via multiple paths.
    """
    if direction not in ("outgoing", "incoming", "both"):
        raise ValueError(
            f"traverse_relationlink: direction must be outgoing / incoming / "
            f"both; got {direction!r}"
        )
    if not link_types:
        raise ValueError(
            "traverse_relationlink: link_types must be non-empty (would "
            "otherwise sprawl through every edge type — keep walks scoped)"
        )
    depth = max(1, min(MAX_TRAVERSE_DEPTH, max_depth))

    async def _run(sql: str) -> list[dict[str, Any]]:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                sql,
                workspace_id,
                from_entity_id,
                list(link_types),
                depth,
            )
        return [dict(r) for r in rows]

    raw: list[dict[str, Any]] = []
    if direction in ("outgoing", "both"):
        raw.extend(await _run(_TRAVERSE_OUTGOING_SQL))
    if direction in ("incoming", "both"):
        raw.extend(await _run(_TRAVERSE_INCOMING_SQL))

    # Dedupe by (entity_id, entity_kind, via_link_type) and keep the
    # MIN depth — same node reached via shorter path wins for callers
    # that care about path length.
    best: dict[tuple[str, str, str], int] = {}
    for row in raw:
        key = (row["entity_id"], row["entity_kind"], row["via_link_type"])
        depth_val = int(row["depth"])
        if key not in best or depth_val < best[key]:
            best[key] = depth_val
    return [
        ReachableNode(
            entity_id=entity_id,
            entity_kind=entity_kind,
            via_link_type=via_link_type,
            depth_from_start=depth_val,
        )
        for (entity_id, entity_kind, via_link_type), depth_val in best.items()
    ]


# ---------------------------------------------------------------------------
# Write path (KR-9: real Sea MCP call via kora__create_relationlink)
# ---------------------------------------------------------------------------


async def create_relationlink(
    *,
    workspace_id: str,
    from_entity_kind: str,
    from_entity_id: str,
    to_entity_kind: str,
    to_entity_id: str,
    link_type: str,
    rationale: Optional[str] = None,
    rationale_block_id: Optional[str] = None,
    evidence_block_ids: Optional[list[str]] = None,
    mcp_client: Any,
) -> str:
    """Create a typed edge via the ``kora__create_relationlink`` MCP tool.

    Returns the new ``link_id`` (UUID-as-text). K-10's substrate-side
    flow (per its PR body + IsoKron PM #32 §1): thin wrapper around
    ``public.kora_create_relationlink`` SECDEF which does the
    actor_registry JOIN actor_kind validation + active-edge uniqueness
    check + emits ``kora.relationlink.created`` chain event FIRST +
    INSERTs the row with the returned event_id as ``chain_event_id``
    binding (single atomic transaction via SECDEF).

    Args:
        workspace_id: Clerk ``org_*`` TEXT.
        from_entity_id / from_entity_kind / to_entity_id / to_entity_kind:
            edge endpoints. ``entity_kind`` is one of the canonical
            17 entity kinds; ``entity_id`` is a UUID.
        link_type: V1 vocabulary (21 entries per ADR-0033); un-CHECK'd
            at DB layer — application-layer per-pair gate validates.
        rationale: free-form rationale text (legacy from KR-3 ST2 API).
            Not part of K-10's input schema; held for one-release back-
            compat and dropped silently. Use ``rationale_block_id``
            instead.
        rationale_block_id: optional Block reference (UUID); preferred
            over freeform ``rationale``.
        evidence_block_ids: optional list of Block references; defaults
            to empty list.
        mcp_client: a started :class:`IsoKronMCPClient`. Required.

    Raises:
        ValueError: ``mcp_client`` is ``None``.
        IsoKronMCPInvocationError: substrate-side error
            (actor_kind mismatch, active-edge uniqueness violation,
            cap gate failure, etc.).
        RuntimeError: response shape drifted (missing or non-str
            ``link_id``).
    """
    del rationale  # KR-9 keeps the param for one-release back-compat
    if mcp_client is None:
        raise ValueError(
            "create_relationlink: mcp_client is required (resolve via "
            "IsoKronConnection.get_mcp_client() before calling)"
        )
    args: dict[str, Any] = {
        "workspace_id": workspace_id,
        "from_entity_id": from_entity_id,
        "from_entity_kind": from_entity_kind,
        "to_entity_id": to_entity_id,
        "to_entity_kind": to_entity_kind,
        "link_type": link_type,
        "evidence_block_ids": evidence_block_ids or [],
    }
    if rationale_block_id is not None:
        args["rationale_block_id"] = rationale_block_id
    result = await mcp_client.invoke("kora__create_relationlink", args)
    # K-10 contract: {'link_id': '<uuid>', 'chain_event_id': '<uuid>'}.
    link_id = result.get("link_id") if isinstance(result, dict) else None
    if not isinstance(link_id, str):
        raise RuntimeError(
            f"kora__create_relationlink returned unexpected shape: "
            f"{result!r}; expected "
            f"{{'link_id': '<uuid>', 'chain_event_id': '<uuid>'}}"
        )
    return link_id
