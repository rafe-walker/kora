"""Endpoint tests for KR-P2-I-integration ST5 — /api/operational-state
flipped from stub to live OperationalStateHolder read.

Covers:
  - When the holder is not initialized: stub-shape + ``error`` field +
    ``stub: True``
  - When the holder is initialized: live state from get_holder() +
    no ``stub`` flag + ``transition_history`` is the in-memory ring +
    ``valid_next_states`` derived from transitions_from
  - Ring buffer: ``transition_to`` appends; ``history(limit=N)`` honors limit;
    ring is bounded at 10
  - ``TransitionRecord`` shape matches the endpoint dict shape
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agent.operational_state import (
    ClaimPermission,
    DegradationReason,
    OperationalState,
    PrimaryState,
)
from agent.operational_state_holder import (
    OperationalStateHolder,
    TransitionRecord,
    _reset_holder_for_tests,
    init_holder,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    _reset_holder_for_tests()
    yield
    _reset_holder_for_tests()


# ---------------------------------------------------------------------------
# Ring buffer mechanics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transition_appends_to_history_ring():
    holder = OperationalStateHolder(
        OperationalState(
            primary_state=PrimaryState.BOOTING,
            claim_permission=ClaimPermission.NONE,
        )
    )
    await holder.transition_to(
        PrimaryState.READY,
        trigger="all §9.2 gates pass",
        new_claim_permission=ClaimPermission.NORMAL,
    )
    await holder.transition_to(
        PrimaryState.ACTIVE, trigger="claim acquired"
    )

    history = holder.history()
    assert len(history) == 2
    assert history[0]["from_state"] == "booting"
    assert history[0]["to_state"] == "ready"
    assert history[0]["trigger"] == "all §9.2 gates pass"
    assert history[1]["from_state"] == "ready"
    assert history[1]["to_state"] == "active"
    assert history[1]["trigger"] == "claim acquired"


@pytest.mark.asyncio
async def test_history_ring_is_bounded_at_ten():
    """The ring should drop oldest entries when more than 10 transitions
    happen. Important so the admin-panel payload never balloons."""
    holder = OperationalStateHolder(
        OperationalState(
            primary_state=PrimaryState.READY,
            claim_permission=ClaimPermission.NORMAL,
        )
    )
    # 12 same-state transitions (degradation toggle) — table check
    # bypass keeps each valid.
    for i in range(12):
        await holder.transition_to(
            PrimaryState.READY,
            trigger=f"transition #{i}",
        )

    history = holder.history()
    assert len(history) == 10
    # Oldest two dropped; newest is "transition #11".
    assert history[0]["trigger"] == "transition #2"
    assert history[-1]["trigger"] == "transition #11"


@pytest.mark.asyncio
async def test_history_limit_clamps_to_request_size():
    holder = OperationalStateHolder(
        OperationalState(
            primary_state=PrimaryState.READY,
            claim_permission=ClaimPermission.NORMAL,
        )
    )
    for i in range(5):
        await holder.transition_to(
            PrimaryState.READY, trigger=f"t{i}"
        )

    assert len(holder.history(limit=3)) == 3
    assert holder.history(limit=3)[-1]["trigger"] == "t4"
    # Asking for more than we have returns all entries.
    assert len(holder.history(limit=100)) == 5


def test_transition_record_to_dict_shape():
    record = TransitionRecord(
        timestamp="2026-05-21T17:00:00Z",
        from_state="booting",
        to_state="ready",
        trigger="all §9.2 gates pass",
    )
    assert record.to_dict() == {
        "timestamp": "2026-05-21T17:00:00Z",
        "from_state": "booting",
        "to_state": "ready",
        "trigger": "all §9.2 gates pass",
    }


@pytest.mark.asyncio
async def test_history_timestamp_is_iso8601_utc():
    holder = OperationalStateHolder(
        OperationalState(
            primary_state=PrimaryState.BOOTING,
            claim_permission=ClaimPermission.NONE,
        )
    )
    await holder.transition_to(
        PrimaryState.READY, trigger="all §9.2 gates pass"
    )
    ts = holder.history()[0]["timestamp"]
    # Round-trippable as a UTC datetime; ends in "Z".
    parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    assert parsed.tzinfo is timezone.utc


# ---------------------------------------------------------------------------
# Endpoint flip — uninitialized holder branch
# ---------------------------------------------------------------------------


async def _call_endpoint():
    """Import + invoke get_operational_state directly so we don't need
    to spin up FastAPI's full stack for a JSON-payload contract test."""
    from kora_cli.web_server import get_operational_state

    return await get_operational_state()


@pytest.mark.asyncio
async def test_endpoint_returns_stub_shape_with_error_when_holder_uninitialized():
    # _reset_holder_for_tests has cleared the singleton via fixture.
    result = await _call_endpoint()

    assert result["stub"] is True
    assert result["error"] == "OperationalStateHolder not yet initialized"
    assert result["primary_state"] == "booting"
    assert result["claim_permission"] == "none"
    assert result["degradation_reasons"] == []
    assert result["is_degraded"] is False
    assert result["transition_history"] == []
    assert result["valid_next_states"] == []


# ---------------------------------------------------------------------------
# Endpoint flip — initialized holder branch (live state)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_returns_live_state_when_holder_initialized():
    holder = init_holder(
        OperationalState(
            primary_state=PrimaryState.BOOTING,
            claim_permission=ClaimPermission.NONE,
        )
    )
    await holder.transition_to(
        PrimaryState.READY,
        trigger="all §9.2 gates pass",
        new_claim_permission=ClaimPermission.NORMAL,
    )

    result = await _call_endpoint()

    # The stub flag must be ABSENT on the live path — CC#2's panel
    # renders the stub banner only when the field is present + truthy.
    assert "stub" not in result
    assert "error" not in result
    assert result["primary_state"] == "ready"
    assert result["claim_permission"] == "normal"
    assert result["degradation_reasons"] == []
    assert result["is_degraded"] is False

    # transition_history is the in-memory ring; expect the BOOTING →
    # READY entry we just made.
    assert len(result["transition_history"]) == 1
    assert result["transition_history"][0]["from_state"] == "booting"
    assert result["transition_history"][0]["to_state"] == "ready"

    # valid_next_states derived from transitions_from(READY).
    next_pairs = {(t["to_state"], t["trigger"]) for t in result["valid_next_states"]}
    assert ("active", "claim acquired") in next_pairs
    assert ("paused", "STOP-KORA L1–3, cost 100%, operator") in next_pairs
    assert ("stopped", "STOP-KORA L4/L5") in next_pairs


@pytest.mark.asyncio
async def test_endpoint_renders_degradation_reasons_sorted():
    holder = init_holder(
        OperationalState(
            primary_state=PrimaryState.READY,
            claim_permission=ClaimPermission.CRITICAL_ONLY,
            degradation_reasons=frozenset(
                {
                    DegradationReason.DISPATCH,
                    DegradationReason.AUTH,
                    DegradationReason.COST,
                }
            ),
        )
    )
    result = await _call_endpoint()
    # Sorted alphabetically — cockpit diffs rely on stable order.
    assert result["degradation_reasons"] == ["auth", "cost", "dispatch"]
    assert result["is_degraded"] is True
    assert result["claim_permission"] == "critical_only"


@pytest.mark.asyncio
async def test_endpoint_payload_keys_match_pre_flip_shape_modulo_stub():
    """The CC#2 admin panel was shipped against the v1 stub. The flip
    must preserve every key the stub returned — only the ``stub`` flag
    is dropped on the success path (and ``error`` is added on the
    uninitialized path)."""
    holder = init_holder(
        OperationalState(primary_state=PrimaryState.READY)
    )
    result = await _call_endpoint()

    expected_keys = {
        "primary_state",
        "claim_permission",
        "degradation_reasons",
        "is_degraded",
        "transition_history",
        "valid_next_states",
    }
    assert expected_keys.issubset(result.keys())
