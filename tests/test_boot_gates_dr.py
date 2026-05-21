"""Unit tests for ``agent/boot_gates_dr.py`` (KR-P2-M ST1).

Covers ``SubstrateContractVersionGate``:
  - PASS when substrate's version matches Kora's EXPECTED constant
  - FAIL when substrate's version differs (INVARIANT class)
  - FAIL on substrate read raise
  - FAIL on no-provider / no-connection
  - FAIL on unexpected response shape

Also asserts the gate is registered in
``build_default_gate_sequence`` at position 2 (after gate 1, before
gate 4) per the R4.1 §9.2 ordering.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable, Optional
from unittest.mock import MagicMock

import pytest

from agent.boot_gates import BootContext, GateClass, GateOutcome
from agent.boot_gates_dr import (
    EXPECTED_SUBSTRATE_CONTRACT_VERSION,
    SubstrateContractVersionGate,
)
from agent.boot_gates_impl import build_default_gate_sequence


# ---------------------------------------------------------------------------
# Fake provider
# ---------------------------------------------------------------------------


def _close_and_return(returns: Any) -> Callable:
    def _side_effect(coro, *, timeout=None):
        if hasattr(coro, "close"):
            try:
                coro.close()
            except Exception:
                pass
        return returns
    return _side_effect


def _close_and_raise(exc: BaseException) -> Callable:
    def _side_effect(coro, *, timeout=None):
        if hasattr(coro, "close"):
            try:
                coro.close()
            except Exception:
                pass
        raise exc
    return _side_effect


def _make_provider(
    *,
    submit_side_effect: Optional[Callable] = None,
    no_connection: bool = False,
) -> Optional[SimpleNamespace]:
    """Build a stubbed IsoKronMemoryProvider whose submit_and_wait
    drives the gate's asyncpg fetch."""
    if no_connection:
        return SimpleNamespace(_connection=None)
    if submit_side_effect is None:
        submit_side_effect = _close_and_return(
            EXPECTED_SUBSTRATE_CONTRACT_VERSION
        )
    connection = SimpleNamespace(
        get_pg_pool=MagicMock(return_value="fake-pool"),
        submit_and_wait=MagicMock(side_effect=submit_side_effect),
    )
    return SimpleNamespace(_connection=connection)


def _context(provider) -> BootContext:
    return BootContext(memory_provider=provider)


# ---------------------------------------------------------------------------
# Class-attribute sanity
# ---------------------------------------------------------------------------


def test_gate_class_attributes():
    assert SubstrateContractVersionGate.gate_id == "3_substrate_contract_version"
    assert SubstrateContractVersionGate.gate_class is GateClass.INVARIANT
    assert "substrate_contract_version" in SubstrateContractVersionGate.title


def test_expected_version_is_a_positive_integer():
    """The compiled-in constant must be positive (substrate's CHECK
    enforces > 0); mismatch with the substrate default of 1 is the
    initial value's contract."""
    assert isinstance(EXPECTED_SUBSTRATE_CONTRACT_VERSION, int)
    assert EXPECTED_SUBSTRATE_CONTRACT_VERSION > 0


# ---------------------------------------------------------------------------
# PASS path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_passes_when_substrate_version_matches_expected():
    provider = _make_provider(
        submit_side_effect=_close_and_return(EXPECTED_SUBSTRATE_CONTRACT_VERSION),
    )
    result = await SubstrateContractVersionGate().run(_context(provider))
    assert result.outcome is GateOutcome.PASS
    assert str(EXPECTED_SUBSTRATE_CONTRACT_VERSION) in result.detail
    assert "matches" in result.detail


# ---------------------------------------------------------------------------
# FAIL paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fails_when_substrate_version_differs_from_expected():
    drifted = EXPECTED_SUBSTRATE_CONTRACT_VERSION + 1
    provider = _make_provider(
        submit_side_effect=_close_and_return(drifted),
    )
    result = await SubstrateContractVersionGate().run(_context(provider))
    assert result.outcome is GateOutcome.FAIL
    assert "mismatch" in result.detail
    assert f"expected={EXPECTED_SUBSTRATE_CONTRACT_VERSION}" in result.detail
    assert f"actual={drifted}" in result.detail


@pytest.mark.asyncio
async def test_fails_when_substrate_read_raises():
    provider = _make_provider(
        submit_side_effect=_close_and_raise(RuntimeError("substrate down")),
    )
    result = await SubstrateContractVersionGate().run(_context(provider))
    assert result.outcome is GateOutcome.FAIL
    assert "substrate_contract_version() read raised" in result.detail
    assert "substrate down" in result.detail


@pytest.mark.asyncio
async def test_fails_when_no_memory_provider():
    result = await SubstrateContractVersionGate().run(_context(None))
    assert result.outcome is GateOutcome.FAIL
    assert "memory_provider is not set" in result.detail


@pytest.mark.asyncio
async def test_fails_when_no_connection():
    provider = _make_provider(no_connection=True)
    result = await SubstrateContractVersionGate().run(_context(provider))
    assert result.outcome is GateOutcome.FAIL
    assert "_connection is not initialized" in result.detail


@pytest.mark.asyncio
async def test_fails_when_response_shape_unexpected():
    provider = _make_provider(
        submit_side_effect=_close_and_return("not-an-int"),
    )
    result = await SubstrateContractVersionGate().run(_context(provider))
    assert result.outcome is GateOutcome.FAIL
    assert "unexpected" in result.detail


# ---------------------------------------------------------------------------
# Sequence registration
# ---------------------------------------------------------------------------


def test_gate_registered_in_default_sequence_after_gate_1_before_gate_4():
    seq = build_default_gate_sequence()
    ids = [g.gate_id for g in seq]
    # Gate 3 must come after gate 1 and before gate 4
    g1_idx = ids.index("1_claude_auth")
    g3_idx = ids.index("3_substrate_contract_version")
    g4_idx = ids.index("4_kora_runtime_role_perms")
    assert g1_idx < g3_idx < g4_idx
    # Specifically: gate 3 sits at position 2 (right after gate 1)
    assert g3_idx == g1_idx + 1


def test_default_sequence_has_all_kr_p2_h_gates_plus_gate_3_and_3b():
    """Sanity: KR-P2-M ST1 + ST3 don't drop any pre-existing gate;
    they insert gate 3 + gate 3b alongside the seven from KR-P2-H."""
    seq = build_default_gate_sequence()
    ids = {g.gate_id for g in seq}
    assert ids == {
        "1_claude_auth",
        "3_substrate_contract_version",   # KR-P2-M ST1
        "3b_epoch_dr_check",               # KR-P2-M ST3
        "4_kora_runtime_role_perms",
        "5_kronicle_mcp_reachable",
        "6_wsk_token_valid",
        "7_canonical_kora_actor",
        "8_charter_capability_matrix_load",
        "10_kr7_boot_smoke",
    }
    assert len(seq) == 9


# ===========================================================================
# Gate 3b — Gate3bEpochCheck (KR-P2-M ST3)
# ===========================================================================


from unittest.mock import AsyncMock
from agent.boot_gates_dr import Gate3bEpochCheck
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
def _reset_holder_between_tests():
    _reset_holder_for_tests()
    yield
    _reset_holder_for_tests()


def _epoch_dispatch(*, substrate: int, known) -> Callable:
    """Build a submit_and_wait side_effect that returns substrate then
    known on consecutive calls."""
    calls: list[int] = []

    def _side_effect(coro, *, timeout=None):
        if hasattr(coro, "close"):
            try:
                coro.close()
            except Exception:
                pass
        calls.append(len(calls))
        # First call → substrate_epoch read; second → kora_known_epoch
        if len(calls) == 1:
            return substrate
        return known

    return _side_effect


def _context_with_holder(provider) -> BootContext:
    holder = init_holder(
        OperationalState(
            primary_state=PrimaryState.BOOTING,
            claim_permission=ClaimPermission.NONE,
        )
    )
    return BootContext(memory_provider=provider, holder=holder)


# ----- Gate class attributes -----


def test_gate_3b_class_attributes():
    assert Gate3bEpochCheck.gate_id == "3b_epoch_dr_check"
    assert Gate3bEpochCheck.gate_class is GateClass.INVARIANT_PAUSE


# ----- PASS paths -----


@pytest.mark.asyncio
async def test_gate_3b_passes_on_first_boot_when_known_is_null():
    provider = _make_provider(
        submit_side_effect=_epoch_dispatch(substrate=7, known=None),
    )
    ctx = _context_with_holder(provider)
    result = await Gate3bEpochCheck().run(ctx)
    assert result.outcome is GateOutcome.PASS
    assert "first boot" in result.detail
    assert "substrate_epoch=7" in result.detail
    # Holder still in BOOTING (no transition for PASS)
    assert ctx.holder.current.primary_state is PrimaryState.BOOTING


@pytest.mark.asyncio
async def test_gate_3b_passes_when_epochs_match():
    provider = _make_provider(
        submit_side_effect=_epoch_dispatch(substrate=5, known=5),
    )
    ctx = _context_with_holder(provider)
    result = await Gate3bEpochCheck().run(ctx)
    assert result.outcome is GateOutcome.PASS
    assert "epochs match" in result.detail
    assert ctx.holder.current.primary_state is PrimaryState.BOOTING


# ----- FAIL + PAUSED transition on mismatch -----


@pytest.mark.asyncio
async def test_gate_3b_fails_with_paused_transition_on_mismatch(monkeypatch):
    """Mismatch invokes dr_handler → emits kora.dr.observed +
    transitions holder to PAUSED{substrate}. Gate returns FAIL with
    class INVARIANT_PAUSE so the coordinator routes to BootResult.PAUSED."""
    provider = _make_provider(
        submit_side_effect=_epoch_dispatch(substrate=8, known=3),
    )
    ctx = _context_with_holder(provider)

    # Stub the chain emit so the test doesn't hit substrate.
    monkeypatch.setattr(
        "agent.dr_handler._emit_dr_observed", AsyncMock(return_value=None)
    )

    result = await Gate3bEpochCheck().run(ctx)
    assert result.outcome is GateOutcome.FAIL
    assert result.gate_class is GateClass.INVARIANT_PAUSE
    assert "epoch mismatch" in result.detail
    assert "substrate_epoch=8" in result.detail
    assert "kora_known_epoch=3" in result.detail

    # Holder transitioned to PAUSED with SUBSTRATE reason.
    assert ctx.holder.current.primary_state is PrimaryState.PAUSED
    assert DegradationReason.SUBSTRATE in ctx.holder.current.degradation_reasons


# ----- FAIL paths (no transition) -----


@pytest.mark.asyncio
async def test_gate_3b_fails_when_no_provider():
    holder = init_holder(
        OperationalState(primary_state=PrimaryState.BOOTING)
    )
    ctx = BootContext(memory_provider=None, holder=holder)
    result = await Gate3bEpochCheck().run(ctx)
    assert result.outcome is GateOutcome.FAIL
    assert "memory_provider is not set" in result.detail
    # Holder untouched
    assert ctx.holder.current.primary_state is PrimaryState.BOOTING


@pytest.mark.asyncio
async def test_gate_3b_fails_when_no_holder():
    provider = _make_provider(
        submit_side_effect=_epoch_dispatch(substrate=5, known=5),
    )
    ctx = BootContext(memory_provider=provider, holder=None)
    result = await Gate3bEpochCheck().run(ctx)
    assert result.outcome is GateOutcome.FAIL
    assert "holder is not set" in result.detail


@pytest.mark.asyncio
async def test_gate_3b_fails_when_no_connection():
    provider = _make_provider(no_connection=True)
    ctx = _context_with_holder(provider)
    result = await Gate3bEpochCheck().run(ctx)
    assert result.outcome is GateOutcome.FAIL
    assert "_connection is not initialized" in result.detail


@pytest.mark.asyncio
async def test_gate_3b_fails_when_substrate_read_raises():
    provider = _make_provider(
        submit_side_effect=_close_and_raise(RuntimeError("substrate down")),
    )
    ctx = _context_with_holder(provider)
    result = await Gate3bEpochCheck().run(ctx)
    assert result.outcome is GateOutcome.FAIL
    assert "epoch read raised" in result.detail
    assert "substrate down" in result.detail
    # Holder untouched (read failed before handler invoked)
    assert ctx.holder.current.primary_state is PrimaryState.BOOTING


# ----- Sequence registration -----


def test_gate_3b_registered_between_gate_3_and_gate_4():
    seq = build_default_gate_sequence()
    ids = [g.gate_id for g in seq]
    g3 = ids.index("3_substrate_contract_version")
    g3b = ids.index("3b_epoch_dr_check")
    g4 = ids.index("4_kora_runtime_role_perms")
    assert g3 < g3b < g4
    # Specifically adjacent
    assert g3b == g3 + 1
    assert g4 == g3b + 1
