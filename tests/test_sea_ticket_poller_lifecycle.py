"""Unit tests for ``plugins/memory/isokron/sea_ticket_poller_lifecycle.py``
(KR-P2-E ST5).

Covers the fail-open guards in ``build_and_start_sea_ticket_poller``:

  - Provider plugin not discoverable → returns None + logs WARNING
  - Provider is_available() = False → returns None + logs WARNING
  - Provider initialize() raises → returns None + logs exception
  - MCP client lookup raises → returns None + logs exception
  - Happy path → constructs poller, spawns task, returns (poller, task)
  - Custom agent_loop_invoker forwarded into the poller
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugins.memory.isokron.sea_ticket_poller import SeaTicketPoller
from plugins.memory.isokron.sea_ticket_poller_lifecycle import (
    build_and_start_sea_ticket_poller,
)


# ---------------------------------------------------------------------------
# Fake provider — exposes the exact surface the lifecycle helper consults
# ---------------------------------------------------------------------------


def _make_provider(
    *,
    available: bool = True,
    init_raises: Exception | None = None,
    mcp_client_raises: Exception | None = None,
) -> Any:
    provider = MagicMock()
    provider.is_available = MagicMock(return_value=available)
    if init_raises is not None:
        provider.initialize = MagicMock(side_effect=init_raises)
    else:
        provider.initialize = MagicMock(return_value=None)

    if mcp_client_raises is not None:
        provider._connection.get_mcp_client = MagicMock(side_effect=mcp_client_raises)
    else:
        provider._connection.get_mcp_client = MagicMock(return_value=MagicMock())
    return provider


# ---------------------------------------------------------------------------
# Fail-open paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_none_when_provider_not_discoverable(caplog):
    with patch(
        "plugins.memory.load_memory_provider", return_value=None
    ), caplog.at_level(
        logging.WARNING, logger="plugins.memory.isokron.sea_ticket_poller_lifecycle"
    ):
        result = await build_and_start_sea_ticket_poller()

    assert result is None
    assert any(
        "plugin not discoverable" in record.message for record in caplog.records
    )


@pytest.mark.asyncio
async def test_returns_none_when_is_available_false(caplog):
    provider = _make_provider(available=False)
    with patch(
        "plugins.memory.load_memory_provider", return_value=provider
    ), caplog.at_level(
        logging.WARNING, logger="plugins.memory.isokron.sea_ticket_poller_lifecycle"
    ):
        result = await build_and_start_sea_ticket_poller()

    assert result is None
    assert any(
        "is_available() = False" in record.message for record in caplog.records
    )
    # initialize() must NOT have been called.
    provider.initialize.assert_not_called()


@pytest.mark.asyncio
async def test_returns_none_when_initialize_raises(caplog):
    provider = _make_provider(init_raises=RuntimeError("connection refused"))
    with patch(
        "plugins.memory.load_memory_provider", return_value=provider
    ), caplog.at_level(
        logging.ERROR, logger="plugins.memory.isokron.sea_ticket_poller_lifecycle"
    ):
        result = await build_and_start_sea_ticket_poller()

    assert result is None
    assert any(
        "initialize() raised" in record.message for record in caplog.records
    )


@pytest.mark.asyncio
async def test_returns_none_when_mcp_client_raises(caplog):
    provider = _make_provider(mcp_client_raises=RuntimeError("Sea MCP down"))
    with patch(
        "plugins.memory.load_memory_provider", return_value=provider
    ), caplog.at_level(
        logging.ERROR, logger="plugins.memory.isokron.sea_ticket_poller_lifecycle"
    ):
        result = await build_and_start_sea_ticket_poller()

    assert result is None
    assert any(
        "MCP client" in record.message for record in caplog.records
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_returns_poller_and_running_task():
    provider = _make_provider()
    with patch(
        "plugins.memory.load_memory_provider", return_value=provider
    ):
        result = await build_and_start_sea_ticket_poller()

    assert result is not None
    poller, task = result
    assert isinstance(poller, SeaTicketPoller)
    assert isinstance(task, asyncio.Task)
    assert not task.done()

    # Provider was initialized with the gateway-poller session_id.
    provider.initialize.assert_called_once()
    init_call = provider.initialize.call_args
    assert init_call.kwargs.get("session_id") == "gateway-sea-ticket-poller"

    # Clean up: stop the task so the test event loop closes cleanly.
    poller.stop()
    await task


@pytest.mark.asyncio
async def test_custom_agent_loop_invoker_is_forwarded_into_poller():
    provider = _make_provider()

    captured: list = []

    async def custom_invoker(t, c, hb):
        captured.append((t, c, hb))
        return None  # never invoked in this test

    with patch(
        "plugins.memory.load_memory_provider", return_value=provider
    ):
        result = await build_and_start_sea_ticket_poller(
            agent_loop_invoker=custom_invoker
        )

    assert result is not None
    poller, task = result
    assert poller._agent_loop_invoker is custom_invoker

    poller.stop()
    await task


@pytest.mark.asyncio
async def test_provider_without_is_available_method_treated_as_available():
    """Test doubles / deprecated providers might not implement
    is_available(). The lifecycle helper defaults to "available"
    rather than skipping; production providers always implement it."""
    provider = MagicMock()
    # Explicitly remove is_available so getattr returns None.
    del provider.is_available
    provider.initialize = MagicMock(return_value=None)
    provider._connection.get_mcp_client = MagicMock(return_value=MagicMock())

    with patch(
        "plugins.memory.load_memory_provider", return_value=provider
    ):
        result = await build_and_start_sea_ticket_poller()

    assert result is not None
    poller, task = result
    poller.stop()
    await task
