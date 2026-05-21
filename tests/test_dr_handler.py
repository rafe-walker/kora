"""Unit tests for ``agent/dr_handler.py`` (KR-P2-M ST3).

Covers ``handle_epoch_mismatch``:
  - Successful emit + holder transition to PAUSED{substrate}
  - Emit failure doesn't block the transition (best-effort)
  - Missing provider / connection / workspace_id → emit skipped,
    transition still fires
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.dr_handler import DR_OBSERVED_EVENT, handle_epoch_mismatch
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
# Fixtures
# ---------------------------------------------------------------------------


def _holder():
    return init_holder(
        OperationalState(
            primary_state=PrimaryState.BOOTING,
            claim_permission=ClaimPermission.NONE,
        )
    )


def _make_provider(
    *,
    submit_returns="evt-001",
    submit_raises=None,
    workspace_id="ws-test",
) -> SimpleNamespace:
    def _submit(coro, *, timeout=10.0):
        if hasattr(coro, "close"):
            try:
                coro.close()
            except Exception:
                pass
        if submit_raises is not None:
            raise submit_raises
        return submit_returns

    connection = SimpleNamespace(
        get_mcp_client=MagicMock(return_value="fake-mcp-client"),
        submit_and_wait=MagicMock(side_effect=_submit),
    )
    return SimpleNamespace(
        _connection=connection,
        _resolve_workspace_id=lambda: workspace_id,
    )


# ---------------------------------------------------------------------------
# Happy path — emit succeeds + holder transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_succeeds_then_holder_transitions_to_paused_substrate():
    provider = _make_provider()
    holder = _holder()

    await handle_epoch_mismatch(
        memory_provider=provider,
        holder=holder,
        observed_substrate_epoch=8,
        last_known_epoch=3,
    )

    # Holder transitioned to PAUSED with SUBSTRATE reason.
    state = holder.current
    assert state.primary_state is PrimaryState.PAUSED
    assert DegradationReason.SUBSTRATE in state.degradation_reasons
    # Emit was attempted (submit_and_wait called once with the
    # kora.dr.observed emit coroutine).
    assert provider._connection.submit_and_wait.call_count == 1


# ---------------------------------------------------------------------------
# Emit failure does NOT block the transition (best-effort)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_failure_does_not_block_transition(caplog):
    """If the kora.dr.observed emit raises (substrate hiccup at the
    moment of DR detection), the holder still transitions to PAUSED.
    Cockpit observability is degraded but the runtime state is correct."""
    provider = _make_provider(submit_raises=RuntimeError("substrate down"))
    holder = _holder()

    with caplog.at_level(logging.ERROR, logger="agent.dr_handler"):
        await handle_epoch_mismatch(
            memory_provider=provider,
            holder=holder,
            observed_substrate_epoch=5,
            last_known_epoch=2,
        )

    # Holder STILL transitioned despite emit failure.
    assert holder.current.primary_state is PrimaryState.PAUSED
    assert DegradationReason.SUBSTRATE in holder.current.degradation_reasons
    # Operator-greppable error log fired.
    assert any(
        DR_OBSERVED_EVENT in record.message
        and "emit raised" in record.message
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# Missing infra — emit skipped, transition still fires
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_provider_skips_emit_but_transitions(caplog):
    holder = _holder()
    with caplog.at_level(logging.WARNING, logger="agent.dr_handler"):
        await handle_epoch_mismatch(
            memory_provider=None,
            holder=holder,
            observed_substrate_epoch=5,
            last_known_epoch=2,
        )
    assert holder.current.primary_state is PrimaryState.PAUSED
    assert any(
        "memory_provider is None" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_no_connection_skips_emit_but_transitions(caplog):
    provider = SimpleNamespace(
        _connection=None,
        _resolve_workspace_id=lambda: "ws-test",
    )
    holder = _holder()
    with caplog.at_level(logging.WARNING, logger="agent.dr_handler"):
        await handle_epoch_mismatch(
            memory_provider=provider,
            holder=holder,
            observed_substrate_epoch=5,
            last_known_epoch=2,
        )
    assert holder.current.primary_state is PrimaryState.PAUSED
    assert any(
        "_connection is None" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_workspace_id_unresolved_skips_emit_but_transitions(caplog):
    provider = _make_provider(workspace_id=None)
    holder = _holder()
    with caplog.at_level(logging.WARNING, logger="agent.dr_handler"):
        await handle_epoch_mismatch(
            memory_provider=provider,
            holder=holder,
            observed_substrate_epoch=5,
            last_known_epoch=2,
        )
    assert holder.current.primary_state is PrimaryState.PAUSED
    assert any(
        "workspace_id unresolved" in record.message
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# Trigger string does NOT match operational_state_emit per-trigger
# literal rules — so the listener emits ONLY the generic
# kora.operational_state.transitioned event (not e.g. the cost-limit
# literal). This is a contract with the existing emit module.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paused_transition_trigger_avoids_listener_literals():
    """The dr_handler's PAUSED trigger should NOT contain
    ``"cost 100%"`` (would falsely fire kora.paused.cost_limit).
    Verified by inspecting the transition's history entry."""
    provider = _make_provider()
    holder = _holder()
    await handle_epoch_mismatch(
        memory_provider=provider,
        holder=holder,
        observed_substrate_epoch=8,
        last_known_epoch=3,
    )
    history = holder.history()
    assert any(
        h["to_state"] == "paused" and "cost 100%" not in h["trigger"]
        for h in history
    )
    # And the trigger mentions the dr signal source
    assert any(
        "epoch mismatch" in h["trigger"].lower()
        or "kora.dr.observed" in h["trigger"]
        for h in history
    )
