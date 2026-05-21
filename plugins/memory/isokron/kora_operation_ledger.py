"""``kora_operation_ledger`` writer (KR-P2-E ST2).

Writer + reread pattern against the ledger table shipped in migration
``packages/db/migrations/0093_kora_operation_ledger.sql``. Per R4.1
§9.5 + pin P4, with the atomic-mint amendment from
top-level/0102/0103:

* ``work_attempt_id`` is minted INSIDE the substrate's
  ``kora_claim_sea_ticket`` SECDEF and returned to the runtime. THIS
  module does NOT mint attempt ids — the dropped ``mint_work_attempt_id``
  method from the original bucket draft is intentionally absent.
* Each agent-loop dispatch within an attempt allocates one row here.
  ``sequence_within_attempt`` is allocated by the runtime writer as
  ``max(sequence_within_attempt) + 1`` over rows for
  ``(work_attempt_id)``. Single-writer-per-attempt invariant — there
  is no cross-process contention because the consumer loop runs a
  single asyncio task per claim.
* ``kora_operation_id`` is the load-bearing idempotency anchor: the
  unique constraint on ``event_log.kora_operation_id``
  (foundation/0156-0157) plus this table's UNIQUE on the same column
  means a duplicate dispatch surfaces a clean uniqueness violation
  the caller can recover from.

State machine (column ``status`` — a real PG ENUM
``public.kora_operation_ledger_status``):

::

  allocated ──► dispatched ──► committed
       ╰────► abandoned ◄─────╯  (retry that no-ops or fails)

# Retry semantics

If the agent loop crashes mid-attempt and the next claim re-enters
work for the same ``work_attempt_id`` (this happens via the
substrate-side resume path), the runtime calls
:meth:`KoraOperationLedger.reread_for_retry` keyed by
``(work_attempt_id, sequence_within_attempt)``. The returned row's
``status`` tells the caller what to do:

* ``allocated`` — the previous attempt allocated the row but never
  dispatched. Caller may dispatch now using the row's existing
  ``kora_operation_id`` for idempotency.
* ``dispatched`` — substrate-side dispatch landed but commit
  didn't. Inspect ``dispatch_result`` to decide commit-vs-abandon.
* ``committed`` — fully completed; caller advances to the next op.
* ``abandoned`` — terminal. Caller skips.

# 0093 BEFORE INSERT trigger

The migration installs a ``_kora_operation_ledger_check_work_attempt``
trigger that rejects INSERTs whose ``NEW.work_attempt_id`` ≠
``tickets.work_attempt_id``. This protects the ledger from late
inserts after a new claim rotates the attempt id. The runtime's
single-writer-per-attempt invariant + the SECDEF's atomic mint
guarantee that satisfies this trigger as long as the caller threads
the ``work_attempt_id`` returned by ``kora__claim_sea_ticket``
straight into ``allocate_operation``. A trigger rejection surfaces
here as :class:`KoraOperationLedgerError`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KoraOperationRow:
    """Projection of one ``kora_operation_ledger`` row.

    Field order + names match the column order in
    ``packages/db/migrations/0093_kora_operation_ledger.sql``. All
    UUID / TEXT fields are surfaced as :class:`str`.
    """

    work_attempt_id: str
    sequence_within_attempt: int
    kora_operation_id: str
    workspace_id: str
    ticket_id: str
    status: str  # one of allocated / dispatched / committed / abandoned
    tool_name: str
    dispatch_result: Optional[dict[str, Any]]
    dispatch_error: Optional[str]
    created_at: datetime
    updated_at: datetime


class KoraOperationLedgerError(RuntimeError):
    """Surfaces any substrate-side failure on a ledger write or read —
    asyncpg exceptions (including the 0093 trigger rejection), unique
    violations on duplicate ``kora_operation_id``, NOT NULL violations,
    etc."""


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

# Allocate via a CTE so the sequence read + INSERT happen atomically
# in one statement. The CTE always returns exactly one row (MAX over
# empty set is NULL → COALESCE to -1 → +1 = 0 for the first op of an
# attempt), so the downstream INSERT … SELECT inserts exactly one row.
# Single-writer-per-attempt makes this safe without a row lock.
_ALLOCATE_SQL = """
WITH next_seq AS (
  SELECT COALESCE(MAX(sequence_within_attempt), -1) + 1 AS seq
    FROM public.kora_operation_ledger
   WHERE work_attempt_id = $1
)
INSERT INTO public.kora_operation_ledger (
  work_attempt_id,
  sequence_within_attempt,
  workspace_id,
  ticket_id,
  tool_name,
  status
)
SELECT $1, seq, $2, $3, $4, 'allocated'
  FROM next_seq
RETURNING
  work_attempt_id,
  sequence_within_attempt,
  kora_operation_id,
  workspace_id,
  ticket_id,
  status::text AS status,
  tool_name,
  dispatch_result,
  dispatch_error,
  created_at,
  updated_at
"""


_MARK_DISPATCHED_SQL = """
UPDATE public.kora_operation_ledger
   SET status = 'dispatched',
       dispatch_result = $2,
       updated_at = NOW()
 WHERE kora_operation_id = $1
RETURNING
  work_attempt_id,
  sequence_within_attempt,
  kora_operation_id,
  workspace_id,
  ticket_id,
  status::text AS status,
  tool_name,
  dispatch_result,
  dispatch_error,
  created_at,
  updated_at
"""


_MARK_COMMITTED_SQL = """
UPDATE public.kora_operation_ledger
   SET status = 'committed',
       updated_at = NOW()
 WHERE kora_operation_id = $1
RETURNING
  work_attempt_id,
  sequence_within_attempt,
  kora_operation_id,
  workspace_id,
  ticket_id,
  status::text AS status,
  tool_name,
  dispatch_result,
  dispatch_error,
  created_at,
  updated_at
"""


_MARK_ABANDONED_SQL = """
UPDATE public.kora_operation_ledger
   SET status = 'abandoned',
       dispatch_error = $2,
       updated_at = NOW()
 WHERE kora_operation_id = $1
RETURNING
  work_attempt_id,
  sequence_within_attempt,
  kora_operation_id,
  workspace_id,
  ticket_id,
  status::text AS status,
  tool_name,
  dispatch_result,
  dispatch_error,
  created_at,
  updated_at
"""


_REREAD_SQL = """
SELECT
  work_attempt_id,
  sequence_within_attempt,
  kora_operation_id,
  workspace_id,
  ticket_id,
  status::text AS status,
  tool_name,
  dispatch_result,
  dispatch_error,
  created_at,
  updated_at
FROM public.kora_operation_ledger
WHERE work_attempt_id = $1
  AND sequence_within_attempt = $2
"""


def _row_to_kora_operation(row: Any) -> KoraOperationRow:
    """Convert an asyncpg Record (or dict) to KoraOperationRow."""
    return KoraOperationRow(
        work_attempt_id=str(row["work_attempt_id"]),
        sequence_within_attempt=int(row["sequence_within_attempt"]),
        kora_operation_id=str(row["kora_operation_id"]),
        workspace_id=row["workspace_id"],
        ticket_id=str(row["ticket_id"]),
        status=row["status"],
        tool_name=row["tool_name"],
        dispatch_result=row["dispatch_result"],
        dispatch_error=row["dispatch_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# KoraOperationLedger
# ---------------------------------------------------------------------------


class KoraOperationLedger:
    """Writer + reread API for ``kora_operation_ledger``.

    Construct one per process — the underlying connection's asyncpg
    pool is shared. Methods are async; they submit the underlying
    SQL onto the IsoKron dedicated IO loop via
    :meth:`IsoKronConnection._submit_async` so the call site can
    ``await`` results from the agent's event loop.

    No ``mint_work_attempt_id`` method: the substrate's
    ``kora_claim_sea_ticket`` SECDEF mints the attempt id atomically
    with the claim (top-level/0102, pin P4). Callers pass the
    returned ``work_attempt_id`` into :meth:`allocate_operation`
    directly.
    """

    def __init__(self, connection: Any) -> None:
        """``connection`` is an :class:`IsoKronConnection` (already
        started). The runtime obtains it from
        ``IsoKronMemoryProvider._connection``."""
        self._connection = connection

    # ------------------------------------------------------------------
    # Allocate
    # ------------------------------------------------------------------

    async def allocate_operation(
        self,
        *,
        work_attempt_id: str,
        workspace_id: str,
        ticket_id: str,
        tool_name: str,
    ) -> KoraOperationRow:
        """Allocate a new ledger row for a dispatch.

        Inserts with status='allocated' + auto-generated
        ``kora_operation_id`` + computed ``sequence_within_attempt``.
        Returns the projected row including the new
        ``kora_operation_id`` the caller threads into the dispatch.
        """
        row = await self._run_returning(
            _ALLOCATE_SQL,
            work_attempt_id,
            workspace_id,
            ticket_id,
            tool_name,
            failure_context=f"allocate_operation(work_attempt={work_attempt_id})",
        )
        return _row_to_kora_operation(row)

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    async def mark_dispatched(
        self,
        kora_operation_id: str,
        dispatch_result: Optional[dict[str, Any]] = None,
    ) -> KoraOperationRow:
        """Move ``allocated → dispatched`` for ``kora_operation_id``.

        ``dispatch_result`` is the JSONB payload the tool returned
        (free-form). Passing ``None`` keeps the column NULL —
        substrate-side cockpit consumers tolerate that.
        """
        row = await self._run_returning(
            _MARK_DISPATCHED_SQL,
            kora_operation_id,
            dispatch_result,
            failure_context=f"mark_dispatched({kora_operation_id})",
        )
        return _row_to_kora_operation(row)

    async def mark_committed(
        self, kora_operation_id: str
    ) -> KoraOperationRow:
        """Move ``dispatched → committed`` for ``kora_operation_id``."""
        row = await self._run_returning(
            _MARK_COMMITTED_SQL,
            kora_operation_id,
            failure_context=f"mark_committed({kora_operation_id})",
        )
        return _row_to_kora_operation(row)

    async def mark_abandoned(
        self, kora_operation_id: str, reason: str
    ) -> KoraOperationRow:
        """Move any state → ``abandoned`` for ``kora_operation_id``.

        ``reason`` is recorded in ``dispatch_error`` so operators can
        grep it.
        """
        row = await self._run_returning(
            _MARK_ABANDONED_SQL,
            kora_operation_id,
            reason,
            failure_context=f"mark_abandoned({kora_operation_id})",
        )
        return _row_to_kora_operation(row)

    # ------------------------------------------------------------------
    # Reread for retry
    # ------------------------------------------------------------------

    async def reread_for_retry(
        self,
        *,
        work_attempt_id: str,
        sequence_within_attempt: int,
    ) -> Optional[KoraOperationRow]:
        """Re-read a row by its composite primary key.

        Returns ``None`` if no row exists at
        ``(work_attempt_id, sequence_within_attempt)``. Used when the
        agent loop resumes after a crash — the caller inspects
        ``status`` to decide whether to redispatch (status='allocated'),
        commit (status='dispatched'), or skip (status in
        committed/abandoned).
        """

        async def _fetch() -> Any:
            pool = self._connection.get_pg_pool()
            async with pool.acquire() as conn:
                return await conn.fetchrow(
                    _REREAD_SQL, work_attempt_id, sequence_within_attempt
                )

        try:
            future = self._connection._submit_async(_fetch())
            row = await asyncio.wrap_future(future)
        except Exception as exc:
            raise KoraOperationLedgerError(
                f"reread_for_retry({work_attempt_id}, "
                f"{sequence_within_attempt}) raised: {exc!r}"
            ) from exc
        return None if row is None else _row_to_kora_operation(row)

    # ------------------------------------------------------------------
    # Shared internals
    # ------------------------------------------------------------------

    async def _run_returning(
        self, sql: str, *args: Any, failure_context: str
    ) -> Any:
        """Run a single-row-returning SQL through the IsoKron pool.

        Raises :class:`KoraOperationLedgerError` with
        ``failure_context`` prefixed if asyncpg raises or if the
        statement returned no row (the UPDATE paths return 0 rows
        when the ``kora_operation_id`` is not found; surface that as
        an explicit error rather than a silent no-op).
        """

        async def _execute() -> Any:
            pool = self._connection.get_pg_pool()
            async with pool.acquire() as conn:
                return await conn.fetchrow(sql, *args)

        try:
            future = self._connection._submit_async(_execute())
            row = await asyncio.wrap_future(future)
        except Exception as exc:
            raise KoraOperationLedgerError(
                f"{failure_context} raised: {exc!r}"
            ) from exc
        if row is None:
            raise KoraOperationLedgerError(
                f"{failure_context} returned no row (operation_id not "
                f"found, or 0093 trigger rejected the insert)"
            )
        return row
