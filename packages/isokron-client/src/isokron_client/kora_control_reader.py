"""KoraControlReader — runtime read + lifecycle-advance surface for
``public.kora_control`` (substrate-round Bucket A, R4.1 §9.3).

This module is the **runtime-side** counterpart to the substrate's:

  - ``public.kora_control`` table (``foundation/0089_kora_control_table.sql``)
  - ``public.transition_kora_control`` SECDEF
    (``0090_kora_control_secdefs.sql``)

The reader does NOT enforce STOP-KORA actions. It is a pure
read + lifecycle-advance surface; ST2's :mod:`agent.stop_kora_handler`
maps the level → action, and ST3's ``tool_executor`` wire-in invokes
the reader (+ handler) on every pre-flight check.

# Why direct asyncpg, not MCP

There is no MCP wrapper for ``transition_kora_control`` or any read
helper for ``kora_control`` in ``rafe-walker/isokron@main`` —
``packages/sea-mcp-server/src/tools/`` registers only the four
K-7/8/9/10 tools (append-event, capability-row, relationlink,
scratchpad-write). Per substrate-round Bucket A's design (workspace-
scoped RLS for reads, ``transition_kora_control`` SECDEF granted to
``app_authenticated`` for transitions), the runtime calls the SECDEF
directly via the IsoKron asyncpg pool. This matches the existing
read-side pattern (``constitution.py``, ``reads.py``, ``scratchpad.py``)
and is the canonical Bucket A access path.

This is a deliberate deviation from the original KR-P2-J ST1 bucket
sketch which threaded an ``IsoKronMCPClient`` through the constructor —
the sketch assumed an MCP wrapper that doesn't exist. Caller now
constructs with only ``memory_provider`` + ``kora_actor_id``.

# GUC name (load-bearing detail)

The substrate ``kora_control`` RLS policy keys off
``current_setting('app.workspace_id', true)``. This is a DIFFERENT GUC
name than the kronicle-tier reads (``app.current_workspace_id``).
The reader sets ``app.workspace_id`` inside its transaction before
SELECTing. The substrate has two GUC conventions across migrations;
matching the policy literally is the only safe path. Flagged for
substrate-team cleanup separately.

# "Highest open level wins" ordering rule (R4.1 §9.3)

A later lower-level command does NOT de-escalate an earlier higher-
level command. Only a ``level=0`` reset transitions open commands away
(``superseded``) — and only the operator can issue resets. The reader's
SELECT excludes ``kind = 'reset'`` because level=0 reset rows act on
other rows (their effect is the supersession side-effect at issue
time, written by ``issue_kora_control``) and do not themselves require
runtime enforcement.

Sort: ``level DESC, sequence ASC``. Highest level wins; at the same
level, the older command (lowest sequence) wins. The runtime processes
one command at a time — the reader returns at most one row, and the
caller (handler) decides what action to take.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Row shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KoraControlCommand:
    """One row from ``public.kora_control`` as observed by the runtime.

    Field names + types mirror the substrate schema in
    ``foundation/0089_kora_control_table.sql``.

    Issuer identity is three fields — ``issuer_session_id`` (cockpit
    session), ``issuer_actor_id`` (UUID), ``issuer_actor_kind`` (string
    captured at write time, never re-resolved). The substrate
    ``kora_control_issuer_actor_kind_not_kora`` CHECK constraint
    guarantees ``issuer_actor_kind != 'kora'`` — the load-bearing
    audit invariant.
    """

    command_id: str
    workspace_id: str
    issuer_session_id: str
    issuer_actor_id: str
    issuer_actor_kind: str
    level: int
    kind: str
    reason: Optional[str]
    target_session: Optional[str]
    sequence: int
    lifecycle_state: str
    created_at: datetime
    visible_to_runtime_at: datetime
    expires_at: Optional[datetime]
    observed_at: Optional[datetime]
    acknowledged_at: Optional[datetime]
    enforced_at: Optional[datetime]


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------


# Active-command SELECT. RLS narrows to the workspace via the
# ``app.workspace_id`` GUC the reader sets before this query runs.
# Filters: non-terminal lifecycle states only; ``kind = 'stop'`` (the
# level=0 reset has no runtime action — its effect is the
# supersession side-effect at issue time); not-yet-expired. Sort:
# highest level wins; same-level tie broken by oldest sequence.
SELECT_ACTIVE_KORA_CONTROL_COMMAND_SQL: Final[str] = """
    SELECT
        command_id::text             AS command_id,
        workspace_id                 AS workspace_id,
        issuer_session_id            AS issuer_session_id,
        issuer_actor_id::text        AS issuer_actor_id,
        issuer_actor_kind            AS issuer_actor_kind,
        level                        AS level,
        kind                         AS kind,
        reason                       AS reason,
        target_session               AS target_session,
        sequence                     AS sequence,
        lifecycle_state              AS lifecycle_state,
        created_at                   AS created_at,
        visible_to_runtime_at        AS visible_to_runtime_at,
        expires_at                   AS expires_at,
        observed_at                  AS observed_at,
        acknowledged_at              AS acknowledged_at,
        enforced_at                  AS enforced_at
      FROM public.kora_control
     WHERE lifecycle_state IN
           ('created', 'visible_to_runtime', 'acknowledged', 'enforcing')
       AND kind = 'stop'
       AND (expires_at IS NULL OR expires_at > NOW())
     ORDER BY level DESC, sequence ASC
     LIMIT 1
"""


# Transition call. The substrate SECDEF validates the target_state +
# does the forward-only transition (or returns the current state
# idempotently for re-marks of terminal/current states). Emits
# ``kora_control.enforced`` / ``kora_control.failed`` internally on
# terminal transitions only — intermediate states (visible_to_runtime,
# acknowledged, enforcing) are state-tracking-only and do not emit
# (per ``0090_kora_control_secdefs.sql`` header design rationale).
CALL_TRANSITION_KORA_CONTROL_SQL: Final[str] = """
    SELECT
        out_command_id::text       AS command_id,
        out_lifecycle_state        AS lifecycle_state,
        out_transitioned           AS transitioned,
        out_chain_event_id::text   AS chain_event_id
      FROM public.transition_kora_control($1::text, $2::uuid, $3::uuid, $4::text)
"""


# Workspace-scoped GUC name used by the kora_control RLS policy. See
# module docstring — NOT the same as ``app.current_workspace_id`` used
# by the kronicle-tier reads.
KORA_CONTROL_WORKSPACE_GUC: Final[str] = "app.workspace_id"


# Allowed transition target states the runtime may request. The
# substrate's SECDEF will reject ('superseded', 'expired', 'escalated'
# are not runtime-driven). Listed here for clarity + a defensive
# assertion at the helper's boundary.
_RUNTIME_TARGET_STATES: Final[frozenset[str]] = frozenset({
    "visible_to_runtime",
    "acknowledged",
    "enforcing",
    "enforced",
    "failed",
})


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class KoraControlReader:
    """Reads ``public.kora_control`` for commands the runtime should act on,
    and advances the substrate-side lifecycle via
    ``public.transition_kora_control``.

    Per R4.1 §9.3:
      - ``get_active_command`` is called before every claim AND every
        tool call (the consumer-loop pre-claim path lives in KR-P2-E ST5;
        the per-tool-call path lives in KR-P2-J ST3).
      - Highest open level wins; de-escalation requires explicit
        ``level=0`` reset issued by the operator (Kora cannot reset).
      - Consumption is exactly-once at the substrate side via the
        SECDEF's ``SELECT … FOR UPDATE`` row lock + idempotent
        re-marks of terminal states. The reader does not keep
        in-memory dedup state.
    """

    def __init__(self, memory_provider: Any, kora_actor_id: str):
        """Construct the reader.

        Args:
            memory_provider: ``IsoKronMemoryProvider``. The reader pulls
                the asyncpg pool from ``memory_provider._connection`` on
                each call (lazy — survives provider reconnect cycles).
                Workspace_id resolves via
                ``memory_provider._resolve_workspace_id()``.
            kora_actor_id: Kora's UUID in ``actor_registry`` for the
                workspace this reader serves. Caller (typically the
                tool-executor wire-in in ST3) resolves at instantiation
                time and passes it in. The transition SECDEF requires
                ``p_runtime_actor_id UUID`` and ``actor_registry`` will
                reject mismatches with ``invalid_parameter_value``.
        """
        self._memory_provider = memory_provider
        self._kora_actor_id = kora_actor_id

    # -- Public surface ------------------------------------------------------

    async def get_active_command(
        self, actor_id: Optional[str] = None
    ) -> Optional[KoraControlCommand]:
        """Return the highest-level open STOP-KORA command, or ``None``.

        ``actor_id`` is informational — kora_control is workspace-
        scoped, not actor-scoped, so the SELECT does not filter by it.
        The argument is kept on the surface for symmetry with the
        bucket-spec sketch and to support future actor-scoped extensions.

        Returns ``None`` when:
          - The workspace_id can't be resolved (no IsoKron context),
          - The asyncpg pool isn't available (provider not initialized),
          - No matching row exists.

        Does NOT advance the lifecycle. Caller's responsibility (via
        :meth:`mark_observed` etc.) once the command is acted on.
        """
        del actor_id  # informational; not used in the SELECT
        workspace_id = self._resolve_workspace_id()
        if workspace_id is None:
            return None
        pool = self._get_pool()
        if pool is None:
            return None

        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config($1, $2, true)",
                    KORA_CONTROL_WORKSPACE_GUC,
                    workspace_id,
                )
                row = await conn.fetchrow(SELECT_ACTIVE_KORA_CONTROL_COMMAND_SQL)

        if row is None:
            return None
        return _row_to_command(row)

    async def get_all_observed_commands(
        self,
    ) -> Optional[dict[str, list[dict[str, Any]]]]:
        """Return ALL kora_control rows for the workspace, grouped by
        lifecycle position (active / recently_enforced / history).

        Thin wrapper around
        :func:`plugins.memory.isokron.observed_kora_control.get_observed_state_via_provider`
        — kept on the class for symmetry with KR-P2-CLEANUP ST3 spec
        wording, but the module-level helper is what the admin-panel
        endpoint calls directly so it can construct a reader without
        a real ``kora_actor_id`` (the read is workspace-scoped only,
        and the actor_id is only required for
        ``transition_kora_control`` calls).

        Returns ``None`` on any failure path; caller falls back to the
        stub shape + ``error`` field.
        """
        from .observed_kora_control import (
            get_observed_state_via_provider,
        )

        return await get_observed_state_via_provider(
            provider=self._memory_provider
        )

    async def mark_observed(self, command_id: str) -> None:
        """Advance to ``visible_to_runtime``.

        Sets ``observed_at`` on the row (via the SECDEF's
        ``COALESCE``-monotonic UPDATE). No chain event emitted —
        intermediate states are state-tracking-only per substrate
        design (terminal-only emit; see ``0090_kora_control_secdefs.sql``
        header rationale).
        """
        await self._call_transition(command_id, "visible_to_runtime")

    async def mark_acknowledged(self, command_id: str) -> None:
        """Advance to ``acknowledged``. No chain emit (see :meth:`mark_observed`)."""
        await self._call_transition(command_id, "acknowledged")

    async def mark_enforcing(self, command_id: str) -> None:
        """Advance to ``enforcing``. No chain emit."""
        await self._call_transition(command_id, "enforcing")

    async def mark_enforced(self, command_id: str) -> None:
        """Advance to ``enforced``. Substrate SECDEF emits
        ``kora_control.enforced`` chain event internally."""
        await self._call_transition(command_id, "enforced")

    async def mark_failed(
        self, command_id: str, reason: str
    ) -> None:
        """Advance to ``failed``. Substrate SECDEF emits
        ``kora_control.failed`` chain event internally.

        The ``reason`` argument is **not** propagated through the
        substrate — ``transition_kora_control`` does not accept a
        reason parameter, and the ``kora_control.failed`` chain event
        payload contains only ``command_id``, ``workspace_id``,
        ``runtime_actor_id``, ``failed_at``. The reason is logged at
        WARN locally so operator triage can correlate the local log
        with the substrate-side chain event by command_id + timestamp.
        """
        logger.warning(
            "[kora.control.failed] command_id=%s reason=%s "
            "(reason is local-log only; not propagated through chain)",
            command_id,
            reason,
        )
        await self._call_transition(command_id, "failed")

    # -- Internals -----------------------------------------------------------

    def _resolve_workspace_id(self) -> Optional[str]:
        """Best-effort workspace_id resolution from the memory_provider."""
        try:
            return self._memory_provider._resolve_workspace_id()
        except Exception:  # pragma: no cover — defensive against fakes
            return None

    def _get_pool(self) -> Optional[Any]:
        """Best-effort asyncpg pool extraction from the memory_provider."""
        connection = getattr(self._memory_provider, "_connection", None)
        if connection is None:
            return None
        try:
            return connection.get_pg_pool()
        except Exception:  # pragma: no cover — defensive
            return None

    async def _call_transition(
        self, command_id: str, target_state: str
    ) -> None:
        """Invoke ``public.transition_kora_control`` for ``command_id``.

        The substrate SECDEF is idempotent on the current state and on
        terminal states — re-marking returns silently. Forward-only
        transitions; non-forward requests surface as
        ``invalid_parameter_value`` from the SECDEF and propagate as
        asyncpg ``PostgresError``. The reader does not swallow those —
        caller logs + decides.
        """
        if target_state not in _RUNTIME_TARGET_STATES:
            raise ValueError(
                f"target_state {target_state!r} is not a runtime-driven "
                "lifecycle state; expected one of "
                f"{sorted(_RUNTIME_TARGET_STATES)}"
            )
        workspace_id = self._resolve_workspace_id()
        if workspace_id is None:
            raise RuntimeError(
                "KoraControlReader: workspace_id is unresolved; "
                "cannot call transition_kora_control"
            )
        pool = self._get_pool()
        if pool is None:
            raise RuntimeError(
                "KoraControlReader: asyncpg pool unavailable; "
                "memory_provider._connection is not initialized"
            )
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                CALL_TRANSITION_KORA_CONTROL_SQL,
                workspace_id,
                command_id,
                self._kora_actor_id,
                target_state,
            )
        if row is None:  # pragma: no cover — SECDEF always returns a row
            return
        logger.info(
            "[kora.control.transition] command_id=%s target=%s "
            "result_state=%s transitioned=%s chain_event_id=%s",
            command_id,
            target_state,
            row["lifecycle_state"],
            row["transitioned"],
            row["chain_event_id"],
        )


# ---------------------------------------------------------------------------
# Row → dataclass conversion
# ---------------------------------------------------------------------------


def _row_to_command(row: Any) -> KoraControlCommand:
    """Convert an asyncpg row (or fake dict-like) to a KoraControlCommand.

    Defensive on optional fields — the timestamp columns
    (``observed_at``, ``acknowledged_at``, ``enforced_at``,
    ``expires_at``) are nullable per the substrate schema.
    """
    return KoraControlCommand(
        command_id=row["command_id"],
        workspace_id=row["workspace_id"],
        issuer_session_id=row["issuer_session_id"],
        issuer_actor_id=row["issuer_actor_id"],
        issuer_actor_kind=row["issuer_actor_kind"],
        level=row["level"],
        kind=row["kind"],
        reason=row["reason"],
        target_session=row["target_session"],
        sequence=row["sequence"],
        lifecycle_state=row["lifecycle_state"],
        created_at=row["created_at"],
        visible_to_runtime_at=row["visible_to_runtime_at"],
        expires_at=row["expires_at"],
        observed_at=row["observed_at"],
        acknowledged_at=row["acknowledged_at"],
        enforced_at=row["enforced_at"],
    )
