"""R4.1 §9.8 DR epoch read/write helpers (KR-P2-M ST2).

Three async helpers over the substrate-shipped (substrate Bucket C —
``top-level/0100_kora_dr_epoch_substrate.sql``):

- :func:`read_substrate_epoch` — STABLE accessor; runtime polls at
  boot (gate 3b) + at PAUSED{substrate} clearance time.
- :func:`read_kora_known_epoch` — STABLE accessor; returns ``None``
  until Kora's first successful boot completes (the substrate column
  is nullable; default NULL).
- :func:`write_kora_known_epoch` — Kora-only SECDEF. Monotonic
  (substrate refuses backward roll). Idempotent on same-value writes
  (substrate just UPDATEs without raising). Caller passes the observed
  value as an arg to avoid a stale-read race inside the SECDEF.

All helpers are async + take an asyncpg pool. Callers running on the
runner's event loop bridge via ``provider._connection.submit_and_wait``
or by handing the pool from ``provider._connection.get_pg_pool()``.

# Auth notes

  - Read accessors require no auth (STABLE SQL functions, granted to
    PUBLIC implicitly).
  - ``public.kora_write_known_epoch(observed, kora_actor_id)`` SECDEF
    validates ``actor_kind='kora'`` against ``actor_registry`` and
    refuses any other caller. The runtime resolves Kora's UUID once at
    boot (the existing pattern in
    ``agent/stop_kora_pre_flight.py:_resolve_kora_actor_uuid``;
    KR-P2-M ST4 wires this into the writer's call site).

# Monotonicity

The substrate SECDEF raises ``check_violation`` (``23514``) when
``observed_epoch < current kora_known_epoch``. Per R4.1 §9.8:
``kora_known_epoch`` only advances. If Kora ever observes a
substrate_epoch lower than her last-known value, that itself is the
DR signal — but rather than try to roll the substrate column backward,
the runtime stays at the higher value + emits ``kora.dr.observed``
(ST3 / dr_handler). The write helper raises
:exc:`KoraKnownEpochMonotonicViolation` so callers can distinguish
that path from a transport hiccup.
"""

from __future__ import annotations

import logging
from typing import Any, Final, Optional

logger = logging.getLogger(__name__)


# Read queries — wrap the STABLE accessors. No auth needed.
_SELECT_SUBSTRATE_EPOCH_SQL: Final[str] = (
    "SELECT public.substrate_epoch() AS epoch"
)
_SELECT_KORA_KNOWN_EPOCH_SQL: Final[str] = (
    "SELECT public.kora_known_epoch() AS epoch"
)

# Write SECDEF call — pass observed_epoch + Kora's actor UUID.
_CALL_KORA_WRITE_KNOWN_EPOCH_SQL: Final[str] = (
    "SELECT public.kora_write_known_epoch($1::bigint, $2::uuid) AS written_epoch"
)


# Substrate ``check_violation`` SQLSTATE — emitted on monotonic
# rollback attempt. asyncpg raises ``asyncpg.exceptions.CheckViolationError``
# (subclass of PostgresError) carrying this code.
_PG_CHECK_VIOLATION_SQLSTATE: Final[str] = "23514"


class KoraKnownEpochMonotonicViolation(RuntimeError):
    """Raised when the SECDEF refuses a backward roll.

    Wraps the substrate's ``check_violation`` for the
    ``observed < current`` case. The original exception is chained via
    ``__cause__`` for operator triage.

    Per R4.1 §9.8: an observed substrate_epoch *less than* Kora's
    last-known value is itself the DR signal. The dr_handler (ST3)
    emits ``kora.dr.observed`` and transitions the holder to
    PAUSED{substrate} when this fires.
    """

    def __init__(self, observed: int, current: int):
        self.observed = observed
        self.current = current
        super().__init__(
            f"kora_known_epoch monotonic violation: observed={observed} "
            f"< current kora_known_epoch={current}. Substrate refused "
            f"the backward roll. This is the R4.1 §9.8 DR signal — "
            f"caller (dr_handler) emits kora.dr.observed + transitions "
            f"holder to PAUSED{{substrate}}."
        )


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


async def read_substrate_epoch(pool: Any) -> int:
    """Return the live ``substrate_epoch`` value.

    The accessor is STABLE — safe to call without a transaction.
    Returns a positive ``int``; substrate ``CHECK (> 0)`` guarantees
    non-zero. Raises :exc:`RuntimeError` if the singleton row is
    somehow missing (unreachable in practice — the seed INSERT is in
    the migration).
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_SELECT_SUBSTRATE_EPOCH_SQL)
    if row is None:
        raise RuntimeError(
            "substrate_epoch() returned no row — "
            "kora_dr_epoch_substrate singleton missing"
        )
    return int(row["epoch"])


async def read_kora_known_epoch(pool: Any) -> Optional[int]:
    """Return Kora's last-observed substrate_epoch, or ``None`` if
    Kora has not yet completed a successful boot.

    NULL → ``None`` (Kora's first-ever boot).
    Positive int → the last value written via
    :func:`write_kora_known_epoch`.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_SELECT_KORA_KNOWN_EPOCH_SQL)
    if row is None:
        raise RuntimeError(
            "kora_known_epoch() returned no row — "
            "kora_dr_epoch_substrate singleton missing"
        )
    value = row["epoch"]
    if value is None:
        return None
    return int(value)


# ---------------------------------------------------------------------------
# Write helper (SECDEF)
# ---------------------------------------------------------------------------


async def write_kora_known_epoch(
    pool: Any,
    *,
    observed_epoch: int,
    kora_actor_id: str,
) -> int:
    """Write ``observed_epoch`` to ``kora_known_epoch`` via the substrate
    SECDEF.

    Idempotent on same-value writes: the SECDEF UPDATEs the column to
    the passed value without raising, even if the column already holds
    that value. Re-callable safely at boot and at PAUSED clearance.

    Args:
        pool: asyncpg pool from
            ``provider._connection.get_pg_pool()``.
        observed_epoch: The value Kora just observed via
            :func:`read_substrate_epoch`. Must be a positive integer
            (substrate ``CHECK`` enforces).
        kora_actor_id: Kora's UUID in ``actor_registry`` for the
            workspace. Resolved via the existing
            ``_resolve_kora_actor_uuid`` helper at the caller's
            boundary (KR-P2-M ST4 wires this in).

    Returns:
        The value that was written (echoed by the SECDEF).

    Raises:
        KoraKnownEpochMonotonicViolation: substrate refused because
            ``observed_epoch < current kora_known_epoch``. Caller
            (dr_handler) treats this as the R4.1 §9.8 DR signal —
            emit ``kora.dr.observed`` + transition to
            PAUSED{substrate}.
        ValueError: ``observed_epoch`` is non-positive or not int.
        Exception: substrate-side error (auth / role mismatch /
            actor_registry lookup failure) — propagated to caller.
    """
    if not isinstance(observed_epoch, int) or observed_epoch <= 0:
        raise ValueError(
            f"observed_epoch must be a positive int; got "
            f"{observed_epoch!r}"
        )
    if not kora_actor_id:
        raise ValueError("kora_actor_id is required")

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                _CALL_KORA_WRITE_KNOWN_EPOCH_SQL,
                observed_epoch,
                kora_actor_id,
            )
    except Exception as exc:
        # Detect the substrate's monotonic-violation CHECK and re-wrap
        # so callers can pattern-match cleanly. The asyncpg exception
        # surface varies; we read the sqlstate attribute when present
        # and fall back to message inspection.
        sqlstate = getattr(exc, "sqlstate", None) or getattr(
            exc, "pgcode", None
        )
        msg = str(exc)
        is_monotonic_violation = (
            sqlstate == _PG_CHECK_VIOLATION_SQLSTATE
            and "monotonic violation" in msg.lower()
        )
        if is_monotonic_violation:
            # Pull current value out of the message for the wrapper.
            # The SECDEF formats it as ``observed N < kora_known_epoch M``.
            try:
                # Best-effort parse — exact format from the SECDEF.
                # Fall back to (0, 0) if substrate's format ever drifts.
                tokens = msg.split()
                observed_idx = tokens.index("observed") + 1
                current_idx = tokens.index("kora_known_epoch") + 1
                observed_parsed = int(tokens[observed_idx])
                current_parsed = int(tokens[current_idx].rstrip(".,;"))
            except (ValueError, IndexError):
                observed_parsed = observed_epoch
                current_parsed = 0
            raise KoraKnownEpochMonotonicViolation(
                observed=observed_parsed, current=current_parsed
            ) from exc
        raise

    if row is None:
        # Defensive — the SECDEF always RETURNs the written value.
        raise RuntimeError(
            "kora_write_known_epoch SECDEF returned no row "
            "(unexpected — substrate contract violation)"
        )

    written = int(row["written_epoch"])
    logger.info(
        "[kora.dr_epoch] kora_known_epoch written: observed=%d "
        "(returned=%d)",
        observed_epoch,
        written,
    )
    return written


# ---------------------------------------------------------------------------
# DR-panel summary aggregator (KR-P2-DR-FLIP)
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List


@dataclass(frozen=True, slots=True)
class DRStateSummary:
    """Aggregated DR/epoch state for the operator-facing /api/dr-state.

    All fields are projection-ready for the FE shape. epoch_history is
    intentionally empty in v1 — no ``kora_known_epoch_history`` table
    exists yet, so there's no audit-trail source to project from. When
    a future bucket adds the history table (or a derived view from
    ``kora.dr.observed`` events + boot-success markers), this list
    populates without an FE change.
    """

    substrate_epoch: int
    kora_known_epoch: Optional[int]
    match_status: str  # clean | mismatch_detected | pending_runbook | unknown
    kora_paused_substrate: bool
    last_check_at: str  # ISO-8601 (request time)
    recent_dr_events: List[dict] = field(default_factory=list)
    epoch_history: List[dict] = field(default_factory=list)


def _derive_match_status(
    substrate_epoch: int,
    kora_known_epoch: Optional[int],
    kora_paused_substrate: bool,
) -> str:
    """Resolve the four-state match enum from the two epoch values + holder state.

    Truth table (R4.1 §9.8):
        kora_known == None              → unknown (Kora hasn't booted yet)
        kora_known == substrate_epoch
          + holder PAUSED{substrate}    → pending_runbook (epochs match,
                                          but the holder is still in the
                                          paused state — operator needs
                                          to issue kora_control reset to
                                          clear the PAUSED edge)
          + holder NOT PAUSED           → clean
        kora_known < substrate_epoch    → mismatch_detected (gate 3b would
                                          catch this on next boot; the
                                          DR-panel surfaces it now)
        kora_known > substrate_epoch    → unknown (substrate is monotonic
                                          post-PITR; this combination is
                                          unreachable in practice — guard
                                          treats it as a sentinel)
    """
    if kora_known_epoch is None:
        return "unknown"
    if kora_known_epoch == substrate_epoch:
        return "pending_runbook" if kora_paused_substrate else "clean"
    if kora_known_epoch < substrate_epoch:
        return "mismatch_detected"
    return "unknown"


def _check_kora_paused_substrate() -> bool:
    """True iff the OperationalState holder reports PAUSED{substrate}.

    Reads via the holder singleton (``agent.operational_state_holder.get_holder``).
    Returns False when the holder isn't initialised (CI / dev runs
    without the boot-time wire-in) — the caller treats that as
    "no PAUSED edge", which is the right default.
    """
    try:
        from agent.operational_state_holder import get_holder
        from agent.operational_state import DegradationReason, PrimaryState

        holder = get_holder()
        if holder is None:
            return False
        state = holder.current()
        return (
            state.primary_state is PrimaryState.PAUSED
            and DegradationReason.SUBSTRATE in state.degradation_reasons
        )
    except Exception:
        # Defensive: any import / attribute / enum mismatch falls through
        # as "not paused". The DR panel is a diagnostic surface, not a
        # safety surface — the runtime holder is the authoritative state
        # for actual PAUSED enforcement.
        logger.exception("[kora.dr_panel] holder PAUSED check failed")
        return False


def _project_dr_event(row: Any) -> dict:
    """Project a :class:`DRObservedEventRow` into the API shape.

    Pulls ``from_epoch`` / ``to_epoch`` / ``discarded_*`` / ``cleared_*``
    from the event payload. Missing fields surface as ``None`` so the
    FE renders "—" instead of crashing on a pre-contract event.
    """
    payload = row.payload if isinstance(row.payload, dict) else {}

    def _int_field(name: str) -> int:
        value = payload.get(name)
        return int(value) if isinstance(value, (int, float)) else 0

    return {
        "event_type": "kora.dr.observed",
        "occurred_at": row.occurred_at,
        "from_epoch": _int_field("from_epoch"),
        "to_epoch": _int_field("to_epoch"),
        "discarded_operation_ids": _int_field("discarded_operation_ids"),
        "discarded_ledger_rows": _int_field("discarded_ledger_rows"),
        "cleared_at": payload.get("cleared_at"),
        "cleared_by": payload.get("cleared_by"),
    }


async def get_dr_state_summary(
    memory_provider: Any,
    workspace_id: str,
    *,
    dr_event_limit: int = 10,
) -> DRStateSummary:
    """Aggregate DR/epoch state for the operator-facing /api/dr-state.

    Reads (in order):
      1. substrate_epoch via :func:`read_substrate_epoch`
      2. kora_known_epoch via :func:`read_kora_known_epoch`
      3. holder.current() for the PAUSED{substrate} flag
      4. recent ``kora.dr.observed`` events via
         :func:`read_dr_observed_events`

    The 4 reads run sequentially because they hit the same pool; the
    panel is manual-reload only so per-request latency budget is
    generous.

    Raises:
        IsoKronConnectionError: ``memory_provider._connection`` is None
            (provider not started). Caller's try/except wraps this
            into the stub-fallback + ``error`` field response branch
            so the operator sees the cause rather than a 500.
        RuntimeError: substrate accessor returned no row (singleton
            missing — unreachable in practice; the seed INSERT is in
            the migration).
        Exception: asyncpg / network failure — propagated.
    """
    from .events import read_dr_observed_events

    connection = getattr(memory_provider, "_connection", None)
    if connection is None:
        raise RuntimeError(
            "memory_provider._connection is None — provider not started"
        )
    pool = connection.get_pg_pool()

    substrate_epoch = await read_substrate_epoch(pool)
    kora_known_epoch = await read_kora_known_epoch(pool)
    kora_paused_substrate = _check_kora_paused_substrate()
    dr_event_rows = await read_dr_observed_events(
        workspace_id, pool, limit=dr_event_limit
    )

    match_status = _derive_match_status(
        substrate_epoch, kora_known_epoch, kora_paused_substrate
    )
    last_check_at = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    recent_dr_events = [_project_dr_event(row) for row in dr_event_rows]

    return DRStateSummary(
        substrate_epoch=substrate_epoch,
        kora_known_epoch=kora_known_epoch,
        match_status=match_status,
        kora_paused_substrate=kora_paused_substrate,
        last_check_at=last_check_at,
        recent_dr_events=recent_dr_events,
        epoch_history=[],
    )
