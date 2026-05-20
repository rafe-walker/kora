"""KR-3 ST3 — Tool surface finalize tests.

Covers the three pieces the spec + PM dispatch emphasized:

1. Readiness: combined 7-tool surface registered + toolset name
   exported; system_prompt_block §6a Typed-graph tools advertises
   the surface to the model.
2. Round-trip integration: iso_node + iso_link tool families produce
   consistent envelopes across the full create-attempt → read →
   search → list → traverse flow (against mocked substrate).
3. Capability-check stub verification: every iso_* tool invocation
   logs the D-kr3-st1 deviation ID (per spec § 161).
4. Hermes flat memory_tool deprecation: every call to the flat
   surface emits a `[kora.memory.deprecated]` WARNING.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

import pytest

from plugins.memory.isokron.scratchpad import (
    ScratchpadEntry,
    ScratchpadKind,
    VisibilityScope,
)
from plugins.memory.isokron.tools import (
    ISO_LINK_TOOL_SCHEMAS,
    ISO_NODE_TOOL_SCHEMAS,
    ISO_TYPED_GRAPH_TOOL_SCHEMAS,
    ISOKRON_TOOLSET_NAME,
    NODE_KINDS,
)
from plugins.memory.isokron.tools.iso_node import _pack_content


WORKSPACE_ID = "org_test_st3"


# ---------------------------------------------------------------------------
# Readiness — combined surface + toolset name
# ---------------------------------------------------------------------------


def test_combined_surface_is_seven_tools_with_unique_names():
    """4 iso_node_* + 3 iso_link_* = 7 unique tool names."""
    assert len(ISO_NODE_TOOL_SCHEMAS) == 4
    assert len(ISO_LINK_TOOL_SCHEMAS) == 3
    assert len(ISO_TYPED_GRAPH_TOOL_SCHEMAS) == 7
    names = [s["name"] for s in ISO_TYPED_GRAPH_TOOL_SCHEMAS]
    assert len(set(names)) == 7
    assert set(names) == {
        "iso_node_create",
        "iso_node_read",
        "iso_node_search",
        "iso_node_supersede",
        "iso_link_create",
        "iso_link_traverse",
        "iso_link_list_for_node",
    }


def test_isokron_toolset_name_is_stable():
    """Exported constant — used by operator config (memory.toolsets.*)."""
    assert ISOKRON_TOOLSET_NAME == "isokron_memory"


def test_provider_get_tool_schemas_returns_combined_surface():
    """The provider's surface is the combined 7-tool list."""
    from plugins.memory.isokron.provider import IsoKronMemoryProvider

    provider = IsoKronMemoryProvider(
        config={
            "isokron_dsn": "postgres://kora:secret@localhost:5432/isokron",
            "mcp_endpoint": "stdio://node ./sea-mcp-server.js",
            "default_workspace_id": WORKSPACE_ID,
        }
    )
    schemas = provider.get_tool_schemas()
    assert len(schemas) == 7
    assert {s["name"] for s in schemas} == {
        s["name"] for s in ISO_TYPED_GRAPH_TOOL_SCHEMAS
    }


# ---------------------------------------------------------------------------
# System prompt block — §6a Typed-graph tools section
# ---------------------------------------------------------------------------


def test_system_prompt_block_advertises_all_seven_tools():
    """The model sees every tool name in the §6a Typed-graph tools section."""
    from plugins.memory.isokron.provider import _render_typed_graph_tool_surface

    rendered = _render_typed_graph_tool_surface()
    assert "§6a Typed-graph tools" in rendered
    assert "7 tools" in rendered
    for schema in ISO_TYPED_GRAPH_TOOL_SCHEMAS:
        assert schema["name"] in rendered


def test_system_prompt_block_advertises_18_node_kinds_guidance():
    """The §6a section nudges the model toward the typed-kind discipline."""
    from plugins.memory.isokron.provider import _render_typed_graph_tool_surface

    rendered = _render_typed_graph_tool_surface()
    assert "18 canonical" in rendered
    assert "prefer the most specific kind over Concept" in rendered


def test_system_prompt_block_advertises_hermes_memory_deprecation():
    """The §6a section tells the model the flat memory tool is deprecated."""
    from plugins.memory.isokron.provider import _render_typed_graph_tool_surface

    rendered = _render_typed_graph_tool_surface()
    assert "deprecated" in rendered
    assert "memory" in rendered
    assert "iso_node_*" in rendered


# ---------------------------------------------------------------------------
# Round-trip integration tests (iso_node + iso_link families)
# ---------------------------------------------------------------------------


class _FakeProviderConnection:
    def __init__(self, *, list_rows=None, traverse_rows=None):
        from tests.plugins.memory.test_iso_link_tools import _FakeConnection, _FakePool

        self._conn = _FakeConnection(
            list_rows=list_rows or [], traverse_rows=traverse_rows or []
        )
        self._pool = _FakePool(self._conn)
        self.submitted: list = []

    def get_pg_pool(self):
        return self._pool

    def submit_and_wait(self, coro, *, timeout: float = 10.0):
        self.submitted.append(coro)
        return asyncio.run(coro)


def _entry(*, entry_id: str, node_kind: str, title: str, body: str = "body") -> ScratchpadEntry:
    return ScratchpadEntry(
        scratchpad_entry_id=entry_id,
        actor_kind="kora",
        actor_label="Kora",
        content_inline=_pack_content(node_kind, title, body),
        content_uri=None,
        content_hash="0" * 64,
        visibility_scope=VisibilityScope.AGENT_PRIVATE,
        scratchpad_kind=ScratchpadKind.REASONING_TRAIL,
        created_at="2026-05-20T12:00:00Z",
    )


def _make_provider(
    *,
    own: Optional[list[ScratchpadEntry]] = None,
    list_rows: Optional[list[dict[str, Any]]] = None,
    traverse_rows: Optional[list[dict[str, Any]]] = None,
):
    from plugins.memory.isokron.provider import IsoKronMemoryProvider

    provider = IsoKronMemoryProvider(
        config={
            "isokron_dsn": "postgres://kora:secret@localhost:5432/isokron",
            "mcp_endpoint": "stdio://node ./sea-mcp-server.js",
            "default_workspace_id": WORKSPACE_ID,
        }
    )
    fake_conn = _FakeProviderConnection(
        list_rows=list_rows, traverse_rows=traverse_rows
    )
    setattr(provider, "_connection", fake_conn)
    setattr(provider, "read_own_scratchpad", lambda **kw: list(own or []))
    setattr(provider, "read_cross_agent_scratchpad", lambda **kw: [])
    return provider, fake_conn


def test_round_trip_iso_node_create_then_search_envelope_shape():
    """create defers; search runs immediately against the seed.

    The model's expected program flow: try create → if deferred,
    surface the deviation_id; search for context regardless.
    """
    seed = [_entry(entry_id="seed-1", node_kind="Decision", title="seed-decision")]
    provider, _conn = _make_provider(own=seed)

    create = json.loads(
        provider.handle_tool_call(
            "iso_node_create",
            {
                "node_kind": "Decision",
                "title": "new entry",
                "content_summary": "fresh observation",
            },
        )
    )
    assert create["ok"] is False
    assert create["deferred"] is True
    assert create["deviation_id"] == "D-kr2-st3-no-scratchpad-write-mcp-tool"

    search = json.loads(
        provider.handle_tool_call(
            "iso_node_search", {"node_kind": "Decision"}
        )
    )
    assert search["ok"] is True
    assert search["count"] == 1
    assert search["results"][0]["title"] == "seed-decision"


def test_round_trip_iso_node_read_then_supersede():
    seed = [
        _entry(entry_id="orig-1", node_kind="Pattern", title="old-pattern"),
    ]
    provider, _conn = _make_provider(own=seed)

    read = json.loads(
        provider.handle_tool_call("iso_node_read", {"entry_id": "orig-1"})
    )
    assert read["ok"] is True
    assert read["node"]["node_kind"] == "Pattern"

    supersede = json.loads(
        provider.handle_tool_call(
            "iso_node_supersede",
            {
                "superseded_entry_id": "orig-1",
                "new_content_summary": "refined",
                "supersession_reason": "learned more",
            },
        )
    )
    assert supersede["ok"] is False
    assert supersede["deferred"] is True


def test_round_trip_iso_link_traverse_envelope():
    """Traverse runs cleanly through the prefix dispatcher."""
    traverse_rows = [
        {
            "entity_id": "dest-1",
            "entity_kind": "Decision",
            "via_link_type": "relates_to",
            "depth": 1,
        }
    ]
    provider, _conn = _make_provider(traverse_rows=traverse_rows)
    result = json.loads(
        provider.handle_tool_call(
            "iso_link_traverse",
            {"from_entity_id": "start", "link_types": ["relates_to"]},
        )
    )
    assert result["ok"] is True
    assert result["count"] == 1


def test_round_trip_iso_link_list_for_node_envelope():
    link_row = {
        "link_id": "L1",
        "workspace_id": WORKSPACE_ID,
        "from_entity_kind": "Decision",
        "from_entity_id": "n1",
        "to_entity_kind": "Decision",
        "to_entity_id": "n2",
        "link_type": "supersedes",
        "link_weight": None,
        "validity_state": "active",
        "created_at": "2026-05-20T12:00:00Z",
    }
    provider, _conn = _make_provider(list_rows=[link_row])
    result = json.loads(
        provider.handle_tool_call(
            "iso_link_list_for_node", {"entity_id": "n1"}
        )
    )
    assert result["ok"] is True
    assert result["count"] == 1
    assert result["results"][0]["link_type"] == "supersedes"


# ---------------------------------------------------------------------------
# Capability-stub verification (every iso_* call must log the deviation)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool, args",
    [
        ("iso_node_create", {
            "node_kind": "Decision",
            "title": "t",
            "content_summary": "s",
        }),
        ("iso_node_read", {"entry_id": "missing-id"}),
        ("iso_node_search", {}),
        ("iso_node_supersede", {
            "superseded_entry_id": "no-such-id",
            "new_content_summary": "x",
            "supersession_reason": "y",
        }),
        ("iso_link_create", {
            "from_entity_id": "a",
            "from_entity_kind": "Decision",
            "to_entity_id": "b",
            "to_entity_kind": "Decision",
            "link_type": "relates_to",
        }),
        ("iso_link_traverse", {
            "from_entity_id": "a",
            "link_types": ["relates_to"],
        }),
        ("iso_link_list_for_node", {"entity_id": "a"}),
    ],
)
def test_every_iso_tool_logs_capability_stub_deviation(caplog, tool, args):
    """The capability stub fires + logs D-kr3-st1 on every iso_* tool call."""
    provider, _conn = _make_provider()
    with caplog.at_level(
        logging.WARNING, logger="plugins.memory.isokron.tools.iso_node"
    ):
        provider.handle_tool_call(tool, args)
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "D-kr3-st1-capability-check-deferred" in m for m in msgs
    ), f"{tool} did not log the D-kr3-st1 deviation"


# ---------------------------------------------------------------------------
# Hermes flat memory_tool deprecation
# ---------------------------------------------------------------------------


def test_flat_memory_tool_logs_deprecation_warning_on_every_call(
    caplog, tmp_path, monkeypatch
):
    """Every memory_tool() call emits `[kora.memory.deprecated]` WARNING."""
    from tools.memory_tool import MemoryStore, memory_tool

    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    store = MemoryStore()
    store.load_from_disk()
    with caplog.at_level(logging.WARNING, logger="tools.memory_tool"):
        memory_tool("add", target="memory", content="any", store=store)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("[kora.memory.deprecated]" in m for m in msgs)
    assert any("memory.add" in m for m in msgs)
    assert any("iso_node_create" in m for m in msgs)


def test_flat_memory_tool_still_returns_normal_result_post_deprecation(
    tmp_path, monkeypatch
):
    """One-release runway: the tool still works; deprecation is informational."""
    from tools.memory_tool import MemoryStore, memory_tool

    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    store = MemoryStore()
    store.load_from_disk()
    raw = memory_tool(
        "add", target="memory", content="a brand new fact", store=store
    )
    result = json.loads(raw)
    # The flat memory_tool's response shape is a dict — the deprecation
    # log doesn't break the normal return.
    assert isinstance(result, dict)
