"""Tests for ``agent/operational_state_wire.py`` (updated for KR-P2-H ST3).

KR-P2-H ST3 replaces the previous unconditional ``BOOTING → READY``
transition with the R4.1 §9.2 boot gate sequence. These tests verify
the new behavior:

- Happy path: boot-coordinator returns ``BootResult.READY`` → holder
  transitions to READY + ``claim_permission`` bump to NORMAL.
- STOPPED outcome: coordinator returns ``BootResult.STOPPED`` → wire-in
  calls ``sys.exit(1)``.
- No-connection branch: holder stays in BOOTING + WARN.
- Coordinator raises: wire-in catches + WARNs (fail-soft for
  programmer-error / import failures; ``SystemExit`` is NOT caught).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.boot_coordinator import BootResult, BootSummary
from agent.boot_gates import GateClass, GateOutcome, GateResult
from agent.operational_state import (
    ClaimPermission,
    PrimaryState,
)
from agent.operational_state_holder import (
    _reset_holder_for_tests,
    get_holder,
)
from agent.operational_state_wire import wire_operational_state


@pytest.fixture(autouse=True)
def _reset_singleton():
    _reset_holder_for_tests()
    yield
    _reset_holder_for_tests()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gate_result(
    gate_id: str = "1_claude_auth",
    *,
    outcome: GateOutcome = GateOutcome.PASS,
    gate_class: GateClass = GateClass.TRANSIENT,
    attempts: int = 1,
    detail: str = "ok",
) -> GateResult:
    now = datetime.now(timezone.utc)
    return GateResult(
        gate_id=gate_id,
        gate_class=gate_class,
        outcome=outcome,
        detail=detail,
        elapsed_ms=1,
        started_at=now,
        completed_at=now,
        attempts=attempts,
    )


def _make_provider(
    *,
    has_connection: bool = True,
    summary_to_return: BootSummary | None = None,
    bump_should_raise: BaseException | None = None,
) -> Any:
    """Build a stubbed IsoKronMemoryProvider.

    The fake ``submit_and_wait`` is path-aware: the first call
    (run_boot_sequence) returns the canned BootSummary; subsequent
    calls (the holder claim_permission bump) close the coroutine and
    return None (or raise if ``bump_should_raise`` is set).
    """
    if summary_to_return is None:
        summary_to_return = BootSummary(
            result=BootResult.READY,
            gate_results=[_gate_result()],
            failed_gate=None,
        )

    call_count = {"n": 0}

    def _submit_and_wait(coro, *, timeout=10.0):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First call = run_boot_sequence — close the coro (we won't
            # actually execute it) and return the canned summary.
            try:
                coro.close()
            except Exception:
                pass
            return summary_to_return
        # Subsequent calls (holder.transition_to for claim_permission bump)
        if bump_should_raise is not None:
            try:
                coro.close()
            except Exception:
                pass
            raise bump_should_raise
        # Actually drive the transition_to coro on a test loop so the
        # state change applies. We need to execute the coro because it
        # mutates holder state.
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    connection = MagicMock()
    connection.submit_and_wait = MagicMock(side_effect=_submit_and_wait)

    provider = MagicMock()
    provider._connection = connection if has_connection else None
    provider._resolve_workspace_id.return_value = "ws-test"
    return provider


def _patch_make_emit_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wire-in registers an emit listener on the holder. The real
    listener emits chain events; in these tests we replace it with a
    no-op AsyncMock so the holder's listener-fire path doesn't try to
    call substrate-side emit code.
    """
    monkeypatch.setattr(
        "agent.operational_state_emit.make_emit_listener",
        lambda provider: AsyncMock(return_value=None),
    )


# ---------------------------------------------------------------------------
# Happy path — BootResult.READY transitions holder + bumps claim_permission
# ---------------------------------------------------------------------------


def test_ready_outcome_transitions_holder_and_bumps_claim_permission(
    monkeypatch,
):
    """When the coordinator returns READY, the wire-in logs + bumps
    ``claim_permission`` to NORMAL via a second transition_to call.

    Note: the coordinator itself does the BOOTING → READY transition
    internally (mocked here via submit_and_wait returning the summary
    directly). The wire-in's responsibility post-coordinator is the
    claim_permission bump.
    """
    _patch_make_emit_listener(monkeypatch)

    # The coordinator runs the BOOTING → READY transition internally
    # in real code. The mocked submit_and_wait short-circuits that,
    # so we manually advance the holder to READY before the wire-in
    # checks holder state. In production, the coordinator would have
    # done this already.
    summary = BootSummary(
        result=BootResult.READY,
        gate_results=[_gate_result(), _gate_result(gate_id="7_canonical_kora_actor", gate_class=GateClass.INVARIANT)],
        failed_gate=None,
    )
    provider = _make_provider(summary_to_return=summary)

    # Pre-advance holder to READY so the second-call bump's
    # READY → READY (same-state) lands cleanly. In production the
    # coordinator did this BEFORE the wire-in's second submit_and_wait.
    def _advance_holder_before_bump(*args, **kwargs):
        from agent.operational_state_holder import init_holder
        from agent.operational_state import OperationalState
        holder = init_holder(
            OperationalState(
                primary_state=PrimaryState.BOOTING,
                claim_permission=ClaimPermission.NONE,
            )
        )
        # The coordinator would have done this; simulate for the test.
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                holder.transition_to(
                    PrimaryState.READY,
                    trigger="boot gates passed (see kora.boot.ready event for per-gate detail)",
                )
            )
        finally:
            loop.close()
        return summary

    provider._connection.submit_and_wait.side_effect = (
        lambda coro, **kw: _drive_through_test_loop(
            coro, fallback=_advance_holder_before_bump
        )
    )

    wire_operational_state(provider)

    holder = get_holder()
    assert holder is not None
    assert holder.current.primary_state is PrimaryState.READY
    assert holder.current.claim_permission is ClaimPermission.NORMAL


def _drive_through_test_loop(coro, *, fallback):
    """Execute the coroutine on a test event loop; if it's not a coro,
    fall back to ``fallback()``.

    We use this so the wire-in's holder.transition_to call (for the
    claim_permission bump) actually executes and mutates holder state.
    The run_boot_sequence coro is closed without execution; we return
    a canned BootSummary via the ``fallback``.
    """
    import asyncio
    import inspect

    if inspect.iscoroutine(coro):
        # Detect run_boot_sequence vs holder.transition_to by the
        # coroutine's qualname.
        name = getattr(coro.cr_code, "co_qualname", "") or coro.cr_code.co_name
        if "run_boot_sequence" in name:
            coro.close()
            return fallback()
        # Real coro (holder.transition_to) — drive it.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
    return fallback()


# ---------------------------------------------------------------------------
# STOPPED outcome — wire-in calls sys.exit(1)
# ---------------------------------------------------------------------------


def test_stopped_outcome_calls_sys_exit_with_non_zero(monkeypatch, caplog):
    """When the coordinator returns STOPPED, the wire-in logs ERROR and
    calls sys.exit(1). The coordinator already emitted kora.boot.failed
    and transitioned the holder to STOPPED."""
    _patch_make_emit_listener(monkeypatch)

    failed = _gate_result(
        gate_id="7_canonical_kora_actor",
        outcome=GateOutcome.FAIL,
        gate_class=GateClass.INVARIANT,
        attempts=1,
        detail="no actor_kind='kora' row in actor_registry",
    )
    summary = BootSummary(
        result=BootResult.STOPPED,
        gate_results=[_gate_result(), failed],
        failed_gate=failed,
    )
    provider = _make_provider(summary_to_return=summary)

    with caplog.at_level(
        logging.ERROR, logger="agent.operational_state_wire"
    ):
        with pytest.raises(SystemExit) as exc_info:
            wire_operational_state(provider)

    assert exc_info.value.code == 1
    # Failure message names the failing gate for operator triage.
    assert any(
        "boot gate failed" in record.message
        and "7_canonical_kora_actor" in record.message
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# No-connection branch — holder stays in BOOTING, WARN logged
# ---------------------------------------------------------------------------


def test_no_connection_leaves_holder_in_booting_and_warns(monkeypatch, caplog):
    _patch_make_emit_listener(monkeypatch)
    provider = _make_provider(has_connection=False)

    with caplog.at_level(
        logging.WARNING, logger="agent.operational_state_wire"
    ):
        wire_operational_state(provider)

    holder = get_holder()
    assert holder is not None
    assert holder.current.primary_state is PrimaryState.BOOTING
    assert holder.current.claim_permission is ClaimPermission.NONE
    assert any(
        "no _connection" in record.message
        and "boot gate sequence" in record.message
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# Coordinator raises → wire-in catches + WARNs (fail-soft for
# programmer-error / import failures; NOT for STOPPED outcome which is
# expressed via BootSummary, not exception).
# ---------------------------------------------------------------------------


def test_coordinator_raises_unexpectedly_is_caught_and_warned(
    monkeypatch, caplog
):
    """A submit_and_wait raise (e.g. import failure inside the
    coordinator) is caught + logged WARN. The wire-in is fail-soft on
    programmer errors. STOPPED outcomes use SystemExit instead."""
    _patch_make_emit_listener(monkeypatch)

    provider = MagicMock()
    provider._resolve_workspace_id.return_value = "ws-test"
    connection = MagicMock()
    connection.submit_and_wait = MagicMock(
        side_effect=RuntimeError("coordinator import failed")
    )
    provider._connection = connection

    with caplog.at_level(
        logging.WARNING, logger="agent.operational_state_wire"
    ):
        # Must NOT raise.
        wire_operational_state(provider)

    assert any(
        "wire-in raised" in record.message
        and "coordinator import failed" in record.message
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# SystemExit is not caught (it's how STOPPED propagates)
# ---------------------------------------------------------------------------


def test_system_exit_from_inside_propagates(monkeypatch):
    """The wire-in's catch-all explicitly does NOT swallow SystemExit
    so the STOPPED-outcome ``sys.exit(1)`` actually exits the process."""
    _patch_make_emit_listener(monkeypatch)

    provider = MagicMock()
    provider._resolve_workspace_id.return_value = "ws-test"
    connection = MagicMock()

    def _submit(coro, *, timeout=10.0):
        try:
            coro.close()
        except Exception:
            pass
        raise SystemExit(1)  # simulate the STOPPED-exit path

    connection.submit_and_wait = MagicMock(side_effect=_submit)
    provider._connection = connection

    with pytest.raises(SystemExit) as exc_info:
        wire_operational_state(provider)
    assert exc_info.value.code == 1


# ===========================================================================
# KR-P2-M ST3 — BootResult.PAUSED branch (wire-in does NOT sys.exit)
# ===========================================================================


def test_paused_outcome_does_not_call_sys_exit(monkeypatch, caplog):
    """BootResult.PAUSED — coordinator already transitioned holder to
    PAUSED{substrate} and emitted kora.dr.observed (via gate 3b). The
    wire-in just logs and returns; process stays running for operator
    clearance."""
    _patch_make_emit_listener(monkeypatch)

    failed = _gate_result(
        gate_id="3b_epoch_dr_check",
        outcome=GateOutcome.FAIL,
        gate_class=GateClass.INVARIANT_PAUSE,
        attempts=1,
        detail="epoch mismatch — observed substrate_epoch=8 vs kora_known_epoch=3",
    )
    summary = BootSummary(
        result=BootResult.PAUSED,
        gate_results=[_gate_result(), failed],
        failed_gate=failed,
    )
    provider = _make_provider(summary_to_return=summary)

    with caplog.at_level(
        logging.WARNING, logger="agent.operational_state_wire"
    ):
        # Must NOT raise SystemExit
        wire_operational_state(provider)

    # Operator-greppable warning naming the failed gate and the
    # operator-clearance pathway.
    assert any(
        "PAUSED" in record.message
        and "3b_epoch_dr_check" in record.message
        for record in caplog.records
    )
    assert any(
        "kora_control reset" in record.message
        for record in caplog.records
    )
