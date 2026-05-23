"""Tests for ``kora_cli.listeners.reasoning_engine_listener`` — ST2.

Covers:
  - current_reasoning_engine() returns None pre-startup
  - startup with injected engine → singleton set
  - shutdown → singleton cleared
  - shutdown closes the underlying engine's HTTP client (best-effort)
  - Production startup failure (missing creds) re-raises so the
    daemon coordinator aborts boot (fail-CLOSED)
  - Registered in LISTENER_REGISTRY under "reasoning_engine"
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kora_cli.listeners import reasoning_engine_listener as rel
from kora_cli.listeners.reasoning_engine_listener import (
    ReasoningEngineListener,
    _clear_singleton,
    _set_singleton,
    current_reasoning_engine,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    _clear_singleton()
    yield
    _clear_singleton()


@pytest.fixture(autouse=True)
def _clear_credential_envs(monkeypatch):
    """Ensure neither credential is set unless a test sets it
    explicitly — keeps the fail-CLOSED path testable."""
    monkeypatch.delenv("KORA_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)


@pytest.mark.asyncio
async def test_current_engine_is_none_before_startup():
    assert current_reasoning_engine() is None


@pytest.mark.asyncio
async def test_startup_with_injected_engine_sets_singleton():
    fake_engine = MagicMock()
    fake_engine.close = AsyncMock()
    listener = ReasoningEngineListener(engine=fake_engine)
    await listener.startup()
    assert current_reasoning_engine() is fake_engine


@pytest.mark.asyncio
async def test_shutdown_clears_singleton_and_closes_engine():
    fake_engine = MagicMock()
    fake_engine.close = AsyncMock()
    listener = ReasoningEngineListener(engine=fake_engine)
    await listener.startup()
    assert current_reasoning_engine() is fake_engine

    await listener.shutdown()
    assert current_reasoning_engine() is None
    fake_engine.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_safe_when_close_raises():
    """An engine whose close() throws shouldn't propagate; we still
    clear the singleton + return cleanly."""
    fake_engine = MagicMock()
    fake_engine.close = AsyncMock(side_effect=RuntimeError("close boom"))
    listener = ReasoningEngineListener(engine=fake_engine)
    await listener.startup()
    await listener.shutdown()  # must not raise
    assert current_reasoning_engine() is None


@pytest.mark.asyncio
async def test_shutdown_safe_without_startup():
    """Shutdown called without prior startup → no-op, no crash."""
    listener = ReasoningEngineListener()
    await listener.shutdown()


@pytest.mark.asyncio
async def test_production_startup_reraises_on_misconfig():
    """No injected engine + both credential envs unset → production
    startup tries to construct AnthropicReasoningEngine, which
    raises ReasoningEngineNotConfigured. The listener re-raises so
    the daemon coordinator aborts boot (fail-CLOSED per spec).
    """
    from kora_cli.reasoning.anthropic_engine import (
        ReasoningEngineNotConfigured,
    )

    listener = ReasoningEngineListener()  # no engine injected
    with pytest.raises(ReasoningEngineNotConfigured):
        await listener.startup()


def test_registered_in_listener_registry():
    """Module-import side effect: register_daemon_listener('reasoning_engine', _factory)."""
    from kora_cli.daemon import LISTENER_REGISTRY

    names = [name for name, _factory in LISTENER_REGISTRY]
    assert "reasoning_engine" in names


@pytest.mark.asyncio
async def test_set_and_clear_singleton_helpers():
    """The private helpers are used by the listener — verify they
    do what they say."""
    fake = MagicMock()
    _set_singleton(fake)
    assert current_reasoning_engine() is fake
    _clear_singleton()
    assert current_reasoning_engine() is None
