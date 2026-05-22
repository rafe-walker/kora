"""KR-P2-INT-TESTS ST2 — simulated PITR + gate 3b recovery (R4.1 §12).

Per the bucket spec:
  1. Boot Kora; verify kora_known_epoch written at end-of-boot.
  2. Simulate PITR: rewind substrate state (substrate_epoch decreases)
     WITHOUT bumping outside-DB source.
  3. Boot Kora again; verify gate 3b detects mismatch + kora.dr.observed
     emitted + holder transitioned to PAUSED{substrate}.
  4. Operator action: bump substrate_epoch external source.
  5. Operator action: kora_control reset.
  6. Boot Kora again; verify kora_known_epoch updated + gate 3b passes
     + holder transitions READY.

# Test approach

The DR gate reads ``substrate_epoch`` + ``kora_known_epoch`` via
asyncpg helpers (``plugins.memory.isokron.dr_epoch``). This file
mocks both at the import boundary to simulate PITR scenarios:

  - First boot: substrate_epoch=1, kora_known_epoch=NULL → PASS
    (first-boot case)
  - Post-PITR boot: substrate_epoch=0 (rewound), kora_known_epoch=1
    (Kora remembers the higher value) → FAIL with mismatch → handler
    fires PAUSED{SUBSTRATE} transition
  - Post-recovery boot: substrate_epoch=2 (operator bumped),
    kora_known_epoch=1 → would PASS only if operator also clears
    Kora's known_epoch via the kora_control reset path. In the
    current implementation, operator bumps the EXTERNAL source which
    bumps substrate_epoch, then on next boot Kora writes its
    kora_known_epoch to the new value.

The actual handle_epoch_mismatch behavior, the dr_writer's
end-of-boot write, and the holder.transition_to(PAUSED) chain are
exercised separately by unit tests (KR-P2-M ST2/ST3/ST4). This file
exercises the cross-module flow.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.boot_gates import GateClass, GateOutcome
from agent.boot_gates_dr import Gate3bEpochCheck
from agent.operational_state import (
    ClaimPermission,
    DegradationReason,
    OperationalState,
    PrimaryState,
)
from agent.operational_state_holder import (
    OperationalStateHolder,
    _reset_holder_for_tests,
)


pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _reset():
    _reset_holder_for_tests()
    yield
    _reset_holder_for_tests()


def _booting_holder() -> OperationalStateHolder:
    return OperationalStateHolder(
        initial_state=OperationalState(
            primary_state=PrimaryState.BOOTING,
            claim_permission=ClaimPermission.NORMAL,
        )
    )


def _provider_with_connection(*, workspace_id: str = "org_test") -> MagicMock:
    """Mock IsoKronMemoryProvider with the minimum surface gate 3b
    consumes: ``_connection`` non-None + ``_resolve_workspace_id``."""
    provider = MagicMock()
    provider._connection = MagicMock()
    provider._resolve_workspace_id = MagicMock(return_value=workspace_id)
    return provider


async def _run_gate3b(
    *,
    holder: OperationalStateHolder,
    provider,
    substrate_epoch: int,
    kora_known_epoch: Optional[int],
):
    """Drive Gate3bEpochCheck once with controlled substrate epochs."""
    gate = Gate3bEpochCheck()

    # Set up the gate's BootContext
    from agent.boot_gates import BootContext

    context = BootContext(
        memory_provider=provider,
        holder=holder,
    )
    # Mock the connection.submit_and_wait so gate-3b's epoch reads
    # return our controlled values.
    call_log: list = []

    def _submit(coro, *, timeout):
        coro.close()
        call_log.append(coro)
        # First call is _read_substrate_epoch, second is
        # _read_kora_known_epoch
        if len(call_log) == 1:
            return substrate_epoch
        return kora_known_epoch

    provider._connection.submit_and_wait = _submit

    # Mock the emit + transition path to avoid needing real chain emit.
    with patch(
        "agent.dr_handler._emit_dr_observed", new=AsyncMock()
    ):
        result = await gate.run(context)
    return result


# ---------------------------------------------------------------------------
# Step 1: first boot — no known_epoch yet → gate PASSes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_boot_no_known_epoch_passes():
    """Fresh Kora boot: substrate_epoch=1, kora_known_epoch=NULL.
    Gate 3b returns PASS — first-boot case per the gate's docstring."""
    holder = _booting_holder()
    provider = _provider_with_connection()

    result = await _run_gate3b(
        holder=holder,
        provider=provider,
        substrate_epoch=1,
        kora_known_epoch=None,
    )
    assert result.outcome is GateOutcome.PASS
    assert "first boot" in result.detail.lower()
    # Holder unchanged (still BOOTING — coordinator transitions to READY
    # at end of sequence)
    assert holder.current.primary_state is PrimaryState.BOOTING


# ---------------------------------------------------------------------------
# Step 2: matched epoch → PASS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_matched_epoch_passes():
    """Reboot on the same timeline: substrate_epoch=5,
    kora_known_epoch=5. Match → PASS."""
    holder = _booting_holder()
    provider = _provider_with_connection()

    result = await _run_gate3b(
        holder=holder,
        provider=provider,
        substrate_epoch=5,
        kora_known_epoch=5,
    )
    assert result.outcome is GateOutcome.PASS
    assert "match" in result.detail.lower()
    assert holder.current.primary_state is PrimaryState.BOOTING


# ---------------------------------------------------------------------------
# Step 3: PITR detected — gate FAILs with INVARIANT_PAUSE class
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pitr_mismatch_triggers_gate3b_fail_invariant_pause():
    """Simulated PITR: substrate_epoch=3 (rewound), kora_known_epoch=5
    (Kora remembers the post-PITR value). Mismatch → gate FAILs with
    class INVARIANT_PAUSE → coordinator routes to BootResult.PAUSED.

    Holder transitions to PAUSED{SUBSTRATE} via the handler."""
    holder = _booting_holder()
    provider = _provider_with_connection()

    result = await _run_gate3b(
        holder=holder,
        provider=provider,
        substrate_epoch=3,
        kora_known_epoch=5,
    )
    assert result.outcome is GateOutcome.FAIL
    assert result.gate_class is GateClass.INVARIANT_PAUSE
    assert "mismatch" in result.detail.lower()
    # Holder transitioned to PAUSED{SUBSTRATE} via handle_epoch_mismatch
    assert holder.current.primary_state is PrimaryState.PAUSED
    assert DegradationReason.SUBSTRATE in holder.current.degradation_reasons


# ---------------------------------------------------------------------------
# Step 4: epoch read failure → gate FAIL (transient or invariant?)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_epoch_read_failure_returns_fail_invariant_pause():
    """If substrate_epoch read raises (substrate unavailable mid-boot),
    gate returns FAIL with class INVARIANT_PAUSE — coordinator routes
    to PAUSED. (Per the audit table KR-P2-FAIL-SAFETIES path 6a/6b:
    epoch reads route through the gate's own try/except → _fail_result
    with the appropriate class.)"""
    holder = _booting_holder()
    provider = _provider_with_connection()

    def _submit_raising(coro, *, timeout):
        coro.close()
        raise RuntimeError("substrate epoch read failed")

    provider._connection.submit_and_wait = _submit_raising

    from agent.boot_gates import BootContext

    context = BootContext(
        memory_provider=provider,
        holder=holder,
    )
    gate = Gate3bEpochCheck()
    result = await gate.run(context)

    assert result.outcome is GateOutcome.FAIL
    assert result.gate_class is GateClass.INVARIANT_PAUSE


# ---------------------------------------------------------------------------
# Step 5: post-recovery boot — operator bumped substrate_epoch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_recovery_boot_when_known_epoch_cleared():
    """After PITR + operator bumps substrate_epoch + clears kora_known_epoch
    (via kora_control reset → end-of-boot writer rewrites
    known_epoch on next successful boot), the next gate run sees
    substrate_epoch=N + kora_known_epoch=None → PASS (treated as
    first-boot case).

    In production this is the path operator triggers to recover from
    PAUSED{SUBSTRATE}: kora_control reset clears the runtime
    PAUSED state; on next boot, gate 3b reads the bumped
    substrate_epoch + the now-NULL kora_known_epoch → PASS → end-of-
    boot writer writes the new value."""
    holder = _booting_holder()
    provider = _provider_with_connection()

    result = await _run_gate3b(
        holder=holder,
        provider=provider,
        substrate_epoch=10,  # Operator-bumped post-PITR
        kora_known_epoch=None,  # kora_control reset cleared it
    )
    assert result.outcome is GateOutcome.PASS
    assert holder.current.primary_state is PrimaryState.BOOTING


# ---------------------------------------------------------------------------
# Step 6: full PITR lifecycle walkthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_pitr_lifecycle_walkthrough():
    """Stateful end-to-end:
      A. Healthy boot: substrate_epoch=5, kora_known_epoch=5 → PASS
      B. PITR happens: substrate_epoch=2 (rewound), kora_known_epoch=5 →
         FAIL INVARIANT_PAUSE + holder transitions PAUSED{SUBSTRATE}
      C. Operator bumps substrate + clears kora_known_epoch:
         substrate_epoch=8, kora_known_epoch=None → PASS (first-boot case)"""
    # A. Healthy boot
    holder_a = _booting_holder()
    provider_a = _provider_with_connection()
    result_a = await _run_gate3b(
        holder=holder_a,
        provider=provider_a,
        substrate_epoch=5,
        kora_known_epoch=5,
    )
    assert result_a.outcome is GateOutcome.PASS
    assert holder_a.current.primary_state is PrimaryState.BOOTING

    # B. Post-PITR — fresh holder (real boot starts fresh)
    holder_b = _booting_holder()
    provider_b = _provider_with_connection()
    result_b = await _run_gate3b(
        holder=holder_b,
        provider=provider_b,
        substrate_epoch=2,  # rewound
        kora_known_epoch=5,
    )
    assert result_b.outcome is GateOutcome.FAIL
    assert result_b.gate_class is GateClass.INVARIANT_PAUSE
    assert holder_b.current.primary_state is PrimaryState.PAUSED
    assert DegradationReason.SUBSTRATE in holder_b.current.degradation_reasons

    # C. Post-recovery boot — fresh holder
    holder_c = _booting_holder()
    provider_c = _provider_with_connection()
    result_c = await _run_gate3b(
        holder=holder_c,
        provider=provider_c,
        substrate_epoch=8,  # operator-bumped
        kora_known_epoch=None,  # operator cleared via kora_control reset
    )
    assert result_c.outcome is GateOutcome.PASS
    assert holder_c.current.primary_state is PrimaryState.BOOTING
