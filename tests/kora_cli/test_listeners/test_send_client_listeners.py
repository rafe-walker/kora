"""Listener-lifecycle tests for the SlackClient + PurelymailClient
listeners (KR-MCP-SEND-TOOLS).

Covers:
  - SlackClient listener fail-soft on missing KORA_SLACK_BOT_TOKEN
  - SlackClient listener constructs on KORA_SLACK_BOT_TOKEN set
  - current_slack_client() lifecycle (None / live / None across
    startup → shutdown)
  - PurelymailClient listener fail-soft on missing SMTP env
  - PurelymailClient listener constructs on full SMTP env
  - current_purelymail_client() lifecycle
  - Both listeners registered in LISTENER_REGISTRY at import time
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kora_cli import daemon as daemon_mod
from kora_cli.listeners.purelymail_client_listener import (
    PurelymailClientListener,
    _clear_singleton as _clear_purelymail_singleton,
    current_purelymail_client,
)
from kora_cli.listeners.slack_client_listener import (
    SlackClientListener,
    _clear_singleton as _clear_slack_singleton,
    current_slack_client,
)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    _clear_slack_singleton()
    _clear_purelymail_singleton()
    # Wipe any leftover env from prior tests
    for env in (
        "KORA_SLACK_BOT_TOKEN",
        "KORA_PUREMAIL_SMTP_USERNAME",
        "KORA_PUREMAIL_SMTP_APP_PASSWORD",
        "KORA_PUREMAIL_SMTP_HOST",
        "KORA_PUREMAIL_SMTP_PORT",
        "KORA_EMAIL_KORA_ALLOWED_FROM_DOMAINS",
    ):
        monkeypatch.delenv(env, raising=False)
    yield
    _clear_slack_singleton()
    _clear_purelymail_singleton()


# ---------------------------------------------------------------------------
# Registry-side wire-in
# ---------------------------------------------------------------------------


def test_slack_client_listener_registered_at_import_time():
    from kora_cli.listeners import slack_client_listener  # noqa: F401

    names = {name for name, _ in daemon_mod.LISTENER_REGISTRY}
    assert "slack_client" in names


def test_purelymail_client_listener_registered_at_import_time():
    from kora_cli.listeners import purelymail_client_listener  # noqa: F401

    names = {name for name, _ in daemon_mod.LISTENER_REGISTRY}
    assert "purelymail_client" in names


# ---------------------------------------------------------------------------
# SlackClient listener — fail-soft startup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_listener_fail_soft_when_token_unset(caplog):
    """No KORA_SLACK_BOT_TOKEN → singleton stays None; daemon
    continues. Outbound disabled, INFO log line."""
    import logging

    listener = SlackClientListener()
    with caplog.at_level(
        logging.INFO, logger="kora_cli.listeners.slack_client_listener"
    ):
        await listener.startup()
    assert current_slack_client() is None
    assert any(
        "Slack outbound disabled" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_slack_listener_constructs_when_token_set(monkeypatch):
    monkeypatch.setenv("KORA_SLACK_BOT_TOKEN", "xoxb-test-token")
    listener = SlackClientListener()
    await listener.startup()
    assert current_slack_client() is not None
    # current_slack_client returns the SlackClient instance
    from kora_cli.clients.slack_client import SlackClient

    assert isinstance(current_slack_client(), SlackClient)


@pytest.mark.asyncio
async def test_slack_listener_shutdown_clears_singleton(monkeypatch):
    monkeypatch.setenv("KORA_SLACK_BOT_TOKEN", "xoxb-test-token")
    listener = SlackClientListener()
    await listener.startup()
    assert current_slack_client() is not None
    await listener.shutdown()
    assert current_slack_client() is None


# ---------------------------------------------------------------------------
# PurelymailClient listener — fail-soft startup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purelymail_listener_fail_soft_when_username_unset():
    """No SMTP auth env → singleton stays None; daemon continues."""
    listener = PurelymailClientListener()
    await listener.startup()
    assert current_purelymail_client() is None


@pytest.mark.asyncio
async def test_purelymail_listener_fail_soft_when_password_unset(monkeypatch):
    """Username set but password missing → still fail-soft."""
    monkeypatch.setenv(
        "KORA_PUREMAIL_SMTP_USERNAME", "kora@stormhavenenterprises.com"
    )
    listener = PurelymailClientListener()
    await listener.startup()
    assert current_purelymail_client() is None


@pytest.mark.asyncio
async def test_purelymail_listener_constructs_with_full_env(monkeypatch):
    monkeypatch.setenv(
        "KORA_PUREMAIL_SMTP_USERNAME", "kora@stormhavenenterprises.com"
    )
    monkeypatch.setenv("KORA_PUREMAIL_SMTP_APP_PASSWORD", "test_password")
    listener = PurelymailClientListener()
    await listener.startup()
    assert current_purelymail_client() is not None
    from kora_cli.clients.purelymail_client import PurelymailClient

    assert isinstance(current_purelymail_client(), PurelymailClient)


@pytest.mark.asyncio
async def test_purelymail_listener_shutdown_clears_singleton(monkeypatch):
    monkeypatch.setenv(
        "KORA_PUREMAIL_SMTP_USERNAME", "kora@stormhavenenterprises.com"
    )
    monkeypatch.setenv("KORA_PUREMAIL_SMTP_APP_PASSWORD", "test_password")
    listener = PurelymailClientListener()
    await listener.startup()
    assert current_purelymail_client() is not None
    await listener.shutdown()
    assert current_purelymail_client() is None


# ---------------------------------------------------------------------------
# Listener startup-on-exception (defense-in-depth)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_listener_handles_unexpected_construction_error(caplog):
    """If SlackClient construction raises something OTHER than
    SlackClientNotConfigured (e.g. import error from a future
    refactor), the listener still fail-softs + logs WARN."""
    import logging

    with patch(
        "kora_cli.clients.slack_client.SlackClient",
        side_effect=RuntimeError("unexpected init failure"),
    ):
        listener = SlackClientListener()
        with caplog.at_level(
            logging.WARNING,
            logger="kora_cli.listeners.slack_client_listener",
        ):
            await listener.startup()
    assert current_slack_client() is None


@pytest.mark.asyncio
async def test_purelymail_listener_handles_unexpected_construction_error(caplog):
    import logging

    with patch(
        "kora_cli.clients.purelymail_client.PurelymailClient",
        side_effect=RuntimeError("unexpected init failure"),
    ):
        listener = PurelymailClientListener()
        with caplog.at_level(
            logging.WARNING,
            logger="kora_cli.listeners.purelymail_client_listener",
        ):
            await listener.startup()
    assert current_purelymail_client() is None
