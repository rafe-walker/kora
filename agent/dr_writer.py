"""kora_known_epoch writer wiring (KR-P2-M ST4).

R4.1 §9.8 pin P5: ``kora_known_epoch`` is written at two moments:

  1. **End-of-successful-boot** — after the coordinator transitions
     the holder to READY, the runtime records the substrate_epoch she
     just observed. Subsequent boots compare against this value via
     gate 3b.

  2. **Operator clearance from PAUSED{substrate}** — when an operator
     issues a ``kora_control`` reset that clears the DR PAUSED state
     (transition ``PAUSED → READY`` with
     ``remove_reasons={DegradationReason.SUBSTRATE}``), the runtime
     writes the post-DR substrate_epoch fresh so future gate 3b
     checks pass.

Both paths call the same substrate SECDEF (``public.kora_write_known_epoch``)
via the ST2 ``plugins.memory.isokron.dr_epoch`` helpers. The SECDEF is
monotonic + idempotent on same-value writes; calling it at both
moments is safe + correct.

# Per R4.1 §9.8 P5: never write before gate 3b ran

Both call sites here are downstream of the boot sequence — the
end-of-boot write fires only on the coordinator's all-pass path
(gate 3b passed); the PAUSED-clearance write fires only after the
operator's explicit reset (which itself implies gate 3b's mismatch
was reviewed + dismissed). No pre-gate-3b write path exists in this
module.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from agent.operational_state import (
    DegradationReason,
    OperationalState,
    PrimaryState,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# Listener signature alias — matches
# ``agent.operational_state_holder.StateTransitionListener``.
DrWriterListener = Callable[
    [OperationalState, OperationalState, str], Awaitable[None]
]


# ---------------------------------------------------------------------------
# End-of-boot write (coordinator's all-pass path)
# ---------------------------------------------------------------------------


async def write_known_epoch_at_boot_end(
    memory_provider: Any,
    kora_actor_uuid: Optional[str],
) -> None:
    """Read the current ``substrate_epoch`` and write it to
    ``kora_known_epoch`` via the Kora-only SECDEF.

    Called from :mod:`agent.boot_coordinator` after the all-gates-passed
    path transitions the holder to READY. ``kora_actor_uuid`` is the
    value gate 7 (``CanonicalKoraActorGate``) populated on
    ``BootContext.kora_actor_uuid``; if the all-pass branch was
    reached, gate 7 ran and the value is set.

    Failure-mode posture: best-effort. WARN-log on any failure;
    do NOT raise. The holder is already READY + the boot.ready event
    is already emitted; a failed end-of-boot write means Kora will
    re-run gate 3b on next boot against the OLD known_epoch, which
    is correct fail-CLOSED behavior (if substrate_epoch hasn't
    changed, gate 3b passes; if it has, gate 3b correctly fires the
    DR signal).

    Monotonic-violation case is logged at ERROR — that's an unusual
    boot-time signal (substrate_epoch advanced again during boot)
    that operator triage will want to know about.
    """
    if memory_provider is None:
        logger.warning(
            "[kora.dr_epoch] end-of-boot write skipped: memory_provider "
            "is None"
        )
        return
    if not kora_actor_uuid:
        # Gate 7 should have populated this; if it didn't (e.g. the
        # gate sequence was customized), the SECDEF auth check would
        # reject the write anyway.
        logger.warning(
            "[kora.dr_epoch] end-of-boot write skipped: "
            "kora_actor_uuid not resolved (gate 7 may not have run)"
        )
        return

    connection = getattr(memory_provider, "_connection", None)
    if connection is None:
        logger.warning(
            "[kora.dr_epoch] end-of-boot write skipped: provider "
            "_connection is None"
        )
        return

    try:
        await _do_write(connection, kora_actor_uuid, source="end-of-boot")
    except Exception as exc:
        # _do_write already logged; nothing more to do at this layer.
        del exc


# ---------------------------------------------------------------------------
# PAUSED-clear listener (operator-clearance path)
# ---------------------------------------------------------------------------


def make_paused_substrate_cleared_listener(
    memory_provider: Any,
) -> DrWriterListener:
    """Return a listener that writes ``kora_known_epoch`` fresh when
    the operator clears the DR PAUSED state.

    Match criteria (all four must hold):
      - ``from_state.primary_state is PAUSED``
      - ``to_state.primary_state is READY``
      - ``SUBSTRATE in from_state.degradation_reasons``
      - ``SUBSTRATE not in to_state.degradation_reasons``

    On match: read the current ``substrate_epoch`` and write it via
    the Kora-only SECDEF. Resolves Kora's actor_id from
    ``actor_registry`` at fire time (gate 7's
    ``BootContext.kora_actor_uuid`` is not in scope at listener-fire
    time, and a fresh resolve is cheap).

    Failure-mode posture: WARN-log on any failure; do NOT raise.
    Listener exceptions are caught by the holder anyway (per
    ``OperationalStateHolder``'s listener-exception isolation), but
    raising here would still spam logs from the holder. Returning
    cleanly is the right shape.
    """

    async def _listener(
        from_state: OperationalState,
        to_state: OperationalState,
        trigger: str,
    ) -> None:
        if not _is_substrate_paused_cleared(from_state, to_state):
            return

        connection = getattr(memory_provider, "_connection", None)
        if connection is None:
            logger.warning(
                "[kora.dr_epoch] PAUSED-clear write skipped: "
                "memory_provider._connection is None (trigger=%r)",
                trigger,
            )
            return

        # Resolve Kora's actor_id at fire time. Boot's context isn't
        # in scope; gate 7 may or may not have run. A direct
        # actor_registry query is cheap.
        kora_actor_uuid = await _resolve_kora_actor_uuid(memory_provider)
        if not kora_actor_uuid:
            logger.warning(
                "[kora.dr_epoch] PAUSED-clear write skipped: kora "
                "actor_id unresolved (trigger=%r)",
                trigger,
            )
            return

        try:
            await _do_write(
                connection, kora_actor_uuid, source="paused-clearance"
            )
        except Exception as exc:
            # _do_write already logged.
            del exc

    return _listener


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _is_substrate_paused_cleared(
    from_state: OperationalState, to_state: OperationalState
) -> bool:
    """Match for: PAUSED+{SUBSTRATE} → READY+{} (SUBSTRATE removed)."""
    if from_state.primary_state is not PrimaryState.PAUSED:
        return False
    if to_state.primary_state is not PrimaryState.READY:
        return False
    if DegradationReason.SUBSTRATE not in from_state.degradation_reasons:
        return False
    if DegradationReason.SUBSTRATE in to_state.degradation_reasons:
        return False
    return True


async def _do_write(
    connection: Any, kora_actor_uuid: str, *, source: str
) -> None:
    """Shared body: read substrate_epoch + write kora_known_epoch.

    Both end-of-boot and PAUSED-clearance use the same substrate
    interactions; the ``source`` arg differentiates the log lines for
    operator triage.

    Best-effort: catches all exceptions, logs at WARN (or ERROR for
    the monotonic-violation case), returns cleanly. The caller never
    raises.
    """
    from plugins.memory.isokron.dr_epoch import (
        KoraKnownEpochMonotonicViolation,
        read_substrate_epoch,
        write_kora_known_epoch,
    )

    pool = connection.get_pg_pool()

    try:
        substrate_epoch = await read_substrate_epoch(pool)
    except Exception as exc:
        logger.warning(
            "[kora.dr_epoch] %s: read_substrate_epoch raised %r — "
            "kora_known_epoch not written",
            source,
            exc,
        )
        return

    try:
        written = await write_kora_known_epoch(
            pool,
            observed_epoch=substrate_epoch,
            kora_actor_id=kora_actor_uuid,
        )
    except KoraKnownEpochMonotonicViolation as exc:
        logger.error(
            "[kora.dr_epoch] %s: write_kora_known_epoch refused "
            "(monotonic violation observed=%d current=%d). Operator "
            "triage required — substrate_epoch likely advanced again "
            "between the read and the write.",
            source,
            exc.observed,
            exc.current,
        )
        return
    except Exception as exc:
        logger.warning(
            "[kora.dr_epoch] %s: write_kora_known_epoch raised %r — "
            "kora_known_epoch may be stale",
            source,
            exc,
        )
        return

    logger.info(
        "[kora.dr_epoch] %s write succeeded: kora_known_epoch=%d",
        source,
        written,
    )


# ---------------------------------------------------------------------------
# Kora actor_id resolution (used by the listener)
# ---------------------------------------------------------------------------


async def _resolve_kora_actor_uuid(memory_provider: Any) -> Optional[str]:
    """Look up Kora's actor UUID in ``actor_registry`` for the active
    workspace. Returns ``None`` on any failure.

    Same query shape as ``agent/stop_kora_pre_flight.py`` and
    ``agent/boot_gates_impl.py:_query_canonical_kora_actor`` —
    ``actor_registry`` has no RLS policy so no GUC is needed.
    """
    try:
        workspace_id = memory_provider._resolve_workspace_id()
    except Exception:
        return None
    if not workspace_id:
        return None

    connection = getattr(memory_provider, "_connection", None)
    if connection is None:
        return None

    pool = connection.get_pg_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT actor_id::text AS actor_id
                  FROM public.actor_registry
                 WHERE workspace_id = $1
                   AND actor_kind = 'kora'
                   AND deactivated_at IS NULL
                 LIMIT 1
                """,
                workspace_id,
            )
    except Exception:
        return None
    return row["actor_id"] if row else None
