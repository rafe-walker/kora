"""Unit tests for ``agent/operational_state.py`` (KR-P2-I-skeleton).

Covers:
  - Enum string values match R4.1 §9.1 verbatim
  - Enum cardinality (5 PrimaryState / 8 DegradationReason / 3 ClaimPermission)
  - OperationalState immutability + ``with_*`` factory semantics
  - ``is_degraded()`` truth table
  - Transition-table query helpers
  - Enum round-trip via ``.value`` / ``Enum(value)``
"""

from __future__ import annotations

import dataclasses

import pytest

from agent.operational_state import (
    ClaimPermission,
    DegradationReason,
    HeartbeatProgress,
    OperationalState,
    PrimaryState,
    StateTransition,
    TRANSITION_TABLE,
    is_valid_transition,
    transitions_from,
    transitions_to,
)


# ---------------------------------------------------------------------------
# Enum value strings (load-bearing wire format)
# ---------------------------------------------------------------------------


def test_primary_state_values_match_r41_section_9_1():
    """Five members, lower-case strings — see R4.1 §9.1."""
    assert {m.value for m in PrimaryState} == {
        "booting",
        "ready",
        "active",
        "paused",
        "stopped",
    }
    assert len(list(PrimaryState)) == 5


def test_degradation_reason_values_match_r41_section_9_1():
    assert {m.value for m in DegradationReason} == {
        "cost",
        "auth",
        "dispatch",
        "substrate",
        "migration",
        "operator",
        "token_expiring",
        "retry_ceiling",
    }
    assert len(list(DegradationReason)) == 8


def test_claim_permission_values_match_r41_section_9_1():
    assert {m.value for m in ClaimPermission} == {
        "none",
        "critical_only",
        "normal",
    }
    assert len(list(ClaimPermission)) == 3


# ---------------------------------------------------------------------------
# OperationalState shape + immutability
# ---------------------------------------------------------------------------


def test_operational_state_is_frozen():
    state = OperationalState(primary_state=PrimaryState.BOOTING)
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.primary_state = PrimaryState.READY  # type: ignore[misc]


def test_operational_state_defaults():
    state = OperationalState(primary_state=PrimaryState.READY)
    assert state.primary_state is PrimaryState.READY
    assert state.degradation_reasons == frozenset()
    assert state.claim_permission is ClaimPermission.NORMAL


def test_is_degraded_truth_table():
    assert (
        OperationalState(primary_state=PrimaryState.READY).is_degraded()
        is False
    )
    assert (
        OperationalState(
            primary_state=PrimaryState.READY,
            degradation_reasons=frozenset({DegradationReason.COST}),
        ).is_degraded()
        is True
    )
    assert (
        OperationalState(
            primary_state=PrimaryState.ACTIVE,
            degradation_reasons=frozenset(
                {DegradationReason.AUTH, DegradationReason.DISPATCH}
            ),
        ).is_degraded()
        is True
    )


# ---------------------------------------------------------------------------
# with_* factory methods — produce new instances; original untouched
# ---------------------------------------------------------------------------


def test_with_added_reason_returns_new_instance_with_reason_added():
    original = OperationalState(primary_state=PrimaryState.READY)
    derived = original.with_added_reason(DegradationReason.COST)
    assert derived is not original
    assert derived.degradation_reasons == frozenset({DegradationReason.COST})
    # Original unchanged
    assert original.degradation_reasons == frozenset()


def test_with_added_reason_is_idempotent_for_already_present():
    original = OperationalState(
        primary_state=PrimaryState.READY,
        degradation_reasons=frozenset({DegradationReason.COST}),
    )
    derived = original.with_added_reason(DegradationReason.COST)
    assert derived.degradation_reasons == frozenset({DegradationReason.COST})


def test_with_removed_reason_returns_new_instance_with_reason_removed():
    original = OperationalState(
        primary_state=PrimaryState.READY,
        degradation_reasons=frozenset(
            {DegradationReason.COST, DegradationReason.AUTH}
        ),
    )
    derived = original.with_removed_reason(DegradationReason.COST)
    assert derived is not original
    assert derived.degradation_reasons == frozenset({DegradationReason.AUTH})
    # Original unchanged
    assert original.degradation_reasons == frozenset(
        {DegradationReason.COST, DegradationReason.AUTH}
    )


def test_with_removed_reason_is_no_op_when_absent():
    original = OperationalState(
        primary_state=PrimaryState.READY,
        degradation_reasons=frozenset({DegradationReason.AUTH}),
    )
    derived = original.with_removed_reason(DegradationReason.COST)
    assert derived.degradation_reasons == frozenset({DegradationReason.AUTH})


def test_with_primary_state_replaces_primary_state_only():
    original = OperationalState(
        primary_state=PrimaryState.READY,
        degradation_reasons=frozenset({DegradationReason.COST}),
        claim_permission=ClaimPermission.CRITICAL_ONLY,
    )
    derived = original.with_primary_state(PrimaryState.ACTIVE)
    assert derived.primary_state is PrimaryState.ACTIVE
    # Other fields preserved
    assert derived.degradation_reasons == frozenset({DegradationReason.COST})
    assert derived.claim_permission is ClaimPermission.CRITICAL_ONLY


def test_with_claim_permission_replaces_claim_permission_only():
    original = OperationalState(
        primary_state=PrimaryState.ACTIVE,
        degradation_reasons=frozenset({DegradationReason.COST}),
    )
    derived = original.with_claim_permission(ClaimPermission.NONE)
    assert derived.claim_permission is ClaimPermission.NONE
    assert derived.primary_state is PrimaryState.ACTIVE
    assert derived.degradation_reasons == frozenset({DegradationReason.COST})


# ---------------------------------------------------------------------------
# Transition-table query helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        (PrimaryState.BOOTING, PrimaryState.READY),
        (PrimaryState.BOOTING, PrimaryState.BOOTING),  # self-loop retry
        (PrimaryState.BOOTING, PrimaryState.STOPPED),
        (PrimaryState.BOOTING, PrimaryState.PAUSED),
        (PrimaryState.READY, PrimaryState.ACTIVE),
        (PrimaryState.ACTIVE, PrimaryState.READY),
        (PrimaryState.READY, PrimaryState.PAUSED),
        (PrimaryState.ACTIVE, PrimaryState.PAUSED),
        (PrimaryState.PAUSED, PrimaryState.READY),
        (PrimaryState.READY, PrimaryState.STOPPED),
        (PrimaryState.ACTIVE, PrimaryState.STOPPED),
        (PrimaryState.PAUSED, PrimaryState.STOPPED),
    ],
)
def test_is_valid_transition_returns_true_for_known_transitions(
    from_state, to_state
):
    assert is_valid_transition(from_state, to_state) is True


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        # STOPPED is terminal — operator must re-boot (no STOPPED→X rows)
        (PrimaryState.STOPPED, PrimaryState.ACTIVE),
        (PrimaryState.STOPPED, PrimaryState.READY),
        (PrimaryState.STOPPED, PrimaryState.BOOTING),
        (PrimaryState.STOPPED, PrimaryState.PAUSED),
        # PAUSED can't directly skip to ACTIVE — must go via READY
        (PrimaryState.PAUSED, PrimaryState.ACTIVE),
        # READY can't self-loop
        (PrimaryState.READY, PrimaryState.READY),
        # ACTIVE can't loop or go to BOOTING
        (PrimaryState.ACTIVE, PrimaryState.ACTIVE),
        (PrimaryState.ACTIVE, PrimaryState.BOOTING),
        # PAUSED can't go to BOOTING
        (PrimaryState.PAUSED, PrimaryState.BOOTING),
        # READY can't go directly to BOOTING
        (PrimaryState.READY, PrimaryState.BOOTING),
    ],
)
def test_is_valid_transition_returns_false_for_unknown_transitions(
    from_state, to_state
):
    assert is_valid_transition(from_state, to_state) is False


def test_transitions_from_booting_yields_all_booting_origin_rows():
    """BOOTING-origin rows (expanded ``any → X`` semantics):
      1. BOOTING → READY (gates pass)
      2. BOOTING → BOOTING (retry)
      3. BOOTING → STOPPED (invariant fail / retry exhausted)
      4. BOOTING → PAUSED (gate 3b epoch mismatch)
      5. BOOTING → PAUSED (STOP-KORA L1–3)
      6. BOOTING → STOPPED (STOP-KORA L4/L5)

    (4) and (5) are distinct same-arrow rows differing only by trigger;
    similarly (3) and (6). The bucket spec's "4 BOOTING-origin" count
    aligns with the R4.1 unexpanded shorthand; the expanded table has
    6 rows because ``any → PAUSED`` and ``any → STOPPED`` each
    contribute a BOOTING row.
    """
    booting_rows = transitions_from(PrimaryState.BOOTING)
    assert len(booting_rows) == 6
    assert all(row.from_state is PrimaryState.BOOTING for row in booting_rows)
    to_states = {row.to_state for row in booting_rows}
    assert to_states == {
        PrimaryState.READY,
        PrimaryState.BOOTING,
        PrimaryState.STOPPED,
        PrimaryState.PAUSED,
    }


def test_transitions_to_stopped_yields_all_stopped_arrival_rows():
    """STOPPED-arrival rows (expanded ``any → STOPPED`` semantics):
      1. BOOTING → STOPPED (invariant fail / retry exhausted)
      2. READY → STOPPED (STOP-KORA L4/L5)
      3. ACTIVE → STOPPED (STOP-KORA L4/L5)
      4. PAUSED → STOPPED (STOP-KORA L4/L5)
      5. BOOTING → STOPPED (STOP-KORA L4/L5)

    BOOTING appears twice (rows 1 and 5) — same arrow, different
    triggers. The bucket spec's "4 STOPPED-arrival" count aligns with
    the R4.1 unexpanded shorthand.
    """
    stopped_rows = transitions_to(PrimaryState.STOPPED)
    assert len(stopped_rows) == 5
    assert all(row.to_state is PrimaryState.STOPPED for row in stopped_rows)
    from_states = {row.from_state for row in stopped_rows}
    assert from_states == {
        PrimaryState.BOOTING,
        PrimaryState.READY,
        PrimaryState.ACTIVE,
        PrimaryState.PAUSED,
    }


def test_transitions_from_stopped_is_empty_terminal():
    """STOPPED is terminal — operator must initiate a new boot."""
    assert transitions_from(PrimaryState.STOPPED) == ()


def test_transition_table_rows_are_state_transition_instances():
    """Defensive: catches accidental tuple/dict drift in the table."""
    assert TRANSITION_TABLE
    for row in TRANSITION_TABLE:
        assert isinstance(row, StateTransition)


# ---------------------------------------------------------------------------
# Round-trip — every enum value can be serialized and re-parsed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("member", list(PrimaryState))
def test_primary_state_round_trips_through_value_string(member):
    assert PrimaryState(member.value) is member


@pytest.mark.parametrize("member", list(DegradationReason))
def test_degradation_reason_round_trips_through_value_string(member):
    assert DegradationReason(member.value) is member


@pytest.mark.parametrize("member", list(ClaimPermission))
def test_claim_permission_round_trips_through_value_string(member):
    assert ClaimPermission(member.value) is member


# ---------------------------------------------------------------------------
# HeartbeatProgress — R4.1 §9.3 P2 documentation/type artifact
# ---------------------------------------------------------------------------


def test_heartbeat_progress_has_exactly_two_members():
    """R4.1 §9.3 P2 defines two concrete progress signals; a bare
    liveness pulse is explicitly NOT one of them. Cardinality is
    load-bearing — the cockpit-BFF SLA watcher consumes the same
    contract."""
    assert {m.value for m in HeartbeatProgress} == {
        "streaming_token_advanced",
        "tool_call_boundary",
    }
    assert len(list(HeartbeatProgress)) == 2


@pytest.mark.parametrize("member", list(HeartbeatProgress))
def test_heartbeat_progress_round_trips_through_value_string(member):
    """String values are stable wire format — consumed by the future
    Runtime Contract manifest reference (cockpit-BFF side)."""
    assert HeartbeatProgress(member.value) is member


def test_heartbeat_progress_is_not_a_chain_emit_literal():
    """Sanity guard: the enum exists only as a documentation /
    type artifact. No `kora.heartbeat.*` literal is admitted by
    `foundation/0159_kora_r41_operational_state_event_vocabulary.sql`;
    adding one would be the audit-class flood the substrate's
    terminal-only-emit design avoids.

    This test asserts the doc intent — if anyone adds an
    ``emit_heartbeat_progress`` function in this module, they'll
    need to either (a) remove this test with a clear PR-body
    justification OR (b) get substrate-team to ship a new event
    literal first.
    """
    import agent.operational_state as os_mod
    assert not hasattr(os_mod, "emit_heartbeat_progress")
