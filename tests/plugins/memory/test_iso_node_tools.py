"""KR-3 ST1 — ``iso_node_*`` tool family tests.

Covers schema validity, dispatch, content packing/unpacking, search
filters, deferred-write semantics for ``iso_node_create`` and
``iso_node_supersede``, and the capability-check stub's logging.

Provider write paths still defer through
``ScratchpadWriteNotAvailableError`` (BUILD_DEVIATIONS
D-kr2-st3-no-scratchpad-write-mcp-tool); these tests assert the tools
surface the defer as a structured ``{"ok": False, "deferred": true,
"deviation_id": ...}`` payload rather than letting the exception
escape.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, List

import pytest

from plugins.memory.isokron.scratchpad import (
    ScratchpadEntry,
    ScratchpadKind,
    VisibilityScope,
)
from plugins.memory.isokron.tools import (
    ISO_NODE_TOOL_SCHEMAS,
    NODE_KINDS,
    assert_kora_can_perform,
    handle_iso_node_tool_call,
)
from plugins.memory.isokron.tools.iso_node import (
    _pack_content,
    _unpack_content,
)


WORKSPACE_ID = "org_test_iso_node"


# ---------------------------------------------------------------------------
# Fake provider — owns only the bits the tools touch
# ---------------------------------------------------------------------------


class _FakeProviderConnection:
    """Replaces IsoKronConnection in tool tests.

    ``submit_and_wait`` runs the coroutine via ``asyncio.run`` so the
    deferred-error path surfaces exactly as in production.
    """

    def __init__(self):
        self.submitted: list = []

    def submit_and_wait(self, coro, *, timeout: float = 10.0):
        self.submitted.append(coro)
        return asyncio.run(coro)


def _make_provider(
    *,
    workspace_id: str = WORKSPACE_ID,
    own: List[ScratchpadEntry] | None = None,
    cross: List[ScratchpadEntry] | None = None,
):
    """Build a provider with stubbed reads + fake connection."""
    from plugins.memory.isokron.provider import IsoKronMemoryProvider

    provider = IsoKronMemoryProvider(
        config={
            "isokron_dsn": "postgres://kora:secret@localhost:5432/isokron",
            "mcp_endpoint": "stdio://node ./sea-mcp-server.js",
            "default_workspace_id": workspace_id,
        }
    )
    fake_conn = _FakeProviderConnection()
    setattr(provider, "_connection", fake_conn)

    # Replace the sync read accessors with stubs returning whatever the
    # test wants. The tool handlers go through these methods directly.
    setattr(provider, "read_own_scratchpad", lambda **kw: list(own or []))
    setattr(
        provider, "read_cross_agent_scratchpad", lambda **kw: list(cross or [])
    )
    return provider, fake_conn


def _entry(
    *,
    entry_id: str,
    node_kind: str = "Decision",
    title: str = "title-x",
    body: str = "body-y",
    actor_kind: str = "kora",
    visibility: VisibilityScope = VisibilityScope.AGENT_PRIVATE,
    scratchpad_kind: ScratchpadKind = ScratchpadKind.REASONING_TRAIL,
) -> ScratchpadEntry:
    return ScratchpadEntry(
        scratchpad_entry_id=entry_id,
        actor_kind=actor_kind,
        actor_label=actor_kind.capitalize(),
        content_inline=_pack_content(node_kind, title, body),
        content_uri=None,
        content_hash="deadbeef" * 8,
        visibility_scope=visibility,
        scratchpad_kind=scratchpad_kind,
        created_at="2026-05-20T12:00:00Z",
    )


# ---------------------------------------------------------------------------
# Schemas + registration
# ---------------------------------------------------------------------------


def test_iso_node_schemas_are_well_formed():
    """All 4 tool schemas have name/description/parameters; OpenAI function shape."""
    assert len(ISO_NODE_TOOL_SCHEMAS) == 4
    names = {s["name"] for s in ISO_NODE_TOOL_SCHEMAS}
    assert names == {
        "iso_node_create",
        "iso_node_read",
        "iso_node_search",
        "iso_node_supersede",
    }
    for schema in ISO_NODE_TOOL_SCHEMAS:
        assert isinstance(schema["name"], str)
        assert isinstance(schema["description"], str)
        params = schema["parameters"]
        assert params["type"] == "object"
        assert isinstance(params["properties"], dict)


def test_node_kinds_canonical_list_has_18_entries():
    """The 17 IsoKron entity kinds + KronicleBlock = 18."""
    assert len(NODE_KINDS) == 18
    assert "Decision" in NODE_KINDS
    assert "KronicleBlock" in NODE_KINDS


def test_iso_node_create_schema_enum_matches_node_kinds():
    """The JSON Schema enum is in sync with the Python NODE_KINDS tuple."""
    schema = next(s for s in ISO_NODE_TOOL_SCHEMAS if s["name"] == "iso_node_create")
    enum = schema["parameters"]["properties"]["node_kind"]["enum"]
    assert tuple(enum) == NODE_KINDS


# ---------------------------------------------------------------------------
# Content packing
# ---------------------------------------------------------------------------


def test_pack_then_unpack_round_trips_fields():
    packed = _pack_content("Decision", "Pick IsoKron", "We picked it because ...")
    unpacked = _unpack_content(packed)
    assert unpacked["node_kind"] == "Decision"
    assert unpacked["title"] == "Pick IsoKron"
    assert unpacked["body"] == "We picked it because ..."


def test_unpack_handles_legacy_unpacked_content():
    """Non-iso_node entries surface as body-only (no node_kind / title)."""
    unpacked = _unpack_content("just a plain scratchpad row")
    assert unpacked["node_kind"] is None
    assert unpacked["title"] is None
    assert unpacked["body"] == "just a plain scratchpad row"


def test_unpack_handles_null_content_inline():
    """``content_uri``-backed entries have content_inline=None."""
    unpacked = _unpack_content(None)
    assert unpacked == {"node_kind": None, "title": None, "body": None}


# ---------------------------------------------------------------------------
# Capability check stub
# ---------------------------------------------------------------------------


def test_assert_kora_can_perform_stub_logs_deviation_id(caplog):
    """The stub always allows but logs the deviation ID for grep."""
    with caplog.at_level(logging.WARNING, logger="plugins.memory.isokron.tools.iso_node"):
        assert_kora_can_perform("cap_test")
    messages = [r.getMessage() for r in caplog.records]
    assert any("D-kr3-st1-capability-check-deferred" in m for m in messages)
    assert any("cap_test" in m for m in messages)


# ---------------------------------------------------------------------------
# iso_node_create
# ---------------------------------------------------------------------------


def test_iso_node_create_returns_deferred_payload(caplog):
    """The deferred scratchpad write surfaces as a structured JSON envelope."""
    provider, _conn = _make_provider()
    with caplog.at_level(logging.WARNING, logger="plugins.memory.isokron"):
        result = handle_iso_node_tool_call(
            provider,
            "iso_node_create",
            {
                "node_kind": "Decision",
                "title": "Test",
                "content_summary": "Body",
                "cross_agent_dereferenceable": False,
            },
        )
    decoded = json.loads(result)
    assert decoded["ok"] is False
    assert decoded["deferred"] is True
    assert decoded["deviation_id"] == "D-kr2-st3-no-scratchpad-write-mcp-tool"
    assert "[kora.isokron.todo]" in decoded["message"]


def test_iso_node_create_rejects_invalid_node_kind():
    provider, _conn = _make_provider()
    result = handle_iso_node_tool_call(
        provider,
        "iso_node_create",
        {"node_kind": "NotARealKind", "title": "t", "content_summary": "s"},
    )
    decoded = json.loads(result)
    assert decoded["ok"] is False
    assert "node_kind" in decoded["error"]


def test_iso_node_create_rejects_invalid_scratchpad_kind():
    provider, _conn = _make_provider()
    result = handle_iso_node_tool_call(
        provider,
        "iso_node_create",
        {
            "node_kind": "Decision",
            "title": "t",
            "content_summary": "s",
            "scratchpad_kind": "not_a_real_kind",
        },
    )
    decoded = json.loads(result)
    assert decoded["ok"] is False
    assert "scratchpad_kind" in decoded["error"]


def test_iso_node_create_missing_required_fields_returns_error():
    provider, _conn = _make_provider()
    result = handle_iso_node_tool_call(
        provider, "iso_node_create", {"node_kind": "Decision"}
    )
    decoded = json.loads(result)
    assert decoded["ok"] is False
    assert "title" in decoded["error"] or "content_summary" in decoded["error"]


# ---------------------------------------------------------------------------
# iso_node_read
# ---------------------------------------------------------------------------


def test_iso_node_read_finds_entry_in_own_cache():
    target = _entry(entry_id="aaaa-1", title="Decision X")
    provider, _conn = _make_provider(own=[target])
    result = handle_iso_node_tool_call(
        provider, "iso_node_read", {"entry_id": "aaaa-1"}
    )
    decoded = json.loads(result)
    assert decoded["ok"] is True
    assert decoded["node"]["entry_id"] == "aaaa-1"
    assert decoded["node"]["node_kind"] == "Decision"
    assert decoded["node"]["title"] == "Decision X"


def test_iso_node_read_finds_entry_in_cross_agent_cache():
    target = _entry(
        entry_id="bbbb-2",
        actor_kind="critic",
        visibility=VisibilityScope.CROSS_AGENT_DEREFERENCEABLE,
    )
    provider, _conn = _make_provider(own=[], cross=[target])
    result = handle_iso_node_tool_call(
        provider, "iso_node_read", {"entry_id": "bbbb-2"}
    )
    decoded = json.loads(result)
    assert decoded["ok"] is True
    assert decoded["node"]["actor_kind"] == "critic"


def test_iso_node_read_returns_error_on_missing_entry():
    provider, _conn = _make_provider(own=[])
    result = handle_iso_node_tool_call(
        provider, "iso_node_read", {"entry_id": "does-not-exist"}
    )
    decoded = json.loads(result)
    assert decoded["ok"] is False
    assert "no entry" in decoded["error"]


# ---------------------------------------------------------------------------
# iso_node_search
# ---------------------------------------------------------------------------


def test_iso_node_search_filters_by_kind():
    entries = [
        _entry(entry_id="1", node_kind="Decision", title="d-1"),
        _entry(entry_id="2", node_kind="Gotcha", title="g-1"),
        _entry(entry_id="3", node_kind="Decision", title="d-2"),
    ]
    provider, _conn = _make_provider(own=entries)
    result = handle_iso_node_tool_call(
        provider, "iso_node_search", {"node_kind": "Decision"}
    )
    decoded = json.loads(result)
    assert decoded["ok"] is True
    assert decoded["count"] == 2
    assert {r["entry_id"] for r in decoded["results"]} == {"1", "3"}


def test_iso_node_search_filters_by_text_query_case_insensitive():
    entries = [
        _entry(entry_id="1", title="Migration plan", body="postgres details"),
        _entry(entry_id="2", title="Schema notes", body="indexing strategy"),
    ]
    provider, _conn = _make_provider(own=entries)
    result = handle_iso_node_tool_call(
        provider, "iso_node_search", {"text_query": "MIGRATION"}
    )
    decoded = json.loads(result)
    assert decoded["ok"] is True
    assert decoded["count"] == 1
    assert decoded["results"][0]["entry_id"] == "1"


def test_iso_node_search_respects_limit_cap():
    entries = [_entry(entry_id=str(i)) for i in range(100)]
    provider, _conn = _make_provider(own=entries)
    # Asks for 200; server-side caps at 50.
    result = handle_iso_node_tool_call(
        provider, "iso_node_search", {"limit": 200}
    )
    decoded = json.loads(result)
    assert decoded["count"] == 50


def test_iso_node_search_cross_agent_only_includes_other_actors():
    own_entries = [_entry(entry_id="o1", node_kind="Decision", title="own")]
    cross_entries = [
        _entry(
            entry_id="c1",
            node_kind="Decision",
            title="critic-handoff",
            actor_kind="critic",
            visibility=VisibilityScope.CROSS_AGENT_DEREFERENCEABLE,
        ),
    ]
    provider, _conn = _make_provider(own=own_entries, cross=cross_entries)

    own_only = json.loads(
        handle_iso_node_tool_call(
            provider, "iso_node_search", {"node_kind": "Decision"}
        )
    )
    assert own_only["count"] == 1
    assert own_only["results"][0]["actor_kind"] == "kora"

    with_cross = json.loads(
        handle_iso_node_tool_call(
            provider,
            "iso_node_search",
            {"node_kind": "Decision", "cross_agent_only": True},
        )
    )
    assert with_cross["count"] == 2
    assert {r["actor_kind"] for r in with_cross["results"]} == {"kora", "critic"}


# ---------------------------------------------------------------------------
# iso_node_supersede
# ---------------------------------------------------------------------------


def test_iso_node_supersede_inherits_node_kind_from_original():
    original = _entry(entry_id="orig-1", node_kind="Pattern", title="old-title")
    provider, conn = _make_provider(own=[original])
    result = handle_iso_node_tool_call(
        provider,
        "iso_node_supersede",
        {
            "superseded_entry_id": "orig-1",
            "new_content_summary": "refined understanding",
            "supersession_reason": "learned more",
        },
    )
    decoded = json.loads(result)
    # Write defers like iso_node_create — but it must NOT short-circuit
    # before resolving the original (otherwise the new entry would lose
    # its inherited node_kind when the substrate tool lands).
    assert decoded["ok"] is False
    assert decoded["deferred"] is True
    assert decoded["deviation_id"] == "D-kr2-st3-no-scratchpad-write-mcp-tool"
    # The defer happened during the write attempt — meaning the original
    # lookup succeeded + the packing happened (a coroutine was submitted).
    assert len(conn.submitted) == 1


def test_iso_node_supersede_errors_when_original_missing():
    provider, _conn = _make_provider(own=[])
    result = handle_iso_node_tool_call(
        provider,
        "iso_node_supersede",
        {
            "superseded_entry_id": "ghost-id",
            "new_content_summary": "x",
            "supersession_reason": "y",
        },
    )
    decoded = json.loads(result)
    assert decoded["ok"] is False
    assert "cannot resolve superseded entry" in decoded["error"]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_handle_iso_node_tool_call_unknown_tool_raises():
    provider, _conn = _make_provider()
    with pytest.raises(NotImplementedError) as excinfo:
        handle_iso_node_tool_call(provider, "iso_node_not_a_real_tool", {})
    assert "isokron" in str(excinfo.value)


def test_provider_handle_tool_call_dispatches_iso_node_prefix():
    """Provider.handle_tool_call routes iso_node_* by prefix."""
    provider, _conn = _make_provider(own=[_entry(entry_id="x1")])
    raw = provider.handle_tool_call("iso_node_read", {"entry_id": "x1"})
    decoded = json.loads(raw)
    assert decoded["ok"] is True
