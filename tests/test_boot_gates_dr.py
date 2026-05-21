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


def test_default_sequence_has_all_kr_p2_h_gates_plus_gate_3():
    """Sanity: KR-P2-M ST1 doesn't drop any pre-existing gate; it
    inserts gate 3 alongside the seven from KR-P2-H."""
    seq = build_default_gate_sequence()
    ids = {g.gate_id for g in seq}
    assert ids == {
        "1_claude_auth",
        "3_substrate_contract_version",  # NEW
        "4_kora_runtime_role_perms",
        "5_kronicle_mcp_reachable",
        "6_wsk_token_valid",
        "7_canonical_kora_actor",
        "8_charter_capability_matrix_load",
        "10_kr7_boot_smoke",
    }
    assert len(seq) == 8
