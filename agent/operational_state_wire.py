"""Boot-time wire-in for the operational-state holder (KR-P2-I-integration ST3).

Glues the three pieces shipped earlier in the bucket:

* :mod:`agent.operational_state` — the immutable state shape +
  transition table (KR-P2-I-skeleton, PR #27).
* :mod:`agent.operational_state_holder` — the live state holder +
  transition broker (ST1, this bucket).
* :mod:`agent.operational_state_emit` — the chain-event listener
  (ST2, this bucket).

The wire-in runs once per agent boot, after the
:class:`~plugins.memory.isokron.IsoKronMemoryProvider` connection has
started. It:

1. Initializes the module-level holder with an initial state of
   ``BOOTING`` + ``ClaimPermission.NONE``.
2. Registers the chain-event emit listener bound to the live provider.
3. Triggers the ``BOOTING → READY`` transition — **unconditional in
   v1**. The R4.1 §9.2 gate-check guard (which decides whether to go
   ``READY`` vs. fall through to ``STOPPED`` based on invariant gates)
   is the KR-P2-H follow-on bucket; it has not landed yet because it
   depends on substrate-round Bucket C event vocab.

Failure isolation:

* The transition itself cannot fail — ``BOOTING → READY`` is a valid
  row in :data:`~agent.operational_state.TRANSITION_TABLE`.
* If the emit listener raises during the transition,
  :class:`~agent.operational_state_holder.OperationalStateHolder`
  catches and logs the exception so a broken emit can't break boot.
* The wire-in helper additionally catches any unexpected exception
  so a holder/import/runtime issue does not abort the agent — the
  provider stays functional, only the observability surface is
  degraded. The exception is logged with a recognizable tag so
  operators can grep for it.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# Trigger string is taken verbatim from
# :data:`agent.operational_state.TRANSITION_TABLE` — the (BOOTING, READY)
# row uses this exact wording. Match-discipline matters: ST2's emit
# listener selects ``kora.boot.ready`` when ``"all §9.2 gates pass"``
# appears in the trigger; keep these strings literal-equal.
_BOOT_READY_TRIGGER = "all §9.2 gates pass"


def wire_operational_state(provider: Any) -> None:
    """Run the boot-time operational-state wire-in.

    ``provider`` is the live :class:`IsoKronMemoryProvider` — already
    initialized, with ``_connection.start()`` returned. The function
    catches every exception so that no failure here aborts agent boot;
    instead operators get a single, greppable WARNING line.
    """
    try:
        # Lazy imports — agent_init.py runs early and we want the
        # operational-state modules off the import critical path for
        # any agent that doesn't carry the IsoKron provider.
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
                "_connection; cannot run initial BOOTING → READY "
                "transition. Holder stays in BOOTING."
            )
            return

        # Run the async transition on the IsoKron dedicated IO loop
        # via submit_and_wait — agent_init.py is synchronous, so we
        # can't `await` here. submit_and_wait drives the coro on the
        # connection's worker thread and blocks the boot thread until
        # the listener (emit) returns.
        connection.submit_and_wait(
            holder.transition_to(
                PrimaryState.READY,
                trigger=_BOOT_READY_TRIGGER,
                new_claim_permission=ClaimPermission.NORMAL,
            ),
            timeout=15.0,
        )
        logger.info(
            "[kora.operational_state.wire_in] BOOTING → READY "
            "transitioned; emit listener registered."
        )

    except Exception as exc:
        # Catch-all so a broken wire-in cannot abort agent boot.
        # KR-P2-H follow-on revisits this: gate guards may want
        # certain wire-in failures to BLOCK boot (e.g. fail-LOUD on
        # substrate unreachable). For ST3 we ship the
        # don't-block-boot posture.
        logger.warning(
            "[kora.operational_state.wire_in] wire-in raised %r — "
            "agent boot continues but operational-state observability "
            "is degraded (holder may be uninitialized or stuck in "
            "BOOTING). KR-P2-H gate guards will revisit this path.",
            exc,
        )
