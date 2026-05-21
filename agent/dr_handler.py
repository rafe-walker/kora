"""DR epoch-mismatch handler (KR-P2-M ST3).

Invoked from :class:`agent.boot_gates_dr.Gate3bEpochCheck` when it
observes ``substrate_epoch != kora_known_epoch`` (or any monotonic
violation that the substrate refused). Per R4.1 §9.8:

  1. Emit ``kora.dr.observed`` chain event with the (observed, known)
     pair so cockpit + audit-trail consumers see the DR signal.
  2. Transition the operational-state holder ``BOOTING → PAUSED`` with
     ``add_reasons={DegradationReason.SUBSTRATE}``.
  3. Discard in-flight runtime state — **N/A at boot time** because
     gate 3b runs before the consumer loop has minted any
     ``kora_operation_id`` or started any inference. The mid-runtime
     epoch-bump-detection path (Phase 3) is the consumer of step (3);
     this bucket only handles boot-time detection per § 4.

After this handler runs, the gate returns a FAIL ``GateResult`` with
class ``INVARIANT_PAUSE``. The coordinator (see
:mod:`agent.boot_coordinator`) sees the class and routes to
``BootResult.PAUSED`` without re-transitioning or re-emitting.

Operator clearance: an operator-issued ``kora_control`` reset (level=0)
sets the holder back to READY + clears the SUBSTRATE reason; KR-P2-M
ST4 wires the ``kora_known_epoch`` write into that clearance path so
Kora's known epoch advances to the new substrate_epoch on resume.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from agent.operational_state import DegradationReason, PrimaryState

if TYPE_CHECKING:
    from agent.operational_state_holder import OperationalStateHolder

logger = logging.getLogger(__name__)


# Chain event literal — PM-verified in foundation/0159 during KR-P2-H
# verifications. The runtime emit is the dr-specific audit event.
DR_OBSERVED_EVENT: str = "kora.dr.observed"


# Trigger string for the PAUSED transition. Intentionally does NOT
# match the operational_state_emit listener's per-trigger literal
# rules (no "cost 100%" substring, etc.), so the listener emits ONLY
# the generic ``kora.operational_state.transitioned`` event. The
# rich dr-audit emit is the handler's responsibility.
_TRIGGER_DR_OBSERVED: str = (
    "gate 3b epoch mismatch (R4.1 §9.8); see kora.dr.observed event"
)


async def handle_epoch_mismatch(
    *,
    memory_provider: Any,
    holder: "OperationalStateHolder",
    observed_substrate_epoch: int,
    last_known_epoch: Optional[int],
) -> None:
    """Coordinate the R4.1 §9.8 mismatch response.

    Best-effort: if the emit fails (substrate-side hiccup at the
    moment of DR detection), the handler logs ERROR but still
    transitions the holder. The cockpit-BFF watcher will see the
    holder go to PAUSED{substrate} via the generic transition emit
    fired by the operational_state_emit listener — the dr-specific
    literal is the cleaner audit signal but not load-bearing for
    the transition itself.

    Args:
        memory_provider: ``IsoKronMemoryProvider`` for the chain emit.
        holder: ``OperationalStateHolder`` for the PAUSED transition.
        observed_substrate_epoch: The value Kora just read from
            ``public.substrate_epoch()``.
        last_known_epoch: Kora's previously-written
            ``kora_known_epoch``. ``None`` on Kora's very first boot
            — in that case gate 3b shouldn't have called this handler
            (no mismatch is possible), but the value is recorded in
            the payload for audit completeness.
    """
    await _emit_dr_observed(
        memory_provider,
        observed=observed_substrate_epoch,
        known=last_known_epoch,
    )
    await holder.transition_to(
        PrimaryState.PAUSED,
        trigger=_TRIGGER_DR_OBSERVED,
        add_reasons={DegradationReason.SUBSTRATE},
    )
    logger.warning(
        "[kora.dr_handler] DR epoch mismatch handled: "
        "observed=%d known=%s — holder is now PAUSED{substrate}. "
        "Operator must clear via cockpit kora_control reset.",
        observed_substrate_epoch,
        last_known_epoch,
    )


# ---------------------------------------------------------------------------
# Chain emit (best-effort)
# ---------------------------------------------------------------------------


async def _emit_dr_observed(
    memory_provider: Any,
    *,
    observed: int,
    known: Optional[int],
) -> None:
    """Emit ``kora.dr.observed`` with the (observed, known) pair.

    Failure-mode posture: WARN-log + return. The holder transition
    fires regardless — the dr-audit signal is operator-facing
    observability, not load-bearing for the runtime's PAUSED state.
    """
    if memory_provider is None:
        logger.warning(
            "[kora.dr_handler] memory_provider is None; cannot emit "
            "%s. Holder transition will still fire.",
            DR_OBSERVED_EVENT,
        )
        return

    connection = getattr(memory_provider, "_connection", None)
    if connection is None:
        logger.warning(
            "[kora.dr_handler] memory_provider._connection is None; "
            "cannot emit %s. Holder transition will still fire.",
            DR_OBSERVED_EVENT,
        )
        return

    try:
        workspace_id = memory_provider._resolve_workspace_id()
    except Exception as exc:
        logger.warning(
            "[kora.dr_handler] workspace_id resolution raised: %r; "
            "cannot emit %s.",
            exc,
            DR_OBSERVED_EVENT,
        )
        return
    if not workspace_id:
        logger.warning(
            "[kora.dr_handler] workspace_id unresolved; cannot emit %s.",
            DR_OBSERVED_EVENT,
        )
        return

    payload = {
        "observed_substrate_epoch": observed,
        "last_known_epoch": known,
    }

    from plugins.memory.isokron.events import emit_kora_event

    try:
        mcp_client = connection.get_mcp_client()
        event_id = connection.submit_and_wait(
            emit_kora_event(
                workspace_id=workspace_id,
                event_type=DR_OBSERVED_EVENT,
                payload=payload,
                mcp_client=mcp_client,
            ),
            timeout=10.0,
        )
        logger.info(
            "[%s] emitted; event_id=%s observed=%d known=%s",
            DR_OBSERVED_EVENT,
            event_id,
            observed,
            known,
        )
    except Exception as exc:
        logger.error(
            "[%s] emit raised: %r — proceeding with PAUSED transition. "
            "Cockpit-side dr audit is degraded but the holder state "
            "change is still observable via the generic "
            "kora.operational_state.transitioned listener emit.",
            DR_OBSERVED_EVENT,
            exc,
        )
