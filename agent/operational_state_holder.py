"""Live ``OperationalState`` holder + transition broker (KR-P2-I-integration ST1).

The skeleton at :mod:`agent.operational_state` ships the immutable
state shape + transition table + query helpers; it has no notion of
"what is Kora's current state right now." This module adds that.

Design notes
============

* **Singleton via module-level accessor.** :func:`init_holder` is
  idempotent — first call constructs the holder with the supplied
  initial state, subsequent calls are no-ops. This mirrors how the
  IsoKronMemoryProvider singleton is accessed (one provider per
  process; tests reset via the dedicated helper).
* **Serial transitions via asyncio.Lock.** Every ``transition_to``
  call acquires the holder lock so concurrent transitions can't race
  on the held state. The lock is released BEFORE listeners fire to
  avoid deadlock if a listener tries to read the holder (or itself
  transitions — though re-entrant transitions are not a supported
  pattern).
* **Transitions validated against TRANSITION_TABLE.** The §9.1 table
  is the source of truth; ``transition_to`` raises
  :class:`InvalidStateTransitionError` when called with a (from, to)
  pair that has no row. Same-state calls (degradation-reason updates
  with no primary_state change) skip the table check, since R4.1 §9.1
  models DEGRADED as a flag, not a primary_state edge.
* **Listeners are observability, not policy.** A listener exception
  is logged but does not roll the held state back. Policy-critical
  emit (the ST2 chain-event listener) raises loudly inside the
  listener itself so the operator sees the failure in logs — but the
  state machine keeps moving.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from agent.operational_state import (
    ClaimPermission,
    DegradationReason,
    OperationalState,
    PrimaryState,
    is_valid_transition,
)

logger = logging.getLogger(__name__)


StateTransitionListener = Callable[
    [OperationalState, OperationalState, str], Awaitable[None]
]
"""Listener signature: ``(old_state, new_state, trigger) -> Awaitable[None]``.

Listeners are called once per successful transition, after the held
state has been swapped to ``new_state`` and the lock released."""


# Last N transitions retained in-memory for the admin panel's
# "recent transitions" view. Durable history lives in the chain-event
# log (every transition writes ``kora.operational_state.transitioned``
# via ST2's emit listener); this ring is for the cockpit's
# immediate-history rendering and only survives until process restart.
_HISTORY_RING_SIZE = 10


@dataclass(frozen=True, slots=True)
class TransitionRecord:
    """One row of the in-memory transition history.

    Field shape matches the dict the ``/api/operational-state``
    endpoint returns under ``transition_history`` — kept small so
    the admin-panel payload stays trim.
    """

    timestamp: str  # ISO-8601 UTC, e.g. "2026-05-21T17:00:00Z"
    from_state: str  # PrimaryState.value
    to_state: str
    trigger: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "trigger": self.trigger,
        }


class InvalidStateTransitionError(ValueError):
    """Raised by :meth:`OperationalStateHolder.transition_to` when the
    requested ``(from_primary_state, to_primary_state)`` pair is not
    present in :data:`agent.operational_state.TRANSITION_TABLE`.

    Same-state calls (primary_state unchanged) do not raise — see the
    module docstring.
    """


class OperationalStateHolder:
    """Holds the live :class:`OperationalState` and brokers transitions.

    Constructed via :func:`init_holder`; accessed via :func:`get_holder`.
    Not meant to be instantiated directly outside tests.
    """

    def __init__(self, initial_state: OperationalState) -> None:
        self._state: OperationalState = initial_state
        self._lock: asyncio.Lock = asyncio.Lock()
        self._listeners: list[StateTransitionListener] = []
        # Ring buffer for the admin panel's recent-transitions view.
        # Append-on-transition under the holder lock; read via
        # ``history()`` (returns a snapshot list).
        self._history: deque[TransitionRecord] = deque(
            maxlen=_HISTORY_RING_SIZE
        )

    @property
    def current(self) -> OperationalState:
        """Return the current state snapshot.

        Safe to read without holding the lock — :class:`OperationalState`
        is frozen, so the returned reference cannot be mutated by a
        concurrent ``transition_to``.
        """
        return self._state

    async def transition_to(
        self,
        new_primary_state: PrimaryState,
        trigger: str,
        *,
        new_claim_permission: Optional[ClaimPermission] = None,
        add_reasons: Optional[set[DegradationReason]] = None,
        remove_reasons: Optional[set[DegradationReason]] = None,
    ) -> OperationalState:
        """Validate, apply, notify listeners. Return the new state.

        Validation:
          * If ``new_primary_state`` differs from the current primary
            state, ``(from, to)`` must be in
            :data:`~agent.operational_state.TRANSITION_TABLE`;
            otherwise raises :class:`InvalidStateTransitionError`.
          * If ``new_primary_state`` equals the current primary state,
            no transition-table check — degradation-reason and
            claim-permission updates are unconditionally allowed.

        Listener semantics:
          * Listeners fire AFTER the held state is swapped and the
            holder lock is released. The trigger string is forwarded
            unchanged.
          * A listener exception is logged but does NOT roll back the
            transition. Listeners are observability, not policy.
        """
        async with self._lock:
            old_state = self._state

            if new_primary_state is not old_state.primary_state:
                if not is_valid_transition(
                    old_state.primary_state, new_primary_state
                ):
                    raise InvalidStateTransitionError(
                        f"Invalid transition: "
                        f"{old_state.primary_state.value} → "
                        f"{new_primary_state.value} is not in "
                        f"TRANSITION_TABLE (trigger={trigger!r})"
                    )

            new_state = old_state.with_primary_state(new_primary_state)
            if new_claim_permission is not None:
                new_state = new_state.with_claim_permission(
                    new_claim_permission
                )
            for reason in add_reasons or ():
                new_state = new_state.with_added_reason(reason)
            for reason in remove_reasons or ():
                new_state = new_state.with_removed_reason(reason)

            self._state = new_state

            # Record the transition in the in-memory ring buffer. We
            # append under the lock so the buffer ordering matches
            # the held-state swap. Listeners read the buffer outside
            # the lock — that's fine, deque appends are atomic.
            self._history.append(
                TransitionRecord(
                    timestamp=datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                    from_state=old_state.primary_state.value,
                    to_state=new_state.primary_state.value,
                    trigger=trigger,
                )
            )

        # Fire listeners outside the lock so a listener that reads the
        # holder (or — defensively — calls transition_to) doesn't
        # deadlock.
        for listener in list(self._listeners):
            try:
                await listener(old_state, new_state, trigger)
            except Exception:
                logger.exception(
                    "[OperationalStateHolder] listener %r raised on "
                    "transition %s → %s (trigger=%r). One broken "
                    "listener does not block the others.",
                    listener,
                    old_state.primary_state.value,
                    new_state.primary_state.value,
                    trigger,
                )

        return new_state

    def history(self, *, limit: int = _HISTORY_RING_SIZE) -> list[dict[str, Any]]:
        """Return the most recent ``limit`` transitions as a list of dicts.

        Ordered oldest → newest (matches the deque iteration order),
        so the admin panel can render them top-to-bottom without
        reversing. ``limit`` is clamped to the actual ring size; the
        durable history lives in the chain-event log.
        """
        # Snapshot via ``list()`` so the caller can't mutate our deque.
        records = list(self._history)
        if limit is not None and limit < len(records):
            records = records[-limit:]
        return [r.to_dict() for r in records]

    def add_listener(self, listener: StateTransitionListener) -> None:
        """Register a listener fired after every successful transition.

        Order is insertion order. There is no remove_listener — the
        runtime registers a fixed set of listeners at boot and tears
        them down at process exit. Tests use
        :func:`_reset_holder_for_tests` to start fresh.
        """
        self._listeners.append(listener)


_HOLDER: Optional[OperationalStateHolder] = None


def init_holder(initial_state: OperationalState) -> OperationalStateHolder:
    """Initialize the process-wide holder. Idempotent: subsequent calls
    return the existing instance and ignore ``initial_state``.

    Typical first call sits at the end of IsoKronMemoryProvider boot
    (ST3) with
    ``OperationalState(primary_state=BOOTING, claim_permission=NONE)``.
    """
    global _HOLDER
    if _HOLDER is None:
        _HOLDER = OperationalStateHolder(initial_state)
    return _HOLDER


def get_holder() -> Optional[OperationalStateHolder]:
    """Return the live holder, or ``None`` if :func:`init_holder` has
    not been called yet (e.g. boot incomplete, or the IsoKron provider
    failed to construct).
    """
    return _HOLDER


def _reset_holder_for_tests() -> None:
    """Clear the singleton. Tests only — production code never calls this."""
    global _HOLDER
    _HOLDER = None
