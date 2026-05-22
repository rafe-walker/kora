"""KR-P2-K ST5 — tests for ``SeaTicketPoller`` ramped-resume gate.

Covers:
  - ``start_ramped_resume`` sets the window deadline.
  - With no ramp active, the gate is a no-op.
  - With a ramp active and no prior claim, the gate is a no-op
    (first claim passes through).
  - With a ramp active and a prior claim < min-interval ago, the gate
    sleeps for the remainder.
  - With a ramp active and a prior claim > min-interval ago, the
    gate is a no-op.
  - Past the ramp deadline, the gate clears its own state and is a
    no-op for subsequent calls.
  - Calling ``start_ramped_resume`` while a window is active extends
    the deadline (idempotent re-arm).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from plugins.memory.isokron.sea_ticket_poller import SeaTicketPoller


def _make_poller(
    *,
    duration: int = 3600,
    min_interval: int = 30,
) -> SeaTicketPoller:
    return SeaTicketPoller(
        mcp_client=MagicMock(),
        memory_provider=MagicMock(),
        ledger=MagicMock(),
        ramped_resume_duration_seconds=duration,
        ramped_resume_min_interval_seconds=min_interval,
    )


# ---------------------------------------------------------------------------
# start_ramped_resume — sets the deadline
# ---------------------------------------------------------------------------


def test_start_ramped_resume_sets_until_to_now_plus_duration():
    poller = _make_poller(duration=3600)
    before = datetime.now(timezone.utc)
    poller.start_ramped_resume()
    after = datetime.now(timezone.utc)

    until = poller._ramped_resume_until
    assert until is not None
    expected_low = before + timedelta(seconds=3600)
    expected_high = after + timedelta(seconds=3600)
    assert expected_low <= until <= expected_high


def test_start_ramped_resume_idempotent_extends_window():
    poller = _make_poller(duration=3600)
    poller.start_ramped_resume()
    first_until = poller._ramped_resume_until

    # Force a small wait so the second call's "now" is strictly later
    poller._ramped_resume_until = first_until - timedelta(seconds=1)
    poller.start_ramped_resume()
    second_until = poller._ramped_resume_until

    assert second_until is not None
    assert first_until is not None
    assert second_until > first_until


# ---------------------------------------------------------------------------
# Gate — no ramp active
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_no_ramp_is_noop():
    poller = _make_poller()
    # Should return immediately
    await asyncio.wait_for(poller._await_ramped_resume_gate(), timeout=1.0)


# ---------------------------------------------------------------------------
# Gate — ramp active, no prior claim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_ramp_active_no_prior_claim_is_noop():
    poller = _make_poller()
    poller.start_ramped_resume()
    assert poller._last_claim_started_at is None

    # First claim of the window passes immediately
    await asyncio.wait_for(poller._await_ramped_resume_gate(), timeout=1.0)


# ---------------------------------------------------------------------------
# Gate — ramp active, recent claim → sleep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_ramp_active_recent_claim_sleeps_remainder(monkeypatch):
    poller = _make_poller(min_interval=30)
    poller.start_ramped_resume()
    # Most recent claim was 5 seconds ago — gate should sleep ~25 more.
    poller._last_claim_started_at = datetime.now(timezone.utc) - timedelta(
        seconds=5
    )

    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    await poller._await_ramped_resume_gate()

    assert len(sleeps) == 1
    # ~25 seconds remaining (allow generous slack)
    assert 23.0 <= sleeps[0] <= 27.0


# ---------------------------------------------------------------------------
# Gate — ramp active, claim old enough → no sleep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_ramp_active_old_claim_no_sleep(monkeypatch):
    poller = _make_poller(min_interval=30)
    poller.start_ramped_resume()
    # Most recent claim was 60s ago — already past min_interval.
    poller._last_claim_started_at = datetime.now(timezone.utc) - timedelta(
        seconds=60
    )

    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    await poller._await_ramped_resume_gate()
    assert sleeps == []


# ---------------------------------------------------------------------------
# Gate — past deadline self-clears
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_past_deadline_clears_state():
    poller = _make_poller()
    # Manually plant an expired window.
    poller._ramped_resume_until = datetime.now(timezone.utc) - timedelta(
        seconds=10
    )
    poller._last_claim_started_at = datetime.now(timezone.utc) - timedelta(
        seconds=5
    )

    await asyncio.wait_for(poller._await_ramped_resume_gate(), timeout=1.0)

    # Window cleared so subsequent calls skip the elapsed check entirely.
    assert poller._ramped_resume_until is None


@pytest.mark.asyncio
async def test_gate_after_self_clear_subsequent_call_is_noop(monkeypatch):
    poller = _make_poller()
    poller._ramped_resume_until = datetime.now(timezone.utc) - timedelta(
        seconds=10
    )

    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    # First call clears state.
    await poller._await_ramped_resume_gate()
    # Second call must be a fast no-op.
    await poller._await_ramped_resume_gate()
    assert sleeps == []
    assert poller._ramped_resume_until is None


# ---------------------------------------------------------------------------
# Tunable defaults — operator override propagates
# ---------------------------------------------------------------------------


def test_operator_override_propagates_to_state():
    """The defaults match the bucket spec (3600s window, 30s
    interval). Operators can tune via constructor kwargs."""
    poller = _make_poller(duration=7200, min_interval=60)
    poller.start_ramped_resume()
    until = poller._ramped_resume_until
    assert until is not None
    now = datetime.now(timezone.utc)
    # Window is the longer 7200s.
    delta = (until - now).total_seconds()
    assert 7100 <= delta <= 7300
    assert poller._ramped_resume_min_interval_seconds == 60
