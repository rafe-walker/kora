"""Unit tests for ``agent/operational_state_emit.py`` (KR-P2-I-integration ST2).

Covers:
  - Payload shape matches the spec (from/to/claim_permission/sorted reasons/trigger)
  - Per-trigger extra-literal selection (boot.ready / boot.failed / paused.cost_limit)
  - Trigger substring matching is lenient (handles varied callsite wording)
  - Always-emit generic + conditionally-emit extra
  - Fail-LOUD on missing provider / connection / workspace_id
  - Fail-LOUD on substrate-side raise (no silent swallow)
  - make_emit_listener returns a listener with the right signature
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.operational_state import (
    ClaimPermission,
    DegradationReason,
    OperationalState,
    PrimaryState,
)
from agent.operational_state_emit import (
    BOOT_FAILED_EVENT,
    BOOT_READY_EVENT,
    GENERIC_TRANSITION_EVENT,
    OperationalStateEmitError,
    PAUSED_COST_LIMIT_EVENT,
    _build_payload,
    _select_extra_literal,
    emit_state_transition,
    make_emit_listener,
)


# ---------------------------------------------------------------------------
# _build_payload — shape check (no I/O)
# ---------------------------------------------------------------------------


def test_payload_shape_matches_spec():
    old = OperationalState(
        primary_state=PrimaryState.BOOTING,
        claim_permission=ClaimPermission.NONE,
    )
    new = OperationalState(
        primary_state=PrimaryState.READY,
        claim_permission=ClaimPermission.NORMAL,
        degradation_reasons=frozenset(
            {DegradationReason.AUTH, DegradationReason.DISPATCH}
        ),
    )
    payload = _build_payload(old, new, "all §9.2 gates pass")
    assert payload == {
        "from_primary_state": "booting",
        "to_primary_state": "ready",
        "claim_permission": "normal",
        # Sorted alphabetically — cockpit relies on stable ordering for diffs.
        "degradation_reasons": ["auth", "dispatch"],
        "trigger": "all §9.2 gates pass",
    }


def test_payload_reflects_NEW_claim_permission_and_reasons():
    """If a degradation was cleared in this transition, payload must
    show the post-transition (clean) set — not the pre-transition set."""
    old = OperationalState(
        primary_state=PrimaryState.READY,
        degradation_reasons=frozenset({DegradationReason.COST}),
        claim_permission=ClaimPermission.CRITICAL_ONLY,
    )
    new = OperationalState(
        primary_state=PrimaryState.READY,
        claim_permission=ClaimPermission.NORMAL,
    )
    payload = _build_payload(old, new, "monthly credit refresh confirmed")
    assert payload["degradation_reasons"] == []
    assert payload["claim_permission"] == "normal"


# ---------------------------------------------------------------------------
# _select_extra_literal — per-trigger informational events
# ---------------------------------------------------------------------------


def _state(ps: PrimaryState, **kw) -> OperationalState:
    return OperationalState(primary_state=ps, **kw)


def test_booting_to_ready_with_gates_passed_emits_boot_ready():
    extra = _select_extra_literal(
        _state(PrimaryState.BOOTING),
        _state(PrimaryState.READY),
        "all §9.2 gates pass",
    )
    assert extra == BOOT_READY_EVENT


def test_booting_to_ready_with_other_trigger_emits_no_extra():
    extra = _select_extra_literal(
        _state(PrimaryState.BOOTING),
        _state(PrimaryState.READY),
        "some other trigger",
    )
    assert extra is None


def test_booting_to_stopped_with_invariant_gate_failure_emits_boot_failed():
    extra = _select_extra_literal(
        _state(PrimaryState.BOOTING),
        _state(PrimaryState.STOPPED),
        "invariant gate failure, or retry budget exhausted",
    )
    assert extra == BOOT_FAILED_EVENT


def test_booting_to_stopped_via_operator_stop_emits_no_extra():
    """STOP-KORA L4/L5 from BOOTING reaches the same arrow as invariant
    gate failure but is operator action, not a boot failure — no
    boot.failed event."""
    extra = _select_extra_literal(
        _state(PrimaryState.BOOTING),
        _state(PrimaryState.STOPPED),
        "STOP-KORA L4/L5",
    )
    assert extra is None


def test_any_to_paused_with_cost_100pct_emits_paused_cost_limit():
    for from_ps in (
        PrimaryState.READY,
        PrimaryState.ACTIVE,
        PrimaryState.BOOTING,
    ):
        extra = _select_extra_literal(
            _state(from_ps),
            _state(PrimaryState.PAUSED),
            "STOP-KORA L1–3, cost 100%, operator",
        )
        assert extra == PAUSED_COST_LIMIT_EVENT


def test_operator_pause_emits_no_extra_literal():
    """Operator-only pause without cost-100% wording → no per-trigger
    literal. Per substrate-team's design — only generic + the three
    informational literals that exist in vocab."""
    extra = _select_extra_literal(
        _state(PrimaryState.READY),
        _state(PrimaryState.PAUSED),
        "operator clears via kora_control",
    )
    assert extra is None


def test_substrate_pause_emits_no_extra_literal():
    extra = _select_extra_literal(
        _state(PrimaryState.BOOTING),
        _state(PrimaryState.PAUSED),
        "gate 3b epoch mismatch (§9.8)",
    )
    assert extra is None


def test_trigger_substring_match_is_lenient():
    """Caller may pass slightly varied wording — match the substring."""
    for trigger_variant in (
        "cost 100%",
        "cost 100% — auto-pause",
        "STOP-KORA L2: cost 100% triggered budget watcher",
    ):
        extra = _select_extra_literal(
            _state(PrimaryState.ACTIVE),
            _state(PrimaryState.PAUSED),
            trigger_variant,
        )
        assert extra == PAUSED_COST_LIMIT_EVENT, (
            f"variant {trigger_variant!r} should match"
        )


# ---------------------------------------------------------------------------
# emit_state_transition — fail-LOUD preflight checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_raises_when_provider_is_none():
    with pytest.raises(OperationalStateEmitError) as exc_info:
        await emit_state_transition(
            None,
            _state(PrimaryState.BOOTING),
            _state(PrimaryState.READY),
            "all §9.2 gates pass",
        )
    assert exc_info.value.event_type == GENERIC_TRANSITION_EVENT
    assert "provider is None" in exc_info.value.cause


@pytest.mark.asyncio
async def test_emit_raises_when_connection_missing():
    provider = MagicMock()
    provider._connection = None
    with pytest.raises(OperationalStateEmitError) as exc_info:
        await emit_state_transition(
            provider,
            _state(PrimaryState.BOOTING),
            _state(PrimaryState.READY),
            "all §9.2 gates pass",
        )
    assert "connection not initialized" in exc_info.value.cause


@pytest.mark.asyncio
async def test_emit_raises_when_workspace_id_resolution_fails():
    provider = MagicMock()
    provider._connection = MagicMock()
    provider._resolve_workspace_id.side_effect = RuntimeError("resolve boom")
    with pytest.raises(OperationalStateEmitError) as exc_info:
        await emit_state_transition(
            provider,
            _state(PrimaryState.BOOTING),
            _state(PrimaryState.READY),
            "all §9.2 gates pass",
        )
    assert "workspace_id resolution raised" in exc_info.value.cause


@pytest.mark.asyncio
async def test_emit_raises_when_workspace_id_empty():
    provider = MagicMock()
    provider._connection = MagicMock()
    provider._resolve_workspace_id.return_value = ""
    with pytest.raises(OperationalStateEmitError) as exc_info:
        await emit_state_transition(
            provider,
            _state(PrimaryState.BOOTING),
            _state(PrimaryState.READY),
            "all §9.2 gates pass",
        )
    assert "workspace_id is None/empty" in exc_info.value.cause


# ---------------------------------------------------------------------------
# emit_state_transition — happy path + substrate-failure path
# ---------------------------------------------------------------------------


def _make_provider_with_capturing_mcp() -> tuple[Any, list[dict[str, Any]]]:
    """Return (provider, captured_invocations).

    Stubs the IsoKron connection so we can capture the (event_type,
    payload) pairs handed to kora__append_event without spinning up
    the real Sea MCP transport.
    """
    captured: list[dict[str, Any]] = []

    async def fake_invoke(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        captured.append({"tool_name": tool_name, "args": args})
        return {"event_id": f"evt-{len(captured)}"}

    mcp_client = MagicMock()
    mcp_client.invoke = fake_invoke

    # Build a worker-thread IO-loop stub that mimics production
    # IsoKronConnection._submit_async: runs the coro on a fresh
    # thread + loop, returns a concurrent.futures.Future. See the
    # tests/test_sea_ticket_poller.py:_FakeConnection note for the
    # root-cause + fix-shape on the prior get_event_loop pattern.
    import concurrent.futures
    import threading

    def submit_coro(coro):
        fut: concurrent.futures.Future = concurrent.futures.Future()

        def _runner():
            new_loop = asyncio.new_event_loop()
            try:
                fut.set_result(new_loop.run_until_complete(coro))
            except BaseException as exc:
                fut.set_exception(exc)
            finally:
                new_loop.close()

        threading.Thread(target=_runner, daemon=True).start()
        return fut

    connection = MagicMock()
    connection.get_mcp_client.return_value = mcp_client
    connection._submit_async.side_effect = submit_coro

    provider = MagicMock()
    provider._connection = connection
    provider._resolve_workspace_id.return_value = "org_test_workspace"
    return provider, captured


@pytest.mark.asyncio
async def test_emit_writes_generic_event_with_payload():
    provider, captured = _make_provider_with_capturing_mcp()

    await emit_state_transition(
        provider,
        OperationalState(primary_state=PrimaryState.READY),
        OperationalState(
            primary_state=PrimaryState.ACTIVE,
            claim_permission=ClaimPermission.NORMAL,
        ),
        "claim acquired",
    )

    # ACTIVE has no per-trigger informational literal — exactly one call.
    assert len(captured) == 1
    invocation = captured[0]
    assert invocation["tool_name"] == "kora__append_event"
    assert invocation["args"]["workspace_id"] == "org_test_workspace"
    assert invocation["args"]["event_type"] == GENERIC_TRANSITION_EVENT
    assert invocation["args"]["payload"]["from_primary_state"] == "ready"
    assert invocation["args"]["payload"]["to_primary_state"] == "active"
    assert invocation["args"]["payload"]["trigger"] == "claim acquired"


@pytest.mark.asyncio
async def test_emit_writes_generic_plus_boot_ready_on_booting_to_ready():
    provider, captured = _make_provider_with_capturing_mcp()

    await emit_state_transition(
        provider,
        OperationalState(primary_state=PrimaryState.BOOTING),
        OperationalState(
            primary_state=PrimaryState.READY,
            claim_permission=ClaimPermission.NORMAL,
        ),
        "all §9.2 gates pass",
    )

    assert len(captured) == 2
    assert captured[0]["args"]["event_type"] == GENERIC_TRANSITION_EVENT
    assert captured[1]["args"]["event_type"] == BOOT_READY_EVENT
    # Both events carry the same payload — cockpit can correlate by
    # (workspace_id, payload) if needed.
    assert captured[0]["args"]["payload"] == captured[1]["args"]["payload"]


@pytest.mark.asyncio
async def test_emit_raises_on_substrate_failure():
    """Substrate raise during kora__append_event must bubble as
    OperationalStateEmitError — no silent swallow."""

    async def failing_invoke(_tool_name, _args):
        raise RuntimeError("Sea MCP unavailable")

    mcp_client = MagicMock()
    mcp_client.invoke = failing_invoke

    import concurrent.futures
    import threading

    def submit_coro(coro):
        # See tests/test_sea_ticket_poller.py:_FakeConnection for the
        # root-cause + fix-shape note. Same bug, same fix.
        fut: concurrent.futures.Future = concurrent.futures.Future()

        def _runner():
            new_loop = asyncio.new_event_loop()
            try:
                fut.set_result(new_loop.run_until_complete(coro))
            except BaseException as exc:
                fut.set_exception(exc)
            finally:
                new_loop.close()

        threading.Thread(target=_runner, daemon=True).start()
        return fut

    connection = MagicMock()
    connection.get_mcp_client.return_value = mcp_client
    connection._submit_async.side_effect = submit_coro

    provider = MagicMock()
    provider._connection = connection
    provider._resolve_workspace_id.return_value = "org_test"

    with pytest.raises(OperationalStateEmitError) as exc_info:
        await emit_state_transition(
            provider,
            OperationalState(primary_state=PrimaryState.BOOTING),
            OperationalState(primary_state=PrimaryState.READY),
            "all §9.2 gates pass",
        )
    assert exc_info.value.event_type == GENERIC_TRANSITION_EVENT
    assert "Sea MCP unavailable" in exc_info.value.cause


# ---------------------------------------------------------------------------
# make_emit_listener — listener-factory shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_make_emit_listener_returns_callable_listener():
    provider, captured = _make_provider_with_capturing_mcp()
    listener = make_emit_listener(provider)

    await listener(
        OperationalState(primary_state=PrimaryState.READY),
        OperationalState(
            primary_state=PrimaryState.ACTIVE,
            claim_permission=ClaimPermission.NORMAL,
        ),
        "claim acquired",
    )

    assert len(captured) == 1
    assert captured[0]["args"]["event_type"] == GENERIC_TRANSITION_EVENT
