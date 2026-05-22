"""KR-P2-L ST1 — IsoKronMCPClient health-rollup timestamp tracking.

Pins that ``last_invoke_at`` + ``last_successful_append_event_at`` +
``last_successful_refresh_claim_at`` are updated on successful
invokes and NOT on failed ones. These are the read sources for the
``dispatch_reachable`` / ``last_successful_write`` / ``last_heartbeat``
health subsignals.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from plugins.memory.isokron.config import IsoKronProviderConfig
from plugins.memory.isokron.mcp_client import (
    IsoKronMCPClient,
    IsoKronMCPInvocationError,
)


def _make_client_with_session(structured_content: Any = None) -> IsoKronMCPClient:
    config = IsoKronProviderConfig(
        isokron_dsn="postgres://test/test",
        mcp_endpoint="stdio:///usr/bin/echo",
    )
    client = IsoKronMCPClient(config)
    session = SimpleNamespace()
    result = SimpleNamespace(
        isError=False,
        structuredContent=structured_content or {"event_id": "evt-1"},
        content=[],
    )
    session.call_tool = AsyncMock(return_value=result)
    client._session = session
    return client


@pytest.mark.asyncio
async def test_last_invoke_at_unset_before_first_call():
    client = _make_client_with_session()
    assert client.last_invoke_at is None
    assert client.last_successful_append_event_at is None
    assert client.last_successful_refresh_claim_at is None


@pytest.mark.asyncio
async def test_last_invoke_at_set_on_any_successful_invoke():
    client = _make_client_with_session()
    before = datetime.now(timezone.utc)
    await client.invoke("kora__claim_sea_ticket", {"workspace_id": "org"})
    after = datetime.now(timezone.utc)
    assert client.last_invoke_at is not None
    assert before <= client.last_invoke_at <= after
    # Non-append-event tool should not bump the append-event timestamp
    assert client.last_successful_append_event_at is None
    # Non-refresh-claim tool should not bump the refresh-claim timestamp
    assert client.last_successful_refresh_claim_at is None


@pytest.mark.asyncio
async def test_append_event_invoke_bumps_both_invoke_and_append_event_at():
    client = _make_client_with_session()
    await client.invoke("kora__append_event", {"workspace_id": "org"})
    assert client.last_invoke_at is not None
    assert client.last_successful_append_event_at is not None
    # Both set in the same call → very close in time
    assert (
        abs(
            (
                client.last_invoke_at
                - client.last_successful_append_event_at
            ).total_seconds()
        )
        < 0.001
    )


@pytest.mark.asyncio
async def test_refresh_claim_invoke_bumps_both_invoke_and_refresh_claim_at():
    client = _make_client_with_session()
    await client.invoke("kora__refresh_claim", {"workspace_id": "org"})
    assert client.last_invoke_at is not None
    assert client.last_successful_refresh_claim_at is not None
    assert client.last_successful_append_event_at is None


@pytest.mark.asyncio
async def test_failed_invoke_does_not_bump_timestamps():
    """A substrate-side error should NOT corrupt the dispatch_reachable
    signal — only successful invokes count."""
    config = IsoKronProviderConfig(
        isokron_dsn="postgres://test/test",
        mcp_endpoint="stdio:///usr/bin/echo",
    )
    client = IsoKronMCPClient(config)
    session = SimpleNamespace()
    # Result with isError=True → invoke raises
    err_result = SimpleNamespace(
        isError=True,
        structuredContent=None,
        content=[SimpleNamespace(text="boom")],
    )
    session.call_tool = AsyncMock(return_value=err_result)
    client._session = session

    with pytest.raises(IsoKronMCPInvocationError):
        await client.invoke("kora__append_event", {"workspace_id": "org"})

    assert client.last_invoke_at is None
    assert client.last_successful_append_event_at is None


@pytest.mark.asyncio
async def test_invoke_raise_does_not_bump_timestamps():
    """If the SDK raises (transport-level error), timestamps stay None."""
    config = IsoKronProviderConfig(
        isokron_dsn="postgres://test/test",
        mcp_endpoint="stdio:///usr/bin/echo",
    )
    client = IsoKronMCPClient(config)
    session = SimpleNamespace()
    session.call_tool = AsyncMock(side_effect=RuntimeError("transport boom"))
    client._session = session

    with patch(
        "plugins.memory.isokron.mcp_client._import_mcp_helpers",
        return_value={
            "sanitize_error": lambda x: x,
            "exc_str": lambda exc: repr(exc),
        },
    ):
        with pytest.raises(IsoKronMCPInvocationError):
            await client.invoke("kora__claim_sea_ticket", {})

    assert client.last_invoke_at is None


@pytest.mark.asyncio
async def test_repeated_successful_invokes_advance_timestamp():
    client = _make_client_with_session()
    await client.invoke("kora__append_event", {"workspace_id": "org"})
    t1 = client.last_successful_append_event_at
    await client.invoke("kora__append_event", {"workspace_id": "org"})
    t2 = client.last_successful_append_event_at
    assert t1 is not None and t2 is not None
    assert t2 >= t1  # monotonically forward
