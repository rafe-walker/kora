"""Unit tests for ``agent/boot_gates.py`` (KR-P2-H ST1 framework).

Covers:
  - GateClass / GateOutcome enum shape
  - GateResult immutability + default attempts field
  - BootContext mutability + defaults
  - BootGateRunner constructor validation
  - run_all production-mode behavior:
      * all gates PASS → full result list
      * INVARIANT FAIL → short-circuit immediately
      * TRANSIENT FAIL within budget → backoff + retry, eventual PASS
      * TRANSIENT FAIL exhausts budget → terminal FAIL
  - run_all diagnostic-mode behavior:
      * no retry on TRANSIENT
      * no short-circuit on FAIL
      * single attempt per gate
  - Exception in gate.run is caught + wrapped as FAIL
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import ClassVar, Optional

import pytest

from agent.boot_gates import (
    BootContext,
    BootGateRunner,
    Gate,
    GateClass,
    GateOutcome,
    GateResult,
)


# ---------------------------------------------------------------------------
# Test gates — programmable PASS/FAIL sequence
# ---------------------------------------------------------------------------


class _ProgrammableGate(Gate):
    """A gate whose run() outcome is driven by a queue of pre-baked results."""

    def __init__(
        self,
        gate_id: str,
        gate_class: GateClass,
        outcomes: list[GateOutcome],
        title: str = "test gate",
    ) -> None:
        self.gate_id = gate_id  # type: ignore[misc]
        self.gate_class = gate_class  # type: ignore[misc]
        self.title = title  # type: ignore[misc]
        self._outcomes = list(outcomes)
        self.call_count = 0

    async def run(self, context: BootContext) -> GateResult:
        self.call_count += 1
        if not self._outcomes:
            raise RuntimeError(
                f"{self.gate_id}: programmed outcomes exhausted on call "
                f"{self.call_count}"
            )
        outcome = self._outcomes.pop(0)
        now = datetime.now(timezone.utc)
        return GateResult(
            gate_id=self.gate_id,
            gate_class=self.gate_class,
            outcome=outcome,
            detail=f"call#{self.call_count}",
            elapsed_ms=1,
            started_at=now,
            completed_at=now,
        )


class _RaisingGate(Gate):
    """A gate that raises an uncaught exception when run."""

    gate_id: ClassVar[str] = "test_raising"
    gate_class: ClassVar[GateClass] = GateClass.TRANSIENT
    title: ClassVar[str] = "raising gate"

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.call_count = 0

    async def run(self, context: BootContext) -> GateResult:
        self.call_count += 1
        raise self._exc


# ---------------------------------------------------------------------------
# Enum + dataclass shape
# ---------------------------------------------------------------------------


def test_gate_class_has_3_members():
    """TRANSIENT + INVARIANT + INVARIANT_PAUSE. The third was added by
    KR-P2-M ST3 (gate 3b epoch mismatch routes to PAUSED, not STOPPED)."""
    assert {m.value for m in GateClass} == {
        "transient", "invariant", "invariant_pause"
    }
    assert len(list(GateClass)) == 3


def test_gate_outcome_has_2_members():
    assert {m.value for m in GateOutcome} == {"pass", "fail"}


def test_gate_result_is_frozen():
    now = datetime.now(timezone.utc)
    r = GateResult(
        gate_id="g",
        gate_class=GateClass.TRANSIENT,
        outcome=GateOutcome.PASS,
        detail="ok",
        elapsed_ms=1,
        started_at=now,
        completed_at=now,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.outcome = GateOutcome.FAIL  # type: ignore[misc]


def test_gate_result_default_attempts_is_1():
    now = datetime.now(timezone.utc)
    r = GateResult(
        gate_id="g",
        gate_class=GateClass.TRANSIENT,
        outcome=GateOutcome.PASS,
        detail="ok",
        elapsed_ms=1,
        started_at=now,
        completed_at=now,
    )
    assert r.attempts == 1


def test_boot_context_defaults_to_optional_none_fields():
    ctx = BootContext()
    assert ctx.memory_provider is None
    assert ctx.holder is None
    assert ctx.workspace_id is None
    assert ctx.kora_actor_uuid is None
    assert ctx.extras == {}


def test_boot_context_is_mutable_for_cross_gate_state():
    """Gates write into the context as they run — intentionally mutable."""
    ctx = BootContext()
    ctx.kora_actor_uuid = "uuid-1"
    ctx.extras["custom_key"] = "value"
    assert ctx.kora_actor_uuid == "uuid-1"
    assert ctx.extras["custom_key"] == "value"


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


def test_runner_rejects_retry_budget_below_one():
    with pytest.raises(ValueError, match="retry_budget"):
        BootGateRunner(gates=[], context=BootContext(), retry_budget=0)


def test_runner_rejects_non_positive_backoff_base():
    with pytest.raises(ValueError, match="backoff_base"):
        BootGateRunner(
            gates=[], context=BootContext(), backoff_base_seconds=0
        )


def test_runner_rejects_cap_lower_than_base():
    with pytest.raises(ValueError, match="backoff_cap"):
        BootGateRunner(
            gates=[],
            context=BootContext(),
            backoff_base_seconds=10.0,
            backoff_cap_seconds=5.0,
        )


# ---------------------------------------------------------------------------
# Production mode — all gates pass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_all_returns_full_list_when_all_gates_pass():
    g1 = _ProgrammableGate("g1", GateClass.TRANSIENT, [GateOutcome.PASS])
    g2 = _ProgrammableGate("g2", GateClass.INVARIANT, [GateOutcome.PASS])
    g3 = _ProgrammableGate("g3", GateClass.TRANSIENT, [GateOutcome.PASS])

    runner = BootGateRunner(gates=[g1, g2, g3], context=BootContext())
    results = await runner.run_all()

    assert len(results) == 3
    assert all(r.outcome is GateOutcome.PASS for r in results)
    assert [r.gate_id for r in results] == ["g1", "g2", "g3"]
    assert [r.attempts for r in results] == [1, 1, 1]


# ---------------------------------------------------------------------------
# Production mode — INVARIANT fail short-circuits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invariant_fail_stops_sequence_immediately():
    g1 = _ProgrammableGate("g1", GateClass.TRANSIENT, [GateOutcome.PASS])
    g2 = _ProgrammableGate("g2", GateClass.INVARIANT, [GateOutcome.FAIL])
    g3 = _ProgrammableGate("g3", GateClass.TRANSIENT, [GateOutcome.PASS])

    runner = BootGateRunner(gates=[g1, g2, g3], context=BootContext())
    results = await runner.run_all()

    # Sequence stops at g2; g3 never runs.
    assert len(results) == 2
    assert results[0].outcome is GateOutcome.PASS
    assert results[1].outcome is GateOutcome.FAIL
    assert results[1].gate_id == "g2"
    assert results[1].attempts == 1  # INVARIANT — no retry
    assert g3.call_count == 0


# ---------------------------------------------------------------------------
# Production mode — TRANSIENT retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_retries_and_eventually_passes():
    """A transient gate that FAILs twice then PASSes on attempt 3
    yields a single PASS GateResult with attempts=3."""
    g = _ProgrammableGate(
        "g_transient",
        GateClass.TRANSIENT,
        [GateOutcome.FAIL, GateOutcome.FAIL, GateOutcome.PASS],
    )
    runner = BootGateRunner(
        gates=[g],
        context=BootContext(),
        retry_budget=5,
        # Drop backoff so the test runs fast.
        backoff_base_seconds=0.001,
        backoff_cap_seconds=0.002,
    )
    results = await runner.run_all()

    assert len(results) == 1
    assert results[0].outcome is GateOutcome.PASS
    assert results[0].attempts == 3
    assert g.call_count == 3


@pytest.mark.asyncio
async def test_transient_exhausts_budget_terminal_fail():
    """A transient gate that FAILs every attempt until budget exhausted
    yields a terminal FAIL with attempts == retry_budget."""
    g = _ProgrammableGate(
        "g_transient",
        GateClass.TRANSIENT,
        [GateOutcome.FAIL] * 10,  # enough fails to exhaust any budget
    )
    runner = BootGateRunner(
        gates=[g],
        context=BootContext(),
        retry_budget=3,
        backoff_base_seconds=0.001,
        backoff_cap_seconds=0.002,
    )
    results = await runner.run_all()

    assert len(results) == 1
    assert results[0].outcome is GateOutcome.FAIL
    assert results[0].attempts == 3
    assert g.call_count == 3


@pytest.mark.asyncio
async def test_transient_fail_after_retry_stops_subsequent_gates():
    """Production-mode short-circuit applies to TRANSIENT exhaustion too."""
    g1 = _ProgrammableGate(
        "g1", GateClass.TRANSIENT, [GateOutcome.FAIL, GateOutcome.FAIL]
    )
    g2 = _ProgrammableGate("g2", GateClass.TRANSIENT, [GateOutcome.PASS])

    runner = BootGateRunner(
        gates=[g1, g2],
        context=BootContext(),
        retry_budget=2,
        backoff_base_seconds=0.001,
        backoff_cap_seconds=0.002,
    )
    results = await runner.run_all()

    assert len(results) == 1  # g2 never runs
    assert results[0].outcome is GateOutcome.FAIL
    assert g2.call_count == 0


# ---------------------------------------------------------------------------
# Diagnostic mode — no retry, no short-circuit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diagnostic_mode_runs_all_gates_regardless_of_failures():
    g1 = _ProgrammableGate("g1", GateClass.INVARIANT, [GateOutcome.FAIL])
    g2 = _ProgrammableGate("g2", GateClass.TRANSIENT, [GateOutcome.PASS])
    g3 = _ProgrammableGate("g3", GateClass.TRANSIENT, [GateOutcome.FAIL])

    runner = BootGateRunner(
        gates=[g1, g2, g3],
        context=BootContext(),
        diagnostic_mode=True,
    )
    results = await runner.run_all()

    # All three ran (no short-circuit).
    assert len(results) == 3
    assert [r.outcome for r in results] == [
        GateOutcome.FAIL,
        GateOutcome.PASS,
        GateOutcome.FAIL,
    ]


@pytest.mark.asyncio
async def test_diagnostic_mode_does_not_retry_transient_failures():
    """Diagnostic mode is a probe — each gate runs exactly once."""
    g = _ProgrammableGate(
        "g_transient",
        GateClass.TRANSIENT,
        [GateOutcome.FAIL] * 5,  # enough fails to retry if not diagnostic
    )
    runner = BootGateRunner(
        gates=[g],
        context=BootContext(),
        diagnostic_mode=True,
        retry_budget=5,  # ignored in diagnostic mode
    )
    results = await runner.run_all()

    assert len(results) == 1
    assert results[0].outcome is GateOutcome.FAIL
    assert results[0].attempts == 1
    assert g.call_count == 1


def test_diagnostic_mode_property_reflects_constructor_arg():
    r_prod = BootGateRunner(gates=[], context=BootContext())
    r_diag = BootGateRunner(
        gates=[], context=BootContext(), diagnostic_mode=True
    )
    assert r_prod.diagnostic_mode is False
    assert r_diag.diagnostic_mode is True


# ---------------------------------------------------------------------------
# Exception handling — uncaught gate exceptions become FAIL results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uncaught_exception_in_gate_becomes_fail_result():
    """A gate that raises is not a programmer-error-out condition for
    the runner; the runner wraps it as a FAIL result so the sequence
    can short-circuit gracefully."""
    g = _RaisingGate(RuntimeError("substrate dispatch down"))
    runner = BootGateRunner(
        gates=[g],
        context=BootContext(),
        # Retry-1 so we don't burn ~30s of backoff in the test.
        retry_budget=1,
        backoff_base_seconds=0.001,
        backoff_cap_seconds=0.002,
    )
    results = await runner.run_all()

    assert len(results) == 1
    assert results[0].outcome is GateOutcome.FAIL
    assert "substrate dispatch down" in results[0].detail
    assert "unexpected exception" in results[0].detail


@pytest.mark.asyncio
async def test_uncaught_exception_retries_under_transient_budget():
    """Exceptions are FAILs; TRANSIENT gates retry on FAIL."""
    g = _RaisingGate(RuntimeError("flaky network"))
    runner = BootGateRunner(
        gates=[g],
        context=BootContext(),
        retry_budget=3,
        backoff_base_seconds=0.001,
        backoff_cap_seconds=0.002,
    )
    results = await runner.run_all()

    # Exhausted budget → terminal FAIL with attempts=3.
    assert len(results) == 1
    assert results[0].outcome is GateOutcome.FAIL
    assert results[0].attempts == 3
    assert g.call_count == 3
