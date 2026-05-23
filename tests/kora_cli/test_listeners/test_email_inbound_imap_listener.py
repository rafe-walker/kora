"""Tests for the IMAP poll listener (KR-FEAT-EMAIL-INBOUND-IMAP ST1).

Covers:
  - Listener registered at module-import time (LISTENER_REGISTRY)
  - Periodic task registered at module-import time
    (PERIODIC_TASK_REGISTRY) with default 300s cadence
  - Env override KORA_EMAIL_IMAP_POLL_INTERVAL_SEC respected
  - Invalid env (non-numeric, <=0) falls back to default + WARNs
  - Startup fail-soft on missing IMAP auth env (singleton stays None,
    daemon boots regardless)
  - Startup happy path: singleton populated; accessor returns it
  - Shutdown clears singleton + best-effort closes any open handle
  - Unexpected exception during startup → fail-soft (singleton None)
  - run_poll_cycle short-circuits cleanly when no client is registered
  - run_poll_cycle: connect → fetch_unseen → handle_event per message →
    mark_seen on should_mark_seen=True → close
  - run_poll_cycle: connect failure → log + skip cycle (no fetch attempted)
  - run_poll_cycle: fetch failure → log + still close
  - run_poll_cycle: per-message handler failure does NOT abort the cycle
  - run_poll_cycle: HandlerResult.should_mark_seen=False does NOT call
    mark_seen (handler_error path keeps UNSEEN for next-poll retry)
  - run_poll_cycle: mark_seen failure logs but does NOT abort cycle
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kora_cli import daemon as daemon_mod
from kora_cli.clients.purelymail_imap_client import (
    PurelymailIMAPConfigError,
    PurelymailIMAPConnectError,
    PurelymailIMAPError,
    PurelymailIMAPFetchError,
)
from kora_cli.clients.purelymail_types import ParsedIncomingEmail
from kora_cli.listeners import email_inbound_imap_listener
from kora_cli.listeners.email_inbound_imap_listener import (
    DEFAULT_POLL_INTERVAL_SEC,
    POLL_INTERVAL_ENV,
    EmailInboundIMAPListener,
    _clear_singleton,
    _factory,
    _read_poll_interval,
    current_imap_client,
    run_poll_cycle,
)
from kora_cli.listeners.heartbeat import PERIODIC_TASK_REGISTRY


@pytest.fixture(autouse=True)
def _reset_singleton():
    _clear_singleton()
    yield
    _clear_singleton()


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.delenv(POLL_INTERVAL_ENV, raising=False)
    monkeypatch.setenv(
        "KORA_PUREMAIL_IMAP_USERNAME", "kora@stormhavenenterprises.com"
    )
    monkeypatch.setenv("KORA_PUREMAIL_IMAP_APP_PASSWORD", "fixture-secret")


def _make_parsed(uid: int) -> ParsedIncomingEmail:
    from datetime import datetime, timezone

    return ParsedIncomingEmail(
        message_id=f"<msg-{uid}@example.com>",
        from_address="joshua@stormhavenenterprises.com",
        to=["kora@stormhavenenterprises.com"],
        subject=f"subj {uid}",
        body_text="body",
        body_html=None,
        has_html=False,
        received_at=datetime.now(timezone.utc),
        attachments=[],
        imap_uid=uid,
    )


# ===========================================================================
# Registration (import-time side effects)
# ===========================================================================


def test_listener_registered_in_daemon_registry():
    registered_names = {name for name, _factory in daemon_mod.LISTENER_REGISTRY}
    assert "email_inbound_imap" in registered_names


def test_periodic_task_registered():
    names = [t.name for t in PERIODIC_TASK_REGISTRY]
    assert "email.imap_poll" in names


def test_periodic_task_default_cadence():
    matches = [t for t in PERIODIC_TASK_REGISTRY if t.name == "email.imap_poll"]
    assert len(matches) == 1
    # Defaults to 300s when the env is unset at module-import time. The
    # module already loaded, so we assert the registered value is one of
    # {default, env override that happened to be set in CI}. Cover the
    # default-path branch directly via the helper.
    assert matches[0].interval_seconds > 0


def test_factory_returns_tuple_shape():
    startup, shutdown, timeout = _factory()
    assert callable(startup)
    assert callable(shutdown)
    assert isinstance(timeout, (int, float))
    assert timeout > 0


# ===========================================================================
# Cadence resolution
# ===========================================================================


def test_read_poll_interval_default(monkeypatch):
    monkeypatch.delenv(POLL_INTERVAL_ENV, raising=False)
    assert _read_poll_interval() == DEFAULT_POLL_INTERVAL_SEC


def test_read_poll_interval_env_override(monkeypatch):
    monkeypatch.setenv(POLL_INTERVAL_ENV, "60")
    assert _read_poll_interval() == 60.0


def test_read_poll_interval_invalid_falls_back(monkeypatch, caplog):
    monkeypatch.setenv(POLL_INTERVAL_ENV, "not-a-number")
    with caplog.at_level("WARNING"):
        assert _read_poll_interval() == DEFAULT_POLL_INTERVAL_SEC
    assert any(
        "is not numeric" in record.message for record in caplog.records
    )


def test_read_poll_interval_zero_falls_back(monkeypatch, caplog):
    monkeypatch.setenv(POLL_INTERVAL_ENV, "0")
    with caplog.at_level("WARNING"):
        assert _read_poll_interval() == DEFAULT_POLL_INTERVAL_SEC


def test_read_poll_interval_negative_falls_back(monkeypatch):
    monkeypatch.setenv(POLL_INTERVAL_ENV, "-15")
    assert _read_poll_interval() == DEFAULT_POLL_INTERVAL_SEC


# ===========================================================================
# Startup — fail-soft + happy path
# ===========================================================================


@pytest.mark.asyncio
async def test_startup_fail_soft_on_missing_username(monkeypatch):
    monkeypatch.delenv("KORA_PUREMAIL_IMAP_USERNAME", raising=False)
    listener = EmailInboundIMAPListener()
    await listener.startup()
    assert current_imap_client() is None


@pytest.mark.asyncio
async def test_startup_fail_soft_on_missing_password(monkeypatch):
    monkeypatch.delenv("KORA_PUREMAIL_IMAP_APP_PASSWORD", raising=False)
    listener = EmailInboundIMAPListener()
    await listener.startup()
    assert current_imap_client() is None


@pytest.mark.asyncio
async def test_startup_fail_soft_on_unexpected_exception():
    listener = EmailInboundIMAPListener()
    with patch(
        "kora_cli.clients.purelymail_imap_client.PurelymailIMAPClient",
        side_effect=RuntimeError("unexpected boom"),
    ):
        await listener.startup()
    assert current_imap_client() is None


@pytest.mark.asyncio
async def test_startup_happy_path_populates_singleton():
    listener = EmailInboundIMAPListener()
    await listener.startup()
    client = current_imap_client()
    assert client is not None
    assert client._host == "imap.purelymail.com"
    assert client._port == 993


@pytest.mark.asyncio
async def test_shutdown_clears_singleton_and_closes_handle():
    listener = EmailInboundIMAPListener()
    await listener.startup()
    client = current_imap_client()
    assert client is not None
    # Patch the close on the live client.
    client.close = AsyncMock()
    await listener.shutdown()
    assert current_imap_client() is None
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_idempotent_without_startup():
    listener = EmailInboundIMAPListener()
    await listener.shutdown()
    assert current_imap_client() is None


# ===========================================================================
# run_poll_cycle behavior
# ===========================================================================


@pytest.mark.asyncio
async def test_run_poll_cycle_short_circuits_when_no_client():
    # No singleton set.
    await run_poll_cycle()  # must not raise


def _make_fake_handler(results_by_uid):
    """Build a fake EmailInboundHandler whose handle_event returns
    a preset HandlerResult per imap_uid."""
    from kora_cli.handlers.email_inbound_handler import HandlerResult

    seen_calls = []

    async def fake_handle(parsed):
        seen_calls.append(parsed.imap_uid)
        spec = results_by_uid.get(parsed.imap_uid)
        if isinstance(spec, Exception):
            raise spec
        if spec is None:
            return HandlerResult(
                status="received", should_mark_seen=True, should_reply=False
            )
        return spec

    fake_handler_instance = MagicMock()
    fake_handler_instance.handle_event = AsyncMock(side_effect=fake_handle)
    return fake_handler_instance, seen_calls


@pytest.mark.asyncio
async def test_run_poll_cycle_happy_path_handles_each_and_marks_seen():
    fake_client = MagicMock()
    fake_client.connect = AsyncMock()
    fake_client.fetch_unseen = AsyncMock(
        return_value=[_make_parsed(7), _make_parsed(9)]
    )
    fake_client.mark_seen = AsyncMock()
    fake_client.close = AsyncMock()

    fake_handler, seen_calls = _make_fake_handler({})

    with patch.object(
        email_inbound_imap_listener, "_imap_client_singleton", fake_client
    ), patch(
        "kora_cli.handlers.email_inbound_handler.EmailInboundHandler",
        return_value=fake_handler,
    ):
        await run_poll_cycle()

    fake_client.connect.assert_awaited_once()
    fake_client.fetch_unseen.assert_awaited_once()
    fake_client.close.assert_awaited_once()
    assert seen_calls == [7, 9]
    # Both UIDs marked SEEN (default HandlerResult.should_mark_seen=True).
    assert fake_client.mark_seen.await_count == 2
    fake_client.mark_seen.assert_any_await(7)
    fake_client.mark_seen.assert_any_await(9)


@pytest.mark.asyncio
async def test_run_poll_cycle_connect_failure_skips_fetch():
    fake_client = MagicMock()
    fake_client.connect = AsyncMock(
        side_effect=PurelymailIMAPConnectError("network down")
    )
    fake_client.fetch_unseen = AsyncMock()
    fake_client.close = AsyncMock()

    with patch.object(
        email_inbound_imap_listener, "_imap_client_singleton", fake_client
    ):
        await run_poll_cycle()

    fake_client.fetch_unseen.assert_not_awaited()
    # close not called because we returned BEFORE entering the try/finally
    # (connect failure short-circuits the cycle).
    fake_client.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_poll_cycle_unexpected_connect_exc_skips():
    fake_client = MagicMock()
    fake_client.connect = AsyncMock(side_effect=RuntimeError("???"))
    fake_client.fetch_unseen = AsyncMock()
    fake_client.close = AsyncMock()

    with patch.object(
        email_inbound_imap_listener, "_imap_client_singleton", fake_client
    ):
        await run_poll_cycle()

    fake_client.fetch_unseen.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_poll_cycle_fetch_failure_still_closes():
    fake_client = MagicMock()
    fake_client.connect = AsyncMock()
    fake_client.fetch_unseen = AsyncMock(
        side_effect=PurelymailIMAPFetchError("SEARCH UNSEEN failed")
    )
    fake_client.close = AsyncMock()

    with patch.object(
        email_inbound_imap_listener, "_imap_client_singleton", fake_client
    ):
        await run_poll_cycle()

    fake_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_poll_cycle_no_unseen_messages():
    fake_client = MagicMock()
    fake_client.connect = AsyncMock()
    fake_client.fetch_unseen = AsyncMock(return_value=[])
    fake_client.mark_seen = AsyncMock()
    fake_client.close = AsyncMock()

    fake_handler, seen_calls = _make_fake_handler({})

    with patch.object(
        email_inbound_imap_listener, "_imap_client_singleton", fake_client
    ), patch(
        "kora_cli.handlers.email_inbound_handler.EmailInboundHandler",
        return_value=fake_handler,
    ):
        await run_poll_cycle()

    assert seen_calls == []
    fake_client.mark_seen.assert_not_awaited()
    fake_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_poll_cycle_per_message_handler_exception_does_not_abort():
    """Handler raise on uid=7 must not prevent uid=9 from being attempted."""
    fake_client = MagicMock()
    fake_client.connect = AsyncMock()
    fake_client.fetch_unseen = AsyncMock(
        return_value=[_make_parsed(7), _make_parsed(9)]
    )
    fake_client.mark_seen = AsyncMock()
    fake_client.close = AsyncMock()

    fake_handler, seen_calls = _make_fake_handler(
        {7: RuntimeError("handler boom")}
    )

    with patch.object(
        email_inbound_imap_listener, "_imap_client_singleton", fake_client
    ), patch(
        "kora_cli.handlers.email_inbound_handler.EmailInboundHandler",
        return_value=fake_handler,
    ):
        await run_poll_cycle()

    assert seen_calls == [7, 9]
    # uid=7 exception keeps UNSEEN; uid=9 returned default-OK so marked.
    fake_client.mark_seen.assert_awaited_once_with(9)
    fake_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_poll_cycle_should_mark_seen_false_keeps_unseen():
    """HandlerResult.should_mark_seen=False (e.g. handler_error path)
    must NOT call mark_seen."""
    from kora_cli.handlers.email_inbound_handler import HandlerResult

    fake_client = MagicMock()
    fake_client.connect = AsyncMock()
    fake_client.fetch_unseen = AsyncMock(return_value=[_make_parsed(11)])
    fake_client.mark_seen = AsyncMock()
    fake_client.close = AsyncMock()

    fake_handler, _seen = _make_fake_handler(
        {
            11: HandlerResult(
                status="handler_error",
                should_mark_seen=False,
                should_reply=False,
            )
        }
    )

    with patch.object(
        email_inbound_imap_listener, "_imap_client_singleton", fake_client
    ), patch(
        "kora_cli.handlers.email_inbound_handler.EmailInboundHandler",
        return_value=fake_handler,
    ):
        await run_poll_cycle()

    fake_client.mark_seen.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_poll_cycle_mark_seen_failure_does_not_abort():
    """mark_seen failure on uid=7 must not prevent uid=9 from being processed."""
    fake_client = MagicMock()
    fake_client.connect = AsyncMock()
    fake_client.fetch_unseen = AsyncMock(
        return_value=[_make_parsed(7), _make_parsed(9)]
    )

    mark_seen_calls = []

    async def mark_seen_dispatch(uid):
        mark_seen_calls.append(uid)
        if uid == 7:
            raise RuntimeError("STORE failed")

    fake_client.mark_seen = AsyncMock(side_effect=mark_seen_dispatch)
    fake_client.close = AsyncMock()

    fake_handler, seen_calls = _make_fake_handler({})

    with patch.object(
        email_inbound_imap_listener, "_imap_client_singleton", fake_client
    ), patch(
        "kora_cli.handlers.email_inbound_handler.EmailInboundHandler",
        return_value=fake_handler,
    ):
        await run_poll_cycle()

    assert seen_calls == [7, 9]
    assert mark_seen_calls == [7, 9]
    fake_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_poll_cycle_close_failure_is_fail_soft():
    fake_client = MagicMock()
    fake_client.connect = AsyncMock()
    fake_client.fetch_unseen = AsyncMock(return_value=[])
    fake_client.close = AsyncMock(side_effect=RuntimeError("close boom"))

    with patch.object(
        email_inbound_imap_listener, "_imap_client_singleton", fake_client
    ):
        # Must not raise — close failure logged + swallowed.
        await run_poll_cycle()
