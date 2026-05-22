"""KR-P2-INT-TESTS ST6 — poison-ticket handling (R4.1 §12).

Per the bucket spec:
  1. Inject a Sea_Ticket that consistently causes a logical-class
     failure (per R4.1 §9.4 only LOGICAL failures count toward
     failed_terminal threshold).
  2. Verify failure counter increments per attempt.
  3. After 3 logical failures: ticket transitions to failed_terminal.
  4. Verify subsequent polls don't re-claim the failed_terminal ticket.
  5. Verify cockpit operator-action set (unblock / reassign /
     force_retry / cancel) is reachable.

# What's tested in this file

Contract-test for the RUNTIME-side failure classifier
(``classify_failure`` from
``plugins.memory.isokron.sea_ticket_resolution``). This validates:

  - Logical-class failures (Constitution FAIL,
    ConstitutionPreScreenFailError) map to FAILED_TERMINAL
  - Transient-class failures (NetworkDispatchFailureError, agent-loop
    timeout) map to FAILED_RETRYABLE — DO NOT count toward
    failed_terminal threshold
  - The per-tool default classification falls back conservatively
    (FAILED_RETRYABLE) for unknown exceptions
  - SeaTicketResolution enum carries the wire-format string
    consumers index on

# What's not tested (out of scope)

  - Substrate-side failure_count column + threshold-3 transition
    (substrate-team's lane — handled by the
    ``kora_sea_ticket.failure_count`` + status='failed_terminal'
    SECDEFs)
  - Subsequent-poll skip behavior — the substrate's
    ``get_next_available_sea_ticket`` SQL function filters
    failed_terminal rows. Runtime-side simply doesn't see them.
  - Cockpit operator-action endpoints (CC#2 lane).

The bucket spec acknowledges these as substrate-only; runtime
validates the classifier contract that feeds the substrate's
counter.
"""

from __future__ import annotations

import pytest

from plugins.memory.isokron.sea_ticket_poller import SeaTicketResolution
from plugins.memory.isokron.sea_ticket_resolution import (
    AgentLoopTimeoutError,
    ConstitutionPreScreenFailError,
    ConstitutionPreScreenInconclusiveError,
    NetworkDispatchFailureError,
    classify_failure,
)


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Logical failures → FAILED_TERMINAL (counted toward threshold-3)
# ---------------------------------------------------------------------------


def test_constitution_fail_classifies_as_failed_terminal():
    """Per R4.1 §9.4: Constitution pre-screen FAIL is a logical
    failure — policy denies the tool. Retrying with the same policy
    state buys nothing. Maps to FAILED_TERMINAL → COUNTS toward
    failed_terminal threshold."""
    exc = ConstitutionPreScreenFailError("policy denies tool=write_file")
    classified = classify_failure(exc)
    assert classified.resolution is SeaTicketResolution.FAILED_TERMINAL
    assert classified.next_eligible_offset_seconds is None
    assert "policy denies" in classified.reason.lower()


def test_claim_sea_ticket_failure_classifies_as_failed_terminal():
    """A bare tool-name tag on kora__claim_sea_ticket failure
    indicates a policy/constraint failure (substrate-side decided
    not to grant claim). Per the per-tool table: FAILED_TERMINAL."""
    classified = classify_failure(
        Exception("claim refused"),
        tool_name="kora__claim_sea_ticket",
    )
    assert classified.resolution is SeaTicketResolution.FAILED_TERMINAL


# ---------------------------------------------------------------------------
# Transient failures → FAILED_RETRYABLE (NOT counted toward threshold)
# ---------------------------------------------------------------------------


def test_constitution_inconclusive_classifies_as_failed_retryable():
    """INCONCLUSIVE is transient (Critic/Oracle disagreed or context
    insufficient). Retryable after backoff. Does NOT count toward
    failed_terminal."""
    classified = classify_failure(
        ConstitutionPreScreenInconclusiveError("oracle pending")
    )
    assert classified.resolution is SeaTicketResolution.FAILED_RETRYABLE
    assert classified.next_eligible_offset_seconds is not None
    assert classified.next_eligible_offset_seconds > 0


def test_agent_loop_timeout_classifies_as_failed_retryable():
    """Wall-clock budget exhaustion → transient. R4.1 §9.4 maps to
    FAILED_RETRYABLE with a 10-minute backoff."""
    classified = classify_failure(AgentLoopTimeoutError("budget exhausted"))
    assert classified.resolution is SeaTicketResolution.FAILED_RETRYABLE
    assert classified.next_eligible_offset_seconds is not None


def test_network_dispatch_failure_classifies_as_released():
    """Network-tier transient (Sea MCP transport blip). Per R4.1 §9.4:
    DOES NOT count toward failed_terminal (NO retry-count attribution
    for transient class)."""
    classified = classify_failure(
        NetworkDispatchFailureError("substrate down briefly")
    )
    assert classified.resolution is SeaTicketResolution.RELEASED


def test_unknown_exception_falls_through_to_retryable_default():
    """Conservative default: unknown failures get the benefit of the
    doubt as FAILED_RETRYABLE. Substrate-side retry budget catches
    genuinely terminal ones."""
    classified = classify_failure(
        RuntimeError("unexpected agent loop crash")
    )
    assert classified.resolution is SeaTicketResolution.FAILED_RETRYABLE


# ---------------------------------------------------------------------------
# Per-tool table — chain-emit transient classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name,expected",
    [
        ("kora__append_event", SeaTicketResolution.FAILED_RETRYABLE),
        ("kora__release_claim", SeaTicketResolution.FAILED_RETRYABLE),
        ("kora__refresh_claim", SeaTicketResolution.FAILED_RETRYABLE),
    ],
)
def test_substrate_plumbing_tool_failures_classify_as_retryable(
    tool_name, expected
):
    """MCP plumbing failures (auth flap, substrate brief outage) are
    transient — retryable. Substrate-side retry budget at higher tiers
    escalates if persistent."""
    classified = classify_failure(
        Exception(f"{tool_name} raised"), tool_name=tool_name
    )
    assert classified.resolution is expected


# ---------------------------------------------------------------------------
# Wire-format pin — SeaTicketResolution string values
# ---------------------------------------------------------------------------


def test_failed_terminal_wire_format_pinned():
    """Cockpit consumers + substrate counter increments index on the
    string value 'failed_terminal' — pin it."""
    assert SeaTicketResolution.FAILED_TERMINAL.value == "failed_terminal"


def test_failed_retryable_wire_format_pinned():
    assert SeaTicketResolution.FAILED_RETRYABLE.value == "failed_retryable"


def test_released_wire_format_pinned():
    """Network-class transient — substrate-side does NOT increment
    failure_count for this resolution."""
    assert SeaTicketResolution.RELEASED.value == "released"


# ---------------------------------------------------------------------------
# Poison-ticket scenario — sequential failures of LOGICAL class
# ---------------------------------------------------------------------------


def test_three_sequential_logical_failures_classify_terminally_each_time():
    """Simulates the 3-attempt sequence the bucket spec describes:
    the same poison ticket triggers Constitution FAIL on each
    attempt. Each classify call returns FAILED_TERMINAL → substrate
    SECDEF (not in scope) increments failure_count to 3 → ticket
    transitions to status='failed_terminal'. The 4th attempt would
    not be reached because the substrate's
    get_next_available_sea_ticket no longer returns this ticket."""
    for attempt in range(1, 4):
        exc = ConstitutionPreScreenFailError(
            f"attempt {attempt}: policy denies"
        )
        classified = classify_failure(exc)
        assert classified.resolution is SeaTicketResolution.FAILED_TERMINAL, (
            f"attempt {attempt}: expected FAILED_TERMINAL, got "
            f"{classified.resolution}"
        )


def test_mixed_logical_and_transient_failures_only_count_logical():
    """Per R4.1 §9.4: only LOGICAL-class failures count toward the
    failed_terminal threshold. A ticket that fails twice with
    logical (Constitution FAIL) + once with transient (network) +
    once more with logical = 3 logical failures total. The transient
    failure does NOT count.

    This test pins the classifier's discrimination so the substrate
    counter logic has a clean signal to count off of."""
    seq = [
        ConstitutionPreScreenFailError("attempt 1 logical"),
        NetworkDispatchFailureError("attempt 2 transient"),
        ConstitutionPreScreenFailError("attempt 3 logical"),
        ConstitutionPreScreenFailError("attempt 4 logical"),
    ]
    classifications = [classify_failure(exc) for exc in seq]
    # Logical attempts → FAILED_TERMINAL
    assert classifications[0].resolution is SeaTicketResolution.FAILED_TERMINAL
    assert classifications[2].resolution is SeaTicketResolution.FAILED_TERMINAL
    assert classifications[3].resolution is SeaTicketResolution.FAILED_TERMINAL
    # Transient attempt → RELEASED (NOT counted toward threshold)
    assert classifications[1].resolution is SeaTicketResolution.RELEASED
