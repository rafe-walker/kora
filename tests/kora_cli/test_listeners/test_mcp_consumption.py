"""Tests for ``kora_cli.listeners.mcp_consumption`` (KR-MCP-CONSUMPTION ST1).

Covers:
  - Listener starts cleanly with empty catalog (no endpoints
    configured)
  - Listener starts with default github + cloudflare catalog;
    no auth env set → pool constructed but no connections open
  - ``current_pool()`` returns the live pool after startup
  - ``current_pool()`` returns None before startup AND after
    shutdown
  - ``pool.close_all()`` is called on listener shutdown
  - Lazy startup contract: ``has_open_connection()`` returns False
    for every endpoint immediately after startup
  - Factory tuple shape: returns ``(startup, shutdown,
    shutdown_timeout)`` matching the DEFAULT_SHUTDOWN_TIMEOUT
  - Registry-side: ``register_daemon_listener("mcp_consumption", ...)``
    is invoked at module-import time (LISTENER_REGISTRY contains
    the entry)
  - Coordinator-level lifecycle: full startup → shutdown cycle
    via DaemonCoordinator with mcp_consumption registered alongside
    other listeners; LIFO shutdown order honored
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kora_cli import daemon as daemon_mod
from kora_cli.daemon import DEFAULT_SHUTDOWN_TIMEOUT, DaemonCoordinator
from kora_cli.listeners.mcp_consumption import (
    MCPConsumptionListener,
    _clear_singleton,
    _factory,
    current_pool,
)
from kora_mcp.catalog import DEFAULT_CATALOG
from kora_mcp.pool import MCPClientPool
from kora_mcp.registry import MCPEndpointConfig, MCPRegistryConfig


@pytest.fixture(autouse=True)
def _reset_singleton():
    _clear_singleton()
    yield
    _clear_singleton()


# ---------------------------------------------------------------------------
# Listener lifecycle — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_with_empty_catalog():
    """Empty catalog (operator overrode defaults to nothing) is a
    valid state. The pool constructs cleanly with zero endpoints."""
    empty_registry = MCPRegistryConfig(endpoints=[])
    with patch(
        "kora_cli.listeners.mcp_consumption.load_effective_catalog",
        return_value=empty_registry,
    ):
        listener = MCPConsumptionListener()
        await listener.startup()
        assert listener.pool is not None
        assert listener.pool.endpoint_names() == []
        await listener.shutdown()


@pytest.mark.asyncio
async def test_startup_with_default_catalog():
    """Default github + cloudflare catalog. Pool constructs with
    both endpoints; no connections open."""
    default_registry = MCPRegistryConfig(endpoints=list(DEFAULT_CATALOG))
    with patch(
        "kora_cli.listeners.mcp_consumption.load_effective_catalog",
        return_value=default_registry,
    ):
        listener = MCPConsumptionListener()
        await listener.startup()
        assert listener.pool is not None
        assert set(listener.pool.endpoint_names()) == {"github", "cloudflare"}
        await listener.shutdown()


# ---------------------------------------------------------------------------
# current_pool() accessor (mirrors current_coordinator() pattern)
# ---------------------------------------------------------------------------


def test_current_pool_returns_none_before_startup():
    """Singleton stays None until any listener instance starts."""
    assert current_pool() is None


@pytest.mark.asyncio
async def test_current_pool_returns_live_pool_after_startup():
    default_registry = MCPRegistryConfig(endpoints=list(DEFAULT_CATALOG))
    with patch(
        "kora_cli.listeners.mcp_consumption.load_effective_catalog",
        return_value=default_registry,
    ):
        listener = MCPConsumptionListener()
        await listener.startup()
        assert current_pool() is listener.pool
        assert isinstance(current_pool(), MCPClientPool)
        await listener.shutdown()


@pytest.mark.asyncio
async def test_current_pool_returns_none_after_shutdown():
    default_registry = MCPRegistryConfig(endpoints=list(DEFAULT_CATALOG))
    with patch(
        "kora_cli.listeners.mcp_consumption.load_effective_catalog",
        return_value=default_registry,
    ):
        listener = MCPConsumptionListener()
        await listener.startup()
        assert current_pool() is not None
        await listener.shutdown()
        assert current_pool() is None


# ---------------------------------------------------------------------------
# close_all called on shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_all_called_on_shutdown():
    default_registry = MCPRegistryConfig(endpoints=list(DEFAULT_CATALOG))
    with patch(
        "kora_cli.listeners.mcp_consumption.load_effective_catalog",
        return_value=default_registry,
    ):
        listener = MCPConsumptionListener()
        await listener.startup()
        pool = listener.pool
        assert pool is not None
        with patch.object(
            pool, "close_all", wraps=pool.close_all
        ) as close_spy:
            await listener.shutdown()
            close_spy.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_clears_singleton_even_if_close_raises():
    """Defensive: if close_all raises, current_pool() must still
    clear back to None — otherwise a stale pool reference lingers
    across daemon restarts."""
    default_registry = MCPRegistryConfig(endpoints=list(DEFAULT_CATALOG))
    with patch(
        "kora_cli.listeners.mcp_consumption.load_effective_catalog",
        return_value=default_registry,
    ):
        listener = MCPConsumptionListener()
        await listener.startup()
        assert current_pool() is not None
        pool = listener.pool
        assert pool is not None
        with patch.object(
            pool, "close_all", side_effect=RuntimeError("close boom")
        ):
            with pytest.raises(RuntimeError, match="close boom"):
                await listener.shutdown()
        assert current_pool() is None


@pytest.mark.asyncio
async def test_shutdown_is_noop_when_startup_did_not_run():
    listener = MCPConsumptionListener()
    # No startup; shutdown should be a clean no-op
    await listener.shutdown()
    assert current_pool() is None


# ---------------------------------------------------------------------------
# Lazy-startup invariant (per §4 Q2 default — no eager connects)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lazy_startup_does_not_open_connections():
    """The KR-MCP-1 pool design pins lazy connect; this listener
    must preserve that contract — no transport opens during
    coordinator startup, otherwise daemon boot blocks on slow
    remote MCPs."""
    default_registry = MCPRegistryConfig(endpoints=list(DEFAULT_CATALOG))
    with patch(
        "kora_cli.listeners.mcp_consumption.load_effective_catalog",
        return_value=default_registry,
    ):
        listener = MCPConsumptionListener()
        await listener.startup()
        assert listener.pool is not None
        for prefix in listener.pool.endpoint_names():
            assert listener.pool.has_open_connection(prefix) is False, (
                f"endpoint {prefix!r} has an open connection after lazy "
                f"startup — eager connect is a §4 Q2 violation"
            )
        await listener.shutdown()


@pytest.mark.asyncio
async def test_lazy_startup_with_single_endpoint():
    """Operator catalog with one ad-hoc endpoint also constructs
    cleanly + remains unopened."""
    registry = MCPRegistryConfig(
        endpoints=[
            MCPEndpointConfig(
                name="custom",
                transport="stdio",
                endpoint="echo custom",
                auth_token_env=None,
            )
        ]
    )
    with patch(
        "kora_cli.listeners.mcp_consumption.load_effective_catalog",
        return_value=registry,
    ):
        listener = MCPConsumptionListener()
        await listener.startup()
        assert listener.pool is not None
        assert listener.pool.endpoint_names() == ["custom"]
        assert listener.pool.has_open_connection("custom") is False
        await listener.shutdown()


# ---------------------------------------------------------------------------
# Factory + registration
# ---------------------------------------------------------------------------


def test_factory_returns_three_tuple_with_default_shutdown_timeout():
    startup, shutdown, timeout = _factory()
    assert callable(startup)
    assert callable(shutdown)
    assert timeout == DEFAULT_SHUTDOWN_TIMEOUT
    assert timeout == 10.0  # spec contract: 10s shutdown timeout


def test_listener_registered_in_global_registry():
    """Import-time side effect: register_daemon_listener fires when
    kora_cli.listeners.mcp_consumption imports. The
    kora_cli.listeners package __init__ imports this module so the
    registry entry is always present once the daemon package is
    loaded."""
    # Force import so registration fires (already imported but
    # safe in this test path).
    from kora_cli.listeners import mcp_consumption  # noqa: F401

    registered_names = {name for name, _factory in daemon_mod.LISTENER_REGISTRY}
    assert "mcp_consumption" in registered_names


# ---------------------------------------------------------------------------
# Coordinator-level lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coordinator_lifo_shutdown_with_mcp_consumption():
    """When the coordinator registers mcp_consumption alongside
    other listeners, the LIFO shutdown order is honored — pool
    closes BEFORE any listener registered earlier."""
    import asyncio

    default_registry = MCPRegistryConfig(endpoints=list(DEFAULT_CATALOG))
    shutdown_order: list[str] = []

    async def _a_startup():
        pass

    async def _a_shutdown():
        shutdown_order.append("a")

    async def _b_startup():
        pass

    async def _b_shutdown():
        shutdown_order.append("b")

    consumption_listener = MCPConsumptionListener()

    async def _consumption_shutdown_wrapper():
        await consumption_listener.shutdown()
        shutdown_order.append("mcp_consumption")

    with patch(
        "kora_cli.listeners.mcp_consumption.load_effective_catalog",
        return_value=default_registry,
    ):
        coordinator = DaemonCoordinator()
        coordinator.register_listener(
            "a", _a_startup, _a_shutdown, shutdown_timeout=1.0
        )
        coordinator.register_listener(
            "mcp_consumption",
            consumption_listener.startup,
            _consumption_shutdown_wrapper,
            shutdown_timeout=DEFAULT_SHUTDOWN_TIMEOUT,
        )
        coordinator.register_listener(
            "b", _b_startup, _b_shutdown, shutdown_timeout=1.0
        )

        async def trigger():
            await asyncio.sleep(0)
            # Pool was set on startup; verify before requesting shutdown
            assert current_pool() is not None
            coordinator.request_shutdown("test-lifo-mcp-consumption")

        _, exit_code = await asyncio.gather(trigger(), coordinator.run())

    assert exit_code == 0
    # LIFO: b registered last → stops first; mcp_consumption next; a last
    assert shutdown_order == ["b", "mcp_consumption", "a"]
    assert current_pool() is None
