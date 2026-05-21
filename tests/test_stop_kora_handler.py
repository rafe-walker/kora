"""Unit tests for ``agent/stop_kora_handler.py`` (KR-P2-J ST2).

Covers:
  - ``STOPKoraAction`` enum cardinality + string values (load-bearing
    wire format for ST3 block_result + future cockpit-BFF consumers)
  - ``LEVEL_TO_ACTION`` mapping (every R4.1 §9.3 level mapped)
  - ``BLOCKING_ACTIONS_PRE_CLAIM`` / ``BLOCKING_ACTIONS_PRE_TOOL_CALL``
    membership (context-dependent blocking policy)
  - ``STOPKoraVerdict`` immutability + ``is_blocking()`` truth table
  - ``evaluate_stop_kora`` decision order: ``None`` command → no action;
    reset → no action; every L0-L5 × every context → expected action
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

from agent.stop_kora_handler import (
    BLOCKING_ACTIONS_PRE_CLAIM,
    BLOCKING_ACTIONS_PRE_TOOL_CALL,
    LEVEL_TO_ACTION,
    STOPKoraAction,
    STOPKoraVerdict,
    evaluate_stop_kora,
)
from plugins.memory.isokron.kora_control_reader import KoraControlCommand


# ---------------------------------------------------------------------------
# Test command builder
# ---------------------------------------------------------------------------


def _cmd(
    level: int,
    *,
    kind: str = "stop",
    command_id: str = "11111111-1111-1111-1111-111111111111",
    reason: str = "test command",
    lifecycle_state: str = "visible_to_runtime",
) -> KoraControlCommand:
    now = datetime(2026, 5, 21, 12, 0, 0, tzinfo=timezone.utc)
    return KoraControlCommand(
        command_id=command_id,
        workspace_id="ws-1",
        issuer_session_id="cockpit-session-1",
        issuer_actor_id="22222222-2222-2222-2222-222222222222",
        issuer_actor_kind="operator",
        level=level,
        kind=kind,
        reason=reason,
        target_session=None,
        sequence=1,
        lifecycle_state=lifecycle_state,
        created_at=now,
        visible_to_runtime_at=now,
        expires_at=None,
        observed_at=None,
        acknowledged_at=None,
        enforced_at=None,
    )


# ---------------------------------------------------------------------------
# STOPKoraAction enum shape
# ---------------------------------------------------------------------------


def test_stop_kora_action_has_5_members_with_stable_string_values():
    assert {m.value for m in STOPKoraAction} == {
        "reset_clear_lower",
        "block_new_intake",
        "drain_current_finish",
        "abort_release_claim",
        "external_kill",
    }
    assert len(list(STOPKoraAction)) == 5


@pytest.mark.parametrize("member", list(STOPKoraAction))
def test_stop_kora_action_round_trips_through_value_string(member):
    assert STOPKoraAction(member.value) is member


# ---------------------------------------------------------------------------
# LEVEL_TO_ACTION map
# ---------------------------------------------------------------------------


def test_level_to_action_covers_all_levels_0_through_5():
    assert set(LEVEL_TO_ACTION.keys()) == {0, 1, 2, 3, 4, 5}


def test_level_to_action_r41_table_mapping():
    """R4.1 §9.3 table — explicit per-level assertion."""
    assert LEVEL_TO_ACTION[0] is STOPKoraAction.RESET_CLEAR_LOWER
    assert LEVEL_TO_ACTION[1] is STOPKoraAction.BLOCK_NEW_INTAKE
    assert LEVEL_TO_ACTION[2] is STOPKoraAction.DRAIN_CURRENT_FINISH
    assert LEVEL_TO_ACTION[3] is STOPKoraAction.ABORT_RELEASE_CLAIM
    assert LEVEL_TO_ACTION[4] is STOPKoraAction.EXTERNAL_KILL
    assert LEVEL_TO_ACTION[5] is STOPKoraAction.EXTERNAL_KILL


# ---------------------------------------------------------------------------
# Context-dependent blocking sets
# ---------------------------------------------------------------------------


def test_pre_claim_blocks_on_all_non_reset_actions():
    """pre_claim blocks L1+ including EXTERNAL_KILL — wasted to claim
    moments before SIGTERM."""
    assert BLOCKING_ACTIONS_PRE_CLAIM == frozenset({
        STOPKoraAction.BLOCK_NEW_INTAKE,
        STOPKoraAction.DRAIN_CURRENT_FINISH,
        STOPKoraAction.ABORT_RELEASE_CLAIM,
        STOPKoraAction.EXTERNAL_KILL,
    })
    # RESET_CLEAR_LOWER is NOT blocking — but reset commands map to
    # action=None anyway, so this is double-protection.
    assert STOPKoraAction.RESET_CLEAR_LOWER not in BLOCKING_ACTIONS_PRE_CLAIM


def test_pre_tool_call_blocks_only_on_drain_and_abort():
    """L1 (BLOCK_NEW_INTAKE) is informational at pre_tool_call —
    intake already happened. L4/L5 (EXTERNAL_KILL) is informational —
    blocking the next tool call doesn't change the impending SIGTERM."""
    assert BLOCKING_ACTIONS_PRE_TOOL_CALL == frozenset({
        STOPKoraAction.DRAIN_CURRENT_FINISH,
        STOPKoraAction.ABORT_RELEASE_CLAIM,
    })
    assert STOPKoraAction.BLOCK_NEW_INTAKE not in BLOCKING_ACTIONS_PRE_TOOL_CALL
    assert STOPKoraAction.EXTERNAL_KILL not in BLOCKING_ACTIONS_PRE_TOOL_CALL


# ---------------------------------------------------------------------------
# STOPKoraVerdict — immutability + is_blocking()
# ---------------------------------------------------------------------------


def test_verdict_is_frozen():
    v = STOPKoraVerdict(
        action=None, command_id=None, reason=None, context="pre_claim"
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.action = STOPKoraAction.BLOCK_NEW_INTAKE  # type: ignore[misc]


def test_is_blocking_returns_false_when_action_is_none():
    v = STOPKoraVerdict(
        action=None, command_id=None, reason=None, context="pre_claim"
    )
    assert v.is_blocking() is False


def test_is_blocking_returns_false_when_context_is_none():
    """Defensive: a verdict with no context can't decide blocking."""
    v = STOPKoraVerdict(
        action=STOPKoraAction.DRAIN_CURRENT_FINISH,
        command_id="c",
        reason="r",
        context=None,
    )
    assert v.is_blocking() is False


@pytest.mark.parametrize(
    "action,expected",
    [
        (STOPKoraAction.BLOCK_NEW_INTAKE, True),
        (STOPKoraAction.DRAIN_CURRENT_FINISH, True),
        (STOPKoraAction.ABORT_RELEASE_CLAIM, True),
        (STOPKoraAction.EXTERNAL_KILL, True),
        # RESET_CLEAR_LOWER never appears via evaluate_stop_kora's
        # action= field (reset → action=None), but defensive check:
        (STOPKoraAction.RESET_CLEAR_LOWER, False),
    ],
)
def test_is_blocking_at_pre_claim(action, expected):
    v = STOPKoraVerdict(
        action=action, command_id="c", reason="r", context="pre_claim"
    )
    assert v.is_blocking() is expected


@pytest.mark.parametrize(
    "action,expected",
    [
        (STOPKoraAction.BLOCK_NEW_INTAKE, False),  # L1 informational
        (STOPKoraAction.DRAIN_CURRENT_FINISH, True),
        (STOPKoraAction.ABORT_RELEASE_CLAIM, True),
        (STOPKoraAction.EXTERNAL_KILL, False),  # L4/L5 informational
        (STOPKoraAction.RESET_CLEAR_LOWER, False),
    ],
)
def test_is_blocking_at_pre_tool_call(action, expected):
    v = STOPKoraVerdict(
        action=action, command_id="c", reason="r", context="pre_tool_call"
    )
    assert v.is_blocking() is expected


# ---------------------------------------------------------------------------
# evaluate_stop_kora — decision order
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("context", ["pre_claim", "pre_tool_call"])
def test_none_command_returns_no_action_verdict(context):
    v = evaluate_stop_kora(None, context=context)
    assert v.action is None
    assert v.command_id is None
    assert v.reason is None
    assert v.context == context
    assert v.is_blocking() is False


@pytest.mark.parametrize("context", ["pre_claim", "pre_tool_call"])
def test_reset_command_returns_no_action_verdict(context):
    """L0 reset — supersession was the issuer-side side-effect; runtime
    has no caller-side action."""
    cmd = _cmd(0, kind="reset", reason="operator clearance")
    v = evaluate_stop_kora(cmd, context=context)
    assert v.action is None
    assert v.command_id == cmd.command_id
    assert v.reason == "operator clearance"
    assert v.context == context
    assert v.is_blocking() is False


@pytest.mark.parametrize(
    "level,expected_action",
    [
        (1, STOPKoraAction.BLOCK_NEW_INTAKE),
        (2, STOPKoraAction.DRAIN_CURRENT_FINISH),
        (3, STOPKoraAction.ABORT_RELEASE_CLAIM),
        (4, STOPKoraAction.EXTERNAL_KILL),
        (5, STOPKoraAction.EXTERNAL_KILL),
    ],
)
@pytest.mark.parametrize("context", ["pre_claim", "pre_tool_call"])
def test_stop_command_at_each_level_yields_expected_action(
    level, expected_action, context
):
    cmd = _cmd(level)
    v = evaluate_stop_kora(cmd, context=context)
    assert v.action is expected_action
    assert v.command_id == cmd.command_id
    assert v.reason == "test command"
    assert v.context == context


def test_pre_claim_blocks_on_l1_through_l5():
    """pre_claim blocks on every L1+ action."""
    for level in (1, 2, 3, 4, 5):
        v = evaluate_stop_kora(_cmd(level), context="pre_claim")
        assert v.is_blocking() is True, (
            f"L{level} should block at pre_claim but did not"
        )


def test_pre_tool_call_blocks_only_on_l2_and_l3():
    """pre_tool_call blocks only on DRAIN (L2) and ABORT (L3)."""
    v_l1 = evaluate_stop_kora(_cmd(1), context="pre_tool_call")
    v_l2 = evaluate_stop_kora(_cmd(2), context="pre_tool_call")
    v_l3 = evaluate_stop_kora(_cmd(3), context="pre_tool_call")
    v_l4 = evaluate_stop_kora(_cmd(4), context="pre_tool_call")
    v_l5 = evaluate_stop_kora(_cmd(5), context="pre_tool_call")

    assert v_l1.is_blocking() is False  # informational
    assert v_l2.is_blocking() is True
    assert v_l3.is_blocking() is True
    assert v_l4.is_blocking() is False  # informational (OS kill)
    assert v_l5.is_blocking() is False  # informational (OS kill + revoke)


def test_unmapped_level_falls_through_to_no_action():
    """Defensive: if a future schema extension adds a level we don't
    know, treat as no-op (fail-open for future-compat — alternative is
    to block all tool calls on an unrecognized level)."""
    # Build a command with level 99 (bypassing the substrate CHECK by
    # constructing the dataclass directly — the CHECK is at insert
    # time, not at runtime instantiation).
    cmd = _cmd(99)
    v = evaluate_stop_kora(cmd, context="pre_tool_call")
    assert v.action is None
    assert v.command_id == cmd.command_id
    assert v.is_blocking() is False
