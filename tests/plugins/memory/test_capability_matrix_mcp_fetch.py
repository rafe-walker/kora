"""KR-7b — Capability matrix MCP fetch + fallback tests.

Covers the swap from hand-mirrored C2 dict to MCP-fetched authoritative
data. The hand-mirrored 49-entry default stays as a dev/test fallback
(parity test in ``test_capability_matrix_parity.py`` still guards it
against TS-source drift); the populate function replaces those entries
with the substrate's authoritative data at provider initialize.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from plugins.memory.isokron.capability_matrix_mirror import (
    ACTOR_CAPABILITY_MATRIX_KORA_COLUMN,
    CAPABILITY_MATRIX_MCP_TOOL,
    populate_capability_matrix_from_mcp,
)


# ---------------------------------------------------------------------------
# Auto-restore fixture (mirrors test_provider_end_to_end.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def restore_capability_matrix():
    """Every test in this file mutates the module-level dict; snapshot
    + restore to keep parallel xdist workers + sequential tests clean."""
    snapshot = dict(ACTOR_CAPABILITY_MATRIX_KORA_COLUMN)
    yield
    ACTOR_CAPABILITY_MATRIX_KORA_COLUMN.clear()
    ACTOR_CAPABILITY_MATRIX_KORA_COLUMN.update(snapshot)


# ---------------------------------------------------------------------------
# Fake MCP client
# ---------------------------------------------------------------------------


class _FakeMcpClient:
    """Records invokes + returns either a happy capability_matrix or an error."""

    def __init__(self, *, result=None, raises=None):
        self.invoke_calls: list[tuple[str, dict]] = []
        self._result = result
        self._raises = raises

    async def invoke(self, tool_name: str, args: dict) -> Any:
        self.invoke_calls.append((tool_name, dict(args)))
        if self._raises is not None:
            raise self._raises
        return self._result


# ---------------------------------------------------------------------------
# populate_capability_matrix_from_mcp — happy + shape failures
# ---------------------------------------------------------------------------


def test_populate_invokes_canonical_tool_with_empty_args():
    fetched = {"cap_sea_create": True, "cap_test_other": False}
    client = _FakeMcpClient(result={"capability_matrix": fetched})

    written = asyncio.run(populate_capability_matrix_from_mcp(client))

    assert written == 2
    assert client.invoke_calls == [(CAPABILITY_MATRIX_MCP_TOOL, {})]


def test_populate_replaces_module_level_dict_in_place():
    """KR-7b: the populate mutates the dict in-place so callers that
    imported by reference (capability_check.py via the mirror import)
    see fresh data without re-importing."""
    fetched = {
        "cap_write_agent_scratchpad": True,
        "cap_override_security_or_policy_verdict": False,
        "cap_brand_new_from_k13": True,  # forward-stability
    }
    client = _FakeMcpClient(result={"capability_matrix": fetched})
    asyncio.run(populate_capability_matrix_from_mcp(client))

    assert dict(ACTOR_CAPABILITY_MATRIX_KORA_COLUMN) == fetched
    # Identity preserved — the dict object is the same one
    # capability_check.py imports.
    from plugins.memory.isokron import capability_matrix_mirror as mod

    assert mod.ACTOR_CAPABILITY_MATRIX_KORA_COLUMN is ACTOR_CAPABILITY_MATRIX_KORA_COLUMN


def test_populate_raises_on_none_mcp_client():
    with pytest.raises(ValueError) as excinfo:
        asyncio.run(populate_capability_matrix_from_mcp(None))
    assert "mcp_client is required" in str(excinfo.value)


def test_populate_raises_on_missing_capability_matrix_key():
    client = _FakeMcpClient(result={"oops_no_matrix": {}})
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(populate_capability_matrix_from_mcp(client))
    assert "unexpected shape" in str(excinfo.value)
    assert CAPABILITY_MATRIX_MCP_TOOL in str(excinfo.value)


def test_populate_raises_on_non_dict_response():
    client = _FakeMcpClient(result=["not", "a", "dict"])
    with pytest.raises(RuntimeError):
        asyncio.run(populate_capability_matrix_from_mcp(client))


def test_populate_raises_on_non_bool_capability_value():
    """Defensive against substrate-side regression: non-bool values
    would silently break ``actor_has_capability`` lookups."""
    client = _FakeMcpClient(
        result={"capability_matrix": {"cap_sea_create": "true-as-string"}}
    )
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(populate_capability_matrix_from_mcp(client))
    assert "non-bool" in str(excinfo.value)


def test_populate_raises_on_non_str_capability_key():
    client = _FakeMcpClient(
        result={"capability_matrix": {42: True}}
    )
    with pytest.raises(RuntimeError):
        asyncio.run(populate_capability_matrix_from_mcp(client))


def test_populate_propagates_underlying_mcp_invocation_error():
    """Substrate-side errors surface unchanged — caller (provider.initialize)
    catches and falls back to the hand-mirrored data."""

    class FakeError(Exception):
        pass

    client = _FakeMcpClient(raises=FakeError("tenant not found"))
    with pytest.raises(FakeError):
        asyncio.run(populate_capability_matrix_from_mcp(client))


# ---------------------------------------------------------------------------
# Provider initialize — fallback semantics
# ---------------------------------------------------------------------------


def test_provider_initialize_logs_capability_matrix_populated_on_success(caplog):
    """Happy path: MCP returns a valid matrix; logger.info fires from the
    mirror module's populate call."""
    from plugins.memory.isokron.provider import IsoKronMemoryProvider

    provider = IsoKronMemoryProvider(
        config={
            "isokron_dsn": "postgres://kora:secret@localhost:5432/isokron",
            "mcp_endpoint": "stdio://node ./sea-mcp-server.js",
            "default_workspace_id": "org_test",
        }
    )

    happy_matrix = {"cap_sea_create": True, "cap_test_one": False}
    fake_client = _FakeMcpClient(result={"capability_matrix": happy_matrix})

    class _Conn:
        def start(self):
            pass

        def close(self):
            pass

        def get_mcp_client(self):
            return fake_client

        def submit_and_wait(self, coro, *, timeout=10.0):
            return asyncio.run(coro)

    setattr(provider, "_connection", _Conn())

    with caplog.at_level(
        logging.INFO, logger="plugins.memory.isokron.capability_matrix_mirror"
    ):
        provider.initialize(session_id="s1")

    info_lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "[kora.capability_matrix] populated from kora__read_kora_capability_row"
        in m
        for m in info_lines
    )
    assert dict(ACTOR_CAPABILITY_MATRIX_KORA_COLUMN) == happy_matrix


def test_provider_initialize_logs_fallback_on_fetch_failure(caplog):
    """Substrate unreachable: hand-mirrored fallback stays + WARNING fires.
    Sessions still run."""
    from plugins.memory.isokron.provider import IsoKronMemoryProvider

    provider = IsoKronMemoryProvider(
        config={
            "isokron_dsn": "postgres://kora:secret@localhost:5432/isokron",
            "mcp_endpoint": "stdio://node ./sea-mcp-server.js",
            "default_workspace_id": "org_test",
        }
    )

    fake_client = _FakeMcpClient(raises=RuntimeError("substrate-down"))

    class _Conn:
        def start(self):
            pass

        def close(self):
            pass

        def get_mcp_client(self):
            return fake_client

        def submit_and_wait(self, coro, *, timeout=10.0):
            return asyncio.run(coro)

    setattr(provider, "_connection", _Conn())

    with caplog.at_level(
        logging.WARNING, logger="plugins.memory.isokron.provider"
    ):
        provider.initialize(session_id="s1")

    warn_lines = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
    ]
    assert any("[kora.capability_matrix.fallback]" in m for m in warn_lines)
    # Hand-mirrored fallback intact — at least 40 entries remain.
    assert len(ACTOR_CAPABILITY_MATRIX_KORA_COLUMN) >= 40
    assert "cap_sea_create" in ACTOR_CAPABILITY_MATRIX_KORA_COLUMN


def test_provider_initialize_logs_fallback_when_mcp_client_unavailable(caplog):
    """``get_mcp_client`` itself raises (no MCP transport):
    fallback path with a distinct log line."""
    from plugins.memory.isokron.provider import IsoKronMemoryProvider

    provider = IsoKronMemoryProvider(
        config={
            "isokron_dsn": "postgres://kora:secret@localhost:5432/isokron",
            "mcp_endpoint": "stdio://node ./sea-mcp-server.js",
            "default_workspace_id": "org_test",
        }
    )

    class _Conn:
        def start(self):
            pass

        def close(self):
            pass

        def get_mcp_client(self):
            raise RuntimeError("transport-not-started")

        def submit_and_wait(self, coro, *, timeout=10.0):  # pragma: no cover
            return asyncio.run(coro)

    setattr(provider, "_connection", _Conn())

    with caplog.at_level(
        logging.WARNING, logger="plugins.memory.isokron.provider"
    ):
        provider.initialize(session_id="s1")

    warn_lines = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "[kora.capability_matrix.fallback] could not reach MCP" in m
        for m in warn_lines
    )
    # Fallback intact.
    assert "cap_sea_create" in ACTOR_CAPABILITY_MATRIX_KORA_COLUMN
