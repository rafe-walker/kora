"""Unit tests for ``agent/operational_state_holder.py`` (KR-P2-I-integration ST1).

Covers:
  - Construction + ``current`` property
  - ``transition_to`` validates against TRANSITION_TABLE; raises on bad arrows
  - Same-primary-state calls bypass the table check (degradation updates)
  - ``new_claim_permission`` and ``add_reasons`` / ``remove_reasons`` apply
  - Listeners fire after the held state is updated, with the right args
  - A listener exception does not block subsequent listeners nor roll the state back
  - Concurrent ``transition_to`` calls serialize via the asyncio lock
  - Module-level ``init_holder`` is idempotent; ``get_holder`` reads the singleton
"""

from __future__ import annotations

import asyncio

import pytest

from agent.operational_state import (
    ClaimPermission,
    DegradationReason,
    OperationalState,
    PrimaryState,
)
from agent.operational_state_holder import (
    InvalidStateTransitionError,
    OperationalStateHolder,
    _reset_holder_for_tests,
    get_holder,
    init_holder,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Ensure each test starts with a fresh module-level holder."""
    _reset_holder_for_tests()
    yield
    _reset_holder_for_tests()


def _booting_state() -> OperationalState:
    return OperationalState(
        primary_state=PrimaryState.BOOTING,
        claim_permission=ClaimPermission.NONE,
    )


# ---------------------------------------------------------------------------
# Construction + current
# ---------------------------------------------------------------------------


def test_current_returns_initial_state():
    holder = OperationalStateHolder(_booting_state())
    assert holder.current.primary_state is PrimaryState.BOOTING
    assert holder.current.claim_permission is ClaimPermission.NONE
    assert holder.current.degradation_reasons == frozenset()


# ---------------------------------------------------------------------------
# transition_to — validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transition_to_valid_arrow_updates_state():
    holder = OperationalStateHolder(_booting_state())

    new = await holder.transition_to(
        PrimaryState.READY,
        trigger="all §9.2 gates pass",
        new_claim_permission=ClaimPermission.NORMAL,
    )

    assert new.primary_state is PrimaryState.READY
    assert new.claim_permission is ClaimPermission.NORMAL
    assert holder.current is new


@pytest.mark.asyncio
async def test_transition_to_invalid_arrow_raises():
    """READY → BOOTING has no row in TRANSITION_TABLE; must raise
    rather than silently apply (operator would never know)."""
    holder = OperationalStateHolder(
        OperationalState(
            primary_state=PrimaryState.READY,
            claim_permission=ClaimPermission.NORMAL,
        )
    )

    with pytest.raises(InvalidStateTransitionError) as exc_info:
        await holder.transition_to(PrimaryState.BOOTING, trigger="oops")

    assert "ready → booting" in str(exc_info.value)
    # State unchanged.
    assert holder.current.primary_state is PrimaryState.READY


@pytest.mark.asyncio
async def test_same_primary_state_bypasses_table_check_for_degradation_update():
    """R4.1 §9.1: DEGRADED is the presence of degradation_reasons, not
    a primary_state edge. Adding a degradation reason while READY stays
    READY must NOT raise — there is no (READY, READY) row in the table."""
    holder = OperationalStateHolder(
        OperationalState(
            primary_state=PrimaryState.READY,
            claim_permission=ClaimPermission.NORMAL,
        )
    )

    new = await holder.transition_to(
        PrimaryState.READY,
        trigger="auth degraded",
        add_reasons={DegradationReason.AUTH},
    )

    assert new.primary_state is PrimaryState.READY
    assert new.is_degraded()
    assert DegradationReason.AUTH in new.degradation_reasons


@pytest.mark.asyncio
async def test_self_transition_row_still_works():
    """BOOTING → BOOTING IS in the table ("transient gate failure"). The
    same-state bypass and the table check both allow it."""
    holder = OperationalStateHolder(_booting_state())

    new = await holder.transition_to(
        PrimaryState.BOOTING,
        trigger="transient gate failure",
    )

    assert new.primary_state is PrimaryState.BOOTING


@pytest.mark.asyncio
async def test_add_and_remove_reasons_compose():
    holder = OperationalStateHolder(
        OperationalState(
            primary_state=PrimaryState.READY,
            degradation_reasons=frozenset({DegradationReason.AUTH}),
            claim_permission=ClaimPermission.CRITICAL_ONLY,
        )
    )

    new = await holder.transition_to(
        PrimaryState.READY,
        trigger="auth recovered, dispatch slow",
        add_reasons={DegradationReason.DISPATCH},
        remove_reasons={DegradationReason.AUTH},
    )

    assert new.degradation_reasons == frozenset({DegradationReason.DISPATCH})


# ---------------------------------------------------------------------------
# Listeners
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listener_receives_old_new_trigger_after_state_swap():
    holder = OperationalStateHolder(_booting_state())
    captured: list[tuple] = []

    async def listener(old, new, trigger):
        # When the listener runs, holder.current must already be new.
        captured.append((old.primary_state, new.primary_state, trigger, holder.current.primary_state))

    holder.add_listener(listener)
    await holder.transition_to(
        PrimaryState.READY, trigger="all §9.2 gates pass"
    )

    assert len(captured) == 1
    old_ps, new_ps, trig, current_ps = captured[0]
    assert old_ps is PrimaryState.BOOTING
    assert new_ps is PrimaryState.READY
    assert trig == "all §9.2 gates pass"
    assert current_ps is PrimaryState.READY


@pytest.mark.asyncio
async def test_listener_exception_does_not_block_subsequent_listeners():
    holder = OperationalStateHolder(_booting_state())
    fired: list[str] = []

    async def bad(_o, _n, _t):
        fired.append("bad")
        raise RuntimeError("boom")

    async def good(_o, _n, _t):
        fired.append("good")

    holder.add_listener(bad)
    holder.add_listener(good)
    await holder.transition_to(PrimaryState.READY, trigger="all §9.2 gates pass")

    assert fired == ["bad", "good"]
    # The state still advanced — listeners are observability, not policy.
    assert holder.current.primary_state is PrimaryState.READY


@pytest.mark.asyncio
async def test_listener_exception_does_not_roll_state_back():
    holder = OperationalStateHolder(_booting_state())

    async def always_raises(_o, _n, _t):
        raise RuntimeError("nope")

    holder.add_listener(always_raises)
    await holder.transition_to(PrimaryState.READY, trigger="all §9.2 gates pass")

    assert holder.current.primary_state is PrimaryState.READY


# ---------------------------------------------------------------------------
# Lock semantics — concurrent transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_transitions_serialize():
    """Two concurrent ``transition_to`` calls must not interleave —
    each acquires the lock in turn. We verify by having the first
    transition's listener sleep; the second must wait, see the first
    state already applied, and then apply its own atop."""
    holder = OperationalStateHolder(_booting_state())
    seen_old_states: list[PrimaryState] = []

    async def slow_listener(old, _new, _t):
        seen_old_states.append(old.primary_state)
        await asyncio.sleep(0.05)

    holder.add_listener(slow_listener)

    # Launch READY then ACTIVE concurrently. If they serialized:
    #   - first transition: BOOTING → READY, listener sees old=BOOTING
    #   - second transition: READY → ACTIVE, listener sees old=READY
    # If they raced (no lock): both might see old=BOOTING.
    async def first():
        await holder.transition_to(PrimaryState.READY, trigger="all §9.2 gates pass")

    async def second():
        # Tiny yield so first() acquires the lock first.
        await asyncio.sleep(0.001)
        await holder.transition_to(PrimaryState.ACTIVE, trigger="claim acquired")

    await asyncio.gather(first(), second())

    assert seen_old_states == [PrimaryState.BOOTING, PrimaryState.READY]
    assert holder.current.primary_state is PrimaryState.ACTIVE


# ---------------------------------------------------------------------------
# Module-level singleton accessor
# ---------------------------------------------------------------------------


def test_get_holder_returns_none_before_init():
    assert get_holder() is None


def test_init_holder_constructs_singleton():
    h = init_holder(_booting_state())
    assert h is get_holder()
    assert h.current.primary_state is PrimaryState.BOOTING


def test_init_holder_is_idempotent():
    """Second call must not overwrite the first holder; mirrors the
    IsoKronMemoryProvider singleton's first-wins semantics."""
    first = init_holder(_booting_state())
    second = init_holder(
        OperationalState(primary_state=PrimaryState.READY)
    )
    assert first is second
    # Initial state from the FIRST call wins.
    assert first.current.primary_state is PrimaryState.BOOTING


def test_reset_helper_clears_singleton():
    init_holder(_booting_state())
    assert get_holder() is not None
    _reset_holder_for_tests()
    assert get_holder() is None
