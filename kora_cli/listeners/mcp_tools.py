"""Agent-facing MCP tool implementations (KR-MCP-RUNTIME-SURFACE ST1).

Companion to ``kora_cli/listeners/mcp.py`` (the JSON-RPC router).
``mcp.py`` stays lean — auth + route wiring + dispatch envelope. This
module owns the per-tool implementations + Pydantic response models.

# Tools shipped in ST1 (all READ-ONLY — no state mutations, no
# substrate writes, no ledger entries)

  - ``kora__get_operational_state``  — current PrimaryState + recent
    transitions (last N) + state_entered_at via in-process
    ``OperationalStateHolder`` (KR-P2-I-skeleton).
  - ``kora__get_health_rollup``      — current HealthRollup + per-
    subsignal status via in-process ``HealthRollupHolder`` (KR-P2-L).
  - ``kora__get_recent_ledger_entries`` — last N (default 50, max 200)
    ``kora_operation_ledger`` rows; filterable by status.
  - ``kora__get_recent_chain_events`` — last N (default 50, max 200)
    ``hivex_foundation.event_log`` rows where event_type starts with
    ``kora.``; via the existing ``read_recent_kora_events`` helper.
  - ``kora__list_active_sea_tickets`` — Kora's currently-claimed
    Sea_Tickets via the existing ``read_assigned_sea_tickets`` helper
    (filtered to in-flight states).

``kora__daemon_status`` (from ST2 of KR-D-DAEMON, PR #101) is NOT
re-defined here — it stays in ``mcp.py`` as the proof-of-pipe tool.
ST1 extends the surface; doesn't refactor what's working.

# Pydantic response models — why

Each tool returns a typed Pydantic model. The MCP JSON-RPC envelope
wraps ``model_dump()`` JSON in the ``content[].text`` field. Pydantic
validates at the boundary so a regression in (e.g.) the
``OperationalStateHolder.history()`` return shape surfaces here as a
ValidationError rather than as a malformed MCP response.

# Substrate read access

Tools that touch substrate (ledger + chain events + sea tickets) get
the active ``IsoKronMemoryProvider`` via
``plugins.memory.isokron.get_last_active_provider()``. The provider's
``_connection.get_pg_pool()`` gives the asyncpg pool. If no provider
is active (e.g. running under ``kora daemon --listener mcp`` without
the substrate-attached listeners), the tool returns an empty result
with a ``provider_unavailable: true`` field — fail-soft, since
read-only tools shouldn't crash the JSON-RPC response.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class TransitionRecord(BaseModel):
    """One row of OperationalStateHolder.history().

    Field names mirror the dict keys returned by
    ``OperationalStateHolder.history()`` (see
    ``agent/operational_state_holder.py:TransitionRecord``).
    """

    timestamp: str  # ISO 8601 UTC
    from_state: str
    to_state: str
    trigger: str


class OperationalStateResult(BaseModel):
    primary_state: str = Field(
        ..., description="Current PrimaryState enum value."
    )
    degradation_reasons: List[str] = Field(default_factory=list)
    claim_permission: str
    state_entered_at: Optional[str] = None
    recent_transitions: List[TransitionRecord] = Field(default_factory=list)
    holder_available: bool = True


class HealthSubsignal(BaseModel):
    name: str
    status: str  # ok / degraded / unknown / etc.
    last_update_at: Optional[str] = None
    detail: Optional[str] = None


class HealthRollupResult(BaseModel):
    overall_status: str
    control_plane_status: str
    worker_status: str
    subsignals: List[HealthSubsignal] = Field(default_factory=list)
    holder_available: bool = True


class LedgerEntry(BaseModel):
    kora_operation_id: str
    work_attempt_id: str
    sequence_within_attempt: int
    ticket_id: str
    tool_name: str
    status: str  # allocated / dispatched / committed / abandoned
    created_at: str
    updated_at: str


class LedgerEntriesResult(BaseModel):
    entries: List[LedgerEntry] = Field(default_factory=list)
    limit_applied: int
    status_filter: Optional[str] = None
    provider_unavailable: bool = False


class ChainEventEntry(BaseModel):
    event_id: str
    event_type: str
    emitted_at: str
    actor_kind: Optional[str] = None
    payload_summary: Optional[str] = None


class ChainEventsResult(BaseModel):
    events: List[ChainEventEntry] = Field(default_factory=list)
    limit_applied: int
    event_kind_filter: Optional[str] = None
    provider_unavailable: bool = False


class SeaTicketEntry(BaseModel):
    ticket_id: str
    title: str
    sea_status: str
    sea_priority: Optional[str] = None
    active_claim_token: Optional[str] = None
    claim_count: int
    work_attempt_count: int
    next_eligible_at: Optional[str] = None
    created_at: str


class ActiveSeaTicketsResult(BaseModel):
    tickets: List[SeaTicketEntry] = Field(default_factory=list)
    limit_applied: int
    provider_unavailable: bool = False


# ---------------------------------------------------------------------------
# Tool descriptors — extend mcp.py's TOOLS list at module import
# ---------------------------------------------------------------------------


GET_OPERATIONAL_STATE_TOOL: Dict[str, Any] = {
    "name": "kora__get_operational_state",
    "description": (
        "Return Kora's current PrimaryState + degradation_reasons + "
        "claim_permission + the in-memory transition history (last 20 "
        "transitions; ring-buffered, durable history lives in the "
        "chain-event log)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

GET_HEALTH_ROLLUP_TOOL: Dict[str, Any] = {
    "name": "kora__get_health_rollup",
    "description": (
        "Return the current health rollup: overall + control_plane + "
        "worker status, plus the 8 per-subsignal statuses with last-"
        "update timestamps."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

GET_RECENT_LEDGER_ENTRIES_TOOL: Dict[str, Any] = {
    "name": "kora__get_recent_ledger_entries",
    "description": (
        "Return the last N kora_operation_ledger rows (default 50, "
        "max 200). Filterable by status (allocated/dispatched/"
        "committed/abandoned)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            "status": {
                "type": "string",
                "enum": ["allocated", "dispatched", "committed", "abandoned"],
            },
        },
        "additionalProperties": False,
    },
}

GET_RECENT_CHAIN_EVENTS_TOOL: Dict[str, Any] = {
    "name": "kora__get_recent_chain_events",
    "description": (
        "Return the last N kora.* chain events (default 50, max 200) "
        "from hivex_foundation.event_log. Filterable by event_kind "
        "prefix (e.g. 'kora.sea_ticket.' returns only sea_ticket "
        "events)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            "event_kind": {"type": "string"},
        },
        "additionalProperties": False,
    },
}

LIST_ACTIVE_SEA_TICKETS_TOOL: Dict[str, Any] = {
    "name": "kora__list_active_sea_tickets",
    "description": (
        "Return Kora's currently active Sea_Tickets — tickets assigned "
        "to her actor with sea_status in the in-flight set (claimed / "
        "in_progress / failed_retryable). Default limit 50, max 200."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        "additionalProperties": False,
    },
}


# Combined descriptor list — mcp.py extends its TOOLS by this.
TOOL_DESCRIPTORS: List[Dict[str, Any]] = [
    GET_OPERATIONAL_STATE_TOOL,
    GET_HEALTH_ROLLUP_TOOL,
    GET_RECENT_LEDGER_ENTRIES_TOOL,
    GET_RECENT_CHAIN_EVENTS_TOOL,
    LIST_ACTIVE_SEA_TICKETS_TOOL,
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


def _clamp_limit(raw: Any) -> int:
    """Validate + clamp the ``limit`` arg from the JSON-RPC params dict.

    Pydantic on the request would be cleaner but the JSON-RPC envelope
    is hand-rolled — apply the same shape here.
    """
    if raw is None:
        return _DEFAULT_LIMIT
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    if n < 1:
        return 1
    if n > _MAX_LIMIT:
        return _MAX_LIMIT
    return n


def _get_active_provider() -> Optional[Any]:
    """Lazy import + return active IsoKronMemoryProvider, or None.

    Lazy because some daemon configurations (e.g. ``kora daemon
    --listener mcp --no-web``) skip the substrate-attached listeners
    entirely; importing at module-top in those cases triggers
    side-effects we don't want.
    """
    try:
        from plugins.memory.isokron import get_last_active_provider

        return get_last_active_provider()
    except Exception:  # pragma: no cover — defensive only
        logger.exception("[mcp_tools] failed to resolve active provider")
        return None


async def _execute_get_operational_state() -> OperationalStateResult:
    from agent.operational_state_holder import get_holder

    holder = get_holder()
    if holder is None:
        return OperationalStateResult(
            primary_state="unknown",
            claim_permission="unknown",
            holder_available=False,
        )

    # holder.current is a @property — not a method call.
    state = holder.current
    history_rows = holder.history(limit=20)
    transitions: List[TransitionRecord] = []
    for row in history_rows:
        transitions.append(
            TransitionRecord(
                timestamp=str(row.get("timestamp", "")),
                from_state=str(row.get("from_state", "")),
                to_state=str(row.get("to_state", "")),
                trigger=str(row.get("trigger", "")),
            )
        )

    # state_entered_at is the timestamp of the most-recent transition
    # whose to_state matches the current primary_state. If no history
    # is present (boot), leave None — the caller can interpret as
    # "since process start."
    current_state_value = state.primary_state.value
    state_entered_at: Optional[str] = None
    for row in reversed(history_rows):
        if str(row.get("to_state")) == current_state_value:
            state_entered_at = str(row.get("timestamp", "")) or None
            break

    return OperationalStateResult(
        primary_state=current_state_value,
        degradation_reasons=[r.value for r in state.degradation_reasons],
        claim_permission=state.claim_permission.value,
        state_entered_at=state_entered_at,
        recent_transitions=transitions,
        holder_available=True,
    )


async def _execute_get_health_rollup() -> HealthRollupResult:
    from agent.health_rollup_holder import get_health_rollup_holder

    holder = get_health_rollup_holder()
    if holder is None:
        return HealthRollupResult(
            overall_status="unknown",
            control_plane_status="unknown",
            worker_status="unknown",
            holder_available=False,
        )

    rollup = holder.current()
    subsignals: List[HealthSubsignal] = []
    for subsignal_name, subsignal in (rollup.subsignals or {}).items():
        status_val = getattr(subsignal, "status", None)
        status_str = (
            status_val.value if hasattr(status_val, "value") else str(status_val)
        )
        last_seen = getattr(subsignal, "last_seen", None)
        # ``extra`` carries the subsignal-specific bag the HEALTH-PANEL
        # JSON contract pins. Surface as a stringified summary for the
        # MCP caller's triage — keeps the per-subsignal payload bounded
        # while preserving the operator-actionable bits.
        extra = getattr(subsignal, "extra", None) or {}
        detail = ", ".join(f"{k}={v}" for k, v in sorted(extra.items())) or None
        subsignals.append(
            HealthSubsignal(
                name=subsignal_name,
                status=status_str,
                last_update_at=(
                    last_seen.isoformat()
                    if isinstance(last_seen, datetime)
                    else None
                ),
                detail=detail,
            )
        )

    def _status_str(attr: str) -> str:
        v = getattr(rollup, attr, None)
        if v is None:
            return "unknown"
        return v.value if hasattr(v, "value") else str(v)

    # HealthRollup attribute names are bare (overall / control_plane /
    # worker) — not "*_status" — per the dataclass at
    # ``agent/health_rollup_holder.py:174-176``.
    return HealthRollupResult(
        overall_status=_status_str("overall"),
        control_plane_status=_status_str("control_plane"),
        worker_status=_status_str("worker"),
        subsignals=subsignals,
        holder_available=True,
    )


# SQL — ledger entries projection. Mirror the same column set as
# ``KoraOperationRow`` but ordered most-recent-first.
_LEDGER_RECENT_SQL_BASE = """
SELECT
    kora_operation_id::text         AS kora_operation_id,
    work_attempt_id::text           AS work_attempt_id,
    sequence_within_attempt         AS sequence_within_attempt,
    ticket_id::text                 AS ticket_id,
    tool_name                       AS tool_name,
    status::text                    AS status,
    created_at::text                AS created_at,
    updated_at::text                AS updated_at
  FROM public.kora_operation_ledger
"""


async def _execute_get_recent_ledger_entries(
    *, limit: int, status: Optional[str]
) -> LedgerEntriesResult:
    provider = _get_active_provider()
    if provider is None or getattr(provider, "_connection", None) is None:
        return LedgerEntriesResult(
            limit_applied=limit,
            status_filter=status,
            provider_unavailable=True,
        )
    pool = provider._connection.get_pg_pool()

    sql = _LEDGER_RECENT_SQL_BASE
    params: List[Any] = []
    if status:
        sql += " WHERE status = $1::public.kora_operation_status"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT $%d" % (len(params) + 1)
    params.append(limit)

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    entries = [
        LedgerEntry(
            kora_operation_id=row["kora_operation_id"],
            work_attempt_id=row["work_attempt_id"],
            sequence_within_attempt=row["sequence_within_attempt"],
            ticket_id=row["ticket_id"],
            tool_name=row["tool_name"],
            status=row["status"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
        for row in rows
    ]
    return LedgerEntriesResult(
        entries=entries,
        limit_applied=limit,
        status_filter=status,
    )


async def _execute_get_recent_chain_events(
    *, limit: int, event_kind: Optional[str]
) -> ChainEventsResult:
    from plugins.memory.isokron.events import read_recent_kora_events

    provider = _get_active_provider()
    if provider is None or getattr(provider, "_connection", None) is None:
        return ChainEventsResult(
            limit_applied=limit,
            event_kind_filter=event_kind,
            provider_unavailable=True,
        )
    try:
        workspace_id = provider._resolve_workspace_id()
    except Exception:
        workspace_id = None
    if not workspace_id:
        return ChainEventsResult(
            limit_applied=limit,
            event_kind_filter=event_kind,
            provider_unavailable=True,
        )

    pool = provider._connection.get_pg_pool()
    # The substrate reader doesn't take an event_kind filter; pull
    # `limit` rows + filter in-process. If filtering by an uncommon
    # event_kind, the caller can re-issue with a higher limit. Not
    # worth a substrate SQL change for ST1.
    events = await read_recent_kora_events(workspace_id, pool, limit=limit)

    projected: List[ChainEventEntry] = []
    for ev in events:
        event_type_str = getattr(ev, "event_type", "") or ""
        if event_kind and not event_type_str.startswith(event_kind):
            continue
        emitted_at = getattr(ev, "emitted_at", None)
        projected.append(
            ChainEventEntry(
                event_id=str(getattr(ev, "event_id", "")),
                event_type=event_type_str,
                emitted_at=(
                    emitted_at.isoformat()
                    if isinstance(emitted_at, datetime)
                    else str(emitted_at or "")
                ),
                actor_kind=getattr(ev, "actor_kind", None),
                payload_summary=getattr(ev, "payload_summary", None),
            )
        )

    return ChainEventsResult(
        events=projected,
        limit_applied=limit,
        event_kind_filter=event_kind,
    )


# Sea_Ticket in-flight set — claimed (held) + in_progress + failed_retryable
# (waiting for next_eligible_at). Released / completed / failed_terminal are
# terminal and not "active" from Kora's perspective.
_ACTIVE_SEA_STATUSES = ("claimed", "in_progress", "failed_retryable")


async def _execute_list_active_sea_tickets(
    *, limit: int
) -> ActiveSeaTicketsResult:
    from plugins.memory.isokron.assigned_sea_tickets import (
        _resolve_kora_actor_id,
    )

    provider = _get_active_provider()
    if provider is None or getattr(provider, "_connection", None) is None:
        return ActiveSeaTicketsResult(
            limit_applied=limit, provider_unavailable=True
        )

    actor_id = await _resolve_kora_actor_id(provider)
    if not actor_id:
        return ActiveSeaTicketsResult(
            limit_applied=limit, provider_unavailable=True
        )

    pool = provider._connection.get_pg_pool()
    sql = """
    SELECT
        id::text                       AS id,
        COALESCE(title, '<untitled>')  AS title,
        sea_status                     AS sea_status,
        sea_priority                   AS sea_priority,
        sea_active_claim_token::text   AS active_claim_token,
        claim_count                    AS claim_count,
        work_attempt_count             AS work_attempt_count,
        next_eligible_at::text         AS next_eligible_at,
        created_at::text               AS created_at
      FROM public.tickets
     WHERE sea_assigned_to_actor_id = $1::uuid
       AND kind = 'sea'
       AND sea_status = ANY($2::text[])
     ORDER BY created_at DESC
     LIMIT $3
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, actor_id, list(_ACTIVE_SEA_STATUSES), limit)

    tickets = [
        SeaTicketEntry(
            ticket_id=row["id"],
            title=row["title"],
            sea_status=row["sea_status"],
            sea_priority=row.get("sea_priority"),
            active_claim_token=row.get("active_claim_token"),
            claim_count=int(row.get("claim_count") or 0),
            work_attempt_count=int(row.get("work_attempt_count") or 0),
            next_eligible_at=row.get("next_eligible_at"),
            created_at=row["created_at"],
        )
        for row in rows
    ]
    return ActiveSeaTicketsResult(
        tickets=tickets,
        limit_applied=limit,
    )


# ---------------------------------------------------------------------------
# Dispatch table — mcp.py routes tools/call by name into this map
# ---------------------------------------------------------------------------


# Each entry: tool_name → async callable taking the params dict.
# The callable returns a Pydantic model; mcp.py serializes via
# model_dump(mode='json') for the MCP content[].text field.


async def _dispatch_get_operational_state(params: Dict[str, Any]) -> BaseModel:
    return await _execute_get_operational_state()


async def _dispatch_get_health_rollup(params: Dict[str, Any]) -> BaseModel:
    return await _execute_get_health_rollup()


async def _dispatch_get_recent_ledger_entries(
    params: Dict[str, Any],
) -> BaseModel:
    return await _execute_get_recent_ledger_entries(
        limit=_clamp_limit(params.get("limit")),
        status=params.get("status"),
    )


async def _dispatch_get_recent_chain_events(
    params: Dict[str, Any],
) -> BaseModel:
    return await _execute_get_recent_chain_events(
        limit=_clamp_limit(params.get("limit")),
        event_kind=params.get("event_kind"),
    )


async def _dispatch_list_active_sea_tickets(
    params: Dict[str, Any],
) -> BaseModel:
    return await _execute_list_active_sea_tickets(
        limit=_clamp_limit(params.get("limit"))
    )


# Public — mcp.py imports + merges into its dispatch table.
ToolDispatcher = Callable[[Dict[str, Any]], Awaitable[BaseModel]]
TOOL_DISPATCH: Dict[str, ToolDispatcher] = {
    "kora__get_operational_state": _dispatch_get_operational_state,
    "kora__get_health_rollup": _dispatch_get_health_rollup,
    "kora__get_recent_ledger_entries": _dispatch_get_recent_ledger_entries,
    "kora__get_recent_chain_events": _dispatch_get_recent_chain_events,
    "kora__list_active_sea_tickets": _dispatch_list_active_sea_tickets,
}
