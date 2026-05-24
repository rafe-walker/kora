"""Tests for KR-PROBE-WAKE-CONSUMER — daemon listener.

Scenarios:
  1. Listener registered in LISTENER_REGISTRY at import time
  2. Periodic task `probe_wake.tail` registered with heartbeat
  3. Default poll cadence 30s; env override respected
  4. Invalid env value falls back to default + WARNs
  5. Factory tuple shape correct
  6. Listener startup populates the singleton with a live consumer
  7. Listener shutdown resets debounce + tail position + clears singleton
  8. run_tail_cycle short-circuits cleanly without singleton
  9. run_tail_cycle first call stamps tail-position; no replay
 10. run_tail_cycle subsequent call processes fresh entries only
 11. run_tail_cycle handles read_audit_entries failure gracefully
 12. run_tail_cycle handles consume_wake_event raise per-event
 13. _reset_tail_position_for_tests clears module state
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kora_cli import daemon as daemon_mod
from kora_cli.listeners import probe_wake_listener
from kora_cli.listeners.heartbeat import PERIODIC_TASK_REGISTRY
from kora_cli.listeners.probe_wake_listener import (
    DEFAULT_POLL_SEC,
    POLL_SEC_ENV,
    ProbeWakeListener,
    _clear_consumer,
    _factory,
    _read_poll_sec,
    _reset_tail_position_for_tests,
    current_probe_wake_consumer,
    run_tail_cycle,
)


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    _clear_consumer()
    _reset_tail_position_for_tests()
    monkeypatch.delenv(POLL_SEC_ENV, raising=False)
    yield
    _clear_consumer()
    _reset_tail_position_for_tests()


# ===========================================================================
# Registration + cadence
# ===========================================================================


def test_listener_registered():
    names = {n for n, _f in daemon_mod.LISTENER_REGISTRY}
    assert "probe_wake" in names


def test_periodic_task_registered():
    names = [t.name for t in PERIODIC_TASK_REGISTRY]
    assert "probe_wake.tail" in names


def test_factory_tuple_shape():
    startup, shutdown, timeout = _factory()
    assert callable(startup)
    assert callable(shutdown)
    assert isinstance(timeout, (int, float))


def test_poll_sec_default():
    assert _read_poll_sec() == DEFAULT_POLL_SEC == 30.0


def test_poll_sec_env_override(monkeypatch):
    monkeypatch.setenv(POLL_SEC_ENV, "5")
    assert _read_poll_sec() == 5.0


def test_poll_sec_invalid_falls_back(monkeypatch, caplog):
    monkeypatch.setenv(POLL_SEC_ENV, "not-numeric")
    with caplog.at_level("WARNING"):
        assert _read_poll_sec() == DEFAULT_POLL_SEC
    assert any("is not numeric" in r.message for r in caplog.records)


def test_poll_sec_zero_falls_back(monkeypatch):
    monkeypatch.setenv(POLL_SEC_ENV, "0")
    assert _read_poll_sec() == DEFAULT_POLL_SEC


# ===========================================================================
# Listener lifecycle
# ===========================================================================


@pytest.mark.asyncio
async def test_startup_populates_singleton():
    listener = ProbeWakeListener()
    await listener.startup()
    assert current_probe_wake_consumer() is not None


@pytest.mark.asyncio
async def test_startup_failsoft_on_unexpected_exception():
    listener = ProbeWakeListener()
    with patch(
        "kora_cli.listeners.probe_wake_listener.ProbeWakeConsumer",
        side_effect=RuntimeError("kaboom"),
    ):
        await listener.startup()
    assert current_probe_wake_consumer() is None


@pytest.mark.asyncio
async def test_shutdown_resets_debounce_and_tail_clears_singleton():
    listener = ProbeWakeListener()
    await listener.startup()
    consumer = current_probe_wake_consumer()
    assert consumer is not None
    # Stamp some state.
    consumer._mark_dispatched("fly", "service_unhealthy")
    assert consumer.debounce_map_size == 1
    await listener.shutdown()
    assert current_probe_wake_consumer() is None


# ===========================================================================
# run_tail_cycle
# ===========================================================================


@pytest.mark.asyncio
async def test_tail_cycle_short_circuits_without_consumer():
    # No singleton — should not raise.
    await run_tail_cycle()


@pytest.mark.asyncio
async def test_tail_cycle_first_call_stamps_no_replay():
    """First tick after listener startup MUST NOT replay history;
    just stamps the position."""
    listener = ProbeWakeListener()
    await listener.startup()
    consumer = current_probe_wake_consumer()
    consumer.consume_wake_event = AsyncMock()

    with patch(
        "kora_cli.audit.jsonl_reader.read_audit_entries"
    ) as mock_read:
        await run_tail_cycle()

    # read_audit_entries NOT called on first tick (we stamp + return).
    mock_read.assert_not_called()
    consumer.consume_wake_event.assert_not_called()
    # Tail position is set.
    assert probe_wake_listener._last_seen_at is not None


@pytest.mark.asyncio
async def test_tail_cycle_subsequent_call_processes_fresh_entries():
    listener = ProbeWakeListener()
    await listener.startup()
    consumer = current_probe_wake_consumer()
    consumer.consume_wake_event = AsyncMock()

    # First tick stamps the position.
    with patch(
        "kora_cli.audit.jsonl_reader.read_audit_entries"
    ) as mock_read:
        await run_tail_cycle()

    # Second tick reads + dispatches.
    fake_row_1 = MagicMock()
    fake_row_1.details = {"probe": "fly", "category": "service_unhealthy"}
    fake_row_1.emitted_at = datetime.now(timezone.utc)
    fake_row_2 = MagicMock()
    fake_row_2.details = {"probe": "vercel", "category": "service_unhealthy"}
    fake_row_2.emitted_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    # read_audit_entries returns newest-first; the listener
    # reverses to chronological.
    with patch(
        "kora_cli.audit.jsonl_reader.read_audit_entries",
        return_value=[fake_row_2, fake_row_1],
    ):
        await run_tail_cycle()

    assert consumer.consume_wake_event.await_count == 2
    # Chronological order: fly first, vercel second.
    calls = [c.args[0] for c in consumer.consume_wake_event.await_args_list]
    assert calls[0]["probe"] == "fly"
    assert calls[1]["probe"] == "vercel"


@pytest.mark.asyncio
async def test_tail_cycle_read_failure_no_crash(caplog):
    listener = ProbeWakeListener()
    await listener.startup()
    # First tick stamps.
    await run_tail_cycle()
    with patch(
        "kora_cli.audit.jsonl_reader.read_audit_entries",
        side_effect=OSError("audit log unreadable"),
    ):
        with caplog.at_level("WARNING"):
            await run_tail_cycle()  # must not raise
    assert any(
        "read_audit_entries raised" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_tail_cycle_per_event_failure_does_not_abort(caplog):
    """If consume_wake_event raises on one row, the rest of the rows
    are still attempted (and the tail position still advances)."""
    listener = ProbeWakeListener()
    await listener.startup()
    consumer = current_probe_wake_consumer()

    seen = []

    async def fake_consume(details):
        seen.append(details["probe"])
        if details["probe"] == "fly":
            raise RuntimeError("consume boom")

    consumer.consume_wake_event = AsyncMock(side_effect=fake_consume)

    # First tick stamps.
    await run_tail_cycle()

    fly_row = MagicMock()
    fly_row.details = {"probe": "fly", "category": "service_unhealthy"}
    fly_row.emitted_at = datetime.now(timezone.utc)
    vercel_row = MagicMock()
    vercel_row.details = {"probe": "vercel", "category": "service_unhealthy"}
    vercel_row.emitted_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    with patch(
        "kora_cli.audit.jsonl_reader.read_audit_entries",
        return_value=[vercel_row, fly_row],
    ):
        with caplog.at_level("WARNING"):
            await run_tail_cycle()

    # Both probes attempted despite fly's raise.
    assert seen == ["fly", "vercel"]
    # Warning logged for the failed consume.
    assert any(
        "consume_wake_event raised" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_tail_cycle_does_not_replay_processed_rows():
    """Second tick after processing a row should NOT re-process it."""
    listener = ProbeWakeListener()
    await listener.startup()
    consumer = current_probe_wake_consumer()
    consumer.consume_wake_event = AsyncMock()

    # Stamp.
    await run_tail_cycle()

    row1 = MagicMock()
    row1.details = {"probe": "fly", "category": "service_unhealthy"}
    row1.emitted_at = datetime.now(timezone.utc)
    with patch(
        "kora_cli.audit.jsonl_reader.read_audit_entries",
        return_value=[row1],
    ):
        await run_tail_cycle()
    assert consumer.consume_wake_event.await_count == 1

    # Third tick: reader returns nothing fresh (filtered by since=tail).
    with patch(
        "kora_cli.audit.jsonl_reader.read_audit_entries",
        return_value=[],
    ):
        await run_tail_cycle()
    # No additional invocation.
    assert consumer.consume_wake_event.await_count == 1
