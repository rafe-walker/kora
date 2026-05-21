"""Unit tests for ``agent/boot_gates_impl.py`` (KR-P2-H ST2).

Covers each of the 7 concrete gates: happy path + key failure modes.

Fake provider / connection mirror the shape used in
``tests/test_stop_kora_pre_flight.py`` — a tiny ``SimpleNamespace``
with ``submit_and_wait`` mocked to close unawaited coroutines (so
pytest's "coroutine was never awaited" warning stays quiet) and
return canned values.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, Callable, Optional
from unittest.mock import MagicMock, patch

import pytest

from agent.boot_gates import BootContext, GateClass, GateOutcome
from agent.boot_gates_impl import (
    CanonicalKoraActorGate,
    CharterCapabilityMatrixGate,
    ClaudeAuthGate,
    KR7BootSmokeGate,
    KronicleMCPReachableGate,
    KronicleRolePermsGate,
    WskTokenValidGate,
    build_default_gate_sequence,
)


# ---------------------------------------------------------------------------
# Fake provider / connection — minimal stand-ins for substrate I/O
# ---------------------------------------------------------------------------


def _close_and_return(returns: Any) -> Callable:
    """Build a submit_and_wait side_effect that closes coroutines + returns."""
    def _side_effect(coro, *, timeout=None):
        if hasattr(coro, "close"):
            coro.close()
        return returns
    return _side_effect


def _close_and_raise(exc: BaseException) -> Callable:
    def _side_effect(coro, *, timeout=None):
        if hasattr(coro, "close"):
            coro.close()
        raise exc
    return _side_effect


def _close_and_dispatch(
    routes: dict[str, Any],
) -> Callable:
    """Different return per coroutine name. Routes:
       coro.cr_code.co_name → return value (or raises if value is Exception).
    """
    def _side_effect(coro, *, timeout=None):
        name = getattr(getattr(coro, "cr_code", None), "co_name", None) or ""
        if hasattr(coro, "close"):
            coro.close()
        if name in routes:
            val = routes[name]
            if isinstance(val, BaseException):
                raise val
            return val
        return None
    return _side_effect


def _make_provider(
    *,
    workspace_id: Optional[str] = "ws-1",
    workspace_id_raises: bool = False,
    submit_side_effect: Optional[Callable] = None,
    get_mcp_client_returns: Any = None,
    get_mcp_client_raises: Optional[BaseException] = None,
    prefetch_raises: Optional[BaseException] = None,
    capability_cache_value: Any = "cap_row",
    constitution_cache_value: Any = ("rev-1", "hash-1"),
) -> SimpleNamespace:
    """Provider with the surface boot gates consult."""
    def _resolve_ws():
        if workspace_id_raises:
            raise RuntimeError("workspace_id resolution boom")
        return workspace_id

    def _prefetch(ws):
        if prefetch_raises is not None:
            raise prefetch_raises

    if get_mcp_client_raises is not None:
        def _get_mcp_client():
            raise get_mcp_client_raises
    else:
        def _get_mcp_client():
            return get_mcp_client_returns

    submit_and_wait = MagicMock(
        side_effect=submit_side_effect or _close_and_return(None)
    )

    connection = SimpleNamespace(
        get_pg_pool=MagicMock(return_value="fake-pool"),
        get_mcp_client=_get_mcp_client,
        submit_and_wait=submit_and_wait,
    )
    return SimpleNamespace(
        _resolve_workspace_id=_resolve_ws,
        _connection=connection,
        _prefetch_all=_prefetch,
        _capability_cache=SimpleNamespace(get=lambda ws: capability_cache_value),
        _constitution_cache=SimpleNamespace(get=lambda ws: constitution_cache_value),
    )


def _context(provider) -> BootContext:
    return BootContext(memory_provider=provider)


# ===========================================================================
# Gate 1 — ClaudeAuthGate
# ===========================================================================


@pytest.mark.asyncio
async def test_gate1_passes_when_anthropic_api_key_set_and_plausible():
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-" + "x" * 40}):
        result = await ClaudeAuthGate().run(_context(None))
    assert result.outcome is GateOutcome.PASS
    assert "set (length=" in result.detail


@pytest.mark.asyncio
async def test_gate1_fails_when_anthropic_api_key_unset():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        result = await ClaudeAuthGate().run(_context(None))
    assert result.outcome is GateOutcome.FAIL
    assert "unset or empty" in result.detail


@pytest.mark.asyncio
async def test_gate1_fails_when_anthropic_api_key_truncated():
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-x"}):  # < 20 chars
        result = await ClaudeAuthGate().run(_context(None))
    assert result.outcome is GateOutcome.FAIL
    assert "truncated" in result.detail


def test_gate1_class_attributes():
    assert ClaudeAuthGate.gate_id == "1_claude_auth"
    assert ClaudeAuthGate.gate_class is GateClass.TRANSIENT


# ===========================================================================
# Gate 4 — KronicleRolePermsGate
# ===========================================================================


@pytest.mark.asyncio
async def test_gate4_passes_when_read_ok_and_write_denied():
    provider = _make_provider(
        submit_side_effect=_close_and_dispatch({
            "_probe_actor_registry_select": True,
            "_probe_tickets_write_denied": True,  # deny observed
        })
    )
    result = await KronicleRolePermsGate().run(_context(provider))
    assert result.outcome is GateOutcome.PASS
    assert "correctly RLS-denied" in result.detail


@pytest.mark.asyncio
async def test_gate4_fails_when_actor_registry_read_empty():
    provider = _make_provider(
        submit_side_effect=_close_and_dispatch({
            "_probe_actor_registry_select": False,  # empty
        })
    )
    result = await KronicleRolePermsGate().run(_context(provider))
    assert result.outcome is GateOutcome.FAIL
    assert "actor_registry SELECT returned empty" in result.detail


@pytest.mark.asyncio
async def test_gate4_fails_when_actor_registry_read_raises():
    provider = _make_provider(
        submit_side_effect=_close_and_raise(RuntimeError("pool down")),
    )
    result = await KronicleRolePermsGate().run(_context(provider))
    assert result.outcome is GateOutcome.FAIL
    assert "read probe raised" in result.detail


@pytest.mark.asyncio
async def test_gate4_fails_when_write_succeeds_meaning_rls_missing():
    provider = _make_provider(
        submit_side_effect=_close_and_dispatch({
            "_probe_actor_registry_select": True,
            "_probe_tickets_write_denied": False,  # write succeeded — bad!
        })
    )
    result = await KronicleRolePermsGate().run(_context(provider))
    assert result.outcome is GateOutcome.FAIL
    assert "RLS deny is missing" in result.detail


@pytest.mark.asyncio
async def test_gate4_fails_when_no_memory_provider():
    result = await KronicleRolePermsGate().run(_context(None))
    assert result.outcome is GateOutcome.FAIL
    assert "memory_provider is not set" in result.detail


# ===========================================================================
# Gate 5 — KronicleMCPReachableGate
# ===========================================================================


@pytest.mark.asyncio
async def test_gate5_passes_when_mcp_client_opens():
    provider = _make_provider(get_mcp_client_returns="fake-mcp-client")
    result = await KronicleMCPReachableGate().run(_context(provider))
    assert result.outcome is GateOutcome.PASS
    assert "opened successfully" in result.detail


@pytest.mark.asyncio
async def test_gate5_fails_when_mcp_client_raises():
    provider = _make_provider(
        get_mcp_client_raises=ConnectionRefusedError("dispatch tier down")
    )
    result = await KronicleMCPReachableGate().run(_context(provider))
    assert result.outcome is GateOutcome.FAIL
    assert "MCP client open raised" in result.detail


@pytest.mark.asyncio
async def test_gate5_fails_when_mcp_client_returns_none():
    provider = _make_provider(get_mcp_client_returns=None)
    result = await KronicleMCPReachableGate().run(_context(provider))
    assert result.outcome is GateOutcome.FAIL
    assert "returned None" in result.detail


# ===========================================================================
# Gate 6 — WskTokenValidGate
# ===========================================================================


@pytest.mark.asyncio
async def test_gate6_passes_when_token_set_and_capability_call_succeeds():
    mock_client = MagicMock()
    mock_client.invoke = MagicMock()  # returns a coroutine when called
    provider = _make_provider(
        get_mcp_client_returns=mock_client,
        submit_side_effect=_close_and_return({"cap_a": True, "cap_b": False}),
    )
    with patch.dict(os.environ, {"KORA_SERVICE_TOKEN": "wsk_test_token"}):
        result = await WskTokenValidGate().run(_context(provider))
    assert result.outcome is GateOutcome.PASS
    assert "authenticated" in result.detail


@pytest.mark.asyncio
async def test_gate6_fails_when_token_unset():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("KORA_SERVICE_TOKEN", None)
        result = await WskTokenValidGate().run(_context(None))
    assert result.outcome is GateOutcome.FAIL
    assert "KORA_SERVICE_TOKEN env var is unset" in result.detail


@pytest.mark.asyncio
async def test_gate6_fails_when_capability_call_raises():
    mock_client = MagicMock()
    mock_client.invoke = MagicMock()
    provider = _make_provider(
        get_mcp_client_returns=mock_client,
        submit_side_effect=_close_and_raise(RuntimeError("401 Unauthorized")),
    )
    with patch.dict(os.environ, {"KORA_SERVICE_TOKEN": "wsk_test_token"}):
        result = await WskTokenValidGate().run(_context(provider))
    assert result.outcome is GateOutcome.FAIL
    assert "401 Unauthorized" in result.detail


@pytest.mark.asyncio
async def test_gate6_fails_when_response_shape_unexpected():
    mock_client = MagicMock()
    mock_client.invoke = MagicMock()
    provider = _make_provider(
        get_mcp_client_returns=mock_client,
        submit_side_effect=_close_and_return("not a dict"),
    )
    with patch.dict(os.environ, {"KORA_SERVICE_TOKEN": "wsk_test_token"}):
        result = await WskTokenValidGate().run(_context(provider))
    assert result.outcome is GateOutcome.FAIL
    assert "unexpected shape" in result.detail


# ===========================================================================
# Gate 7 — CanonicalKoraActorGate (INVARIANT)
# ===========================================================================


@pytest.mark.asyncio
async def test_gate7_passes_and_caches_actor_uuid_on_context():
    actor_uuid = "33333333-3333-3333-3333-333333333333"
    provider = _make_provider(
        submit_side_effect=_close_and_return(actor_uuid),
    )
    ctx = _context(provider)
    result = await CanonicalKoraActorGate().run(ctx)
    assert result.outcome is GateOutcome.PASS
    assert actor_uuid in result.detail
    # Cached for downstream gates
    assert ctx.kora_actor_uuid == actor_uuid
    assert ctx.workspace_id == "ws-1"


@pytest.mark.asyncio
async def test_gate7_fails_when_no_kora_actor_row():
    provider = _make_provider(
        submit_side_effect=_close_and_return(None),  # no row
    )
    result = await CanonicalKoraActorGate().run(_context(provider))
    assert result.outcome is GateOutcome.FAIL
    assert "no actor_kind='kora' row" in result.detail
    assert "Plan 01 / migration 0076" in result.detail


@pytest.mark.asyncio
async def test_gate7_fails_when_workspace_unresolved():
    provider = _make_provider(workspace_id=None)
    result = await CanonicalKoraActorGate().run(_context(provider))
    assert result.outcome is GateOutcome.FAIL
    assert "workspace_id is None/empty" in result.detail


@pytest.mark.asyncio
async def test_gate7_fails_when_workspace_resolution_raises():
    provider = _make_provider(workspace_id_raises=True)
    result = await CanonicalKoraActorGate().run(_context(provider))
    assert result.outcome is GateOutcome.FAIL
    assert "workspace_id resolution raised" in result.detail


def test_gate7_is_invariant():
    assert CanonicalKoraActorGate.gate_class is GateClass.INVARIANT


# ===========================================================================
# Gate 8 — CharterCapabilityMatrixGate
# ===========================================================================


@pytest.mark.asyncio
async def test_gate8_passes_after_successful_prefetch():
    provider = _make_provider(
        capability_cache_value="cap_row",
        constitution_cache_value=("rev-x", "hash-x"),
    )
    ctx = _context(provider)
    ctx.workspace_id = "ws-1"
    result = await CharterCapabilityMatrixGate().run(ctx)
    assert result.outcome is GateOutcome.PASS
    assert "constitution present: True" in result.detail


@pytest.mark.asyncio
async def test_gate8_passes_when_constitution_absent_for_fresh_workspace():
    """Fresh workspaces have no authored Constitution — that's OK as
    long as capability matrix loaded."""
    provider = _make_provider(
        capability_cache_value="cap_row",
        constitution_cache_value=None,  # no Constitution yet
    )
    ctx = _context(provider)
    ctx.workspace_id = "ws-fresh"
    result = await CharterCapabilityMatrixGate().run(ctx)
    assert result.outcome is GateOutcome.PASS
    assert "constitution present: False" in result.detail


@pytest.mark.asyncio
async def test_gate8_fails_when_prefetch_raises():
    provider = _make_provider(
        prefetch_raises=RuntimeError("substrate timeout"),
    )
    ctx = _context(provider)
    ctx.workspace_id = "ws-1"
    result = await CharterCapabilityMatrixGate().run(ctx)
    assert result.outcome is GateOutcome.FAIL
    assert "_prefetch_all raised" in result.detail


@pytest.mark.asyncio
async def test_gate8_fails_when_capability_cache_empty_after_prefetch():
    provider = _make_provider(
        capability_cache_value=None,  # cache empty after prefetch — bad
    )
    ctx = _context(provider)
    ctx.workspace_id = "ws-1"
    result = await CharterCapabilityMatrixGate().run(ctx)
    assert result.outcome is GateOutcome.FAIL
    assert "capability matrix cache empty" in result.detail


@pytest.mark.asyncio
async def test_gate8_resolves_workspace_id_when_context_missing_it():
    """If Gate 7 didn't run (e.g. sequence rearranged in a test),
    Gate 8 falls back to resolving workspace_id from the provider."""
    provider = _make_provider(
        capability_cache_value="cap_row",
        constitution_cache_value=("r", "h"),
    )
    ctx = _context(provider)
    # Don't set ctx.workspace_id explicitly — let Gate 8 resolve.
    result = await CharterCapabilityMatrixGate().run(ctx)
    assert result.outcome is GateOutcome.PASS


# ===========================================================================
# Gate 10 — KR7BootSmokeGate (INVARIANT)
# ===========================================================================


@pytest.mark.asyncio
async def test_gate10_passes_when_kr7_returns_capability_data():
    mock_client = MagicMock()
    mock_client.invoke = MagicMock()
    provider = _make_provider(
        get_mcp_client_returns=mock_client,
        submit_side_effect=_close_and_return({"cap_a": True, "cap_b": False}),
    )
    result = await KR7BootSmokeGate().run(_context(provider))
    assert result.outcome is GateOutcome.PASS
    assert "dispatch tier attributed" in result.detail


@pytest.mark.asyncio
async def test_gate10_fails_when_kr7_returns_empty_dict():
    mock_client = MagicMock()
    mock_client.invoke = MagicMock()
    provider = _make_provider(
        get_mcp_client_returns=mock_client,
        submit_side_effect=_close_and_return({}),
    )
    result = await KR7BootSmokeGate().run(_context(provider))
    assert result.outcome is GateOutcome.FAIL
    assert "unexpected/empty shape" in result.detail


@pytest.mark.asyncio
async def test_gate10_fails_when_kr7_raises():
    mock_client = MagicMock()
    mock_client.invoke = MagicMock()
    provider = _make_provider(
        get_mcp_client_returns=mock_client,
        submit_side_effect=_close_and_raise(
            RuntimeError("dispatch attribution failed")
        ),
    )
    result = await KR7BootSmokeGate().run(_context(provider))
    assert result.outcome is GateOutcome.FAIL
    assert "KR-7 smoke invoke raised" in result.detail


def test_gate10_is_invariant():
    assert KR7BootSmokeGate.gate_class is GateClass.INVARIANT


# ===========================================================================
# build_default_gate_sequence — order + cardinality
# ===========================================================================


def test_default_gate_sequence_has_gates_in_r41_order():
    """KR-P2-M ST1 inserted gate 3 between gate 1 and gate 4."""
    seq = build_default_gate_sequence()
    assert [g.gate_id for g in seq] == [
        "1_claude_auth",
        "3_substrate_contract_version",  # KR-P2-M ST1
        "4_kora_runtime_role_perms",
        "5_kronicle_mcp_reachable",
        "6_wsk_token_valid",
        "7_canonical_kora_actor",
        "8_charter_capability_matrix_load",
        "10_kr7_boot_smoke",
    ]


def test_default_gate_sequence_class_mix():
    """Gates 3 + 7 + 10 are INVARIANT per R4.1 §9.2 / §9.8; rest TRANSIENT."""
    seq = build_default_gate_sequence()
    by_class = {g.gate_id: g.gate_class for g in seq}
    assert by_class["1_claude_auth"] is GateClass.TRANSIENT
    assert by_class["3_substrate_contract_version"] is GateClass.INVARIANT
    assert by_class["4_kora_runtime_role_perms"] is GateClass.TRANSIENT
    assert by_class["5_kronicle_mcp_reachable"] is GateClass.TRANSIENT
    assert by_class["6_wsk_token_valid"] is GateClass.TRANSIENT
    assert by_class["7_canonical_kora_actor"] is GateClass.INVARIANT
    assert by_class["8_charter_capability_matrix_load"] is GateClass.TRANSIENT
    assert by_class["10_kr7_boot_smoke"] is GateClass.INVARIANT
