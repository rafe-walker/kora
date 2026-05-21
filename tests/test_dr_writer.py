"""Unit tests for ``agent/dr_writer.py`` (KR-P2-M ST4).

Covers:
  - ``write_known_epoch_at_boot_end`` happy path + skip-on-missing-infra
  - ``write_known_epoch_at_boot_end`` failure-mode posture (read raise,
    write raise, monotonic violation)
  - ``make_paused_substrate_cleared_listener``:
      - matches PAUSED+{SUBSTRATE} → READY+{}
      - does not match other transition shapes
      - on match, resolves actor_id + calls the substrate write
      - missing infra → skips write but does not raise
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.dr_writer import (
    make_paused_substrate_cleared_listener,
    write_known_epoch_at_boot_end,
)
from agent.operational_state import (
    ClaimPermission,
    DegradationReason,
    OperationalState,
    PrimaryState,
)
from plugins.memory.isokron.dr_epoch import (
    KoraKnownEpochMonotonicViolation,
)


KORA_ACTOR_UUID = "33333333-3333-3333-3333-333333333333"


# ---------------------------------------------------------------------------
# Fake provider + helpers
# ---------------------------------------------------------------------------


class _FakeConnection:
    def __init__(self, *, pool):
        self._pool = pool

    def get_pg_pool(self):
        return self._pool


def _make_provider(
    *,
    pool: Optional[Any] = "fake-pool",
    no_connection: bool = False,
    workspace_id: Optional[str] = "ws-test",
) -> SimpleNamespace:
    if no_connection:
        return SimpleNamespace(
            _connection=None,
            _resolve_workspace_id=lambda: workspace_id,
        )
    return SimpleNamespace(
        _connection=_FakeConnection(pool=pool),
        _resolve_workspace_id=lambda: workspace_id,
    )


# ===========================================================================
# write_known_epoch_at_boot_end
# ===========================================================================


@pytest.mark.asyncio
async def test_boot_end_write_happy_path(monkeypatch):
    """Reads substrate_epoch + calls SECDEF write."""
    read_mock = AsyncMock(return_value=7)
    write_mock = AsyncMock(return_value=7)

    monkeypatch.setattr(
        "plugins.memory.isokron.dr_epoch.read_substrate_epoch", read_mock
    )
    monkeypatch.setattr(
        "plugins.memory.isokron.dr_epoch.write_kora_known_epoch", write_mock
    )

    provider = _make_provider()
    await write_known_epoch_at_boot_end(
        memory_provider=provider,
        kora_actor_uuid=KORA_ACTOR_UUID,
    )

    read_mock.assert_awaited_once()
    write_mock.assert_awaited_once()
    _, kwargs = write_mock.call_args
    assert kwargs["observed_epoch"] == 7
    assert kwargs["kora_actor_id"] == KORA_ACTOR_UUID


@pytest.mark.asyncio
async def test_boot_end_write_skips_when_no_provider(caplog):
    with caplog.at_level(logging.WARNING, logger="agent.dr_writer"):
        await write_known_epoch_at_boot_end(
            memory_provider=None,
            kora_actor_uuid=KORA_ACTOR_UUID,
        )
    assert any(
        "memory_provider is None" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_boot_end_write_skips_when_no_actor_uuid(caplog):
    provider = _make_provider()
    with caplog.at_level(logging.WARNING, logger="agent.dr_writer"):
        await write_known_epoch_at_boot_end(
            memory_provider=provider,
            kora_actor_uuid=None,
        )
    assert any(
        "kora_actor_uuid not resolved" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_boot_end_write_skips_when_no_connection(caplog):
    provider = _make_provider(no_connection=True)
    with caplog.at_level(logging.WARNING, logger="agent.dr_writer"):
        await write_known_epoch_at_boot_end(
            memory_provider=provider,
            kora_actor_uuid=KORA_ACTOR_UUID,
        )
    assert any(
        "_connection is None" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_boot_end_write_read_raise_is_logged_not_raised(
    monkeypatch, caplog
):
    monkeypatch.setattr(
        "plugins.memory.isokron.dr_epoch.read_substrate_epoch",
        AsyncMock(side_effect=RuntimeError("substrate down")),
    )
    # Write should NOT be reached
    write_mock = AsyncMock()
    monkeypatch.setattr(
        "plugins.memory.isokron.dr_epoch.write_kora_known_epoch", write_mock
    )

    provider = _make_provider()
    with caplog.at_level(logging.WARNING, logger="agent.dr_writer"):
        await write_known_epoch_at_boot_end(
            memory_provider=provider,
            kora_actor_uuid=KORA_ACTOR_UUID,
        )

    write_mock.assert_not_called()
    assert any(
        "read_substrate_epoch raised" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_boot_end_write_write_raise_is_logged_not_raised(
    monkeypatch, caplog
):
    monkeypatch.setattr(
        "plugins.memory.isokron.dr_epoch.read_substrate_epoch",
        AsyncMock(return_value=7),
    )
    monkeypatch.setattr(
        "plugins.memory.isokron.dr_epoch.write_kora_known_epoch",
        AsyncMock(side_effect=RuntimeError("dispatch failed")),
    )

    provider = _make_provider()
    with caplog.at_level(logging.WARNING, logger="agent.dr_writer"):
        # Must NOT raise
        await write_known_epoch_at_boot_end(
            memory_provider=provider,
            kora_actor_uuid=KORA_ACTOR_UUID,
        )

    assert any(
        "write_kora_known_epoch raised" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_boot_end_write_monotonic_violation_logged_at_error(
    monkeypatch, caplog
):
    monkeypatch.setattr(
        "plugins.memory.isokron.dr_epoch.read_substrate_epoch",
        AsyncMock(return_value=3),  # advanced again between read+write
    )
    monkeypatch.setattr(
        "plugins.memory.isokron.dr_epoch.write_kora_known_epoch",
        AsyncMock(
            side_effect=KoraKnownEpochMonotonicViolation(observed=3, current=7)
        ),
    )

    provider = _make_provider()
    with caplog.at_level(logging.ERROR, logger="agent.dr_writer"):
        await write_known_epoch_at_boot_end(
            memory_provider=provider,
            kora_actor_uuid=KORA_ACTOR_UUID,
        )

    assert any(
        "monotonic violation" in record.message.lower()
        and "Operator triage required" in record.message
        for record in caplog.records
    )


# ===========================================================================
# make_paused_substrate_cleared_listener — match criteria
# ===========================================================================


def _state(
    primary: PrimaryState,
    *,
    reasons: Optional[set[DegradationReason]] = None,
) -> OperationalState:
    return OperationalState(
        primary_state=primary,
        degradation_reasons=frozenset(reasons or set()),
        claim_permission=ClaimPermission.NORMAL,
    )


@pytest.mark.asyncio
async def test_listener_matches_paused_substrate_cleared(monkeypatch):
    """PAUSED+{SUBSTRATE} → READY+{} fires the write."""
    read_mock = AsyncMock(return_value=8)
    write_mock = AsyncMock(return_value=8)
    monkeypatch.setattr(
        "plugins.memory.isokron.dr_epoch.read_substrate_epoch", read_mock
    )
    monkeypatch.setattr(
        "plugins.memory.isokron.dr_epoch.write_kora_known_epoch", write_mock
    )

    # Stub actor_id resolution by patching the internal helper
    monkeypatch.setattr(
        "agent.dr_writer._resolve_kora_actor_uuid",
        AsyncMock(return_value=KORA_ACTOR_UUID),
    )

    provider = _make_provider()
    listener = make_paused_substrate_cleared_listener(provider)

    await listener(
        _state(PrimaryState.PAUSED, reasons={DegradationReason.SUBSTRATE}),
        _state(PrimaryState.READY),
        "operator cleared via cockpit kora_control reset",
    )

    write_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_listener_skips_non_paused_to_ready(monkeypatch):
    """READY → READY (same-state) doesn't match."""
    write_mock = AsyncMock()
    monkeypatch.setattr(
        "plugins.memory.isokron.dr_epoch.write_kora_known_epoch", write_mock
    )

    provider = _make_provider()
    listener = make_paused_substrate_cleared_listener(provider)

    await listener(
        _state(PrimaryState.READY),
        _state(PrimaryState.READY),
        "noop",
    )
    write_mock.assert_not_called()


@pytest.mark.asyncio
async def test_listener_skips_paused_to_stopped(monkeypatch):
    """PAUSED → STOPPED (no clearance) doesn't match."""
    write_mock = AsyncMock()
    monkeypatch.setattr(
        "plugins.memory.isokron.dr_epoch.write_kora_known_epoch", write_mock
    )

    provider = _make_provider()
    listener = make_paused_substrate_cleared_listener(provider)

    await listener(
        _state(PrimaryState.PAUSED, reasons={DegradationReason.SUBSTRATE}),
        _state(PrimaryState.STOPPED),
        "STOP-KORA L4/L5",
    )
    write_mock.assert_not_called()


@pytest.mark.asyncio
async def test_listener_skips_paused_to_ready_without_substrate_removal(
    monkeypatch,
):
    """PAUSED+{COST} → READY+{COST still present} doesn't match — the
    listener is specifically for SUBSTRATE clearance."""
    write_mock = AsyncMock()
    monkeypatch.setattr(
        "plugins.memory.isokron.dr_epoch.write_kora_known_epoch", write_mock
    )

    provider = _make_provider()
    listener = make_paused_substrate_cleared_listener(provider)

    # PAUSED+COST → READY+COST (no SUBSTRATE involved)
    await listener(
        _state(PrimaryState.PAUSED, reasons={DegradationReason.COST}),
        _state(PrimaryState.READY, reasons={DegradationReason.COST}),
        "cost cleared",
    )
    write_mock.assert_not_called()

    # PAUSED+{SUBSTRATE, COST} → READY+{COST} — SUBSTRATE cleared but
    # this should MATCH (the SUBSTRATE reason was removed even though
    # COST remains).
    monkeypatch.setattr(
        "agent.dr_writer._resolve_kora_actor_uuid",
        AsyncMock(return_value=KORA_ACTOR_UUID),
    )
    monkeypatch.setattr(
        "plugins.memory.isokron.dr_epoch.read_substrate_epoch",
        AsyncMock(return_value=3),
    )
    monkeypatch.setattr(
        "plugins.memory.isokron.dr_epoch.write_kora_known_epoch",
        AsyncMock(return_value=3),
    )
    write_mock_2 = AsyncMock()
    monkeypatch.setattr(
        "plugins.memory.isokron.dr_epoch.write_kora_known_epoch", write_mock_2
    )

    await listener(
        _state(
            PrimaryState.PAUSED,
            reasons={DegradationReason.SUBSTRATE, DegradationReason.COST},
        ),
        _state(PrimaryState.READY, reasons={DegradationReason.COST}),
        "operator cleared substrate via kora_control reset",
    )
    write_mock_2.assert_awaited_once()


@pytest.mark.asyncio
async def test_listener_skips_when_no_connection(monkeypatch, caplog):
    write_mock = AsyncMock()
    monkeypatch.setattr(
        "plugins.memory.isokron.dr_epoch.write_kora_known_epoch", write_mock
    )

    provider = _make_provider(no_connection=True)
    listener = make_paused_substrate_cleared_listener(provider)

    with caplog.at_level(logging.WARNING, logger="agent.dr_writer"):
        await listener(
            _state(PrimaryState.PAUSED, reasons={DegradationReason.SUBSTRATE}),
            _state(PrimaryState.READY),
            "operator cleared",
        )

    write_mock.assert_not_called()
    assert any(
        "_connection is None" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_listener_skips_when_actor_id_unresolvable(monkeypatch, caplog):
    write_mock = AsyncMock()
    monkeypatch.setattr(
        "plugins.memory.isokron.dr_epoch.write_kora_known_epoch", write_mock
    )
    monkeypatch.setattr(
        "agent.dr_writer._resolve_kora_actor_uuid",
        AsyncMock(return_value=None),
    )

    provider = _make_provider()
    listener = make_paused_substrate_cleared_listener(provider)

    with caplog.at_level(logging.WARNING, logger="agent.dr_writer"):
        await listener(
            _state(PrimaryState.PAUSED, reasons={DegradationReason.SUBSTRATE}),
            _state(PrimaryState.READY),
            "operator cleared",
        )

    write_mock.assert_not_called()
    assert any(
        "kora actor_id unresolved" in record.message
        for record in caplog.records
    )


# ===========================================================================
# Coordinator integration — end-of-boot write fires only on READY path
# ===========================================================================


@pytest.mark.asyncio
async def test_coordinator_calls_write_at_end_of_boot_on_ready(monkeypatch):
    """Verify the boot_coordinator's all-pass branch invokes
    write_known_epoch_at_boot_end. Patches the function so we can
    assert it was called without running the substrate I/O."""
    from agent.boot_coordinator import BootResult, run_boot_sequence
    from agent.boot_gates import (
        BootContext,
        Gate,
        GateClass,
        GateOutcome,
        GateResult,
    )
    from agent.operational_state_holder import (
        _reset_holder_for_tests,
        init_holder,
    )

    _reset_holder_for_tests()

    class _AlwaysPassGate(Gate):
        gate_id = "test_pass"
        gate_class = GateClass.TRANSIENT
        title = "test"

        async def run(self, context: BootContext) -> GateResult:
            # Set actor_uuid on the context (gate 7 normally does this)
            context.kora_actor_uuid = KORA_ACTOR_UUID
            now = datetime.now(timezone.utc)
            return GateResult(
                gate_id=self.gate_id,
                gate_class=self.gate_class,
                outcome=GateOutcome.PASS,
                detail="ok",
                elapsed_ms=1,
                started_at=now,
                completed_at=now,
            )

    holder = init_holder(
        OperationalState(primary_state=PrimaryState.BOOTING)
    )

    write_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "agent.dr_writer.write_known_epoch_at_boot_end", write_mock
    )

    provider = SimpleNamespace(
        _connection=SimpleNamespace(
            get_mcp_client=MagicMock(return_value="fake-mcp"),
            submit_and_wait=MagicMock(
                side_effect=lambda coro, *, timeout=10.0: (
                    coro.close() if hasattr(coro, "close") else None,
                    "evt-1",
                )[1]
            ),
        ),
        _resolve_workspace_id=lambda: "ws-test",
    )

    summary = await run_boot_sequence(
        memory_provider=provider,
        holder=holder,
        gates=[_AlwaysPassGate()],
    )

    assert summary.result is BootResult.READY
    write_mock.assert_awaited_once_with(
        memory_provider=provider,
        kora_actor_uuid=KORA_ACTOR_UUID,
    )

    _reset_holder_for_tests()
