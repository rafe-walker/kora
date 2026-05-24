"""Chain event emit + recent events read (KR-2 ST4 + KR-7).

Two halves:

- :func:`read_recent_kora_events` — direct asyncpg read against
  ``hivex_foundation.event_log`` filtered to ``event_type LIKE 'kora.%'``.
  ``event_log`` is **tenant_id UUID keyed** (the one genuine substrate
  exception to the workspace_id TEXT pattern, per foundation/0003); the
  query resolves the caller's ``workspace_id`` (Clerk ``org_*`` TEXT)
  to ``tenant_id`` via ``JOIN hivex_foundation.tenant ON
  t.clerk_org_id = $1``. Matches the TS reader at
  ``packages/sea-mcp-server/src/kora/context-assembler/index.ts:287``.

- :func:`emit_kora_event` — chain event emit via the
  ``kora__append_event`` Sea MCP tool. K-9 shipped the substrate tool
  (`f8487059`); KR-7 (this swap) replaced the previous
  ``ChainEventEmitNotAvailableError`` defer with a real
  ``mcp_client.invoke`` call. Returns the new event_id (UUID string).
  Production-test posture: substrate-team's dispatch tier (queued)
  bridges Layer-A wsk_* auth → Layer-B ``actor_kind='kora'`` and
  un-stubs the K-9 handler; until that lands, live calls return
  substrate-side errors but the code shape is correct. KR-7a's
  ``IsoKronMCPClient`` handles the transport.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ChainEventEmitNotAvailableError(RuntimeError):
    """[DEPRECATED in KR-7] Raised by the pre-K-9 deferred-emit path.

    Kept exported for one release so any downstream code or pinned
    tests that still reference the class still resolve. After KR-7
    (which swapped the defer for a real ``mcp_client.invoke`` call)
    this class is no longer raised by ``emit_kora_event``; substrate-
    side failures now surface as
    :class:`IsoKronMCPInvocationError` from ``mcp_client``.

    BUILD_DEVIATIONS ``D-kr2-st4-no-chain-emit-mcp-tool`` is Closed in
    KR-7. Remove this class when KR-N audits show no remaining
    references.
    """

    DEFAULT_MESSAGE = (
        "[kora.isokron.deprecated] ChainEventEmitNotAvailableError is "
        "obsolete after KR-7 — chain event emits now route through "
        "kora__append_event via IsoKronMCPClient. Substrate-side "
        "failures surface as IsoKronMCPInvocationError."
    )

    def __init__(self, message: Optional[str] = None):
        super().__init__(message or self.DEFAULT_MESSAGE)


# ---------------------------------------------------------------------------
# Typed shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecentChainEvent:
    """One ``kora.*`` chain event from ``hivex_foundation.event_log``.

    Mirrors TS-side ``KoraRecentChainEvent`` in
    ``packages/sea-mcp-server/src/kora/context-assembler/types.ts:72``.
    ``payload_summary`` is ``jsonb_pretty(payload)`` truncated to 300
    chars + ellipsis — keeps system prompt bullets readable without
    bloating the prompt on operator-extended payloads.
    """

    event_id: str
    event_type: str
    occurred_at: str  # ISO-8601 from TIMESTAMPTZ
    payload_summary: str


# ---------------------------------------------------------------------------
# Read SQL — matches TS reader verbatim
# ---------------------------------------------------------------------------

SELECT_RECENT_KORA_CHAIN_EVENTS_SQL = """
    SELECT
      el.event_id::text                AS event_id,
      el.event_type                    AS event_type,
      el.occurred_at                   AS occurred_at,
      jsonb_pretty(el.payload)::text   AS payload_text
    FROM hivex_foundation.event_log el
    JOIN hivex_foundation.tenant t ON t.tenant_id = el.tenant_id
    WHERE t.clerk_org_id = $1
      AND el.event_type LIKE 'kora.%'
    ORDER BY el.occurred_at DESC
    LIMIT $2
"""

DEFAULT_RECENT_EVENT_LIMIT = 50
"""Per spec § ST4 + TS-side ``DEFAULT_RECENT_EVENT_LIMIT``."""

RECENT_EVENT_PAYLOAD_TRUNCATE_LENGTH = 300
"""Per TS-side ``RECENT_EVENT_PAYLOAD_TRUNCATE_LENGTH``. Truncated
``jsonb_pretty`` output preserves the leading structural lines so an
operator skimming the system prompt can still see what each event
carried, without bloating long payloads."""


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------


def _truncate_payload(payload_text: str) -> str:
    if len(payload_text) <= RECENT_EVENT_PAYLOAD_TRUNCATE_LENGTH:
        return payload_text
    return payload_text[:RECENT_EVENT_PAYLOAD_TRUNCATE_LENGTH] + "…"


def _project_event(row: dict[str, Any]) -> RecentChainEvent:
    occurred_at = row["occurred_at"]
    if hasattr(occurred_at, "isoformat"):
        occurred_at_iso = occurred_at.isoformat()
    else:
        occurred_at_iso = str(occurred_at)
    return RecentChainEvent(
        event_id=row["event_id"],
        event_type=row["event_type"],
        occurred_at=occurred_at_iso,
        payload_summary=_truncate_payload(row["payload_text"] or ""),
    )


# ---------------------------------------------------------------------------
# DR-observed event read (KR-P2-DR-FLIP)
# ---------------------------------------------------------------------------
# read_recent_kora_events filters LIKE 'kora.%' and returns truncated
# payload TEXT. The DR panel needs structured JSON to project
# from_epoch / to_epoch / discarded_* / cleared_* fields, so this
# sibling helper restricts to event_type = 'kora.dr.observed' and
# returns the parsed JSON payload alongside the event metadata.

SELECT_DR_OBSERVED_EVENTS_SQL = """
    SELECT
      el.event_id::text  AS event_id,
      el.occurred_at     AS occurred_at,
      el.payload         AS payload
    FROM hivex_foundation.event_log el
    JOIN hivex_foundation.tenant t ON t.tenant_id = el.tenant_id
    WHERE t.clerk_org_id = $1
      AND el.event_type = 'kora.dr.observed'
    ORDER BY el.occurred_at DESC
    LIMIT $2
"""

DEFAULT_DR_OBSERVED_LIMIT = 10
"""Default limit per bucket §4 — last 10 events sufficient for v1."""


@dataclass(frozen=True, slots=True)
class DRObservedEventRow:
    """One ``kora.dr.observed`` event with structured payload.

    Used by KR-P2-DR-FLIP's :func:`get_dr_state_summary` to project
    the DR-panel ``recent_dr_events`` array. Payload is the raw
    dict from the event_log row's ``jsonb`` column — caller picks
    out the documented fields and synthesises missing ones (e.g. a
    DR event predating the from/to-epoch contract would surface
    those as ``None``).
    """

    event_id: str
    occurred_at: str  # ISO-8601
    payload: dict[str, Any]


# ---------------------------------------------------------------------------
# General live-tail event read (KR-P2-CHAIN-EVENTS-PANEL)
# ---------------------------------------------------------------------------
# read_dr_observed_events is event-type-specific. The CHAIN-EVENTS panel
# needs an arbitrary-prefix + arbitrary-actor + cursor-paginated read.
# Same tenant JOIN; adds actor_id projection + optional WHERE filters +
# before_ts cursor for "Load older" pagination.

# Note on actor_id / actor_kind:
#   event_log.actor_id exists on the substrate side (confirmed via
#   BUILD_DEVIATIONS K-7's verify-at-first-live-emit note). actor_kind
#   lives on actor_registry (one JOIN away); v1 leaves actor_kind=None
#   and emits only actor_id to keep this read simple. Operators can
#   cross-reference actor_kind via actor_id in the cockpit if needed.


@dataclass(frozen=True, slots=True)
class ChainEventRow:
    """One ``kora.*`` (or arbitrary-prefix) chain event with full payload.

    Used by the KR-P2-CHAIN-EVENTS-PANEL's live tail. Carries the
    structured payload (not truncated) and the actor_id (for operator
    cross-reference); actor_kind requires a JOIN we don't do in v1.
    """

    event_id: str
    event_type: str
    actor_id: Optional[str]
    occurred_at: str  # ISO-8601
    payload: dict[str, Any]


DEFAULT_RECENT_EVENTS_LIMIT = 100
"""Per CHAIN-EVENTS-PANEL spec §3 — default 100, max 500."""

MAX_RECENT_EVENTS_LIMIT = 500
"""Hard cap so a buggy / hostile caller can't ask for 1M rows."""


async def read_recent_events(
    workspace_id: str,
    pool: Any,
    *,
    event_type_prefix: Optional[str] = None,
    actor_id_filter: Optional[str] = None,
    limit: int = DEFAULT_RECENT_EVENTS_LIMIT,
    before_ts: Optional[str] = None,
) -> list[ChainEventRow]:
    """Read recent event_log rows for the workspace.

    Supports:
      * ``event_type_prefix`` — e.g. ``"kora."`` (default behaviour
        prior to KR-P2-CHAIN-EVENTS-PANEL was the implicit "kora.%")
        or ``"kora.constitution."`` for family-narrowed views. Empty
        string / None means "no prefix filter".
      * ``actor_id_filter`` — restrict to events emitted by a specific
        actor (e.g. just Kora). None means "any actor".
      * ``limit`` — clamped to ``[1, MAX_RECENT_EVENTS_LIMIT]``.
      * ``before_ts`` — pagination cursor (ISO-8601 string). When set,
        returns only events with ``occurred_at < before_ts``. The FE
        feeds the previous page's last ``occurred_at`` here to load
        older events.

    Returns rows ordered ``occurred_at DESC`` so the live tail renders
    newest-first by default.

    Defensive on malformed payload: WARN log + ``{}`` fallback so a
    single bad row doesn't take down the whole read.
    """
    import json as _json

    safe_limit = max(1, min(MAX_RECENT_EVENTS_LIMIT, int(limit)))

    where_clauses = ["t.clerk_org_id = $1"]
    params: list[Any] = [workspace_id]

    if event_type_prefix:
        params.append(f"{event_type_prefix}%")
        where_clauses.append(f"el.event_type LIKE ${len(params)}")

    if actor_id_filter:
        params.append(actor_id_filter)
        where_clauses.append(f"el.actor_id::text = ${len(params)}")

    if before_ts:
        params.append(before_ts)
        where_clauses.append(f"el.occurred_at < ${len(params)}::timestamptz")

    params.append(safe_limit)
    limit_param = f"${len(params)}"

    sql = f"""
        SELECT
          el.event_id::text   AS event_id,
          el.event_type       AS event_type,
          el.actor_id::text   AS actor_id,
          el.occurred_at      AS occurred_at,
          el.payload          AS payload
        FROM hivex_foundation.event_log el
        JOIN hivex_foundation.tenant t ON t.tenant_id = el.tenant_id
        WHERE {' AND '.join(where_clauses)}
        ORDER BY el.occurred_at DESC
        LIMIT {limit_param}
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    out: list[ChainEventRow] = []
    for row in rows:
        occurred_at = row["occurred_at"]
        occurred_iso = (
            occurred_at.isoformat()
            if hasattr(occurred_at, "isoformat")
            else str(occurred_at)
        )

        raw_payload = row["payload"]
        if isinstance(raw_payload, dict):
            payload = raw_payload
        elif isinstance(raw_payload, str):
            try:
                payload = _json.loads(raw_payload)
            except (ValueError, TypeError):
                logger.warning(
                    "[kora.event_log] malformed payload on event_id=%s "
                    "(event_type=%s) — using {} fallback",
                    row["event_id"],
                    row["event_type"],
                )
                payload = {}
        else:
            payload = {}

        out.append(
            ChainEventRow(
                event_id=row["event_id"],
                event_type=row["event_type"],
                actor_id=row.get("actor_id"),
                occurred_at=occurred_iso,
                payload=payload,
            )
        )
    return out


# ---------------------------------------------------------------------------
# DR-observed event read (KR-P2-DR-FLIP — pre-existing)
# ---------------------------------------------------------------------------


async def read_dr_observed_events(
    workspace_id: str,
    pool: Any,
    *,
    limit: int = DEFAULT_DR_OBSERVED_LIMIT,
) -> list[DRObservedEventRow]:
    """Read recent ``kora.dr.observed`` events for the workspace.

    Same tenant-resolution JOIN as :func:`read_recent_kora_events`
    (the substrate-team established pattern); restricted to the
    DR-observed event_type so the DR panel doesn't waste a 50-event
    fetch on unrelated chain events just to filter post-hoc.
    """
    import json as _json

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            SELECT_DR_OBSERVED_EVENTS_SQL, workspace_id, limit
        )

    out: list[DRObservedEventRow] = []
    for row in rows:
        occurred_at = row["occurred_at"]
        occurred_iso = (
            occurred_at.isoformat()
            if hasattr(occurred_at, "isoformat")
            else str(occurred_at)
        )
        raw_payload = row["payload"]
        # asyncpg can return jsonb as either dict (if codec installed)
        # or str (if not); handle both for defensiveness.
        if isinstance(raw_payload, str):
            try:
                payload = _json.loads(raw_payload)
            except (ValueError, TypeError):
                payload = {}
        elif isinstance(raw_payload, dict):
            payload = raw_payload
        else:
            payload = {}
        out.append(
            DRObservedEventRow(
                event_id=row["event_id"],
                occurred_at=occurred_iso,
                payload=payload,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Recent-kora-events read (pre-existing — ST4 system_prompt_block)
# ---------------------------------------------------------------------------


async def read_recent_kora_events(
    workspace_id: str,
    pool: Any,
    *,
    limit: int = DEFAULT_RECENT_EVENT_LIMIT,
) -> list[RecentChainEvent]:
    """Read recent ``kora.*`` chain events for the workspace.

    Note: ``hivex_foundation.event_log`` keys off ``tenant_id UUID``,
    not ``workspace_id TEXT`` like the other Kora tables. The JOIN
    resolves the workspace_id (Clerk ``org_*``) to tenant_id via
    ``hivex_foundation.tenant.clerk_org_id``. The TS reader at
    ``packages/sea-mcp-server/src/kora/context-assembler/index.ts:287``
    uses the same JOIN; this is the substrate-team's established
    resolution pattern.

    No RLS GUC needed here: ``event_log``'s row-security model uses
    different machinery (per-partition partial indexes + foreign-key
    constraints to tenant); the JOIN itself enforces tenant isolation
    via the workspace_id binding.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            SELECT_RECENT_KORA_CHAIN_EVENTS_SQL, workspace_id, limit
        )
    return [_project_event(dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# Emit path (deferred — see BUILD_DEVIATIONS D-kr2-st4-no-chain-emit-mcp-tool)
# ---------------------------------------------------------------------------


async def emit_kora_event(
    *,
    workspace_id: str,
    event_type: str,
    payload: Any,
    mcp_client: Any,
) -> str:
    """Emit a ``kora.*`` chain event via the ``kora__append_event`` MCP tool.

    Returns the new ``event_id`` (UUID string) on success.
    ``mcp_client`` must be a started :class:`IsoKronMCPClient`
    (typically obtained via ``IsoKronConnection.get_mcp_client()``).

    ``event_type`` must start with ``kora.`` and appear in the
    ``event_log_event_type_check`` constraint set (foundation/0136 +
    foundation/0138 ship the canonical vocabulary). The Sea MCP tool
    validates with a Zod regex ``^kora\\.[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$``
    — runtime callers pass the literal through; bad values surface as
    ``IsoKronMCPInvocationError`` from the MCP boundary.

    Raises:
        IsoKronMCPInvocationError — substrate-side error (CHECK violation,
            actor_kind resolution failure, chain lock failure, etc.).
        IsoKronMCPNotStartedError — ``mcp_client`` is not started.
        ValueError — ``mcp_client`` is ``None`` (defensive: should have
            been resolved before calling).
    """
    if mcp_client is None:
        raise ValueError(
            "emit_kora_event: mcp_client is required (resolve via "
            "IsoKronConnection.get_mcp_client() before calling)"
        )
    result = await mcp_client.invoke(
        "kora__append_event",
        {
            "workspace_id": workspace_id,
            "event_type": event_type,
            "payload": payload,
        },
    )
    # K-9 contract: tool returns {'event_id': '<uuid>'}.
    event_id = result.get("event_id") if isinstance(result, dict) else None
    if not isinstance(event_id, str):
        # Defensive: surface a clear error if the substrate response
        # shape drifts (the parity is informal — Zod-strict on the
        # input side, but the output is just a dict).
        raise RuntimeError(
            f"kora__append_event returned unexpected shape: {result!r}; "
            f"expected {{'event_id': '<uuid>'}}"
        )
    return event_id
