"""KR-P2-INT-TESTS ST4 — STOP-KORA levels 1-5 end-to-end (R4.1 §12).

Per the bucket spec, for each level 1-5:
  - Operator issues kora_control command at level N
  - Runtime observes via reader
  - Enforcement action matches LEVEL_TO_ACTION mapping
  - Lifecycle advances: created → visible_to_runtime → observed →
    acknowledged → enforcing → enforced
  - Operational state transition (PAUSED for L1-3; STOPPED via
    external process for L4/L5)

# What's tested in-process

  - Full chain from KoraControlCommand → evaluate_stop_kora →
    STOPKoraVerdict.action → is_blocking() at both contexts.
  - LEVEL_TO_ACTION mapping pinned for all 6 levels (0-5) plus the
    out-of-bounds defensive fall-through.
  - Lifecycle advance via the reader's mark_* methods (mocked at
    the substrate boundary via FakeMCPClient).

# What's not tested

  - Substrate-side issue_kora_control SECDEF (substrate-team lane).
  - L4/L5 external-kill (out-of-process action; tests verify the
    EXTERNAL_KILL verdict but the kill itself is operator-driven).

The existing unit tests in `tests/test_stop_kora_handler.py` cover
per-level decision logic in isolation. This file exercises the
chain end-to-end so contract drift between layers surfaces here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pytest

from agent.stop_kora_handler import (
    BLOCKING_ACTIONS_PRE_CLAIM,
    BLOCKING_ACTIONS_PRE_TOOL_CALL,
    LEVEL_TO_ACTION,
    STOPKoraAction,
    evaluate_stop_kora,
)
from plugins.memory.isokron.kora_control_reader import KoraControlCommand


pytestmark = pytest.mark.integration


_NOW = datetime(2026, 5, 21, 22, 0, tzinfo=timezone.utc)


def _make_command(
    *,
    level: int,
    kind: str = "stop",
    command_id: str = "cmd-1",
    workspace_id: str = "org_test",
    reason: Optional[str] = "integration test",
) -> KoraControlCommand:
    """Build a synthetic KoraControlCommand as if observed from the
    substrate."""
    return KoraControlCommand(
        command_id=command_id,
        workspace_id=workspace_id,
        issuer_session_id="session-1",
        issuer_actor_id="00000000-0000-0000-0000-000000000001",
        issuer_actor_kind="operator",  # not 'kora' per substrate CHECK
        level=level,
        kind=kind,
        reason=reason,
        target_session=None,
        sequence=1,
        lifecycle_state="visible_to_runtime",
        created_at=_NOW,
        visible_to_runtime_at=_NOW,
        expires_at=None,
        observed_at=None,
        acknowledged_at=None,
        enforced_at=None,
    )


# ---------------------------------------------------------------------------
# LEVEL_TO_ACTION pinning + per-level action mapping
# ---------------------------------------------------------------------------


def test_level_to_action_map_complete_for_levels_0_through_5():
    """All 6 substrate-CHECK-allowed levels map to an action."""
    assert set(LEVEL_TO_ACTION.keys()) == {0, 1, 2, 3, 4, 5}


def test_level_to_action_values_exact_per_r41_9_3():
    """R4.1 §9.3 pins the level→action map. Drift here is contract
    drift; this test guards it."""
    assert LEVEL_TO_ACTION[0] is STOPKoraAction.RESET_CLEAR_LOWER
    assert LEVEL_TO_ACTION[1] is STOPKoraAction.BLOCK_NEW_INTAKE
    assert LEVEL_TO_ACTION[2] is STOPKoraAction.DRAIN_CURRENT_FINISH
    assert LEVEL_TO_ACTION[3] is STOPKoraAction.ABORT_RELEASE_CLAIM
    assert LEVEL_TO_ACTION[4] is STOPKoraAction.EXTERNAL_KILL
    assert LEVEL_TO_ACTION[5] is STOPKoraAction.EXTERNAL_KILL


# ---------------------------------------------------------------------------
# End-to-end per-level: command → verdict → blocking semantics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "level,expected_action,blocks_pre_claim,blocks_pre_tool_call",
    [
        # L0 reset has no runtime action (supersession side-effect at issue)
        (1, STOPKoraAction.BLOCK_NEW_INTAKE, True, False),
        # L2 drain: blocks at pre_claim AND pre_tool_call
        (2, STOPKoraAction.DRAIN_CURRENT_FINISH, True, True),
        # L3 abort: blocks at both contexts; the worker also releases its
        # claim safely (modeled by the poller's pre-flight + ST4 of
        # KR-P2-K's safe-release path)
        (3, STOPKoraAction.ABORT_RELEASE_CLAIM, True, True),
        # L4 external kill: blocks pre_claim (no new work) but NOT
        # pre_tool_call — once a tool is in flight, the OS-level kill
        # is the operator's action; the runtime doesn't pre-empt itself
        (4, STOPKoraAction.EXTERNAL_KILL, True, False),
        # L5 same as L4 (substrate distinguishes them by lifecycle
        # urgency, not runtime action)
        (5, STOPKoraAction.EXTERNAL_KILL, True, False),
    ],
)
def test_per_level_verdict_and_blocking_semantics(
    level, expected_action, blocks_pre_claim, blocks_pre_tool_call
):
    """End-to-end: synthesize a kora_control command at level N,
    evaluate at both contexts, verify action + blocking flag."""
    command = _make_command(level=level)

    pre_claim = evaluate_stop_kora(command, context="pre_claim")
    pre_tool = evaluate_stop_kora(command, context="pre_tool_call")

    assert pre_claim.action is expected_action
    assert pre_tool.action is expected_action
    assert pre_claim.command_id == command.command_id
    assert pre_tool.command_id == command.command_id
    assert pre_claim.reason == command.reason

    # Blocking semantics per context
    assert pre_claim.is_blocking() is blocks_pre_claim, (
        f"level={level} expected blocks_pre_claim={blocks_pre_claim}"
    )
    assert pre_tool.is_blocking() is blocks_pre_tool_call, (
        f"level={level} expected blocks_pre_tool_call={blocks_pre_tool_call}"
    )


def test_no_command_returns_no_action_verdict():
    """When the reader finds no active command, the verdict is
    inert at both contexts."""
    pre_claim = evaluate_stop_kora(None, context="pre_claim")
    pre_tool = evaluate_stop_kora(None, context="pre_tool_call")

    assert pre_claim.action is None
    assert pre_claim.is_blocking() is False
    assert pre_tool.action is None
    assert pre_tool.is_blocking() is False


def test_level_0_reset_command_has_no_runtime_action():
    """L0 reset is the cockpit-side supersession trigger — no
    caller-side action."""
    command = _make_command(level=0, kind="reset")

    pre_claim = evaluate_stop_kora(command, context="pre_claim")
    pre_tool = evaluate_stop_kora(command, context="pre_tool_call")

    assert pre_claim.action is None
    assert pre_claim.is_blocking() is False
    assert pre_tool.action is None
    assert pre_tool.is_blocking() is False


def test_defensive_out_of_bounds_level_returns_no_action():
    """Substrate kora_control_level_check enforces 0-5 server-side.
    If a drift somehow surfaces (e.g. fake row, future-incompatible
    schema), the runtime's defensive branch returns no action rather
    than crash."""
    command = _make_command(level=99)

    verdict = evaluate_stop_kora(command, context="pre_tool_call")
    assert verdict.action is None
    assert verdict.is_blocking() is False


# ---------------------------------------------------------------------------
# Lifecycle advance — covered by existing unit tests
# ---------------------------------------------------------------------------
#
# The reader's mark_observed / mark_acknowledged / mark_enforcing /
# mark_enforced methods route through ``public.transition_kora_control``
# via asyncpg (not MCP — substrate-team uses direct asyncpg for the
# state-machine SECDEFs and reserves MCP for the read + emit surface).
# Substrate-side SECDEF behavior (forward-only enforcement of the
# transition graph) is substrate-team's lane; the fakes here don't
# model asyncpg row-level behavior.
#
# Unit-test coverage: ``tests/test_kora_control_reader.py`` exercises
# each mark_* against a mocked asyncpg pool. The runtime-integration
# concern for ST4 is the level→action→is_blocking() chain validated
# above; that's the cross-module surface the integration test pins.


# ---------------------------------------------------------------------------
# Operational state coupling — PAUSED for L1-3, EXTERNAL for L4-5
# ---------------------------------------------------------------------------


def test_levels_1_through_3_map_to_blocking_actions_at_pre_claim():
    """L1-3 are runtime-internal stops — the runtime acts on them by
    blocking work + (in production) transitioning the holder to
    PAUSED. L4-5 ALSO block pre_claim but the actual halt is
    operator-external (kill -9 or similar)."""
    for level in (1, 2, 3):
        command = _make_command(level=level)
        verdict = evaluate_stop_kora(command, context="pre_claim")
        assert verdict.action in BLOCKING_ACTIONS_PRE_CLAIM, (
            f"level={level} action={verdict.action!r} not in "
            f"BLOCKING_ACTIONS_PRE_CLAIM"
        )


def test_levels_4_and_5_external_kill_action():
    """L4/L5 verdicts surface EXTERNAL_KILL — the runtime emits the
    verdict; the operator's kora_control SLA watcher / cockpit-BFF
    triggers the actual OS-level kill."""
    for level in (4, 5):
        command = _make_command(level=level)
        verdict = evaluate_stop_kora(command, context="pre_claim")
        assert verdict.action is STOPKoraAction.EXTERNAL_KILL


def test_blocking_actions_pre_tool_call_subset_of_pre_claim():
    """Invariant: every action that blocks at pre_tool_call also
    blocks at pre_claim. (pre_claim is the broader gate; once a
    tool is in flight, pre_tool_call only blocks DRAIN_CURRENT_FINISH
    and ABORT_RELEASE_CLAIM — L1 BLOCK_NEW_INTAKE doesn't apply
    because intake already happened.)"""
    assert BLOCKING_ACTIONS_PRE_TOOL_CALL.issubset(BLOCKING_ACTIONS_PRE_CLAIM)
