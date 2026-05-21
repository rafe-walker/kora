"""Boot-time wire-in for the operational-state holder (KR-P2-I-integration ST3,
updated by KR-P2-H ST3).

Glues the operational-state pieces shipped earlier:

* :mod:`agent.operational_state` — immutable state shape + transition
  table (KR-P2-I-skeleton, PR #27).
* :mod:`agent.operational_state_holder` — live state holder + transition
  broker (KR-P2-I-integration ST1).
* :mod:`agent.operational_state_emit` — chain-event listener
  (KR-P2-I-integration ST2).
* :mod:`agent.boot_coordinator` — gate sequence + transition
  orchestration (**KR-P2-H ST3 — this update**).

The wire-in runs once per agent boot, after the
:class:`~plugins.memory.isokron.IsoKronMemoryProvider` connection has
started. It:

1. Initializes the module-level holder with an initial state of
   ``BOOTING`` + ``ClaimPermission.NONE``.
2. Registers the chain-event emit listener bound to the live provider.
3. Runs the R4.1 §9.2 boot gate sequence via
   :func:`agent.boot_coordinator.run_boot_sequence`. The coordinator
   transitions the holder to ``READY`` on all-pass and to ``STOPPED``
   on any invariant fail or transient budget exhaustion, and emits
   the rich ``kora.boot.{ready,failed}`` chain event with the per-gate
   result list.

# KR-P2-H ST3 change summary

This file previously triggered an **unconditional** ``BOOTING → READY``
transition (KR-P2-I-integration ST3 placeholder). KR-P2-H ST3 replaces
that with the explicit gate-check guard described above. The
unconditional transition is gone; if the gate sequence fails, the
holder ends in ``STOPPED`` and the process exits non-zero.

Failure isolation:

* If any boot gate fails (invariant fail or transient budget exhausted),
  the coordinator transitions the holder to ``STOPPED`` and the wire-in
  calls ``sys.exit(1)``. Per bucket spec § ST3, non-zero exit on
  STOPPED is mandatory — the chain emit + holder transition precede
  the exit, so the audit trail lands first.
* Other exceptions (import failure, coordinator implementation bug)
  are caught + logged so a broken wire-in doesn't abort the agent in
  an inconsistent state. ``SystemExit`` is NOT caught — it propagates
  so the process exit fires.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)


def wire_operational_state(provider: Any) -> None:
    """Run the boot-time operational-state wire-in.

    ``provider`` is the live :class:`IsoKronMemoryProvider` — already
    initialized, with ``_connection.start()`` returned.

    On STOPPED outcome (any boot gate failed), calls ``sys.exit(1)`` —
    per R4.1 §9.2 / bucket spec § ST3. The chain emit + holder
    transition precede the exit, so the audit trail is complete
    before the process terminates.

    Other exceptions (broken imports, coordinator bugs) are caught +
    logged so the agent doesn't abort in an inconsistent state.
    ``SystemExit`` is NOT caught — it propagates so the process
    exit fires.
    """
    try:
        # Lazy imports — keeps boot-coordinator + operational-state
        # modules off the import critical path for any agent that
        # doesn't carry the IsoKron provider.
        from agent.boot_coordinator import BootResult, run_boot_sequence
        from agent.operational_state import (
            ClaimPermission,
            OperationalState,
            PrimaryState,
        )
        from agent.operational_state_emit import make_emit_listener
        from agent.operational_state_holder import init_holder

        initial = OperationalState(
            primary_state=PrimaryState.BOOTING,
            claim_permission=ClaimPermission.NONE,
        )
        holder = init_holder(initial)
        holder.add_listener(make_emit_listener(provider))

        connection = getattr(provider, "_connection", None)
        if connection is None:
            logger.warning(
                "[kora.operational_state.wire_in] provider has no "
                "_connection; cannot run boot gate sequence. Holder "
                "stays in BOOTING. Operator must intervene — the "
                "agent is in an unrunnable state."
            )
            return

        # Run the boot coordinator on the IsoKron dedicated IO loop
        # via submit_and_wait — agent_init.py is synchronous, so we
        # can't ``await`` here. The coordinator handles transitions +
        # chain emit; this thread blocks until the sequence completes.
        #
        # 120s timeout covers a worst-case 7-gate sequence with full
        # retry budget (5 attempts × 30s backoff cap per transient
        # gate) — generous so a slow substrate handshake doesn't
        # trigger a boot-side timeout that the runner can't observe.
        summary = connection.submit_and_wait(
            run_boot_sequence(
                memory_provider=provider,
                holder=holder,
                diagnostic_mode=False,
            ),
            timeout=120.0,
        )

        if summary.result is BootResult.READY:
            logger.info(
                "[kora.operational_state.wire_in] boot gates passed; "
                "holder transitioned to READY. Gates: %s",
                [
                    f"{r.gate_id}({r.outcome.value})"
                    for r in summary.gate_results
                ],
            )
            # Bump claim_permission to NORMAL now that gates passed.
            # The coordinator's READY transition doesn't touch
            # claim_permission; it stays at NONE from the BOOTING
            # initial state. Bump here so the consumer loop can mint
            # claims.
            connection.submit_and_wait(
                holder.transition_to(
                    PrimaryState.READY,
                    trigger="ready + claim_permission bump",
                    new_claim_permission=ClaimPermission.NORMAL,
                ),
                timeout=15.0,
            )
            return

        if summary.result is BootResult.PAUSED:
            # KR-P2-M ST3 — Gate 3b epoch mismatch routed to PAUSED
            # (R4.1 §9.2 / §9.8 special-case). Gate 3b already emitted
            # kora.dr.observed + transitioned the holder to
            # PAUSED{substrate}. Process stays running; operator must
            # clear via cockpit kora_control reset (KR-P2-J reader
            # observes the clearance + KR-P2-M ST4 writes the new
            # kora_known_epoch).
            failed = summary.failed_gate
            logger.warning(
                "[kora.operational_state.wire_in] boot gate routed to "
                "PAUSED: gate_id=%s class=%s detail=%s. Holder is in "
                "PAUSED{substrate}; process stays running for operator "
                "clearance via cockpit kora_control reset. See "
                "kora.dr.observed chain event for the (observed, known) "
                "epoch pair.",
                failed.gate_id if failed else "<unknown>",
                failed.gate_class.value if failed else "<unknown>",
                failed.detail if failed else "<no failed_gate>",
            )
            return

        # BootResult.STOPPED — coordinator already transitioned
        # holder to STOPPED and emitted kora.boot.failed. Per bucket
        # spec § ST3, exit non-zero.
        failed = summary.failed_gate
        logger.error(
            "[kora.operational_state.wire_in] boot gate failed: "
            "gate_id=%s class=%s attempts=%d detail=%s. Holder is "
            "in STOPPED; process exiting with non-zero status. See "
            "kora.boot.failed chain event payload for full GateResult "
            "list.",
            failed.gate_id if failed else "<unknown>",
            failed.gate_class.value if failed else "<unknown>",
            failed.attempts if failed else 0,
            failed.detail if failed else "<no failed_gate>",
        )
        sys.exit(1)

    except SystemExit:
        # Propagate — that's how we exit non-zero on STOPPED.
        raise
    except Exception as exc:
        # Catch-all so a broken wire-in cannot abort agent boot in an
        # INCONSISTENT state. Distinct from the STOPPED-exit above:
        # this branch is for programmer-error / import failures (the
        # coordinator itself never raises — it surfaces failures via
        # BootSummary).
        logger.warning(
            "[kora.operational_state.wire_in] wire-in raised %r — "
            "agent boot continues but operational-state observability "
            "is degraded (holder may be uninitialized or stuck in "
            "BOOTING). The boot-gate sequence did NOT complete.",
            exc,
        )
