"""Chain-event emit on every operational-state transition (KR-P2-I-integration ST2).

Hooks into :class:`agent.operational_state_holder.OperationalStateHolder`
as a listener — every transition writes to the IsoKron
``hivex_foundation.event_log`` via the ``kora__append_event`` Sea MCP
tool.

Event vocabulary (PM-verified against
``packages/db/migrations/foundation/0159_kora_r41_operational_state_event_vocabulary.sql``
on isokron-prod 2026-05-21):

* **Always** emit ``kora.operational_state.transitioned`` — the single
  edge event for the §9.1 state machine. Substrate-team design: one
  literal covers all transitions; the payload carries from/to state
  and the trigger.
* **Additionally** emit a per-trigger informational literal when one
  exists in the vocab and the trigger matches:

  * ``(BOOTING → READY, "all §9.2 gates pass")`` → ``kora.boot.ready``
  * ``(BOOTING → STOPPED, trigger contains "invariant gate failure")`` →
    ``kora.boot.failed``
  * ``(* → PAUSED, trigger contains "cost 100%")`` →
    ``kora.paused.cost_limit``
  * Operator-pause and substrate-pause: no per-trigger literal yet —
    only the generic event fires. If those become useful cockpit
    signals, substrate-team adds them in a follow-on vocab migration.

Failure mode: fail-LOUD. The chain-event log is the durable source of
truth for operational-state observability; a silent emit failure means
the cockpit shows stale state during an outage. Mirrors
``agent/constitution_audit.py``: emit_state_transition raises
:class:`OperationalStateEmitError` on substrate-side failure or
preflight check failure. The holder's listener-exception handler
catches and logs (so one broken emit doesn't deadlock the state
machine), but the raise + log is the observability path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from agent.operational_state import (
    OperationalState,
    PrimaryState,
)
from agent.operational_state_holder import StateTransitionListener

logger = logging.getLogger(__name__)


# Event-type literals — must match the
# event_log_event_type_check constraint at
# foundation/0159_kora_r41_operational_state_event_vocabulary.sql.
GENERIC_TRANSITION_EVENT = "kora.operational_state.transitioned"
BOOT_READY_EVENT = "kora.boot.ready"
BOOT_FAILED_EVENT = "kora.boot.failed"
PAUSED_COST_LIMIT_EVENT = "kora.paused.cost_limit"


class OperationalStateEmitError(Exception):
    """Substrate-side or preflight failure while emitting an
    operational-state transition event.

    Carries the event type that failed and the underlying cause so
    the operator can grep logs for the literal name.
    """

    def __init__(self, event_type: str, cause: str) -> None:
        super().__init__(
            f"emit of '{event_type}' failed: {cause}. Operational-state "
            f"observability is degraded; cockpit may show stale state."
        )
        self.event_type = event_type
        self.cause = cause


def _select_extra_literal(
    from_state: OperationalState, to_state: OperationalState, trigger: str
) -> Optional[str]:
    """Return the per-trigger informational literal that pairs with this
    transition, or ``None`` if none applies.

    Match rules are intentionally lenient — callers may pass slightly
    varying trigger strings (e.g. "cost 100% breached" vs the literal
    TRANSITION_TABLE label "STOP-KORA L1–3, cost 100%, operator"). The
    substring check tolerates both.
    """
    from_ps = from_state.primary_state
    to_ps = to_state.primary_state

    if from_ps is PrimaryState.BOOTING and to_ps is PrimaryState.READY:
        if "all §9.2 gates pass" in trigger:
            return BOOT_READY_EVENT
        return None

    if from_ps is PrimaryState.BOOTING and to_ps is PrimaryState.STOPPED:
        if "invariant gate failure" in trigger:
            return BOOT_FAILED_EVENT
        return None

    if to_ps is PrimaryState.PAUSED:
        if "cost 100%" in trigger:
            return PAUSED_COST_LIMIT_EVENT
        return None

    return None


def _build_payload(
    from_state: OperationalState, to_state: OperationalState, trigger: str
) -> dict[str, Any]:
    """Build the chain-event payload.

    Shape (matches the spec ST2 §3 — kept stable for cockpit consumers):
      - ``from_primary_state``, ``to_primary_state`` — enum value strings
      - ``claim_permission`` — the NEW claim permission
      - ``degradation_reasons`` — the NEW set, sorted alphabetically
      - ``trigger`` — the human-readable trigger string forwarded from
        ``transition_to``
    """
    return {
        "from_primary_state": from_state.primary_state.value,
        "to_primary_state": to_state.primary_state.value,
        "claim_permission": to_state.claim_permission.value,
        "degradation_reasons": sorted(
            r.value for r in to_state.degradation_reasons
        ),
        "trigger": trigger,
    }


async def emit_state_transition(
    provider: Any,
    from_state: OperationalState,
    to_state: OperationalState,
    trigger: str,
) -> None:
    """Emit chain events for a single transition.

    Always emits :data:`GENERIC_TRANSITION_EVENT`; conditionally emits
    one of the per-trigger literals when applicable. The two events
    are emitted sequentially so the substrate-side ordering matches
    the operator-facing "this is the transition, here's the
    informational subtype" reading.

    Raises:
        OperationalStateEmitError: if the IsoKron provider is not
            initialized OR if the substrate-side append_event call
            raises (Sea MCP unavailable, CHECK violation, chain lock
            failure, etc.).
    """
    if provider is None:
        raise OperationalStateEmitError(
            GENERIC_TRANSITION_EVENT,
            "IsoKron provider is None; cannot emit operational-state event",
        )

    connection = getattr(provider, "_connection", None)
    if connection is None:
        raise OperationalStateEmitError(
            GENERIC_TRANSITION_EVENT,
            "IsoKron connection not initialized",
        )

    try:
        workspace_id = provider._resolve_workspace_id()
    except Exception as exc:
        raise OperationalStateEmitError(
            GENERIC_TRANSITION_EVENT,
            f"workspace_id resolution raised: {exc!r}",
        ) from exc
    if not workspace_id:
        raise OperationalStateEmitError(
            GENERIC_TRANSITION_EVENT,
            "workspace_id is None/empty after resolution",
        )

    payload = _build_payload(from_state, to_state, trigger)
    extra_event = _select_extra_literal(from_state, to_state, trigger)

    # Lazy import — avoids pulling the MCP client into the agent
    # import tree at module load.
    from plugins.memory.isokron.events import emit_kora_event

    mcp_client = connection.get_mcp_client()

    # Submit each event to the IsoKron dedicated IO loop, wrap the
    # resulting concurrent.futures.Future so we can await it from
    # this agent-side event loop.
    async def _submit(event_type: str) -> str:
        future = connection._submit_async(
            emit_kora_event(
                workspace_id=workspace_id,
                event_type=event_type,
                payload=payload,
                mcp_client=mcp_client,
            )
        )
        return await asyncio.wrap_future(future)

    try:
        event_id = await _submit(GENERIC_TRANSITION_EVENT)
    except Exception as exc:
        logger.error(
            "[%s] emit failed for transition %s → %s "
            "(trigger=%r): %r",
            GENERIC_TRANSITION_EVENT,
            from_state.primary_state.value,
            to_state.primary_state.value,
            trigger,
            exc,
        )
        raise OperationalStateEmitError(
            GENERIC_TRANSITION_EVENT, repr(exc)
        ) from exc

    logger.info(
        "[%s] emitted for %s → %s (trigger=%r) → event_id=%s",
        GENERIC_TRANSITION_EVENT,
        from_state.primary_state.value,
        to_state.primary_state.value,
        trigger,
        event_id,
    )

    if extra_event is None:
        return

    try:
        extra_id = await _submit(extra_event)
    except Exception as exc:
        logger.error(
            "[%s] supplementary emit failed for transition %s → %s "
            "(trigger=%r): %r. Generic event was emitted; cockpit will "
            "still show the transition but the informational subtype "
            "is missing.",
            extra_event,
            from_state.primary_state.value,
            to_state.primary_state.value,
            trigger,
            exc,
        )
        raise OperationalStateEmitError(extra_event, repr(exc)) from exc

    logger.info(
        "[%s] supplementary emit for %s → %s → event_id=%s",
        extra_event,
        from_state.primary_state.value,
        to_state.primary_state.value,
        extra_id,
    )


def make_emit_listener(provider: Any) -> StateTransitionListener:
    """Build a :class:`StateTransitionListener` bound to ``provider``.

    Wire-in (ST3): after the IsoKronMemoryProvider's connection has
    started, the agent calls ``holder.add_listener(make_emit_listener(provider))``
    once. From then on, every transition that goes through the holder
    emits the generic event (and the matching per-trigger literal when
    applicable).
    """

    async def _listener(
        from_state: OperationalState,
        to_state: OperationalState,
        trigger: str,
    ) -> None:
        await emit_state_transition(provider, from_state, to_state, trigger)

    return _listener
