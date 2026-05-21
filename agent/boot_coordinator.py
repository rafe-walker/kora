"""Boot-sequence coordinator (KR-P2-H ST3).

Glues the framework (ST1) + concrete gates (ST2) + operational-state
holder (KR-P2-I-integration) + chain emit. Per R4.1 §9.2 / §9.3:

  - All gates pass → ``BOOTING → READY`` + ``kora.boot.ready`` emit.
  - INVARIANT or TRANSIENT-budget-exhausted fail → ``BOOTING → STOPPED``
    + ``kora.boot.failed`` emit. Caller decides whether to ``sys.exit``.
  - TRANSIENT fail with budget remaining → stage the per-gate
    :class:`DegradationReason` on the holder, then retry per the
    framework's backoff schedule. On eventual PASS, the staged reason
    is removed.

The coordinator returns a :class:`BootSummary` describing the outcome.
The caller (wire-in in production; CLI in ``kora boot --check-only``)
decides what to do with it.

# Rich chain-event payload

``kora.boot.ready`` / ``kora.boot.failed`` emitted by this coordinator
carry the full ``gate_results`` list (each gate's outcome + elapsed_ms
+ attempts + detail) for operator triage. The
:mod:`agent.operational_state_emit` listener — which fires
*additionally* on the holder transition — emits the generic
``kora.operational_state.transitioned`` event with minimal payload.
Both are useful: the rich event answers "which gate failed and why";
the generic event answers "what's the current state of the machine."

The coordinator uses a trigger string for ``transition_to`` that
**does not** match the listener's per-trigger literal rules
(``"all §9.2 gates pass"`` / ``"invariant gate failure"`` substrings),
so the listener emits ONLY the generic ``kora.operational_state.transitioned``
event and the rich ``kora.boot.{ready,failed}`` emit is the
coordinator's responsibility.

# Diagnostic mode

When ``diagnostic_mode=True``:
  - No holder transitions
  - No chain-event emits
  - No degradation-reason staging during retry
  - All gates run once (no retry), all results returned
  - The :class:`BootSummary` still reports READY / STOPPED based on
    whether any gate FAILed

For ``kora boot --check-only`` (ST4) and CI smoke checks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Final, Mapping, Optional

from agent.boot_gates import (
    BootContext,
    BootGateRunner,
    Gate,
    GateOutcome,
    GateResult,
)
from agent.boot_gates_impl import build_default_gate_sequence
from agent.operational_state import DegradationReason, PrimaryState

if TYPE_CHECKING:
    from agent.operational_state_holder import OperationalStateHolder

logger = logging.getLogger(__name__)


# Event-type literals. PM-verified against
# foundation/0159_kora_r41_operational_state_event_vocabulary.sql.
BOOT_READY_EVENT: Final[str] = "kora.boot.ready"
BOOT_FAILED_EVENT: Final[str] = "kora.boot.failed"


# Per-gate degradation reason staged on the holder while a TRANSIENT
# gate is retrying. On successful retry, the reason is removed. On
# budget exhaustion, the reason becomes moot (holder goes to STOPPED).
#
# Mapping per bucket spec ST3.
_GATE_RETRY_REASON: Final[Mapping[str, DegradationReason]] = {
    "1_claude_auth": DegradationReason.AUTH,
    "4_kora_runtime_role_perms": DegradationReason.DISPATCH,
    "5_kronicle_mcp_reachable": DegradationReason.SUBSTRATE,
    "6_wsk_token_valid": DegradationReason.TOKEN_EXPIRING,
    "7_canonical_kora_actor": DegradationReason.SUBSTRATE,
    "8_charter_capability_matrix_load": DegradationReason.SUBSTRATE,
    "10_kr7_boot_smoke": DegradationReason.DISPATCH,
}


# Trigger strings — pinned to NOT match the operational_state_emit
# listener's per-trigger literal rules so the listener emits only the
# generic ``kora.operational_state.transitioned`` event. The rich
# ``kora.boot.{ready,failed}`` emit is the coordinator's responsibility.
_TRIGGER_BOOT_READY: Final[str] = (
    "boot gates passed (see kora.boot.ready event for per-gate detail)"
)
_TRIGGER_BOOT_FAILED: Final[str] = (
    "boot gate failed (see kora.boot.failed event for per-gate detail)"
)
_TRIGGER_BOOT_RETRY: Final[str] = (
    "transient boot gate retrying"
)


class BootResult(Enum):
    """Overall boot outcome."""

    READY = "ready"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class BootSummary:
    """Result of one boot sequence run.

    ``result`` is READY iff every gate in the sequence returned
    :data:`GateOutcome.PASS`. STOPPED otherwise. ``failed_gate`` is the
    first FAIL result in production mode (the sequence short-circuits
    there) or any FAIL result in diagnostic mode.
    """

    result: BootResult
    gate_results: list[GateResult]
    failed_gate: Optional[GateResult]


async def run_boot_sequence(
    *,
    memory_provider: Any,
    holder: Optional["OperationalStateHolder"] = None,
    gates: Optional[list[Gate]] = None,
    diagnostic_mode: bool = False,
) -> BootSummary:
    """Run the boot gate sequence and apply the result.

    Args:
        memory_provider: ``IsoKronMemoryProvider`` (or ``None`` —
            substrate-dependent gates will FAIL).
        holder: ``OperationalStateHolder`` for transitions. Required in
            production mode; may be ``None`` in diagnostic mode.
        gates: Override the default gate sequence (test injection).
            ``None`` → :func:`build_default_gate_sequence`.
        diagnostic_mode: If ``True``: no holder transitions, no emits,
            no retry, no degradation staging. All gates run once.

    Returns:
        :class:`BootSummary` — the caller decides whether to ``sys.exit``
        / continue / report.
    """
    if not diagnostic_mode and holder is None:
        raise ValueError(
            "holder is required in production mode "
            "(diagnostic_mode=False)"
        )

    selected_gates = list(gates) if gates is not None else build_default_gate_sequence()
    context = BootContext(memory_provider=memory_provider, holder=holder)

    # Track reasons staged during retry so we can remove them on
    # successful boot. Mutable set captured by the on_retry_attempt
    # closure below.
    staged_reasons: set[DegradationReason] = set()

    async def _on_retry_attempt(
        gate: Gate, last_result: GateResult, attempt: int, max_attempts: int
    ) -> None:
        """Stage the per-gate degradation reason during retry."""
        if diagnostic_mode or holder is None:
            return
        reason = _GATE_RETRY_REASON.get(gate.gate_id)
        if reason is None or reason in staged_reasons:
            return
        staged_reasons.add(reason)
        await holder.transition_to(
            PrimaryState.BOOTING,
            trigger=(
                f"{_TRIGGER_BOOT_RETRY} (gate={gate.gate_id}, "
                f"attempt={attempt}/{max_attempts})"
            ),
            add_reasons={reason},
        )

    runner = BootGateRunner(
        gates=selected_gates,
        context=context,
        diagnostic_mode=diagnostic_mode,
        on_retry_attempt=None if diagnostic_mode else _on_retry_attempt,
    )

    results = await runner.run_all()

    failed = _first_failed(results)

    if diagnostic_mode:
        return BootSummary(
            result=BootResult.STOPPED if failed else BootResult.READY,
            gate_results=results,
            failed_gate=failed,
        )

    # Production mode: transition holder + emit chain event.
    assert holder is not None  # guarded above
    if failed is None:
        # All-pass path
        if staged_reasons:
            # Remove the transient reasons we staged during retry.
            await holder.transition_to(
                PrimaryState.BOOTING,
                trigger="boot gates eventually passed; clearing transient reasons",
                remove_reasons=staged_reasons,
            )
        await holder.transition_to(
            PrimaryState.READY,
            trigger=_TRIGGER_BOOT_READY,
        )
        await _emit_boot_event(
            memory_provider, BOOT_READY_EVENT, results, None
        )
        return BootSummary(
            result=BootResult.READY,
            gate_results=results,
            failed_gate=None,
        )

    # Failed path — INVARIANT FAIL or TRANSIENT budget exhausted.
    await holder.transition_to(
        PrimaryState.STOPPED,
        trigger=(
            f"{_TRIGGER_BOOT_FAILED} (gate={failed.gate_id}, "
            f"class={failed.gate_class.value})"
        ),
    )
    await _emit_boot_event(
        memory_provider, BOOT_FAILED_EVENT, results, failed
    )
    return BootSummary(
        result=BootResult.STOPPED,
        gate_results=results,
        failed_gate=failed,
    )


def _first_failed(results: list[GateResult]) -> Optional[GateResult]:
    """Return the first FAIL result in the list, or ``None``."""
    for r in results:
        if r.outcome is GateOutcome.FAIL:
            return r
    return None


async def _emit_boot_event(
    memory_provider: Any,
    event_type: str,
    results: list[GateResult],
    failed: Optional[GateResult],
) -> None:
    """Emit ``kora.boot.{ready,failed}`` with the rich gate-results payload.

    Failure mode: WARN-log + return. The chain emit is best-effort
    observability; a substrate-side hiccup during boot shouldn't
    prevent the holder from already having transitioned. The cockpit's
    operational-state view still works via the generic transition emit
    fired by ``operational_state_emit``'s listener.
    """
    if memory_provider is None:
        logger.warning(
            "[%s] memory_provider is None; cannot emit. Generic "
            "transition event still fired by listener.",
            event_type,
        )
        return

    connection = getattr(memory_provider, "_connection", None)
    if connection is None:
        logger.warning(
            "[%s] memory_provider._connection is None; cannot emit. "
            "Generic transition event still fired by listener.",
            event_type,
        )
        return

    try:
        workspace_id = memory_provider._resolve_workspace_id()
    except Exception as exc:
        logger.warning(
            "[%s] workspace_id resolution raised: %r; cannot emit.",
            event_type,
            exc,
        )
        return
    if not workspace_id:
        logger.warning(
            "[%s] workspace_id unresolved; cannot emit.", event_type
        )
        return

    payload = _build_boot_event_payload(results, failed)

    # Lazy import — keeps the runtime emit path off the boot-coordinator
    # import critical path.
    from plugins.memory.isokron.events import emit_kora_event

    try:
        mcp_client = connection.get_mcp_client()
    except Exception as exc:
        logger.warning(
            "[%s] get_mcp_client raised: %r; cannot emit.",
            event_type,
            exc,
        )
        return

    try:
        event_id = connection.submit_and_wait(
            emit_kora_event(
                workspace_id=workspace_id,
                event_type=event_type,
                payload=payload,
                mcp_client=mcp_client,
            ),
            timeout=10.0,
        )
    except Exception as exc:
        logger.warning(
            "[%s] emit raised: %r — proceeding without rich audit; "
            "generic transition event still fired by listener.",
            event_type,
            exc,
        )
        return

    logger.info(
        "[%s] emitted; event_id=%s; gates=%d failed=%s",
        event_type,
        event_id,
        len(results),
        failed.gate_id if failed else "none",
    )


def _build_boot_event_payload(
    results: list[GateResult], failed: Optional[GateResult]
) -> dict[str, Any]:
    """Build the chain-event payload for ``kora.boot.{ready,failed}``.

    Shape — kept stable for cockpit consumers + operator triage:

      - ``gates`` — list of per-gate dicts with the fields the operator
        playbook recommends grepping (gate_id, class, outcome,
        attempts, elapsed_ms, detail)
      - ``failed_gate_id`` + ``failed_detail`` (only on FAILED events)
    """
    payload: dict[str, Any] = {
        "gates": [
            {
                "gate_id": r.gate_id,
                "gate_class": r.gate_class.value,
                "outcome": r.outcome.value,
                "attempts": r.attempts,
                "elapsed_ms": r.elapsed_ms,
                "detail": r.detail,
            }
            for r in results
        ],
    }
    if failed is not None:
        payload["failed_gate_id"] = failed.gate_id
        payload["failed_detail"] = failed.detail
    return payload
