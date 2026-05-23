"""Tests for the KR-ALERT-NOTIFY ST1 daemon listener.

Bucket §2(e) scenarios:
  1. Listener registered in LISTENER_REGISTRY at import time
  2. Periodic task `alerts.notify` registered with the heartbeat
     scheduler at import time
  3. Default cadence is 180s (3 min); env override respected
  4. Invalid env value falls back to default + WARNs
  5. Listener startup populates the module singleton with a live
     AlertNotifier; current_alert_notifier() returns it
  6. Listener shutdown resets dedup state + clears singleton
  7. run_notification_cycle short-circuits cleanly when no notifier
     is active
  8. run_notification_cycle defense-in-depth catch — notifier raise
     does not kill the scheduler
  9. Factory tuple shape correct (startup, shutdown, timeout)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kora_cli import daemon as daemon_mod
from kora_cli.listeners import alert_notifier_listener
from kora_cli.listeners.alert_notifier_listener import (
    DEFAULT_INTERVAL_SEC,
    INTERVAL_ENV,
    AlertNotifierListener,
    _clear_singleton,
    _factory,
    _read_interval,
    current_alert_notifier,
    run_notification_cycle,
)
from kora_cli.listeners.heartbeat import PERIODIC_TASK_REGISTRY


@pytest.fixture(autouse=True)
def _reset_singleton():
    _clear_singleton()
    yield
    _clear_singleton()


# ===========================================================================
# Registration
# ===========================================================================


def test_listener_registered_in_daemon_registry():
    registered_names = {name for name, _factory in daemon_mod.LISTENER_REGISTRY}
    assert "alert_notifier" in registered_names


def test_periodic_task_registered():
    names = [t.name for t in PERIODIC_TASK_REGISTRY]
    assert "alerts.notify" in names


def test_factory_returns_tuple_shape():
    startup, shutdown, timeout = _factory()
    assert callable(startup)
    assert callable(shutdown)
    assert isinstance(timeout, (int, float))
    assert timeout > 0


# ===========================================================================
# Cadence
# ===========================================================================


def test_read_interval_default(monkeypatch):
    monkeypatch.delenv(INTERVAL_ENV, raising=False)
    assert _read_interval() == DEFAULT_INTERVAL_SEC == 180.0


def test_read_interval_env_override(monkeypatch):
    monkeypatch.setenv(INTERVAL_ENV, "60")
    assert _read_interval() == 60.0


def test_read_interval_invalid_falls_back(monkeypatch, caplog):
    monkeypatch.setenv(INTERVAL_ENV, "not-a-number")
    with caplog.at_level("WARNING"):
        assert _read_interval() == DEFAULT_INTERVAL_SEC
    assert any(
        "is not numeric" in r.message for r in caplog.records
    )


def test_read_interval_zero_falls_back(monkeypatch):
    monkeypatch.setenv(INTERVAL_ENV, "0")
    assert _read_interval() == DEFAULT_INTERVAL_SEC


def test_read_interval_negative_falls_back(monkeypatch):
    monkeypatch.setenv(INTERVAL_ENV, "-5")
    assert _read_interval() == DEFAULT_INTERVAL_SEC


# ===========================================================================
# Listener lifecycle
# ===========================================================================


@pytest.mark.asyncio
async def test_startup_populates_singleton():
    listener = AlertNotifierListener()
    await listener.startup()
    notifier = current_alert_notifier()
    assert notifier is not None


@pytest.mark.asyncio
async def test_startup_failsoft_on_unexpected_exception():
    listener = AlertNotifierListener()
    with patch(
        "kora_cli.listeners.alert_notifier_listener.AlertNotifier",
        side_effect=RuntimeError("unexpected"),
    ):
        await listener.startup()
    assert current_alert_notifier() is None


@pytest.mark.asyncio
async def test_shutdown_resets_dedup_and_clears_singleton():
    listener = AlertNotifierListener()
    await listener.startup()
    notifier = current_alert_notifier()
    assert notifier is not None
    # Spy on reset_dedup_state.
    with patch.object(notifier, "reset_dedup_state") as mock_reset:
        await listener.shutdown()
    mock_reset.assert_called_once()
    assert current_alert_notifier() is None


@pytest.mark.asyncio
async def test_shutdown_idempotent_without_startup():
    listener = AlertNotifierListener()
    await listener.shutdown()
    assert current_alert_notifier() is None


# ===========================================================================
# run_notification_cycle behavior
# ===========================================================================


@pytest.mark.asyncio
async def test_run_cycle_short_circuits_without_singleton():
    # No singleton.
    await run_notification_cycle()  # must not raise


@pytest.mark.asyncio
async def test_run_cycle_invokes_notifier():
    fake_notifier = MagicMock()
    fake_result = MagicMock()
    fake_result.newly_firing_count = 0
    fake_result.dispatch_errors = 0
    fake_result.active_count = 0
    fake_result.newly_resolved_count = 0
    fake_result.slack_dispatched = 0
    fake_result.email_dispatched = 0
    fake_notifier.run_notification_cycle = AsyncMock(return_value=fake_result)
    with patch.object(
        alert_notifier_listener,
        "_notifier_singleton",
        fake_notifier,
    ):
        await run_notification_cycle()
    fake_notifier.run_notification_cycle.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_cycle_swallows_unexpected_exceptions():
    """Defense in depth — the notifier's run_notification_cycle
    already catches inside; this outer guard catches any path that
    bypasses the inner catch."""
    fake_notifier = MagicMock()
    fake_notifier.run_notification_cycle = AsyncMock(
        side_effect=RuntimeError("unexpected from notifier")
    )
    with patch.object(
        alert_notifier_listener,
        "_notifier_singleton",
        fake_notifier,
    ):
        # Must not raise.
        await run_notification_cycle()


# ===========================================================================
# Factory wiring
# ===========================================================================


def test_slack_client_factory_returns_none_when_listener_absent():
    """Factory used internally to lazily resolve the SlackClient.
    When the slack_client_listener isn't imported / running, the
    factory returns None gracefully."""
    from kora_cli.listeners.alert_notifier_listener import (
        _slack_client_factory,
    )

    with patch(
        "kora_cli.listeners.slack_client_listener.current_slack_client",
        return_value=None,
    ):
        assert _slack_client_factory() is None


def test_purelymail_client_factory_returns_none_when_listener_absent():
    from kora_cli.listeners.alert_notifier_listener import (
        _purelymail_client_factory,
    )

    with patch(
        "kora_cli.listeners.purelymail_client_listener.current_purelymail_client",
        return_value=None,
    ):
        assert _purelymail_client_factory() is None
