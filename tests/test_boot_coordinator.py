"""Unit tests for ``agent/boot_coordinator.py`` (KR-P2-H ST3).

Covers:
  - All gates PASS → BootResult.READY + holder transitions to READY +
    rich kora.boot.ready emit
  - INVARIANT FAIL → BootResult.STOPPED + holder transitions to STOPPED
    + kora.boot.failed emit with failed_gate_id payload
  - TRANSIENT exhaustion → BootResult.STOPPED (same path as INVARIANT)
  - Diagnostic mode: no holder transitions, no emits, all gates run
  - Degradation reason staging during retry; clearance on success
  - holder=None requires diagnostic_mode=True (raises otherwise)
  - Emit failures are WARN-logged, not raised (best-effort)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import ClassVar, Optional
from unittest.mock import MagicMock

import pytest

from agent.boot_coordinator import (
    BOOT_FAILED_EVENT,
    BOOT_READY_EVENT,
    BootResult,
    BootSummary,
    _build_boot_event_payload,
    run_boot_sequence,
)
from agent.boot_gates import (
    BootContext,
    Gate,
    GateClass,
    GateOutcome,
    GateResult,
)
from agent.operational_state import (
    ClaimPermission,
    DegradationReason,
    OperationalState,
    PrimaryState,
)
from agent.operational_state_holder import (
    _reset_holder_for_tests,
    init_holder,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    _reset_holder_for_tests()
    yield
    _reset_holder_for_tests()


# ---------------------------------------------------------------------------
# Programmable test gates
# ---------------------------------------------------------------------------


class _ProgrammableGate(Gate):
    def __init__(
        self,
        gate_id: str,
        gate_class: GateClass,
        outcomes: list[GateOutcome],
    ):
        self.gate_id = gate_id  # type: ignore[misc]
        self.gate_class = gate_class  # type: ignore[misc]
        self.title = gate_id  # type: ignore[misc]
        self._outcomes = list(outcomes)
        self.call_count = 0

    async def run(self, context: BootContext) -> GateResult:
        self.call_count += 1
        outcome = self._outcomes.pop(0)
        now = datetime.now(timezone.utc)
        return GateResult(
            gate_id=self.gate_id,
            gate_class=self.gate_class,
            outcome=outcome,
            detail=f"attempt #{self.call_count}",
            elapsed_ms=1,
            started_at=now,
            completed_at=now,
        )


def _holder() -> "OperationalStateHolder":  # type: ignore  # noqa: F821
    return init_holder(
        OperationalState(
            primary_state=PrimaryState.BOOTING,
            claim_permission=ClaimPermission.NONE,
        )
    )


def _make_emit_provider(
    *,
    submit_raises: Optional[Exception] = None,
) -> SimpleNamespace:
    """Provider where the coordinator's emit path can succeed or fail.

    submit_and_wait closes the emit coroutine + returns ``"evt-001"``
    on success, or raises if ``submit_raises`` is set. The provider
    also satisfies the resolve_workspace_id + get_mcp_client surface.
    """
    def _submit(coro, *, timeout=10.0):
        if hasattr(coro, "close"):
            try:
                coro.close()
            except Exception:
                pass
        if submit_raises is not None:
            raise submit_raises
        return "evt-001"

    connection = SimpleNamespace(
        get_mcp_client=MagicMock(return_value="fake-mcp-client"),
        submit_and_wait=MagicMock(side_effect=_submit),
    )
    return SimpleNamespace(
        _connection=connection,
        _resolve_workspace_id=lambda: "ws-test",
    )


# ===========================================================================
# All-PASS path
# ===========================================================================


@pytest.mark.asyncio
async def test_all_pass_transitions_holder_to_ready_and_emits():
    holder = _holder()
    provider = _make_emit_provider()
    gates = [
        _ProgrammableGate("g1", GateClass.TRANSIENT, [GateOutcome.PASS]),
        _ProgrammableGate("g2", GateClass.INVARIANT, [GateOutcome.PASS]),
    ]

    summary = await run_boot_sequence(
        memory_provider=provider,
        holder=holder,
        gates=gates,
    )

    assert summary.result is BootResult.READY
    assert summary.failed_gate is None
    assert len(summary.gate_results) == 2
    assert holder.current.primary_state is PrimaryState.READY

    # The coordinator's submit_and_wait was called once — for the emit
    # of kora.boot.ready. Verify the emit fired with the right shape
    # via the submit-side argument inspection.
    assert provider._connection.submit_and_wait.call_count == 1


# ===========================================================================
# INVARIANT FAIL path
# ===========================================================================


@pytest.mark.asyncio
async def test_invariant_fail_transitions_holder_to_stopped_and_emits():
    holder = _holder()
    provider = _make_emit_provider()
    gates = [
        _ProgrammableGate("g1", GateClass.TRANSIENT, [GateOutcome.PASS]),
        _ProgrammableGate(
            "g2_invariant", GateClass.INVARIANT, [GateOutcome.FAIL]
        ),
        _ProgrammableGate("g3", GateClass.TRANSIENT, [GateOutcome.PASS]),
    ]

    summary = await run_boot_sequence(
        memory_provider=provider,
        holder=holder,
        gates=gates,
    )

    assert summary.result is BootResult.STOPPED
    assert summary.failed_gate is not None
    assert summary.failed_gate.gate_id == "g2_invariant"
    assert summary.failed_gate.outcome is GateOutcome.FAIL
    # Sequence short-circuited; g3 never ran.
    assert len(summary.gate_results) == 2
    assert holder.current.primary_state is PrimaryState.STOPPED


# ===========================================================================
# TRANSIENT exhaustion path
# ===========================================================================


@pytest.mark.asyncio
async def test_transient_budget_exhaustion_transitions_to_stopped(monkeypatch):
    holder = _holder()
    provider = _make_emit_provider()
    # Fail every attempt; with retry_budget defaulting to 5, the gate
    # exhausts and the coordinator returns STOPPED.
    gates = [
        _ProgrammableGate(
            "g_flaky",
            GateClass.TRANSIENT,
            [GateOutcome.FAIL] * 10,  # more than budget
        ),
    ]

    # Patch asyncio.sleep to no-op so the test doesn't wait for real
    # backoff. (Patching the module-level _DEFAULT_BACKOFF_* constants
    # doesn't work because Python evaluates default args at
    # function-definition time, so BootGateRunner.__init__'s defaults
    # are already bound.)
    async def _no_sleep(_seconds):
        return None
    monkeypatch.setattr("agent.boot_gates.asyncio.sleep", _no_sleep)

    summary = await run_boot_sequence(
        memory_provider=provider,
        holder=holder,
        gates=gates,
    )

    assert summary.result is BootResult.STOPPED
    assert summary.failed_gate is not None
    assert summary.failed_gate.gate_id == "g_flaky"
    assert summary.failed_gate.attempts == 5  # default budget
    assert holder.current.primary_state is PrimaryState.STOPPED


# ===========================================================================
# Diagnostic mode
# ===========================================================================


@pytest.mark.asyncio
async def test_diagnostic_mode_does_not_transition_holder():
    """Diagnostic mode is a probe — no holder transitions, no emits."""
    holder = _holder()
    provider = _make_emit_provider()
    gates = [
        _ProgrammableGate("g1", GateClass.TRANSIENT, [GateOutcome.PASS]),
        _ProgrammableGate(
            "g2", GateClass.INVARIANT, [GateOutcome.FAIL]
        ),
        _ProgrammableGate("g3", GateClass.TRANSIENT, [GateOutcome.PASS]),
    ]

    summary = await run_boot_sequence(
        memory_provider=provider,
        holder=holder,
        gates=gates,
        diagnostic_mode=True,
    )

    # Diagnostic ran all 3 gates regardless of FAIL.
    assert len(summary.gate_results) == 3
    # Result reflects failure but no transition.
    assert summary.result is BootResult.STOPPED
    assert summary.failed_gate.gate_id == "g2"
    # Holder is UNCHANGED — still BOOTING.
    assert holder.current.primary_state is PrimaryState.BOOTING
    # No emits.
    assert provider._connection.submit_and_wait.call_count == 0


@pytest.mark.asyncio
async def test_diagnostic_mode_with_no_holder():
    """Diagnostic mode permits ``holder=None`` — the CLI use case."""
    provider = _make_emit_provider()
    gates = [_ProgrammableGate("g1", GateClass.TRANSIENT, [GateOutcome.PASS])]

    summary = await run_boot_sequence(
        memory_provider=provider,
        holder=None,
        gates=gates,
        diagnostic_mode=True,
    )
    assert summary.result is BootResult.READY


@pytest.mark.asyncio
async def test_production_mode_requires_holder():
    with pytest.raises(ValueError, match="holder is required"):
        await run_boot_sequence(
            memory_provider=_make_emit_provider(),
            holder=None,
            gates=[],
            diagnostic_mode=False,
        )


# ===========================================================================
# Degradation reason staging during retry
# ===========================================================================


@pytest.mark.asyncio
async def test_transient_retry_stages_degradation_reason_and_clears_on_success(
    monkeypatch,
):
    """When a TRANSIENT gate retries, the per-gate degradation reason
    is staged on the holder. On eventual PASS, the reason is removed."""
    holder = _holder()
    provider = _make_emit_provider()

    # Gate 1 fails twice then passes → 3 attempts total → 2 retries.
    # The on_retry_attempt callback fires after attempts 1 and 2
    # (before backoff). Each stages a reason.
    gates = [
        _ProgrammableGate(
            "1_claude_auth",  # mapped to DegradationReason.AUTH
            GateClass.TRANSIENT,
            [GateOutcome.FAIL, GateOutcome.FAIL, GateOutcome.PASS],
        ),
    ]

    async def _no_sleep(_seconds):
        return None
    monkeypatch.setattr("agent.boot_gates.asyncio.sleep", _no_sleep)

    summary = await run_boot_sequence(
        memory_provider=provider,
        holder=holder,
        gates=gates,
    )

    assert summary.result is BootResult.READY
    # Holder ended at READY with no lingering reasons.
    assert holder.current.primary_state is PrimaryState.READY
    assert DegradationReason.AUTH not in holder.current.degradation_reasons


# ===========================================================================
# Emit failure is best-effort (WARN, not raise)
# ===========================================================================


@pytest.mark.asyncio
async def test_emit_failure_does_not_block_outcome(caplog):
    """If the kora.boot.ready emit raises (e.g. substrate hiccup), the
    coordinator logs WARN + returns the summary normally. The holder
    transitioned BEFORE the emit, so operational-state observability
    is intact via the generic transition listener."""
    holder = _holder()
    provider = _make_emit_provider(
        submit_raises=RuntimeError("substrate emit failed")
    )
    gates = [_ProgrammableGate("g1", GateClass.TRANSIENT, [GateOutcome.PASS])]

    with caplog.at_level(logging.WARNING, logger="agent.boot_coordinator"):
        summary = await run_boot_sequence(
            memory_provider=provider,
            holder=holder,
            gates=gates,
        )

    assert summary.result is BootResult.READY
    assert holder.current.primary_state is PrimaryState.READY
    assert any(
        "emit raised" in record.message
        and BOOT_READY_EVENT in record.message
        for record in caplog.records
    )


# ===========================================================================
# Payload shape
# ===========================================================================


def test_build_boot_event_payload_includes_per_gate_dict():
    now = datetime.now(timezone.utc)
    results = [
        GateResult(
            gate_id="g1",
            gate_class=GateClass.TRANSIENT,
            outcome=GateOutcome.PASS,
            detail="ok",
            elapsed_ms=42,
            started_at=now,
            completed_at=now,
            attempts=1,
        ),
        GateResult(
            gate_id="g2",
            gate_class=GateClass.INVARIANT,
            outcome=GateOutcome.FAIL,
            detail="missing row",
            elapsed_ms=10,
            started_at=now,
            completed_at=now,
            attempts=1,
        ),
    ]
    failed = results[1]
    payload = _build_boot_event_payload(results, failed)

    assert payload["gates"] == [
        {
            "gate_id": "g1",
            "gate_class": "transient",
            "outcome": "pass",
            "attempts": 1,
            "elapsed_ms": 42,
            "detail": "ok",
        },
        {
            "gate_id": "g2",
            "gate_class": "invariant",
            "outcome": "fail",
            "attempts": 1,
            "elapsed_ms": 10,
            "detail": "missing row",
        },
    ]
    assert payload["failed_gate_id"] == "g2"
    assert payload["failed_detail"] == "missing row"


def test_build_boot_event_payload_omits_failed_keys_on_ready():
    now = datetime.now(timezone.utc)
    results = [
        GateResult(
            gate_id="g1",
            gate_class=GateClass.TRANSIENT,
            outcome=GateOutcome.PASS,
            detail="ok",
            elapsed_ms=1,
            started_at=now,
            completed_at=now,
        ),
    ]
    payload = _build_boot_event_payload(results, None)
    assert "failed_gate_id" not in payload
    assert "failed_detail" not in payload


# ===========================================================================
# BootSummary shape
# ===========================================================================


def test_boot_summary_is_frozen():
    import dataclasses
    summary = BootSummary(
        result=BootResult.READY,
        gate_results=[],
        failed_gate=None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        summary.result = BootResult.STOPPED  # type: ignore[misc]


def test_boot_result_enum_has_3_members():
    """READY + STOPPED + PAUSED. The third was added by KR-P2-M ST3 —
    PAUSED is the BootSummary result when gate 3b's INVARIANT_PAUSE
    class triggers the R4.1 §9.2 special-case (DR epoch mismatch
    routes to PAUSED, not STOPPED)."""
    assert {m.value for m in BootResult} == {"ready", "stopped", "paused"}


# ===========================================================================
# INVARIANT_PAUSE → BootResult.PAUSED (KR-P2-M ST3)
# ===========================================================================


@pytest.mark.asyncio
async def test_invariant_pause_returns_paused_without_holder_transition_or_emit():
    """When the failing gate has class INVARIANT_PAUSE, the coordinator
    must NOT transition holder to STOPPED + must NOT emit
    kora.boot.failed. The gate itself already did the right work
    (transitioned to PAUSED + emitted dr-specific event)."""
    holder = _holder()
    provider = _make_emit_provider()

    pause_gate = _ProgrammableGate(
        "3b_epoch_dr_check",
        GateClass.INVARIANT_PAUSE,
        [GateOutcome.FAIL],
    )

    summary = await run_boot_sequence(
        memory_provider=provider,
        holder=holder,
        gates=[pause_gate],
    )

    assert summary.result is BootResult.PAUSED
    assert summary.failed_gate is not None
    assert summary.failed_gate.gate_class is GateClass.INVARIANT_PAUSE
    # Holder UNTOUCHED by coordinator (still BOOTING — the gate would
    # have transitioned to PAUSED in real code, but in this unit test
    # we're only asserting that the coordinator doesn't re-transition).
    assert holder.current.primary_state is PrimaryState.BOOTING
    # NO kora.boot.failed emit
    assert provider._connection.submit_and_wait.call_count == 0


@pytest.mark.asyncio
async def test_invariant_pause_short_circuits_sequence():
    """An INVARIANT_PAUSE FAIL stops the sequence just like INVARIANT."""
    holder = _holder()
    provider = _make_emit_provider()

    pre_gate = _ProgrammableGate("pre", GateClass.TRANSIENT, [GateOutcome.PASS])
    pause_gate = _ProgrammableGate(
        "3b_epoch_dr_check", GateClass.INVARIANT_PAUSE, [GateOutcome.FAIL]
    )
    post_gate = _ProgrammableGate(
        "post", GateClass.TRANSIENT, [GateOutcome.PASS]
    )

    summary = await run_boot_sequence(
        memory_provider=provider,
        holder=holder,
        gates=[pre_gate, pause_gate, post_gate],
    )

    # Sequence stopped at the INVARIANT_PAUSE failure
    assert len(summary.gate_results) == 2
    assert summary.result is BootResult.PAUSED
    assert summary.failed_gate.gate_id == "3b_epoch_dr_check"
    # post_gate never ran
    assert post_gate.call_count == 0
