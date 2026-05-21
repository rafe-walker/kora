"""SeaTicketPoller — Kora's poll / claim / work / release loop (KR-P2-E ST1).

Implements the consumer-side of the R4.1 §9 work cycle against the
substrate contracts shipped in IsoKron PRs #1047 + #1048 and the
top-level/0102 + 0103 amendments:

* ``public.get_next_available_sea_ticket(p_assigned_to_actor_id UUID)``
  — priority-ordered, ``next_eligible_at``-honoring, LIMIT 1.
* ``kora__claim_sea_ticket`` — mints ``work_attempt_id`` atomically
  with the claim (top-level/0102, pin P4); returns
  ``(result, claim_fence_token, lease_expires_at, claim_count,
  chain_event_id, work_attempt_id)``.
* ``kora__refresh_claim`` — heartbeat / lease-extend; wired in ST3.
* ``kora__release_claim`` — releases the lease. Note the SECDEF takes
  no ``resolution`` field; the resolution is conveyed via a separate
  ``kora.sea_ticket.resolved`` chain event (emitted in ST4 alongside
  ``model_tier_used`` per R4.1 §9.4).

ST1 ships the skeleton: poll → STOP-KORA gate via the real
:class:`KoraControlReader` from KR-P2-J ST1 (rafe-walker/kora @ #38)
→ claim → invoke a placeholder agent loop → release. ST2 plugs the
ledger writer in for the dispatch path; ST3 adds the heartbeat task;
ST4 wires the real agent loop + failure classification; ST5 starts
the poller from ``gateway/run.py``.

# Discrepancies between the bucket spec text and the live substrate

The PM-verified ``§1`` block called out the contract shape. Four
example-vs-actual deltas the spec text glossed (none are contract
changes, just example pseudocode that doesn't match the live schemas
— captured in the KR-P2-E ST1 PR body):

* ``get_next_available_sea_ticket`` has no MCP wrapper — read via
  asyncpg through :meth:`IsoKronConnection.get_pg_pool`.
* ``kora__release_claim`` takes no ``resolution`` arg; resolution
  rides a separate chain event.
* ``kora__refresh_claim`` takes no ``progress_signal`` arg; P2
  compliance is enforced at the caller (refresh only on real
  progress; ST3 handles this).
* ``IsoKronMemoryProvider`` doesn't expose ``kora_actor_id``; the
  poller resolves it once via ``actor_registry`` JOIN at first poll
  + caches.

# What ST1 does NOT do

* No ledger writes (ST2).
* No heartbeat task (ST3).
* No real agent loop or failure classification (ST4) — the
  ``agent_loop_invoker`` callable here is treated as opaque.
* No gateway-startup wire-in (ST5).
* No imports from :mod:`agent.operational_state*` — claim acquire /
  release transitions are emitted EXTERNALLY by
  KR-P2-I-integration ST4, not by the poller itself.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from plugins.memory.isokron.claim_heartbeat import (
    HeartbeatHandle,
    start_heartbeat,
)
from plugins.memory.isokron.kora_control_reader import (
    KoraControlCommand,
    KoraControlReader,
)

logger = logging.getLogger(__name__)


# ``kora__claim_sea_ticket`` returns one of these result codes (verbatim
# from packages/sea-mcp-server/src/tools/kora-claim-tools.ts:64-73 on
# isokron @ 9814763). Modeled as plain strings for now — the substrate-
# side is the source of truth, and the runtime only branches on a few of
# these. If substrate-team extends the enum, this set still accepts the
# new strings; the unknown-result fallback is "treat as not-claimed".
KORA_CLAIM_RESULT_CLAIMED = "claimed"
KORA_CLAIM_RESULT_ALREADY_CLAIMED = "already_claimed"
KORA_CLAIM_RESULT_NOT_ASSIGNED = "not_assigned"
KORA_CLAIM_RESULT_ASSIGNMENT_CLEARED = "assignment_cleared"
KORA_CLAIM_RESULT_TICKET_TERMINAL = "ticket_terminal"
KORA_CLAIM_RESULT_BLOCKED_BY_CONTROL = "blocked_by_control"
KORA_CLAIM_RESULT_BLOCKED_BY_COST = "blocked_by_cost"
KORA_CLAIM_RESULT_CONTRACT_VERSION_MISMATCH = "contract_version_mismatch"


class SeaTicketResolution(Enum):
    """R4.1 §9.4 resolution categories the agent-loop invoker returns.

    The ``value`` strings are the ones that ride in the
    ``kora.sea_ticket.resolved`` chain-event payload's ``resolution``
    field. Substrate-team has no enum CHECK on this string today;
    treat as wire-format-stable nonetheless — cockpit consumers
    index on it.
    """

    COMPLETED = "completed"
    FAILED_TERMINAL = "failed_terminal"
    FAILED_RETRYABLE = "failed_retryable"
    RELEASED = "released"


@dataclass(frozen=True, slots=True)
class SeaTicket:
    """Projection of one row from ``get_next_available_sea_ticket``.

    Field order + names match the SQL function's ``RETURNS TABLE``
    column list at packages/db/migrations/0099 (the 0088 base
    function + 0099's next_eligible_at-aware filter). Kept frozen so
    the poller can pass it across async boundaries without worrying
    about mutation.
    """

    ticket_id: str  # UUID
    workspace_id: str  # TEXT (e.g. "org_xyz")
    ticket_title: str
    ticket_objective: str
    sea_status: str
    sea_priority: str
    sea_idea_kind: str
    sea_captured_at: Optional[datetime]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ClaimState:
    """The substrate-side ``kora__claim_sea_ticket`` result, projected
    into a runtime-usable shape.

    Carries the freshly-minted ``work_attempt_id`` (top-level/0102) +
    the ``claim_fence_token`` needed for refresh + release.
    """

    result: str  # one of KORA_CLAIM_RESULT_*
    claim_fence_token: Optional[str]
    lease_expires_at: Optional[str]  # ISO-8601 string; opaque to the poller
    claim_count: Optional[int]
    chain_event_id: Optional[str]
    work_attempt_id: Optional[str]


# Agent-loop invoker signature. The invoker receives
# (ticket, claim_state, heartbeat) so it can correlate emits with the
# active ``work_attempt_id`` (ST2 ledger writes) and signal P2-compliant
# progress (ST3 heartbeat). ST4 wires the real loop + failure
# classification; ST1 ships a placeholder that returns COMPLETED
# unconditionally.
AgentLoopInvoker = Callable[
    [SeaTicket, ClaimState, HeartbeatHandle], Awaitable[SeaTicketResolution]
]


# SQL for the next-available read. ``get_next_available_sea_ticket``
# has no MCP wrapper on isokron main (see module docstring); the runtime
# reads via the asyncpg pool, same pattern as
# ``plugins/memory/isokron/reads.py``.
_NEXT_AVAILABLE_SQL = """
SELECT ticket_id,
       workspace_id,
       ticket_title,
       ticket_objective,
       sea_status,
       sea_priority,
       sea_idea_kind,
       sea_captured_at,
       created_at
  FROM public.get_next_available_sea_ticket($1::uuid)
"""


# SQL to resolve the canonical Kora ``actor_id`` for a workspace.
# Joined on (workspace_id, actor_kind='kora'); each workspace has a
# unique kora actor row. The poller calls this once at first poll +
# caches — actor_id rotation is rare and would require a restart.
_RESOLVE_KORA_ACTOR_SQL = """
SELECT actor_id
  FROM public.actor_registry
 WHERE workspace_id = $1
   AND actor_kind = 'kora'
 LIMIT 1
"""


async def _placeholder_agent_loop(
    _ticket: SeaTicket,
    _claim: ClaimState,
    _heartbeat: HeartbeatHandle,
) -> SeaTicketResolution:
    """ST1 / ST3 stub for ``agent_loop_invoker``.

    Returns ``COMPLETED`` unconditionally — the real loop lands in
    ST4. Production callers (ST5) MUST pass a real invoker; the
    placeholder here exists so the poller can be unit-tested in
    isolation.
    """
    return SeaTicketResolution.COMPLETED


class SeaTicketPoller:
    """Poll IsoKron for assigned Sea_Tickets, claim, work, release.

    Per spec §3 ST1, this is the skeleton. Lifecycle hooks (heartbeat
    in ST3, ledger in ST2, real agent loop in ST4) plug into the
    ``_claim_and_work`` method by replacing the placeholder invoker
    + adding bracketing logic; the public ``run_forever`` API stays
    stable across the bucket.
    """

    def __init__(
        self,
        mcp_client: Any,  # IsoKronMCPClient — typed Any to avoid import cycle
        memory_provider: Any,  # IsoKronMemoryProvider
        agent_loop_invoker: AgentLoopInvoker = _placeholder_agent_loop,
        *,
        kora_control_reader: Optional[KoraControlReader] = None,
        poll_interval_seconds: int = 60,
        claim_ttl_seconds: int = 600,
        heartbeat_interval_seconds: int = 60,
    ) -> None:
        self._mcp_client = mcp_client
        self._memory_provider = memory_provider
        self._agent_loop_invoker = agent_loop_invoker
        # KR-P2-J ST1's real KoraControlReader takes
        # ``(memory_provider, kora_actor_id)`` at construction.
        # ``kora_actor_id`` is resolved lazily per workspace inside
        # ``_claim_and_work`` (the actor_registry JOIN needs a workspace
        # at hand). If the caller injects a reader explicitly (tests,
        # multi-actor wiring), we use it as-is; otherwise we lazy-build
        # one with the resolved actor_id on the first claim attempt.
        self._kora_control_reader: Optional[KoraControlReader] = (
            kora_control_reader
        )
        self._poll_interval_seconds = poll_interval_seconds
        self._claim_ttl_seconds = claim_ttl_seconds
        self._heartbeat_interval_seconds = heartbeat_interval_seconds

        # Per-workspace actor_id cache. Filled lazily on first poll.
        # Restart clears it — actor_id rotation requires a re-boot
        # anyway, so a process-lifetime cache is safe.
        self._actor_id_cache: dict[str, str] = {}

        # The poll loop respects this flag; ST5 calls stop() at
        # gateway shutdown to break out of the sleep.
        self._stop_requested: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        """Poll loop. Runs until :meth:`stop` is called.

        On each tick: try to claim + work one ticket. If a ticket was
        worked, the next tick happens immediately (drains the queue
        when work is plentiful). If no ticket was available, sleeps
        ``poll_interval_seconds`` so we don't hammer the substrate.
        Errors are logged but never break the loop — substrate-side
        transients shouldn't kill the consumer.
        """
        logger.info(
            "[sea_ticket_poller] starting; poll_interval=%ss "
            "claim_ttl=%ss heartbeat=%ss",
            self._poll_interval_seconds,
            self._claim_ttl_seconds,
            self._heartbeat_interval_seconds,
        )
        while not self._stop_requested:
            try:
                worked = await self._poll_once()
            except Exception:
                logger.exception(
                    "[sea_ticket_poller] poll iteration raised; "
                    "swallowing and continuing"
                )
                worked = 0

            if worked == 0:
                await asyncio.sleep(self._poll_interval_seconds)
        logger.info("[sea_ticket_poller] stopped")

    def stop(self) -> None:
        """Signal the poll loop to exit at the next iteration."""
        self._stop_requested = True

    # ------------------------------------------------------------------
    # Poll iteration — public-ish (called by tests too)
    # ------------------------------------------------------------------

    async def _poll_once(self) -> int:
        """Run one poll iteration. Returns the number of tickets worked
        on this tick (0 or 1 — ``get_next_available_sea_ticket`` is
        LIMIT 1).
        """
        ticket = await self._fetch_next_ticket()
        if ticket is None:
            return 0
        try:
            await self._claim_and_work(ticket)
        except Exception:
            logger.exception(
                "[sea_ticket_poller] _claim_and_work raised for "
                "ticket_id=%s — abandoning to lease expiry",
                ticket.ticket_id,
            )
        return 1

    # ------------------------------------------------------------------
    # Internal: ticket fetch via asyncpg
    # ------------------------------------------------------------------

    async def _resolve_kora_actor_id(self, workspace_id: str) -> Optional[str]:
        """Resolve the canonical Kora ``actor_id`` for a workspace.

        Cached process-wide (per workspace). Returns ``None`` if no
        ``actor_kind='kora'`` row exists for the workspace — the poll
        loop treats that as "no work available" and skips the tick.
        """
        cached = self._actor_id_cache.get(workspace_id)
        if cached is not None:
            return cached
        connection = getattr(self._memory_provider, "_connection", None)
        if connection is None:
            logger.warning(
                "[sea_ticket_poller] memory_provider has no _connection; "
                "cannot resolve kora actor_id for workspace=%s",
                workspace_id,
            )
            return None
        pool = connection.get_pg_pool()

        async def _query() -> Optional[str]:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    _RESOLVE_KORA_ACTOR_SQL, workspace_id
                )
            return None if row is None else str(row["actor_id"])

        future = connection._submit_async(_query())
        actor_id = await asyncio.wrap_future(future)
        if actor_id is not None:
            self._actor_id_cache[workspace_id] = actor_id
        return actor_id

    async def _fetch_next_ticket(self) -> Optional[SeaTicket]:
        """Call ``get_next_available_sea_ticket`` for Kora.

        Resolves Kora's actor_id for the configured default workspace
        first (one-time per process); then runs the SQL function.
        Returns ``None`` if no ticket is available or the actor_id
        can't be resolved.
        """
        workspace_id = self._memory_provider._resolve_workspace_id()
        if not workspace_id:
            logger.warning(
                "[sea_ticket_poller] no workspace_id available; "
                "skipping poll tick"
            )
            return None

        actor_id = await self._resolve_kora_actor_id(workspace_id)
        if actor_id is None:
            return None

        connection = self._memory_provider._connection
        pool = connection.get_pg_pool()

        async def _query() -> Optional[dict[str, Any]]:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(_NEXT_AVAILABLE_SQL, actor_id)
            return None if row is None else dict(row)

        future = connection._submit_async(_query())
        row = await asyncio.wrap_future(future)
        if row is None:
            return None
        return SeaTicket(
            ticket_id=str(row["ticket_id"]),
            workspace_id=row["workspace_id"],
            ticket_title=row["ticket_title"],
            ticket_objective=row["ticket_objective"],
            sea_status=row["sea_status"],
            sea_priority=row["sea_priority"],
            sea_idea_kind=row["sea_idea_kind"],
            sea_captured_at=row["sea_captured_at"],
            created_at=row["created_at"],
        )

    # ------------------------------------------------------------------
    # Internal: claim → work → release
    # ------------------------------------------------------------------

    async def _claim_and_work(self, ticket: SeaTicket) -> None:
        """Claim, invoke the agent loop, release.

        ST1: no ledger writes (ST2), no heartbeat (ST3), placeholder
        agent loop (ST4). The full per-tool failure-classification
        table also lands in ST4 — ST1's resolution is whatever the
        invoker returns (the placeholder returns COMPLETED).
        """
        actor_id = await self._resolve_kora_actor_id(ticket.workspace_id)
        if actor_id is None:
            logger.warning(
                "[sea_ticket_poller] cannot resolve actor_id for "
                "ticket_id=%s workspace=%s — skipping",
                ticket.ticket_id,
                ticket.workspace_id,
            )
            return

        # Pre-claim STOP-KORA check against the real KR-P2-J reader.
        # Lazy-construct on first claim — the real reader takes the
        # workspace's Kora actor_id at construction time (per
        # ``transition_kora_control`` SECDEF + actor_registry JOIN).
        # For v1 single-workspace polling, this lazy-build runs once
        # and the reader is reused; multi-workspace polling is Phase 3.
        if self._kora_control_reader is None:
            self._kora_control_reader = KoraControlReader(
                memory_provider=self._memory_provider,
                kora_actor_id=actor_id,
            )
        control_cmd = await self._kora_control_reader.get_active_command(
            actor_id
        )
        if control_cmd is not None and control_cmd.level >= 1:
            logger.info(
                "[sea_ticket_poller] STOP-KORA L%d active "
                "(reason=%r); skipping claim of ticket_id=%s",
                control_cmd.level,
                control_cmd.reason,
                ticket.ticket_id,
            )
            return

        kora_operation_id = str(uuid.uuid4())

        claim_state = await self._claim(
            workspace_id=ticket.workspace_id,
            kora_operation_id=kora_operation_id,
            sea_ticket_id=ticket.ticket_id,
        )
        if claim_state.result != KORA_CLAIM_RESULT_CLAIMED:
            logger.info(
                "[sea_ticket_poller] claim of ticket_id=%s returned "
                "%r — skipping work",
                ticket.ticket_id,
                claim_state.result,
            )
            return

        if (
            claim_state.claim_fence_token is None
            or claim_state.work_attempt_id is None
        ):
            logger.error(
                "[sea_ticket_poller] claim succeeded but substrate "
                "returned NULL claim_fence_token=%r or "
                "work_attempt_id=%r for ticket_id=%s — refusing to "
                "proceed",
                claim_state.claim_fence_token,
                claim_state.work_attempt_id,
                ticket.ticket_id,
            )
            return

        # ST3: spawn the claim-refresh heartbeat. The handle's
        # ``signal_token_progress`` / ``signal_tool_boundary`` methods
        # satisfy P2 compliance — refresh fires only when the agent
        # loop has made real progress.
        heartbeat = start_heartbeat(
            mcp_client=self._mcp_client,
            workspace_id=ticket.workspace_id,
            kora_operation_id=kora_operation_id,
            sea_ticket_id=ticket.ticket_id,
            claim_fence_token=claim_state.claim_fence_token,
            heartbeat_interval_seconds=self._heartbeat_interval_seconds,
            extend_by_seconds=self._claim_ttl_seconds,
        )

        try:
            try:
                resolution = await self._agent_loop_invoker(
                    ticket, claim_state, heartbeat
                )
            except Exception:
                logger.exception(
                    "[sea_ticket_poller] agent_loop_invoker raised "
                    "for ticket_id=%s — releasing with RELEASED "
                    "resolution",
                    ticket.ticket_id,
                )
                resolution = SeaTicketResolution.RELEASED
        finally:
            await heartbeat.cancel()

        # If the heartbeat detected substrate-side lease loss while
        # work was in progress, the lease is already gone — calling
        # release with the (now invalid) fence_token would just raise.
        # Skip the release; substrate's ``sea_ticket.claim_expired``
        # is the durable record.
        if heartbeat.lease_lost:
            logger.warning(
                "[sea_ticket_poller] heartbeat lost lease mid-work "
                "for ticket_id=%s — skipping release (lease already "
                "expired substrate-side)",
                ticket.ticket_id,
            )
            return

        # ST4 will emit ``kora.sea_ticket.resolved`` here with the
        # resolution string + ``model_tier_used`` payload field. ST3
        # still skips the emit; the chain-event log just shows the
        # substrate-side ``sea_ticket.claim_released`` event from
        # release.
        _ = resolution  # claimed for ST4 wire-in
        await self._release(
            workspace_id=ticket.workspace_id,
            kora_operation_id=kora_operation_id,
            sea_ticket_id=ticket.ticket_id,
            claim_fence_token=claim_state.claim_fence_token,
        )

    async def _claim(
        self,
        *,
        workspace_id: str,
        kora_operation_id: str,
        sea_ticket_id: str,
    ) -> ClaimState:
        """Invoke ``kora__claim_sea_ticket``. Always returns a
        :class:`ClaimState`; a substrate exception is wrapped + logged
        and surfaces as ``ClaimState(result="claim_invoke_failed")``
        so the caller's "did we claim?" check still works."""
        try:
            payload = await self._mcp_client.invoke(
                "kora__claim_sea_ticket",
                {
                    "workspace_id": workspace_id,
                    "kora_operation_id": kora_operation_id,
                    "sea_ticket_id": sea_ticket_id,
                    "lease_duration_seconds": self._claim_ttl_seconds,
                },
            )
        except Exception:
            logger.exception(
                "[sea_ticket_poller] kora__claim_sea_ticket raised "
                "for ticket_id=%s",
                sea_ticket_id,
            )
            return ClaimState(
                result="claim_invoke_failed",
                claim_fence_token=None,
                lease_expires_at=None,
                claim_count=None,
                chain_event_id=None,
                work_attempt_id=None,
            )
        return ClaimState(
            result=str(payload.get("result", "unknown")),
            claim_fence_token=_str_or_none(payload.get("claim_fence_token")),
            lease_expires_at=_str_or_none(payload.get("lease_expires_at")),
            claim_count=(
                int(payload["claim_count"])
                if payload.get("claim_count") is not None
                else None
            ),
            chain_event_id=_str_or_none(payload.get("chain_event_id")),
            work_attempt_id=_str_or_none(payload.get("work_attempt_id")),
        )

    async def _release(
        self,
        *,
        workspace_id: str,
        kora_operation_id: str,
        sea_ticket_id: str,
        claim_fence_token: str,
    ) -> None:
        """Invoke ``kora__release_claim``. Errors are logged + swallowed
        — a failed release surfaces as a stale lease, the sweeper
        cleans it up. Re-raising would block the poll loop."""
        try:
            await self._mcp_client.invoke(
                "kora__release_claim",
                {
                    "workspace_id": workspace_id,
                    "kora_operation_id": kora_operation_id,
                    "sea_ticket_id": sea_ticket_id,
                    "claim_fence_token": claim_fence_token,
                },
            )
        except Exception:
            logger.exception(
                "[sea_ticket_poller] kora__release_claim raised for "
                "ticket_id=%s (token=%s); lease will expire naturally",
                sea_ticket_id,
                claim_fence_token,
            )


def _str_or_none(v: Any) -> Optional[str]:
    """Cast to str unless v is None."""
    return None if v is None else str(v)
