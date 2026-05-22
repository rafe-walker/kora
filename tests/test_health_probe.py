"""KR-P2-L ST2 — tests for ``agent/health_probe.py``.

Covers:
  - Happy path emit invokes ``kora__append_event`` with the right
    payload shape (panel JSON + probe_cadence_seconds)
  - Returns the substrate-assigned event_id on success
  - Fail-soft: holder uninitialized, no memory_provider, no
    connection, no MCP client, no workspace_id — each returns None
    and logs WARN
  - Fail-soft on emit raise (substrate error) — returns None
  - Payload includes all 8 R4.1 §9.7 subsignals + probe_cadence_seconds
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent.health_probe import HEALTH_PROBE_EVENT_TYPE, emit_health_probe
from agent.health_rollup_holder import (
    ALL_SUBSIGNAL_NAMES,
    HealthRollupHolder,
    _reset_health_rollup_holder_for_tests,
    init_health_rollup_holder,
)


@pytest.fixture(autouse=True)
def _reset():
    _reset_health_rollup_holder_for_tests()
    yield
    _reset_health_rollup_holder_for_tests()


def _make_provider(
    *,
    workspace_id: str = "org_test",
    mcp_client_obj=None,
):
    mcp_client_obj = mcp_client_obj or SimpleNamespace(
        invoke=AsyncMock(return_value={"event_id": "evt-1"})
    )
    connection = SimpleNamespace(get_mcp_client=lambda: mcp_client_obj)
    return SimpleNamespace(
        _connection=connection,
        _resolve_workspace_id=lambda: workspace_id,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_emit_returns_event_id():
    init_health_rollup_holder()
    provider = _make_provider()
    event_id = await emit_health_probe(provider)
    assert event_id == "evt-1"


@pytest.mark.asyncio
async def test_emit_calls_kora_append_event_with_full_payload():
    holder = init_health_rollup_holder(probe_cadence_seconds=180)
    invoke_mock = AsyncMock(return_value={"event_id": "evt-2"})
    mcp = SimpleNamespace(invoke=invoke_mock)
    provider = _make_provider(mcp_client_obj=mcp)

    await emit_health_probe(provider, holder=holder)

    assert invoke_mock.await_count == 1
    args = invoke_mock.await_args.args
    assert args[0] == "kora__append_event"
    body = args[1]
    assert body["workspace_id"] == "org_test"
    assert body["event_type"] == HEALTH_PROBE_EVENT_TYPE
    payload = body["payload"]
    # Top-level keys
    assert set(payload.keys()) >= {
        "overall",
        "control_plane",
        "worker",
        "stopped_reason",
        "subsignals",
        "probe_cadence_seconds",
    }
    # All 8 subsignals
    assert set(payload["subsignals"].keys()) == set(ALL_SUBSIGNAL_NAMES)
    # Cadence reflects the holder's value
    assert payload["probe_cadence_seconds"] == 180


@pytest.mark.asyncio
async def test_explicit_holder_overrides_singleton():
    """Passing ``holder=`` explicitly bypasses the singleton lookup."""
    # Singleton holder with cadence 100
    init_health_rollup_holder(probe_cadence_seconds=100)
    # Explicit holder with cadence 999
    explicit_holder = HealthRollupHolder(probe_cadence_seconds=999)
    invoke_mock = AsyncMock(return_value={"event_id": "evt-3"})
    mcp = SimpleNamespace(invoke=invoke_mock)
    provider = _make_provider(mcp_client_obj=mcp)

    await emit_health_probe(provider, holder=explicit_holder)

    payload = invoke_mock.await_args.args[1]["payload"]
    assert payload["probe_cadence_seconds"] == 999


# ---------------------------------------------------------------------------
# Fail-soft branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_holder_not_initialized_returns_none(caplog):
    """No init_health_rollup_holder() call → singleton is None → skip."""
    with caplog.at_level(logging.WARNING, logger="agent.health_probe"):
        result = await emit_health_probe(_make_provider())
    assert result is None
    assert any(
        "HealthRollupHolder not initialized" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_no_memory_provider_returns_none():
    init_health_rollup_holder()
    result = await emit_health_probe(None)
    assert result is None


@pytest.mark.asyncio
async def test_provider_with_no_connection_returns_none(caplog):
    init_health_rollup_holder()
    provider = SimpleNamespace(_connection=None, _resolve_workspace_id=lambda: "org")
    with caplog.at_level(logging.WARNING, logger="agent.health_probe"):
        result = await emit_health_probe(provider)
    assert result is None
    assert any("no _connection" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_get_mcp_client_returns_none_returns_none(caplog):
    init_health_rollup_holder()
    connection = SimpleNamespace(get_mcp_client=lambda: None)
    provider = SimpleNamespace(
        _connection=connection, _resolve_workspace_id=lambda: "org"
    )
    with caplog.at_level(logging.WARNING, logger="agent.health_probe"):
        result = await emit_health_probe(provider)
    assert result is None
    assert any("no MCP client available" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_get_mcp_client_raises_returns_none(caplog):
    init_health_rollup_holder()

    def _boom():
        raise RuntimeError("transport boom")

    connection = SimpleNamespace(get_mcp_client=_boom)
    provider = SimpleNamespace(
        _connection=connection, _resolve_workspace_id=lambda: "org"
    )
    with caplog.at_level(logging.WARNING, logger="agent.health_probe"):
        result = await emit_health_probe(provider)
    assert result is None


@pytest.mark.asyncio
async def test_empty_workspace_id_returns_none(caplog):
    init_health_rollup_holder()
    provider = _make_provider(workspace_id="")
    with caplog.at_level(logging.WARNING, logger="agent.health_probe"):
        result = await emit_health_probe(provider)
    assert result is None
    assert any("no workspace_id" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_emit_kora_event_raises_returns_none(caplog):
    init_health_rollup_holder()
    invoke_mock = AsyncMock(side_effect=RuntimeError("substrate boom"))
    mcp = SimpleNamespace(invoke=invoke_mock)
    provider = _make_provider(mcp_client_obj=mcp)

    with caplog.at_level(logging.WARNING, logger="agent.health_probe"):
        result = await emit_health_probe(provider)
    assert result is None
    assert any("emit_kora_event raised" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_holder_current_raises_returns_none(caplog):
    holder = init_health_rollup_holder()
    with patch.object(holder, "current", side_effect=RuntimeError("collect boom")):
        with caplog.at_level(logging.WARNING, logger="agent.health_probe"):
            result = await emit_health_probe(_make_provider(), holder=holder)
    assert result is None
    assert any("holder.current() raised" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Event type literal pin
# ---------------------------------------------------------------------------


def test_event_type_literal_pinned():
    """The literal must match the substrate vocab in
    foundation/0159 — drift would surface as a CHECK violation."""
    assert HEALTH_PROBE_EVENT_TYPE == "kora.health.probe"


# ---------------------------------------------------------------------------
# Logging on success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_emit_logs_event_id_and_overall(caplog):
    holder = init_health_rollup_holder()
    invoke_mock = AsyncMock(return_value={"event_id": "evt-success"})
    mcp = SimpleNamespace(invoke=invoke_mock)
    provider = _make_provider(mcp_client_obj=mcp)

    with caplog.at_level(logging.INFO, logger="agent.health_probe"):
        event_id = await emit_health_probe(provider, holder=holder)
    assert event_id == "evt-success"
    info_messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "evt-success" in m and "overall=" in m for m in info_messages
    )
