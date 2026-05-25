"""Unit tests for ``plugins/memory/isokron/sea_ticket_resolution.py`` (KR-P2-E ST4).

Covers:
  - classify_failure: sentinel exception types → expected ClassifiedResolution
  - classify_failure: per-tool table lookup with the right backoff offset
  - classify_failure: conservative default for unknown failures
  - emit_sea_ticket_resolved: payload shape (sea_ticket_id, workspace_id,
    resolution, resolution_summary, optional model_tier_used + offset)
  - emit_sea_ticket_resolved: returns event_id on success; None on error
    without raising
  - Verbatim event-type literal matches the substrate vocab
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from plugins.memory.isokron.sea_ticket_poller import (
    SeaTicket,
    SeaTicketResolution,
)
from plugins.memory.isokron.sea_ticket_resolution import (
    AgentLoopTimeoutError,
    ConstitutionPreScreenFailError,
    ConstitutionPreScreenInconclusiveError,
    NetworkDispatchFailureError,
    RESOLVED_EVENT,
    classify_failure,
    emit_sea_ticket_resolved,
)


def _ticket() -> SeaTicket:
    return SeaTicket(
        ticket_id="11111111-1111-1111-1111-111111111111",
        workspace_id="org_test",
        ticket_title="T",
        ticket_objective="O",
        sea_status="assigned",
        sea_priority="medium",
        sea_idea_kind="task",
        sea_captured_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
        created_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# classify_failure — sentinel exception types
# ---------------------------------------------------------------------------


def test_constitution_pre_screen_fail_maps_to_failed_terminal():
    out = classify_failure(ConstitutionPreScreenFailError("policy denied"))
    assert out.resolution is SeaTicketResolution.FAILED_TERMINAL
    assert out.next_eligible_offset_seconds is None
    assert "policy denies" in out.reason


def test_constitution_pre_screen_inconclusive_maps_to_failed_retryable_5min():
    out = classify_failure(
        ConstitutionPreScreenInconclusiveError("undecided")
    )
    assert out.resolution is SeaTicketResolution.FAILED_RETRYABLE
    assert out.next_eligible_offset_seconds == 5 * 60


def test_agent_loop_timeout_maps_to_failed_retryable_10min():
    out = classify_failure(AgentLoopTimeoutError("budget exceeded"))
    assert out.resolution is SeaTicketResolution.FAILED_RETRYABLE
    assert out.next_eligible_offset_seconds == 10 * 60


def test_network_dispatch_maps_to_released_1min():
    out = classify_failure(NetworkDispatchFailureError("Sea MCP down"))
    assert out.resolution is SeaTicketResolution.RELEASED
    assert out.next_eligible_offset_seconds == 1 * 60


# ---------------------------------------------------------------------------
# classify_failure — per-tool table
# ---------------------------------------------------------------------------


def test_kora_claim_sea_ticket_tool_failure_is_failed_terminal():
    out = classify_failure(
        RuntimeError("permission denied"),
        tool_name="kora__claim_sea_ticket",
    )
    assert out.resolution is SeaTicketResolution.FAILED_TERMINAL
    # No retry offset on terminal.
    assert out.next_eligible_offset_seconds is None


def test_kora_append_event_tool_failure_is_failed_retryable_with_offset():
    out = classify_failure(
        RuntimeError("network blip"), tool_name="kora__append_event"
    )
    assert out.resolution is SeaTicketResolution.FAILED_RETRYABLE
    assert out.next_eligible_offset_seconds == 1 * 60


# ---------------------------------------------------------------------------
# classify_failure — conservative default
# ---------------------------------------------------------------------------


def test_unknown_failure_defaults_to_failed_retryable_with_dispatch_offset():
    out = classify_failure(ValueError("???"))
    assert out.resolution is SeaTicketResolution.FAILED_RETRYABLE
    assert out.next_eligible_offset_seconds == 1 * 60
    assert "ValueError" in out.reason


def test_unknown_tool_name_falls_back_to_default():
    out = classify_failure(
        RuntimeError("???"), tool_name="kora__some_future_tool"
    )
    assert out.resolution is SeaTicketResolution.FAILED_RETRYABLE


# ---------------------------------------------------------------------------
# Event-type literal verbatim
# ---------------------------------------------------------------------------


def test_resolved_event_literal_matches_substrate_vocab():
    """foundation/0159 line 295 ships exactly 'kora.sea_ticket.resolved'.
    If substrate-team renames it, this test fails fast and the
    payload-emit + cockpit consumers all need to coordinate."""
    assert RESOLVED_EVENT == "kora.sea_ticket.resolved"


# ---------------------------------------------------------------------------
# emit_sea_ticket_resolved
# ---------------------------------------------------------------------------


def _make_capturing_provider() -> tuple[Any, list[dict[str, Any]]]:
    captured: list[dict[str, Any]] = []

    async def fake_invoke(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        captured.append({"tool_name": tool_name, "args": args})
        return {"event_id": f"evt-{len(captured)}"}

    mcp_client = MagicMock()
    mcp_client.invoke = fake_invoke

    def submit_coro(coro):
        # See tests/test_sea_ticket_poller.py:_FakeConnection for the
        # root-cause + fix-shape note. Same bug, same fix: run the
        # coro on a fresh worker thread + loop so the test's
        # caller-loop and the coro's runner-loop are independent.
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
    return provider, captured


@pytest.mark.asyncio
async def test_emit_sends_payload_with_minimal_fields():
    provider, captured = _make_capturing_provider()

    event_id = await emit_sea_ticket_resolved(
        provider=provider,
        ticket=_ticket(),
        resolution=SeaTicketResolution.COMPLETED,
        resolution_summary="ok",
    )

    assert event_id == "evt-1"
    assert len(captured) == 1
    assert captured[0]["tool_name"] == "kora__append_event"
    invoke_args = captured[0]["args"]
    assert invoke_args["workspace_id"] == "org_test"
    assert invoke_args["event_type"] == "kora.sea_ticket.resolved"
    payload = invoke_args["payload"]
    assert payload["sea_ticket_id"] == "11111111-1111-1111-1111-111111111111"
    assert payload["workspace_id"] == "org_test"
    assert payload["resolution"] == "completed"
    assert payload["resolution_summary"] == "ok"
    # Optional fields omitted when not provided.
    assert "model_tier_used" not in payload
    assert "next_eligible_offset_seconds" not in payload


@pytest.mark.asyncio
async def test_emit_includes_optional_fields_when_provided():
    provider, captured = _make_capturing_provider()

    await emit_sea_ticket_resolved(
        provider=provider,
        ticket=_ticket(),
        resolution=SeaTicketResolution.FAILED_RETRYABLE,
        resolution_summary="timeout",
        model_tier_used="tier_2",
        next_eligible_offset_seconds=600,
    )

    payload = captured[0]["args"]["payload"]
    assert payload["model_tier_used"] == "tier_2"
    assert payload["next_eligible_offset_seconds"] == 600


@pytest.mark.asyncio
async def test_emit_returns_none_when_provider_has_no_connection():
    provider = MagicMock()
    provider._connection = None
    event_id = await emit_sea_ticket_resolved(
        provider=provider,
        ticket=_ticket(),
        resolution=SeaTicketResolution.COMPLETED,
        resolution_summary="ok",
    )
    assert event_id is None


@pytest.mark.asyncio
async def test_emit_returns_none_on_substrate_error_without_raising():
    """Best-effort: emit failures must not block the release call.
    Returns None + logs; caller proceeds to release."""

    async def failing_invoke(_t, _a):
        raise RuntimeError("Sea MCP unreachable")

    mcp_client = MagicMock()
    mcp_client.invoke = failing_invoke

    def submit_coro(coro):
        # See tests/test_sea_ticket_poller.py:_FakeConnection for the
        # root-cause + fix-shape note. Same bug, same fix: worker
        # thread + loop. Exception propagation goes through the
        # Future per concurrent.futures semantics.
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

    event_id = await emit_sea_ticket_resolved(
        provider=provider,
        ticket=_ticket(),
        resolution=SeaTicketResolution.COMPLETED,
        resolution_summary="ok",
    )
    assert event_id is None
