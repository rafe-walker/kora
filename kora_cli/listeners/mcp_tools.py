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
import os
from datetime import datetime, timezone
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


# ===========================================================================
# KR-MCP-RUNTIME-SURFACE ST2 — MUTATING TOOLS
# ===========================================================================
#
# Three tools shipped:
#
#   - kora__request_state_transition
#   - kora__create_sea_ticket
#   - kora__send_webhook_test_event (dev-only — refuses on prd)
#
# All ST2 dispatchers receive the resolved Caller as a 2nd arg for
# audit logging. mcp.py's cap-gate runs BEFORE dispatch — by the
# time a dispatcher is called, the caller has been verified to
# include this tool in their allowed_caps.
#
# # Ledger writes — DEFERRED to substrate-side schema follow-on
#
# The bucket spec called for `kora_operation_ledger` writes per tool
# call. The ledger schema (substrate migration 0093) requires
# work_attempt_id + workspace_id + ticket_id + tool_name — all tied
# to Sea_Ticket dispatch. MCP-driven calls have NONE of those.
#
# Same precedent as KR-D-DAEMON ST3's webhook dead-letter (which
# faced the same schema-vs-spec mismatch): we use STRUCTURED LOGGING
# with a stable [kora.mcp.tool_called] prefix for the audit surface.
# When substrate ships either (a) a permissive ledger shape OR
# (b) a kora.mcp.tool_called chain-event vocab literal, the runtime
# extension is a small change here — the log-line emit is the
# stable seam.
# ===========================================================================


from kora_cli.listeners.mcp_caller_auth import Caller  # noqa: E402


class _ST2_DevOnlyError(RuntimeError):
    """Raised when a dev-only tool is called in a prd environment."""


class _ST2_ToolInputError(ValueError):
    """Raised when a tool's args fail validation (target_state typo,
    missing required field, etc.). Mapped to JSON-RPC -32602."""


def _emit_audit(*, tool: str, caller: Caller, args: Dict[str, Any], result: str) -> None:
    """Stable audit per MCP mutating-tool call — KR-AUDIT-JSONL-SINK.

    **Dual-write**: existing ``[kora.mcp.tool_called]``
    structured-log line preserved VERBATIM (operator grep workflows
    keep working) + :func:`emit_audit` writes a JSONL row to
    ``kora_audit_log.jsonl`` (panel consumption).

    Body content NEVER in the audit — only ``args_keys`` (sorted
    list of key names; values dropped). The 5 mutating-tool call
    sites that invoke this helper pre-filter their ``args`` dicts
    to safe shapes (e.g. ``text_len`` instead of ``text``,
    ``subject_len`` instead of ``subject``).

    Read tools (KR-MCP-RUNTIME-SURFACE ST1) don't currently emit
    audit. When they do (follow-on bucket), they'll use
    ``tool_kind="read"`` on the same ``mcp.tool_called`` seam.
    """
    logger.info(
        "[kora.mcp.tool_called] tool=%s caller_actor_kind=%s args_keys=%s result=%s",
        tool,
        caller.actor_kind,
        sorted(args.keys()),
        result,
    )

    # KR-AUDIT-JSONL-SINK — JSONL bridge to panels.
    from kora_cli.audit import emit_audit

    emit_audit(
        seam="mcp.tool_called",
        details={
            "tool_name": tool,
            "tool_kind": "mutating",
            "caller_actor_kind": caller.actor_kind,
            "args_keys": sorted(args.keys()),
            "result": result,
        },
        source="mcp_http",
    )


# ---------------------------------------------------------------------------
# Tool 7: kora__request_state_transition
# ---------------------------------------------------------------------------


REQUEST_STATE_TRANSITION_TOOL: Dict[str, Any] = {
    "name": "kora__request_state_transition",
    "description": (
        "Request a Kora OperationalStateHolder transition. Validates "
        "against the R4.1 §9.1 TRANSITION_TABLE; emits the standard "
        "operational-state-transitioned listener chain. Caller must "
        "have kora__request_state_transition in allowed_caps. Args: "
        "target_state (booting/ready/active/paused/stopped, "
        "case-insensitive) + reason (free text used as the trigger "
        "string in audit + chain events)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "target_state": {
                "type": "string",
                "enum": ["booting", "ready", "active", "paused", "stopped"],
            },
            "reason": {"type": "string", "minLength": 1},
        },
        "required": ["target_state", "reason"],
        "additionalProperties": False,
    },
    "requires_cap_gate": True,
    "dev_only": False,
}


class StateTransitionResult(BaseModel):
    success: bool
    from_state: str
    to_state: str
    trigger: str
    caller_actor_kind: str


async def _execute_request_state_transition(
    *, target_state: str, reason: str, caller: Caller
) -> StateTransitionResult:
    from agent.operational_state import PrimaryState
    from agent.operational_state_holder import get_holder

    # Normalize + validate target_state.
    if not isinstance(target_state, str) or not target_state.strip():
        raise _ST2_ToolInputError("target_state is required")
    if not isinstance(reason, str) or not reason.strip():
        raise _ST2_ToolInputError("reason is required (non-empty)")

    normalized = target_state.strip().lower()
    try:
        target_enum = PrimaryState(normalized)
    except ValueError:
        raise _ST2_ToolInputError(
            f"unknown target_state {target_state!r}; "
            f"must be one of: booting/ready/active/paused/stopped"
        )

    holder = get_holder()
    if holder is None:
        raise _ST2_ToolInputError(
            "OperationalStateHolder is not initialized — daemon not "
            "running with substrate-attached listeners?"
        )

    # holder.current is a @property — not a method (caught in ST1 K-DG).
    from_state = holder.current.primary_state

    # transition_to validates against TRANSITION_TABLE; raises
    # InvalidStateTransitionError if not allowed. We let that bubble
    # up as a generic -32603 with the message — the caller can read
    # the message + try a different target.
    await holder.transition_to(target_enum, trigger=reason)

    _emit_audit(
        tool="kora__request_state_transition",
        caller=caller,
        args={"target_state": target_state, "reason": reason},
        result=f"{from_state.value}->{target_enum.value}",
    )

    return StateTransitionResult(
        success=True,
        from_state=from_state.value,
        to_state=target_enum.value,
        trigger=reason,
        caller_actor_kind=caller.actor_kind,
    )


async def _dispatch_request_state_transition(
    params: Dict[str, Any], caller: Caller
) -> BaseModel:
    return await _execute_request_state_transition(
        target_state=params.get("target_state", ""),
        reason=params.get("reason", ""),
        caller=caller,
    )


# ---------------------------------------------------------------------------
# Tool 8: kora__create_sea_ticket
# ---------------------------------------------------------------------------


CREATE_SEA_TICKET_TOOL: Dict[str, Any] = {
    "name": "kora__create_sea_ticket",
    "description": (
        "Create a Sea_Ticket on Kora's behalf via the substrate-side "
        "`sea__create_ticket` MCP tool. Bridges an authorized MCP "
        "caller (e.g. another PM) to the substrate. Substrate-side "
        "Zod validation applies to the args. Returns the new "
        "ticket_id on success. Caller must have "
        "kora__create_sea_ticket in allowed_caps."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 1},
            "body": {"type": "string"},
            "priority": {
                "type": "string",
                "enum": ["low", "normal", "high", "frontier"],
            },
        },
        "required": ["title"],
        "additionalProperties": True,
    },
    "requires_cap_gate": True,
    "dev_only": False,
}


class CreateSeaTicketResult(BaseModel):
    success: bool
    ticket_id: Optional[str] = None
    raw_response: Optional[Dict[str, Any]] = None
    caller_actor_kind: str


async def _execute_create_sea_ticket(
    *,
    args: Dict[str, Any],
    caller: Caller,
) -> CreateSeaTicketResult:
    title = args.get("title")
    if not isinstance(title, str) or not title.strip():
        raise _ST2_ToolInputError("title is required (non-empty string)")

    provider = _get_active_provider()
    if provider is None or getattr(provider, "_connection", None) is None:
        raise _ST2_ToolInputError(
            "no active IsoKron provider — daemon not running with "
            "substrate-attached listeners?"
        )
    mcp_client = provider._connection.get_mcp_client()

    # Pass args through verbatim — substrate-side Zod validates the
    # full schema. We add Kora-specific tagging (kind="sea") if the
    # caller didn't.
    forwarded = dict(args)
    forwarded.setdefault("kind", "sea")
    # Tag the originating caller_actor_kind in the request payload
    # so substrate audit logs can attribute the create. The substrate
    # may ignore this field if its schema is strict; passing it is
    # cheap.
    forwarded.setdefault("origin_actor_kind", caller.actor_kind)

    result = await mcp_client.invoke("sea__create_ticket", forwarded)

    ticket_id = None
    if isinstance(result, dict):
        ticket_id = result.get("ticket_id") or result.get("id")

    _emit_audit(
        tool="kora__create_sea_ticket",
        caller=caller,
        args=forwarded,
        result=f"ticket_id={ticket_id}",
    )

    return CreateSeaTicketResult(
        success=True,
        ticket_id=str(ticket_id) if ticket_id else None,
        raw_response=result if isinstance(result, dict) else None,
        caller_actor_kind=caller.actor_kind,
    )


async def _dispatch_create_sea_ticket(
    params: Dict[str, Any], caller: Caller
) -> BaseModel:
    return await _execute_create_sea_ticket(args=params, caller=caller)


# ---------------------------------------------------------------------------
# Tool 9: kora__send_webhook_test_event (DEV-ONLY)
# ---------------------------------------------------------------------------


SEND_WEBHOOK_TEST_EVENT_TOOL: Dict[str, Any] = {
    "name": "kora__send_webhook_test_event",
    "description": (
        "Operator-debug tool: emit the verified-event chain that the "
        "webhook listener would emit on real receipt, useful for "
        "exercising downstream handler wiring without setting up "
        "Slack / Purelymail end-to-end. **DEV-ONLY** — refuses on "
        "prd (KORA_DEPLOY_ENV=prd) to prevent synthetic-event "
        "pollution of production audit trails. Args: endpoint "
        "('slack' | 'email'), payload (free-form dict)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "endpoint": {"type": "string", "enum": ["slack", "email"]},
            "payload": {"type": "object"},
        },
        "required": ["endpoint", "payload"],
        "additionalProperties": False,
    },
    "requires_cap_gate": True,
    "dev_only": True,
}


class WebhookTestEventResult(BaseModel):
    success: bool
    endpoint: str
    payload_keys: List[str]
    caller_actor_kind: str
    deploy_env: str


async def _execute_send_webhook_test_event(
    *,
    endpoint: str,
    payload: Dict[str, Any],
    caller: Caller,
) -> WebhookTestEventResult:
    deploy_env = os.environ.get("KORA_DEPLOY_ENV", "").strip().lower()
    # Prod refusal — fail-CLOSED. The env-value check is the dev-only
    # boundary; the dev_only descriptor flag is just an API hint.
    if deploy_env == "prd":
        raise _ST2_DevOnlyError(
            "kora__send_webhook_test_event refuses on KORA_DEPLOY_ENV=prd "
            "to prevent synthetic-event pollution. Use a staging / dev "
            "environment for handler-wiring tests."
        )

    if endpoint not in ("slack", "email"):
        raise _ST2_ToolInputError(
            f"endpoint must be 'slack' or 'email'; got {endpoint!r}"
        )
    if not isinstance(payload, dict):
        raise _ST2_ToolInputError("payload must be an object")

    # Emit the synthetic-event log line that mirrors the
    # webhook-handler chain. Real chain-event emission (via
    # kora__append_event) needs a vocab literal for synthetic events;
    # for ST2 the log line is the scaffold — Feature 3/5 buckets will
    # extend when the real handlers are wired.
    logger.info(
        "[kora.mcp.synthetic_webhook] endpoint=%s caller=%s payload_keys=%s",
        endpoint,
        caller.actor_kind,
        sorted(payload.keys()),
    )

    _emit_audit(
        tool="kora__send_webhook_test_event",
        caller=caller,
        args={"endpoint": endpoint, "payload": payload},
        result="synthetic_event_emitted",
    )

    return WebhookTestEventResult(
        success=True,
        endpoint=endpoint,
        payload_keys=sorted(payload.keys()),
        caller_actor_kind=caller.actor_kind,
        deploy_env=deploy_env or "unknown",
    )


async def _dispatch_send_webhook_test_event(
    params: Dict[str, Any], caller: Caller
) -> BaseModel:
    return await _execute_send_webhook_test_event(
        endpoint=params.get("endpoint", ""),
        payload=params.get("payload") or {},
        caller=caller,
    )


# ---------------------------------------------------------------------------
# Tool 9: kora__send_slack_dm (KR-MCP-SEND-TOOLS)
# ---------------------------------------------------------------------------


SEND_SLACK_DM_TOOL: Dict[str, Any] = {
    "name": "kora__send_slack_dm",
    "description": (
        "Send a Slack DM via Kora's SlackClient. Restricted to DM "
        "channels (channel_id must start with 'D' OR match "
        "KORA_SLACK_JOSHUA_USER_ID to prevent accidental channel "
        "broadcast). Bot identity is Kora; from-identity is NOT "
        "caller-controllable. Caller must have kora__send_slack_dm "
        "in allowed_caps. Args: channel_id, text (≤4000 chars), "
        "thread_ts (optional). Returns slack_message_ts on success."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "minLength": 1},
            "text": {"type": "string", "minLength": 1, "maxLength": 4000},
            "thread_ts": {"type": ["string", "null"]},
        },
        "required": ["channel_id", "text"],
        "additionalProperties": False,
    },
    "requires_cap_gate": True,
    "dev_only": False,
}


SLACK_DM_TEXT_MAX_LEN = 4000
_JOSHUA_USER_ID_ENV = "KORA_SLACK_JOSHUA_USER_ID"


class SendSlackDmResult(BaseModel):
    success: bool
    slack_message_ts: Optional[str] = None
    sent_at: str
    caller_actor_kind: str


async def _execute_send_slack_dm(
    *,
    channel_id: str,
    text: str,
    thread_ts: Optional[str],
    caller: Caller,
) -> SendSlackDmResult:
    # Input validation — at the MCP layer so the error envelope is
    # JSON-RPC-shaped (-32602 invalid_params) rather than client
    # exception text.
    if not isinstance(channel_id, str) or not channel_id.strip():
        raise _ST2_ToolInputError("channel_id is required (non-empty)")
    if not isinstance(text, str) or not text.strip():
        raise _ST2_ToolInputError("text is required (non-empty)")
    if len(text) > SLACK_DM_TEXT_MAX_LEN:
        raise _ST2_ToolInputError(
            f"text exceeds Slack's {SLACK_DM_TEXT_MAX_LEN}-char limit "
            f"({len(text)} > {SLACK_DM_TEXT_MAX_LEN})"
        )

    # channel_id validation — DM channels only (D-prefix) OR
    # Joshua's user ID (which Slack auto-resolves to DM channel
    # on bot post). Reject U... user-IDs at the MCP layer (would
    # require an extra Slack API call to resolve; operator should
    # pre-resolve). Defense against accidental channel broadcast.
    joshua_user_id = os.environ.get(_JOSHUA_USER_ID_ENV, "").strip()
    if not (
        channel_id.startswith("D")
        or (joshua_user_id and channel_id == joshua_user_id)
    ):
        raise _ST2_ToolInputError(
            f"channel_id {channel_id!r} must start with 'D' (DM "
            f"channel) or match KORA_SLACK_JOSHUA_USER_ID; "
            f"non-DM channel sends are out of scope for this tool"
        )

    # Resolve the daemon-coordinator-managed SlackClient.
    from kora_cli.listeners.slack_client_listener import (
        current_slack_client,
    )

    client = current_slack_client()
    if client is None:
        # Surface as -32001 (capability_denied) with a distinct
        # error_code so callers can branch on availability vs. ACL.
        raise _ST2_ToolInputError(
            "slack_client_unavailable: SlackClient not registered "
            "(KORA_SLACK_BOT_TOKEN unset or daemon not running with "
            "slack_client listener)"
        )

    try:
        response = await client.post_dm(
            channel_id=channel_id, text=text, thread_ts=thread_ts
        )
    except Exception as exc:
        # Sanitize — never let the bot token leak in error text.
        raise _ST2_ToolInputError(
            f"slack_send_failed: {type(exc).__name__}"
        )

    # Audit log entry via a fresh handler instance (just for the
    # outbound-log helper). The handler doesn't need an event payload
    # — we're using its outbound-log writer to keep entries in one
    # file with consistent shape.
    sent_at_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    slack_ts = response.get("ts") if isinstance(response, dict) else None

    try:
        from kora_cli.handlers.slack_dm_handler import SlackDMHandler

        SlackDMHandler()._append_outbound_log_entry(
            channel_id=channel_id,
            thread_ts=thread_ts,
            text=text,
            slack_message_ts=str(slack_ts) if slack_ts else None,
            send_status="ok",
            caller_actor_kind=caller.actor_kind,
        )
    except Exception as log_exc:  # pragma: no cover — log fail-soft
        logger.warning(
            "[kora.mcp.send_slack_dm] outbound log write failed: %r",
            log_exc,
        )

    _emit_audit(
        tool="kora__send_slack_dm",
        caller=caller,
        args={
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "text_len": len(text),
        },
        result=f"slack_ts={slack_ts}",
    )

    return SendSlackDmResult(
        success=True,
        slack_message_ts=str(slack_ts) if slack_ts else None,
        sent_at=sent_at_iso,
        caller_actor_kind=caller.actor_kind,
    )


async def _dispatch_send_slack_dm(
    params: Dict[str, Any], caller: Caller
) -> BaseModel:
    return await _execute_send_slack_dm(
        channel_id=params.get("channel_id", ""),
        text=params.get("text", ""),
        thread_ts=params.get("thread_ts"),
        caller=caller,
    )


# ---------------------------------------------------------------------------
# Tool 10: kora__send_email (KR-MCP-SEND-TOOLS)
# ---------------------------------------------------------------------------


SEND_EMAIL_TOOL: Dict[str, Any] = {
    "name": "kora__send_email",
    "description": (
        "Send an email via Kora's PurelymailClient (SMTP). "
        "from_addr is derived from KORA_PUREMAIL_SMTP_USERNAME and "
        "is NOT caller-controllable (security: prevents sender "
        "impersonation). Recipient cap (≤10) + from-domain "
        "allowlist + 30s timeout + retry-on-transient enforced by "
        "the underlying client. NO attachments via this tool — "
        "deferred to KR-MCP-SEND-TOOLS-ATTACHMENTS. Caller must "
        "have kora__send_email in allowed_caps."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "to": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
                "maxItems": 10,
            },
            "subject": {"type": "string", "minLength": 1},
            "body_text": {"type": "string", "minLength": 1},
            "body_html": {"type": ["string", "null"]},
            "in_reply_to": {"type": ["string", "null"]},
        },
        "required": ["to", "subject", "body_text"],
        "additionalProperties": False,
    },
    "requires_cap_gate": True,
    "dev_only": False,
}


class SendEmailResult(BaseModel):
    success: bool
    message_id: Optional[str] = None
    smtp_code: Optional[int] = None
    sent_at: str
    caller_actor_kind: str
    error: Optional[str] = None


async def _execute_send_email(
    *,
    to: List[str],
    subject: str,
    body_text: str,
    body_html: Optional[str],
    in_reply_to: Optional[str],
    caller: Caller,
) -> SendEmailResult:
    # MCP-layer validation. The PurelymailClient enforces its own
    # caps (≤10 recipients; per-/total-attachment sizes; domain
    # allowlist) but we surface JSON-RPC-shaped errors at this
    # layer for malformed input + before any SMTP traffic.
    if not isinstance(to, list) or not to:
        raise _ST2_ToolInputError("to must be a non-empty list of strings")
    if not isinstance(subject, str) or not subject.strip():
        raise _ST2_ToolInputError("subject is required (non-empty)")
    if not isinstance(body_text, str) or not body_text.strip():
        raise _ST2_ToolInputError("body_text is required (non-empty)")
    if len(to) > 10:
        raise _ST2_ToolInputError(
            f"to has {len(to)} addresses; max 10 per send "
            "(defense against accidental mass-send)"
        )
    for addr in to:
        if not isinstance(addr, str) or "@" not in addr:
            raise _ST2_ToolInputError(
                f"recipient {addr!r} is malformed (must be a string "
                "containing '@')"
            )

    # Resolve the daemon-coordinator-managed PurelymailClient.
    from kora_cli.listeners.purelymail_client_listener import (
        current_purelymail_client,
    )

    client = current_purelymail_client()
    if client is None:
        raise _ST2_ToolInputError(
            "purelymail_client_unavailable: PurelymailClient not "
            "registered (SMTP auth env unset or daemon not running "
            "with purelymail_client listener)"
        )

    # from_addr is the username env value — never caller-controllable.
    from_addr = os.environ.get("KORA_PUREMAIL_SMTP_USERNAME", "").strip()
    if not from_addr:
        raise _ST2_ToolInputError(
            "purelymail_client_unavailable: "
            "KORA_PUREMAIL_SMTP_USERNAME env is unset"
        )

    try:
        result = await client.send_email(
            from_addr=from_addr,
            to=to,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            in_reply_to=in_reply_to,
            attachments=None,  # NOT supported in this bucket
            caller_actor_kind=caller.actor_kind,
        )
    except Exception as exc:
        # Sanitize — PurelymailClient already strips the password
        # from any error string it raises; we add the type-name
        # prefix without leaking caller-controlled content.
        raise _ST2_ToolInputError(
            f"email_send_failed: {type(exc).__name__}"
        )

    _emit_audit(
        tool="kora__send_email",
        caller=caller,
        args={
            "to": to,
            "subject_len": len(subject),
            "body_text_len": len(body_text),
            "has_html": body_html is not None,
            "in_reply_to": in_reply_to,
        },
        result=(
            f"status={result.status} smtp_code={result.smtp_code} "
            f"message_id={result.message_id}"
        ),
    )

    return SendEmailResult(
        success=result.status == "ok",
        message_id=result.message_id,
        smtp_code=result.smtp_code,
        sent_at=result.sent_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        caller_actor_kind=caller.actor_kind,
        error=result.error,
    )


async def _dispatch_send_email(
    params: Dict[str, Any], caller: Caller
) -> BaseModel:
    return await _execute_send_email(
        to=params.get("to", []),
        subject=params.get("subject", ""),
        body_text=params.get("body_text", ""),
        body_html=params.get("body_html"),
        in_reply_to=params.get("in_reply_to"),
        caller=caller,
    )


# ---------------------------------------------------------------------------
# Public ST2 descriptor + dispatch tables
# ---------------------------------------------------------------------------


# Mark ST1 descriptors as cap-gate-default-False for the registry. This
# is informational; the actual gating logic in mcp.py reads each tool's
# own descriptor `requires_cap_gate` flag (ST1 descriptors don't set it
# → defaults to False via _TOOL_FLAGS resolution).
for _desc in TOOL_DESCRIPTORS:
    _desc.setdefault("requires_cap_gate", False)
    _desc.setdefault("dev_only", False)


# ===========================================================================
# KR-MCP-STOP-CONTROL ST1 — pause/resume wrappers
# ===========================================================================
#
# Two new MCP mutating tools that DELEGATE to the existing
# `_execute_request_state_transition` impl with predetermined
# target states. Distinct caps from `kora__request_state_transition`
# so operator can grant pause/resume without granting full
# transition power (which can move to STOPPED).
#
# No new state-machine code — these are pure convenience wrappers
# matching the bucket spec's "ST1 wraps existing
# kora__request_state_transition (no duplicate state-machine code)".
# ===========================================================================


REQUEST_PAUSE_TOOL: Dict[str, Any] = {
    "name": "kora__request_pause",
    "description": (
        "Pause Kora's intake: ACTIVE → PAUSED via OperationalStateHolder. "
        "Daemon stops processing NEW inbound messages but in-flight work "
        "continues (matches operator-issued kora_control L1 intent). "
        "Reversible via kora__request_resume. Caller must have "
        "kora__request_pause in allowed_caps — separate cap from "
        "kora__request_state_transition so operator can grant pause/resume "
        "without granting full transition power."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "minLength": 1},
        },
        "required": ["reason"],
        "additionalProperties": False,
    },
    "requires_cap_gate": True,
    "dev_only": False,
}


REQUEST_RESUME_TOOL: Dict[str, Any] = {
    "name": "kora__request_resume",
    "description": (
        "Resume Kora's intake: PAUSED → READY via "
        "OperationalStateHolder (the canonical R4.1 §9.1 recovery "
        "edge — daemon transitions to READY where she's eligible to "
        "claim work; the next claim cycle moves READY → ACTIVE "
        "naturally). Pair of kora__request_pause. Caller must have "
        "kora__request_resume in allowed_caps."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "minLength": 1},
        },
        "required": ["reason"],
        "additionalProperties": False,
    },
    "requires_cap_gate": True,
    "dev_only": False,
}


async def _execute_request_pause(
    *, reason: str, caller: Caller
) -> StateTransitionResult:
    """Wrap _execute_request_state_transition with target=paused.

    Reuses the existing impl's validation (TRANSITION_TABLE check,
    holder lookup, audit emit, ledger semantics). Only difference
    is the audit's ``tool`` tag — recorded as ``kora__request_pause``
    so operator log-analysis can distinguish pause/resume calls from
    direct state-transition calls.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise _ST2_ToolInputError("reason is required (non-empty)")

    from agent.operational_state import PrimaryState
    from agent.operational_state_holder import get_holder

    holder = get_holder()
    if holder is None:
        raise _ST2_ToolInputError(
            "OperationalStateHolder is not initialized — daemon not "
            "running with substrate-attached listeners?"
        )

    from_state = holder.current.primary_state
    # Pre-check: only valid from ACTIVE. The TRANSITION_TABLE will
    # also catch this, but a specific -32602 with a clear message
    # is friendlier than a generic InvalidStateTransitionError.
    if from_state is not PrimaryState.ACTIVE:
        raise _ST2_ToolInputError(
            f"kora__request_pause valid only when current state is "
            f"active; current is {from_state.value!r}. Use "
            f"kora__get_operational_state to inspect; "
            f"kora__request_resume from paused."
        )

    await holder.transition_to(PrimaryState.PAUSED, trigger=reason)

    _emit_audit(
        tool="kora__request_pause",
        caller=caller,
        args={"reason": reason},
        result=f"{from_state.value}->paused",
    )

    return StateTransitionResult(
        success=True,
        from_state=from_state.value,
        to_state="paused",
        trigger=reason,
        caller_actor_kind=caller.actor_kind,
    )


async def _execute_request_resume(
    *, reason: str, caller: Caller
) -> StateTransitionResult:
    """Pair of _execute_request_pause — PAUSED → READY.

    Target is READY, NOT ACTIVE: per R4.1 §9.1 TRANSITION_TABLE
    the canonical recovery edge from PAUSED is to READY (operator
    clears via kora_control reset, or in this case via the
    request_resume MCP tool). The next claim cycle moves the holder
    READY → ACTIVE naturally; the resume tool's intent is "she's
    eligible to work again," not "she's holding a claim again."
    """
    if not isinstance(reason, str) or not reason.strip():
        raise _ST2_ToolInputError("reason is required (non-empty)")

    from agent.operational_state import PrimaryState
    from agent.operational_state_holder import get_holder

    holder = get_holder()
    if holder is None:
        raise _ST2_ToolInputError(
            "OperationalStateHolder is not initialized — daemon not "
            "running with substrate-attached listeners?"
        )

    from_state = holder.current.primary_state
    if from_state is not PrimaryState.PAUSED:
        raise _ST2_ToolInputError(
            f"kora__request_resume valid only when current state is "
            f"paused; current is {from_state.value!r}. Use "
            f"kora__request_pause from active."
        )

    await holder.transition_to(PrimaryState.READY, trigger=reason)

    _emit_audit(
        tool="kora__request_resume",
        caller=caller,
        args={"reason": reason},
        result=f"{from_state.value}->ready",
    )

    return StateTransitionResult(
        success=True,
        from_state=from_state.value,
        to_state="ready",
        trigger=reason,
        caller_actor_kind=caller.actor_kind,
    )


async def _dispatch_request_pause(
    params: Dict[str, Any], caller: Caller
) -> BaseModel:
    return await _execute_request_pause(
        reason=params.get("reason", ""), caller=caller
    )


async def _dispatch_request_resume(
    params: Dict[str, Any], caller: Caller
) -> BaseModel:
    return await _execute_request_resume(
        reason=params.get("reason", ""), caller=caller
    )


ST2_TOOL_DESCRIPTORS: List[Dict[str, Any]] = [
    REQUEST_STATE_TRANSITION_TOOL,
    CREATE_SEA_TICKET_TOOL,
    SEND_WEBHOOK_TEST_EVENT_TOOL,
    # KR-MCP-SEND-TOOLS additions
    SEND_SLACK_DM_TOOL,
    SEND_EMAIL_TOOL,
    # KR-MCP-STOP-CONTROL ST1 additions — pause/resume wrappers
    REQUEST_PAUSE_TOOL,
    REQUEST_RESUME_TOOL,
]


# ST2 dispatchers take (params, caller). mcp.py imports + uses this.
ST2ToolDispatcher = Callable[
    [Dict[str, Any], Caller], Awaitable[BaseModel]
]
ST2_TOOL_DISPATCH: Dict[str, ST2ToolDispatcher] = {
    "kora__request_state_transition": _dispatch_request_state_transition,
    "kora__create_sea_ticket": _dispatch_create_sea_ticket,
    "kora__send_webhook_test_event": _dispatch_send_webhook_test_event,
    # KR-MCP-SEND-TOOLS additions
    "kora__send_slack_dm": _dispatch_send_slack_dm,
    "kora__send_email": _dispatch_send_email,
    # KR-MCP-STOP-CONTROL ST1 additions
    "kora__request_pause": _dispatch_request_pause,
    "kora__request_resume": _dispatch_request_resume,
}
