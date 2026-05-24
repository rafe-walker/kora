"""Integration tests for the post-cycle hook wired into
``kora_cli/heartbeat_probes/runner.py`` — KR-PROBE-AUDIT-AND-CONVERT.

Scenarios:
  1. Routine cycle with all-healthy snapshots → NO wake events emitted
  2. Cycle with one unhealthy snapshot → ONE wake event for that probe
  3. Cycle with multiple issues → one wake event per affected probe
  4. Hook failure path: detect_issues raises → runner still completes
     cycle + populates cache; cycle does NOT crash
  5. Routine cycle does NOT invoke LLM (no reasoning import triggered)
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from kora_cli.heartbeat_probes.runner import run_all_probes
from kora_cli.heartbeat_probes.types import ServiceHealthSnapshot


def _mock_probe(name: str, status: str = "healthy", error=None, details=None):
    """Build a fake probe object whose ``check()`` returns a
    pre-baked ServiceHealthSnapshot."""

    class MockProbe:
        pass

    probe = MockProbe()
    probe.name = name
    snap = ServiceHealthSnapshot(
        name=name,
        status=status,
        latency_ms=10,
        last_check_at=datetime.now(timezone.utc),
        details=details or {},
        error=error,
    )
    probe.check = AsyncMock(return_value=snap)
    return probe


@pytest.mark.asyncio
async def test_all_healthy_no_wake_events():
    probes = [
        _mock_probe("supabase", "healthy"),
        _mock_probe("fly", "healthy"),
        _mock_probe("vercel", "healthy"),
        _mock_probe("sentry", "healthy"),
        _mock_probe("doppler", "healthy"),
    ]
    with patch("kora_cli.audit.emit_audit") as mock_emit:
        await run_all_probes(probes)
    mock_emit.assert_not_called()


@pytest.mark.asyncio
async def test_one_unhealthy_one_wake_event():
    probes = [
        _mock_probe("supabase", "healthy"),
        _mock_probe("fly", "unhealthy", error="HTTP 500"),
        _mock_probe("vercel", "healthy"),
    ]
    with patch("kora_cli.audit.emit_audit") as mock_emit:
        await run_all_probes(probes)
    # Only the fly issue should have emitted.
    assert mock_emit.call_count == 1
    call = mock_emit.call_args
    assert call.kwargs["seam"] == "probe.wake_requested"
    assert call.kwargs["details"]["probe"] == "fly"


@pytest.mark.asyncio
async def test_multiple_issues_emit_per_probe():
    probes = [
        _mock_probe("supabase", "unhealthy", error="HTTP 502"),
        _mock_probe("fly", "degraded", details={"apps_running": 1}),
        _mock_probe("vercel", "healthy"),
        _mock_probe("sentry", "degraded", details={"unresolved_issues": 42}),
        _mock_probe("doppler", "healthy"),
    ]
    with patch("kora_cli.audit.emit_audit") as mock_emit:
        await run_all_probes(probes)
    # 3 issues fired: supabase critical + fly warning + sentry warning.
    assert mock_emit.call_count == 3
    emitted_probes = {
        c.kwargs["details"]["probe"] for c in mock_emit.call_args_list
    }
    assert emitted_probes == {"supabase", "fly", "sentry"}


@pytest.mark.asyncio
async def test_hook_failure_does_not_crash_cycle(caplog):
    """If detect_issues raises, the runner's hook logs + continues —
    the cache is still populated, the cycle completes successfully."""
    probes = [
        _mock_probe("supabase", "healthy"),
        _mock_probe("fly", "healthy"),
    ]
    with patch(
        "kora_cli.probes.detect_issues",
        side_effect=RuntimeError("detector exploded"),
    ):
        with caplog.at_level("WARNING"):
            results = await run_all_probes(probes)
    # Cycle still completed; cache was populated.
    assert "supabase" in results
    assert "fly" in results
    # Warning logged.
    assert any(
        "post-cycle issue detection raised" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_routine_cycle_does_not_invoke_llm():
    """Spec §2 Phase 2 invariant — the routine probing path is $0
    LLM cost. We assert no reasoning-engine respond() call happens
    during run_all_probes even with all 5 probes failing.

    The runner hook ONLY emits audit events; it does NOT invoke
    reasoning. The consumer side (reasoning wake) is a follow-on.
    """
    probes = [
        _mock_probe("supabase", "unhealthy", error="HTTP 500"),
        _mock_probe("fly", "unhealthy", error="HTTP 500"),
        _mock_probe("vercel", "unhealthy", error="HTTP 500"),
        _mock_probe("sentry", "unhealthy", error="HTTP 500"),
        _mock_probe("doppler", "unhealthy", error="HTTP 500"),
    ]
    # Patch potential reasoning entry points so any accidental call
    # would surface as a test failure.
    with patch("kora_cli.audit.emit_audit"):
        try:
            from kora_cli.reasoning.anthropic_engine import (
                AnthropicReasoningEngine,
            )
            with patch.object(
                AnthropicReasoningEngine, "respond", new=AsyncMock(side_effect=AssertionError("reasoning invoked from routine probe path"))
            ):
                await run_all_probes(probes)
        except ImportError:
            # Reasoning module not importable in test env — that's
            # itself proof the routine probe path doesn't import it.
            await run_all_probes(probes)
