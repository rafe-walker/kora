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


@pytest.fixture(autouse=True)
def _restore_capability_matrix():
    """Force ``ACTOR_CAPABILITY_MATRIX_KORA_COLUMN`` back to the
    hand-mirrored ``SEA + KORA_BROADER`` union BEFORE each test.

    Other tests (``test_provider_end_to_end``, the parity tests,
    ``test_capability_matrix_mirror``) exercise the MCP-populate
    path by calling ``populate_capability_matrix_from_mcp`` with
    a small synthetic dict, which then ``.clear()`` + ``.update()``
    the module-level mirror in place. Under xdist that mutation can
    land on the same worker before tests here run, so the canonical
    55-entry baseline ``assert_kora_can_perform`` expects gets
    replaced with a 1-entry stub and lookups raise KeyError instead
    of the documented CapabilityDeniedError. Rebuilding from the
    static SEA + KORA_BROADER subset dicts (the documented source
    of truth at module load) makes every test in this file
    self-contained regardless of xdist scheduling.
    """
    from plugins.memory.isokron import capability_matrix_mirror as _mm

    canonical = {
        **_mm.SEA_CAPABILITIES_KORA_COLUMN,
        **_mm.KORA_BROADER_CAPABILITIES_KORA_COLUMN,
    }
    _mm.ACTOR_CAPABILITY_MATRIX_KORA_COLUMN.clear()
    _mm.ACTOR_CAPABILITY_MATRIX_KORA_COLUMN.update(canonical)
    yield
    _mm.ACTOR_CAPABILITY_MATRIX_KORA_COLUMN.clear()
    _mm.ACTOR_CAPABILITY_MATRIX_KORA_COLUMN.update(canonical)


# ---------------------------------------------------------------------------
# Fake provider — owns only the bits the tools touch
# ---------------------------------------------------------------------------


class _FakeProviderConnection:
    """Replaces IsoKronConnection in tool tests.

    Exposes ``get_mcp_client()`` for KR-8 + KR-7 swaps. ``submit_and_wait``
    runs the coroutine via ``asyncio.run`` so substrate-side errors
    surface exactly as in production.
    """

    def __init__(self, *, mcp_client=None):
        self.submitted: list = []
        self._mcp_client = mcp_client or _FakeMcpClient()

    def get_mcp_client(self):
        return self._mcp_client

    def submit_and_wait(self, coro, *, timeout: float = 10.0):
        self.submitted.append(coro)
        return asyncio.run(coro)


class _FakeMcpClient:
    """Default fake — returns canonical-shape success for the K-7/K-8/K-9
    tools that iso_node + iso_link handlers hit."""

    def __init__(self, *, invoke_result=None, invoke_raises=None):
        self.invoke_calls: list[tuple[str, dict]] = []
        self._invoke_result = invoke_result
        self._invoke_raises = invoke_raises
        self._counter = 0

    async def invoke(self, tool_name: str, args: dict):
        self.invoke_calls.append((tool_name, dict(args)))
        if self._invoke_raises is not None:
            raise self._invoke_raises
        if self._invoke_result is not None:
            return self._invoke_result
        self._counter += 1
        if tool_name == "kora__write_agent_scratchpad":
            return {
                "scratchpad_entry_id": f"spe-{self._counter:03d}",
                "approved_event_id": f"evt-{self._counter:03d}",
            }
        if tool_name == "kora__append_event":
            return {"event_id": f"evt-{self._counter:03d}"}
        if tool_name == "kora__read_kora_capability_row":
            return {"capability_matrix": {"cap_write_agent_scratchpad": True}}
        if tool_name == "kora__create_relationlink":
            # K-13 added iso_link_create → kora__create_relationlink; tests
            # in this module don't assert on the relationlink payload, just
            # that the call doesn't crash. Return a canonical-shape ack
            # matching plugins/memory/isokron/relationlink.py's expected
            # response (link_id + chain_event_id, both UUID strings).
            return {
                "link_id": f"rl-{self._counter:03d}",
                "chain_event_id": f"evt-{self._counter:03d}",
            }
        raise AssertionError(f"unexpected tool {tool_name!r}")


def _make_provider(
    *,
    workspace_id: str = WORKSPACE_ID,
    own: List[ScratchpadEntry] | None = None,
    cross: List[ScratchpadEntry] | None = None,
    mcp_client=None,
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
    fake_conn = _FakeProviderConnection(mcp_client=mcp_client)
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


def test_assert_kora_can_perform_passes_for_granted_capability():
    """KR-6: the real check passes silently for capabilities Kora holds."""
    # cap_write_agent_scratchpad is granted to Kora per the C2 mirror.
    # No raise, no log — silent pass.
    assert_kora_can_perform("cap_write_agent_scratchpad") is None
    assert_kora_can_perform("cap_sea_create") is None


def test_assert_kora_can_perform_raises_for_denied_capability():
    """KR-6: denied caps raise CapabilityDeniedError (was: silent stub log)."""
    from plugins.memory.isokron.capability_check import CapabilityDeniedError

    with pytest.raises(CapabilityDeniedError) as excinfo:
        assert_kora_can_perform("cap_override_security_or_policy_verdict")
    assert excinfo.value.capability == "cap_override_security_or_policy_verdict"


def test_assert_kora_can_perform_raises_keyerror_for_unknown_cap():
    """KR-6: unknown caps fail-loud (programmer error)."""
    with pytest.raises(KeyError):
        assert_kora_can_perform("cap_does_not_exist_anywhere")


# ---------------------------------------------------------------------------
# iso_node_create
# ---------------------------------------------------------------------------


def test_iso_node_create_returns_ok_envelope_with_substrate_entry_id():
    """KR-8: scratchpad write succeeds via MCP; entry_id returned to model."""
    provider, conn = _make_provider()
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
    assert decoded["ok"] is True
    # The fake's counter assigns spe-001 on first invoke.
    assert decoded["entry_id"] == "spe-001"
    # MCP tool was invoked with the expected shape.
    assert len(conn._mcp_client.invoke_calls) == 1
    tool_name, args = conn._mcp_client.invoke_calls[0]
    assert tool_name == "kora__write_agent_scratchpad"
    assert args["scratchpad_kind"] == "reasoning_trail"
    assert args["visibility_scope"] == "agent_private"


def test_iso_node_create_substrate_error_surfaces_structured_envelope():
    """IsoKronMCPInvocationError flips into {'ok': False, 'substrate_error': True, ...}."""
    from plugins.memory.isokron.mcp_client import IsoKronMCPInvocationError

    error_client = _FakeMcpClient(
        invoke_raises=IsoKronMCPInvocationError(
            "kora__write_agent_scratchpad", "cap_write_agent_scratchpad denied"
        )
    )
    provider, _conn = _make_provider(mcp_client=error_client)
    result = handle_iso_node_tool_call(
        provider,
        "iso_node_create",
        {
            "node_kind": "Decision",
            "title": "Test",
            "content_summary": "Body",
        },
    )
    decoded = json.loads(result)
    assert decoded["ok"] is False
    assert decoded["substrate_error"] is True
    assert decoded["tool_name"] == "kora__write_agent_scratchpad"
    assert "denied" in decoded["message"]


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
    """KR-8: write succeeds via MCP; inherited node_kind is packed into
    the new entry's content_inline header."""
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
    assert decoded["ok"] is True
    assert decoded["entry_id"] == "spe-001"
    # The new entry's content_inline header carries Pattern (inherited).
    invokes = [c for c in conn._mcp_client.invoke_calls if c[0] == "kora__write_agent_scratchpad"]
    assert len(invokes) == 1
    _, args = invokes[0]
    assert "node_kind: Pattern" in args["content_inline"]
    # Supersession reason carried in the body.
    assert "Reason: learned more" in args["content_inline"]
    # A kora.node.superseded emit also fired post-write.
    emit_calls = [c for c in conn._mcp_client.invoke_calls if c[0] == "kora__append_event"]
    assert len(emit_calls) == 1
    _, emit_args = emit_calls[0]
    assert emit_args["event_type"] == "kora.node.superseded"
    assert emit_args["payload"]["new_entry_id"] == "spe-001"


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
