"""Tests for ``kora_cli/boot_cmd.py`` (KR-P2-H ST4 diagnostic CLI).

Covers:
  - ``--check-only`` flag triggers the diagnostic path
  - Without ``--check-only``, prints usage + exits non-zero
  - All-PASS → tabular output + exit 0
  - Any-FAIL → tabular output + exit 1
  - Provider bootstrap failure surfaces in stderr but doesn't crash
  - Keyboard interrupt → exit 130
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from agent.boot_coordinator import BootResult, BootSummary
from agent.boot_gates import GateClass, GateOutcome, GateResult
from kora_cli.boot_cmd import cmd_boot


def _gate_result(
    gate_id: str = "1_claude_auth",
    *,
    outcome: GateOutcome = GateOutcome.PASS,
    gate_class: GateClass = GateClass.TRANSIENT,
    detail: str = "ok",
    elapsed_ms: int = 5,
) -> GateResult:
    now = datetime.now(timezone.utc)
    return GateResult(
        gate_id=gate_id,
        gate_class=gate_class,
        outcome=outcome,
        detail=detail,
        elapsed_ms=elapsed_ms,
        started_at=now,
        completed_at=now,
    )


def _args(check_only: bool = True) -> argparse.Namespace:
    return argparse.Namespace(check_only=check_only)


# ---------------------------------------------------------------------------
# --check-only flag handling
# ---------------------------------------------------------------------------


def test_without_check_only_flag_prints_usage_and_exits_2(capsys):
    rc = cmd_boot(_args(check_only=False))
    assert rc == 2
    captured = capsys.readouterr()
    assert "only --check-only is supported" in captured.err


# ---------------------------------------------------------------------------
# Happy path — all gates pass → exit 0 + tabular output
# ---------------------------------------------------------------------------


def test_all_pass_returns_exit_0_and_prints_table(capsys):
    summary = BootSummary(
        result=BootResult.READY,
        gate_results=[
            _gate_result(
                "1_claude_auth", detail="ANTHROPIC_API_KEY set"
            ),
            _gate_result(
                "7_canonical_kora_actor",
                gate_class=GateClass.INVARIANT,
                detail="canonical kora actor resolved",
            ),
        ],
        failed_gate=None,
    )

    with (
        patch(
            "kora_cli.boot_cmd._load_isokron_provider", return_value="fake-provider"
        ),
        patch(
            "agent.boot_coordinator.run_boot_sequence",
            new_callable=AsyncMock, return_value=summary,
        ),
    ):
        rc = cmd_boot(_args())

    assert rc == 0
    captured = capsys.readouterr()
    assert "R4.1 §9.2 boot gate diagnostic" in captured.out
    assert "1_claude_auth" in captured.out
    assert "ANTHROPIC_API_KEY set" in captured.out
    assert "7_canonical_kora_actor" in captured.out
    assert "Result: READY" in captured.out
    assert "2 gates passed" in captured.out


# ---------------------------------------------------------------------------
# Any FAIL → exit 1 + table shows the failure
# ---------------------------------------------------------------------------


def test_any_fail_returns_exit_1_and_names_failed_gate(capsys):
    failed = _gate_result(
        "7_canonical_kora_actor",
        outcome=GateOutcome.FAIL,
        gate_class=GateClass.INVARIANT,
        detail="no actor_kind='kora' row in actor_registry",
    )
    summary = BootSummary(
        result=BootResult.STOPPED,
        gate_results=[
            _gate_result("1_claude_auth"),
            failed,
        ],
        failed_gate=failed,
    )

    with (
        patch(
            "kora_cli.boot_cmd._load_isokron_provider", return_value="fake-provider"
        ),
        patch(
            "agent.boot_coordinator.run_boot_sequence",
            new_callable=AsyncMock, return_value=summary,
        ),
    ):
        rc = cmd_boot(_args())

    assert rc == 1
    captured = capsys.readouterr()
    assert "FAIL" in captured.out
    assert "7_canonical_kora_actor" in captured.out
    assert "Result: STOPPED" in captured.out
    assert "7_canonical_kora_actor" in captured.out


# ---------------------------------------------------------------------------
# Provider bootstrap raises — captured + diagnostic continues
# ---------------------------------------------------------------------------


def test_provider_bootstrap_raises_falls_back_to_none_provider(capsys):
    """If the provider can't be loaded, the coordinator runs with
    memory_provider=None — gates that depend on substrate FAIL but
    the diagnostic still completes."""
    summary = BootSummary(
        result=BootResult.STOPPED,
        gate_results=[
            _gate_result("1_claude_auth"),
            _gate_result(
                "5_kronicle_mcp_reachable",
                outcome=GateOutcome.FAIL,
                detail="memory_provider is not set",
            ),
        ],
        failed_gate=_gate_result(
            "5_kronicle_mcp_reachable",
            outcome=GateOutcome.FAIL,
            detail="memory_provider is not set",
        ),
    )

    with (
        patch("kora_cli.boot_cmd._load_isokron_provider", return_value=None),
        patch(
            "agent.boot_coordinator.run_boot_sequence",
            new_callable=AsyncMock, return_value=summary,
        ),
    ):
        rc = cmd_boot(_args())

    assert rc == 1
    captured = capsys.readouterr()
    assert "Result: STOPPED" in captured.out


# ---------------------------------------------------------------------------
# Coordinator raises — caught + diagnostic exit code 3
# ---------------------------------------------------------------------------


def test_coordinator_raises_unexpectedly_returns_exit_3(capsys):
    with (
        patch(
            "kora_cli.boot_cmd._load_isokron_provider", return_value="fake-provider"
        ),
        patch(
            "agent.boot_coordinator.run_boot_sequence",
            side_effect=RuntimeError("coordinator crashed"),
        ),
    ):
        rc = cmd_boot(_args())

    assert rc == 3
    captured = capsys.readouterr()
    assert "boot diagnostic raised unexpectedly" in captured.err
    assert "coordinator crashed" in captured.err


# ---------------------------------------------------------------------------
# KeyboardInterrupt → exit 130 (standard for ^C)
# ---------------------------------------------------------------------------


def test_keyboard_interrupt_returns_exit_130(capsys):
    with (
        patch(
            "kora_cli.boot_cmd._load_isokron_provider", return_value="fake-provider"
        ),
        patch(
            "agent.boot_coordinator.run_boot_sequence",
            side_effect=KeyboardInterrupt,
        ),
    ):
        rc = cmd_boot(_args())

    assert rc == 130
    captured = capsys.readouterr()
    assert "interrupted" in captured.err


# ---------------------------------------------------------------------------
# add_boot_parser — argparser wiring
# ---------------------------------------------------------------------------


def test_add_boot_parser_wires_check_only_flag():
    from kora_cli.boot_cmd import add_boot_parser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    add_boot_parser(subparsers)

    args = parser.parse_args(["boot", "--check-only"])
    assert args.command == "boot"
    assert args.check_only is True

    # Default (no flag) — check_only is False
    args = parser.parse_args(["boot"])
    assert args.check_only is False


def test_add_boot_parser_sets_func_to_cmd_boot():
    from kora_cli.boot_cmd import add_boot_parser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    add_boot_parser(subparsers)
    args = parser.parse_args(["boot", "--check-only"])
    assert args.func is cmd_boot


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_awaitable(value):
    """Wrap ``value`` in a coroutine factory so ``asyncio.run(coordinator(...))``
    can ``await`` it inside cmd_boot. Returns a CALLABLE that produces a
    fresh coroutine on each invocation — required because a coroutine
    can be awaited only once, and Mock's return_value reuses the same
    object across calls.
    """

    def _factory(*args, **kwargs):
        async def _coro():
            return value
        return _coro()

    return _factory
