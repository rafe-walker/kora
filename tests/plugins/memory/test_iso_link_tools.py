"""KR-3 ST2 — ``iso_link_*`` typed-edge tool family tests."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, List, Optional

import pytest

from plugins.memory.isokron.relationlink import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    MAX_TRAVERSE_DEPTH,
    PLATFORM_WIDE_LINK_TYPES,
    SEA_IDEA_LINK_TYPES,
    V1_LINK_TYPES,
    RelationLinkWriteNotAvailableError,
    create_relationlink,
    read_relationlink_for_node,
    traverse_relationlink,
)
from plugins.memory.isokron.tools import (
    ISO_LINK_TOOL_SCHEMAS,
    ISO_TYPED_GRAPH_TOOL_SCHEMAS,
    handle_iso_link_tool_call,
)
from plugins.memory.isokron.tools.iso_link import (
    ISO_LINK_CREATE_SCHEMA,
    ISO_LINK_LIST_FOR_NODE_SCHEMA,
    ISO_LINK_TRAVERSE_SCHEMA,
)


WORKSPACE_ID = "org_test_iso_link"


# ---------------------------------------------------------------------------
# Fake pool — records queries + returns canned rows by SQL keyword
# ---------------------------------------------------------------------------


class _FakeConnection:
    def __init__(self, *, list_rows=None, traverse_rows=None):
        self.list_rows = list_rows or []
        self.traverse_rows = traverse_rows or []
        self.calls: list[tuple] = []

    async def fetch(self, sql: str, *args):
        self.calls.append((sql, args))
        if "FROM relationlink" in sql and "WITH RECURSIVE" not in sql:
            return self.list_rows
        if "WITH RECURSIVE walk" in sql:
            return self.traverse_rows
        return []


class _FakeAcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquireCtx(self._conn)


class _FakeProviderConnection:
    def __init__(self, conn: _FakeConnection):
        self._conn = conn
        self._pool = _FakePool(conn)
        self.submitted: list = []

    def get_pg_pool(self):
        return self._pool

    def submit_and_wait(self, coro, *, timeout: float = 10.0):
        self.submitted.append(coro)
        return asyncio.run(coro)


def _make_provider(*, conn: Optional[_FakeConnection] = None):
    from plugins.memory.isokron.provider import IsoKronMemoryProvider

    provider = IsoKronMemoryProvider(
        config={
            "isokron_dsn": "postgres://kora:secret@localhost:5432/isokron",
            "mcp_endpoint": "stdio://node ./sea-mcp-server.js",
            "default_workspace_id": WORKSPACE_ID,
        }
    )
    fake_conn = _FakeProviderConnection(conn or _FakeConnection())
    setattr(provider, "_connection", fake_conn)
    return provider, fake_conn


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


def test_iso_link_schemas_well_formed():
    assert len(ISO_LINK_TOOL_SCHEMAS) == 3
    names = {s["name"] for s in ISO_LINK_TOOL_SCHEMAS}
    assert names == {
        "iso_link_create",
        "iso_link_traverse",
        "iso_link_list_for_node",
    }
    for schema in ISO_LINK_TOOL_SCHEMAS:
        assert {"name", "description", "parameters"} <= schema.keys()


def test_v1_link_types_match_adr_0033():
    """11 sea/idea + 10 platform-wide = 21."""
    assert len(SEA_IDEA_LINK_TYPES) == 11
    assert len(PLATFORM_WIDE_LINK_TYPES) == 10
    assert len(V1_LINK_TYPES) == 21
    # No overlap.
    assert set(SEA_IDEA_LINK_TYPES) & set(PLATFORM_WIDE_LINK_TYPES) == set()


def test_iso_link_create_schema_link_type_enum_matches_v1_vocab():
    enum = ISO_LINK_CREATE_SCHEMA["parameters"]["properties"]["link_type"]["enum"]
    assert tuple(enum) == V1_LINK_TYPES


def test_iso_link_traverse_schema_caps_max_depth_at_3():
    max_depth = ISO_LINK_TRAVERSE_SCHEMA["parameters"]["properties"]["max_depth"]
    assert max_depth["maximum"] == 3
    assert MAX_TRAVERSE_DEPTH == 3


def test_iso_link_list_for_node_schema_caps_limit_at_100():
    limit = ISO_LINK_LIST_FOR_NODE_SCHEMA["parameters"]["properties"]["limit"]
    assert limit["maximum"] == 100
    assert MAX_LIST_LIMIT == 100


def test_combined_typed_graph_surface_is_seven_tools():
    assert len(ISO_TYPED_GRAPH_TOOL_SCHEMAS) == 7


# ---------------------------------------------------------------------------
# Read SQL: list_for_node
# ---------------------------------------------------------------------------


def _link_row(
    *,
    link_id: str,
    from_id: str = "from-1",
    to_id: str = "to-1",
    link_type: str = "relates_to",
) -> dict[str, Any]:
    return {
        "link_id": link_id,
        "workspace_id": WORKSPACE_ID,
        "from_entity_kind": "Decision",
        "from_entity_id": from_id,
        "to_entity_kind": "Decision",
        "to_entity_id": to_id,
        "link_type": link_type,
        "link_weight": None,
        "validity_state": "active",
        "created_at": "2026-05-20T12:00:00Z",
    }


def test_read_relationlink_for_node_binds_workspace_entity_and_limit():
    rows = [_link_row(link_id="a"), _link_row(link_id="b")]
    conn = _FakeConnection(list_rows=rows)
    pool = _FakePool(conn)
    result = asyncio.run(
        read_relationlink_for_node(WORKSPACE_ID, "node-x", pool, limit=25)
    )
    assert len(result) == 2
    sql, args = conn.calls[0]
    assert args == (WORKSPACE_ID, "node-x", 25)
    # SQL uses an OR on from_entity_id / to_entity_id — list_for_node
    # surfaces both directions in one query.
    assert "from_entity_id = $2 OR to_entity_id = $2" in sql
    assert "validity_state = 'active'" in sql


def test_read_relationlink_for_node_caps_limit_at_max():
    conn = _FakeConnection(list_rows=[])
    pool = _FakePool(conn)
    asyncio.run(
        read_relationlink_for_node(WORKSPACE_ID, "node-x", pool, limit=1_000_000)
    )
    _sql, args = conn.calls[0]
    assert args[2] == MAX_LIST_LIMIT == 100


# ---------------------------------------------------------------------------
# traverse
# ---------------------------------------------------------------------------


def _traverse_row(
    *, entity_id: str, link_type: str = "relates_to", depth: int = 1
) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "entity_kind": "Decision",
        "via_link_type": link_type,
        "depth": depth,
    }


def test_traverse_outgoing_runs_one_query():
    conn = _FakeConnection(
        traverse_rows=[_traverse_row(entity_id="n1"), _traverse_row(entity_id="n2")]
    )
    pool = _FakePool(conn)
    result = asyncio.run(
        traverse_relationlink(
            WORKSPACE_ID,
            "start",
            ["relates_to"],
            pool,
            direction="outgoing",
            max_depth=2,
        )
    )
    assert len(result) == 2
    # Single SQL execution for outgoing.
    traverse_sqls = [c for c in conn.calls if "WITH RECURSIVE walk" in c[0]]
    assert len(traverse_sqls) == 1
    sql, args = traverse_sqls[0]
    assert "from_entity_id = $2" in sql  # outgoing anchors at from_entity
    assert args == (WORKSPACE_ID, "start", ["relates_to"], 2)


def test_traverse_both_directions_runs_two_queries():
    conn = _FakeConnection(traverse_rows=[_traverse_row(entity_id="n1")])
    pool = _FakePool(conn)
    asyncio.run(
        traverse_relationlink(
            WORKSPACE_ID, "start", ["supersedes"], pool, direction="both"
        )
    )
    traverse_sqls = [c for c in conn.calls if "WITH RECURSIVE walk" in c[0]]
    assert len(traverse_sqls) == 2
    sqls = "\n".join(s for s, _ in traverse_sqls)
    assert "from_entity_id = $2" in sqls  # outgoing variant
    assert "to_entity_id = $2" in sqls    # incoming variant


def test_traverse_caps_max_depth_at_3():
    conn = _FakeConnection(traverse_rows=[])
    pool = _FakePool(conn)
    asyncio.run(
        traverse_relationlink(
            WORKSPACE_ID, "start", ["relates_to"], pool, max_depth=99
        )
    )
    _sql, args = conn.calls[0]
    assert args[3] == 3  # capped at MAX_TRAVERSE_DEPTH


def test_traverse_rejects_empty_link_types():
    conn = _FakeConnection()
    pool = _FakePool(conn)
    with pytest.raises(ValueError) as excinfo:
        asyncio.run(
            traverse_relationlink(WORKSPACE_ID, "start", [], pool)
        )
    assert "non-empty" in str(excinfo.value)


def test_traverse_dedupes_by_min_depth():
    """When the same node is reached via multiple paths, take the
    shortest depth."""
    conn = _FakeConnection(
        traverse_rows=[
            _traverse_row(entity_id="dup", depth=3),
            _traverse_row(entity_id="dup", depth=1),
            _traverse_row(entity_id="dup", depth=2),
        ]
    )
    pool = _FakePool(conn)
    result = asyncio.run(
        traverse_relationlink(
            WORKSPACE_ID, "start", ["relates_to"], pool, direction="outgoing"
        )
    )
    assert len(result) == 1
    assert result[0].depth_from_start == 1


def test_traverse_rejects_unknown_direction():
    conn = _FakeConnection()
    pool = _FakePool(conn)
    with pytest.raises(ValueError) as excinfo:
        asyncio.run(
            traverse_relationlink(
                WORKSPACE_ID,
                "start",
                ["relates_to"],
                pool,
                direction="sideways",
            )
        )
    assert "direction" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Deferred write
# ---------------------------------------------------------------------------


def test_create_relationlink_raises_deferred_write_error():
    async def _run():
        await create_relationlink(
            workspace_id=WORKSPACE_ID,
            from_entity_kind="Decision",
            from_entity_id="aaa",
            to_entity_kind="Decision",
            to_entity_id="bbb",
            link_type="supersedes",
        )

    with pytest.raises(RelationLinkWriteNotAvailableError) as excinfo:
        asyncio.run(_run())
    msg = str(excinfo.value)
    assert "[kora.isokron.todo]" in msg
    assert "D-kr3-st2-no-relationlink-write-mcp-tool" in msg
    # Message must call out all three blockers operators need to know.
    assert "created_by_actor_kind" in msg
    assert "Sea MCP" in msg
    assert "chain_event_id" in msg


# ---------------------------------------------------------------------------
# Provider-level handler integration
# ---------------------------------------------------------------------------


def test_iso_link_create_handler_returns_deferred_envelope():
    provider, _conn = _make_provider()
    result = handle_iso_link_tool_call(
        provider,
        "iso_link_create",
        {
            "from_entity_id": "aaaa-1",
            "from_entity_kind": "Decision",
            "to_entity_id": "bbbb-2",
            "to_entity_kind": "Pattern",
            "link_type": "supersedes",
            "rationale": "newer decision",
        },
    )
    decoded = json.loads(result)
    assert decoded["ok"] is False
    assert decoded["deferred"] is True
    assert decoded["deviation_id"] == "D-kr3-st2-no-relationlink-write-mcp-tool"


def test_iso_link_create_handler_rejects_invalid_link_type():
    provider, _conn = _make_provider()
    result = handle_iso_link_tool_call(
        provider,
        "iso_link_create",
        {
            "from_entity_id": "aaaa-1",
            "from_entity_kind": "Decision",
            "to_entity_id": "bbbb-2",
            "to_entity_kind": "Pattern",
            "link_type": "not_a_v1_link_type",
        },
    )
    decoded = json.loads(result)
    assert decoded["ok"] is False
    assert "link_type" in decoded["error"]


def test_iso_link_create_handler_rejects_invalid_node_kind():
    provider, _conn = _make_provider()
    result = handle_iso_link_tool_call(
        provider,
        "iso_link_create",
        {
            "from_entity_id": "aaaa-1",
            "from_entity_kind": "NotARealKind",
            "to_entity_id": "bbbb-2",
            "to_entity_kind": "Pattern",
            "link_type": "supersedes",
        },
    )
    decoded = json.loads(result)
    assert decoded["ok"] is False
    assert "entity_kind" in decoded["error"]


def test_iso_link_traverse_handler_returns_results():
    conn = _FakeConnection(
        traverse_rows=[
            _traverse_row(entity_id="dest-1", link_type="relates_to"),
        ]
    )
    provider, _conn = _make_provider(conn=conn)
    result = handle_iso_link_tool_call(
        provider,
        "iso_link_traverse",
        {
            "from_entity_id": "start",
            "link_types": ["relates_to"],
            "direction": "outgoing",
        },
    )
    decoded = json.loads(result)
    assert decoded["ok"] is True
    assert decoded["count"] == 1
    assert decoded["results"][0]["entity_id"] == "dest-1"


def test_iso_link_traverse_handler_validates_link_types_against_v1_vocab():
    provider, _conn = _make_provider()
    result = handle_iso_link_tool_call(
        provider,
        "iso_link_traverse",
        {"from_entity_id": "start", "link_types": ["bogus_link_type"]},
    )
    decoded = json.loads(result)
    assert decoded["ok"] is False
    assert "invalid V1 vocabulary" in decoded["error"]


def test_iso_link_list_for_node_handler_returns_results():
    conn = _FakeConnection(
        list_rows=[
            _link_row(link_id="L1"),
            _link_row(link_id="L2", link_type="supersedes"),
        ]
    )
    provider, _conn = _make_provider(conn=conn)
    result = handle_iso_link_tool_call(
        provider,
        "iso_link_list_for_node",
        {"entity_id": "node-x"},
    )
    decoded = json.loads(result)
    assert decoded["ok"] is True
    assert decoded["count"] == 2
    assert {r["link_id"] for r in decoded["results"]} == {"L1", "L2"}


def test_iso_link_list_for_node_handler_caps_limit():
    conn = _FakeConnection(list_rows=[])
    provider, _conn = _make_provider(conn=conn)
    handle_iso_link_tool_call(
        provider,
        "iso_link_list_for_node",
        {"entity_id": "node-x", "limit": 9999},
    )
    # The SQL call args should be capped at 100.
    sql, args = conn.calls[0]
    assert args[2] == 100


def test_provider_handle_tool_call_routes_iso_link_prefix():
    conn = _FakeConnection(list_rows=[])
    provider, _conn = _make_provider(conn=conn)
    raw = provider.handle_tool_call(
        "iso_link_list_for_node", {"entity_id": "x"}
    )
    decoded = json.loads(raw)
    assert decoded["ok"] is True


def test_iso_link_traverse_capability_check_logs_deviation(caplog):
    """The shared assert_kora_can_perform stub fires + logs the deviation."""
    conn = _FakeConnection(traverse_rows=[])
    provider, _conn = _make_provider(conn=conn)
    with caplog.at_level(
        logging.WARNING, logger="plugins.memory.isokron.tools.iso_node"
    ):
        handle_iso_link_tool_call(
            provider,
            "iso_link_traverse",
            {
                "from_entity_id": "start",
                "link_types": ["relates_to"],
            },
        )
    msgs = [r.getMessage() for r in caplog.records]
    assert any("D-kr3-st1-capability-check-deferred" in m for m in msgs)
    assert any("cap_read_unfiltered_relationlink" in m for m in msgs)
