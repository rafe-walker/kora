"""``iso_node_*`` typed-graph tool family (KR-3 ST1).

Four model-facing MCP tools that replace Hermes' flat ``memory.set`` /
``memory.append`` / ``memory.get`` surface with a typed-node API
backed by ``kronicle.agent_scratchpad_entries`` (Plan 02).

Each tool handler:

1. Resolves ``workspace_id`` via the provider's existing
   ``_resolve_workspace_id`` precedence.
2. Asserts the relevant ``cap_*`` (stub for now — see
   :func:`assert_kora_can_perform`).
3. Performs the substrate operation (read direct, write via the
   deferred MCP surface).
4. Returns a JSON-serializable result dict that the MemoryManager
   serializes for the model.

Returns are always shaped ``{"ok": bool, ...}`` so the model can
program against a stable envelope; deferred writes return
``{"ok": False, "deferred": true, "deviation_id": "D-kr2-st3-..."}``.

# Node kind encoding (v0.1)

The substrate's ``kronicle.agent_scratchpad_entries.scratchpad_kind``
column is a closed enum of 7 values (foundation/0135). Beads-pattern
node_kind is a separate, broader concept (18 canonical IsoKron entity
kinds). v0.1 packs the node_kind into the ``content_inline`` header so
``iso_node_search`` and ``iso_node_read`` can recover it without
schema changes. KR-3a or later promotes this to a dedicated column once
read patterns firm up.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Dict, List

from ..scratchpad import (
    ScratchpadEntry,
    ScratchpadKind,
    VisibilityScope,
)

if TYPE_CHECKING:  # pragma: no cover — avoids import cycle at runtime
    from ..provider import IsoKronMemoryProvider


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical node_kind enum — 17 IsoKron entity kinds + KronicleBlock = 18
# ---------------------------------------------------------------------------

NODE_KINDS: tuple[str, ...] = (
    "Decision",
    "Gotcha",
    "Pattern",
    "Convention",
    "Concept",
    "Ticket",
    "FailedAttempt",
    "AcceptanceTest",
    "GamingPattern",
    "Project",
    "Component",
    "Milestone",
    "CrossCut",
    "ExternalDependency",
    "Resource",
    "Tool",
    "Schema",
    "KronicleBlock",
)

# Valid values for the substrate's ``scratchpad_kind`` ENUM
# (foundation/0135). Used to validate the optional model arg.
_SCRATCHPAD_KINDS: tuple[str, ...] = tuple(k.value for k in ScratchpadKind)


# ---------------------------------------------------------------------------
# Capability check stub
# ---------------------------------------------------------------------------


# Maps each iso_node_* tool to the cap_* it would gate.
# Sourced from packages/sea-mcp-server/src/capability-matrix.ts —
# the KR-6 Python mirror of actorHasCapability will use this map.
_TOOL_CAPABILITIES: Dict[str, str] = {
    "iso_node_create": "cap_write_agent_scratchpad",
    "iso_node_read": "cap_read_precommit_scratchpad",
    "iso_node_search": "cap_read_precommit_scratchpad",
    "iso_node_supersede": "cap_write_agent_scratchpad",
}


# Capability check (KR-6): the real Python mirror of TS-side
# ``actorHasCapability``. Re-exported from this module to preserve
# the existing import path used by iso_link.py + tests. KR-3 ST1's
# stub (which always allowed + logged D-kr3-st1) was replaced in KR-6;
# D-kr3-st1-capability-check-deferred is Closed.
from ..capability_check import (  # noqa: F401 — re-exported
    CapabilityDeniedError,
    actor_has_capability,
    assert_kora_can_perform,
)


# ---------------------------------------------------------------------------
# Tool schemas — OpenAI function-call format (matches mem0/honcho/etc.)
# ---------------------------------------------------------------------------


ISO_NODE_CREATE_SCHEMA: Dict[str, Any] = {
    "name": "iso_node_create",
    "description": (
        "Create a new typed node in your working memory (Plan 02 "
        "scratchpad). Use this for decisions, observations, gotchas, "
        "patterns, or any structured thought you want to retrieve or "
        "hand off to other agents later. Choose node_kind from the 18 "
        "canonical IsoKron entity kinds."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "node_kind": {
                "type": "string",
                "enum": list(NODE_KINDS),
                "description": "One of the 18 canonical entity kinds.",
            },
            "title": {
                "type": "string",
                "maxLength": 200,
                "description": "Short title for the node.",
            },
            "content_summary": {
                "type": "string",
                "maxLength": 1000,
                "description": "Main body of the node.",
            },
            "cross_agent_dereferenceable": {
                "type": "boolean",
                "default": False,
                "description": (
                    "When true, the node is visible to Critic and Oracle. "
                    "Use for handoffs and shared reasoning trails."
                ),
            },
            "scratchpad_kind": {
                "type": "string",
                "enum": list(_SCRATCHPAD_KINDS),
                "description": (
                    "Optional substrate scratchpad_kind enum value "
                    "(reasoning_trail / hypothesis / discarded_option / "
                    "override_rationale / self_critique / route_decision / "
                    "compacted_summary). Defaults to reasoning_trail."
                ),
            },
        },
        "required": ["node_kind", "title", "content_summary"],
    },
}

ISO_NODE_READ_SCHEMA: Dict[str, Any] = {
    "name": "iso_node_read",
    "description": (
        "Read a typed node from your working memory by its entry_id. "
        "Returns full content + metadata + decoded node_kind."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "entry_id": {
                "type": "string",
                "format": "uuid",
                "description": "The scratchpad_entry_id UUID.",
            },
        },
        "required": ["entry_id"],
    },
}

ISO_NODE_SEARCH_SCHEMA: Dict[str, Any] = {
    "name": "iso_node_search",
    "description": (
        "Search your working memory by node_kind and/or free-text. "
        "Returns up to 20 most-recent matching nodes by default."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "node_kind": {
                "type": "string",
                "enum": list(NODE_KINDS),
                "description": "Filter to nodes of this kind (optional).",
            },
            "text_query": {
                "type": "string",
                "description": (
                    "Free-text matched against title + content_summary "
                    "(case-insensitive substring; FTS lands in KR-3a)."
                ),
            },
            "limit": {
                "type": "integer",
                "default": 20,
                "minimum": 1,
                "maximum": 50,
            },
            "cross_agent_only": {
                "type": "boolean",
                "default": False,
                "description": (
                    "When true, include cross-agent dereferenceable entries "
                    "from other actor_kinds (Critic, Oracle, claude_pm)."
                ),
            },
        },
    },
}

ISO_NODE_SUPERSEDE_SCHEMA: Dict[str, Any] = {
    "name": "iso_node_supersede",
    "description": (
        "Append a revised version of an existing node. The old node is "
        "marked superseded (not deleted — IsoKron append-only discipline). "
        "Use when you've learned something new about a Decision / Pattern "
        "/ Gotcha and need to record the update."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "superseded_entry_id": {
                "type": "string",
                "format": "uuid",
                "description": "The scratchpad_entry_id of the node being superseded.",
            },
            "new_title": {
                "type": "string",
                "maxLength": 200,
            },
            "new_content_summary": {
                "type": "string",
                "maxLength": 1000,
            },
            "supersession_reason": {
                "type": "string",
                "description": "Why the supersession happened, in Kora's words.",
            },
        },
        "required": [
            "superseded_entry_id",
            "new_content_summary",
            "supersession_reason",
        ],
    },
}


ISO_NODE_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    ISO_NODE_CREATE_SCHEMA,
    ISO_NODE_READ_SCHEMA,
    ISO_NODE_SEARCH_SCHEMA,
    ISO_NODE_SUPERSEDE_SCHEMA,
]


# ---------------------------------------------------------------------------
# Content encoding — pack node_kind into content_inline for v0.1
# ---------------------------------------------------------------------------


_HEADER_PREFIX = "iso_node v0.1\n"
_NODE_KIND_FIELD = "node_kind: "
_TITLE_FIELD = "title: "
_BODY_DELIM = "\n---\n"


def _pack_content(node_kind: str, title: str, content_summary: str) -> str:
    """Encode (node_kind, title, content_summary) into ``content_inline``.

    Header is short + deterministic so ``iso_node_read`` / search can
    parse it back without bloating storage. The body delimiter
    (``\\n---\\n``) lets the model include arbitrary markdown in
    ``content_summary`` without breaking the parse.
    """
    return (
        _HEADER_PREFIX
        + _NODE_KIND_FIELD
        + node_kind
        + "\n"
        + _TITLE_FIELD
        + title
        + _BODY_DELIM
        + content_summary
    )


def _unpack_content(content_inline: str | None) -> dict[str, Any]:
    """Recover (node_kind, title, body) from packed ``content_inline``.

    Returns a dict with the original fields, plus a fallback ``body``
    for legacy entries (created before iso_node packing) where
    everything is dumped into body.
    """
    if content_inline is None:
        return {"node_kind": None, "title": None, "body": None}
    if not content_inline.startswith(_HEADER_PREFIX):
        # Legacy / non-iso_node entry — surface the raw text as body.
        return {"node_kind": None, "title": None, "body": content_inline}
    rest = content_inline[len(_HEADER_PREFIX):]
    head, _, body = rest.partition(_BODY_DELIM)
    node_kind = None
    title = None
    for line in head.splitlines():
        if line.startswith(_NODE_KIND_FIELD):
            node_kind = line[len(_NODE_KIND_FIELD):]
        elif line.startswith(_TITLE_FIELD):
            title = line[len(_TITLE_FIELD):]
    return {"node_kind": node_kind, "title": title, "body": body}


# ---------------------------------------------------------------------------
# Tool handlers — async-wrapped where they need the connection's loop
# ---------------------------------------------------------------------------


def _validate_node_kind(node_kind: str) -> None:
    if node_kind not in NODE_KINDS:
        raise ValueError(
            f"iso_node: node_kind must be one of the 18 canonical kinds; "
            f"got {node_kind!r}"
        )


def _scratchpad_kind_from_arg(arg: Any) -> ScratchpadKind:
    if arg is None:
        return ScratchpadKind.REASONING_TRAIL
    if not isinstance(arg, str):
        raise ValueError(
            f"iso_node: scratchpad_kind must be a string; got {type(arg).__name__}"
        )
    try:
        return ScratchpadKind(arg)
    except ValueError as exc:
        raise ValueError(
            f"iso_node: scratchpad_kind must be one of {_SCRATCHPAD_KINDS}; "
            f"got {arg!r}"
        ) from exc


def _entry_to_dict(entry: ScratchpadEntry) -> dict[str, Any]:
    """Project a ``ScratchpadEntry`` to the JSON shape iso_node tools return."""
    unpacked = _unpack_content(entry.content_inline)
    return {
        "entry_id": entry.scratchpad_entry_id,
        "actor_kind": entry.actor_kind,
        "actor_label": entry.actor_label,
        "node_kind": unpacked["node_kind"],
        "title": unpacked["title"],
        "body": unpacked["body"],
        "content_uri": entry.content_uri,
        "visibility_scope": entry.visibility_scope.value,
        "scratchpad_kind": entry.scratchpad_kind.value,
        "created_at": entry.created_at,
    }


def _handle_iso_node_create(
    provider: "IsoKronMemoryProvider", args: Dict[str, Any]
) -> Dict[str, Any]:
    node_kind = args.get("node_kind")
    title = args.get("title")
    content_summary = args.get("content_summary")
    if not (isinstance(node_kind, str) and isinstance(title, str)
            and isinstance(content_summary, str)):
        return {
            "ok": False,
            "error": "iso_node_create requires node_kind, title, content_summary",
        }
    try:
        _validate_node_kind(node_kind)
        scratchpad_kind = _scratchpad_kind_from_arg(args.get("scratchpad_kind"))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    cross_agent = bool(args.get("cross_agent_dereferenceable", False))
    visibility = (
        VisibilityScope.CROSS_AGENT_DEREFERENCEABLE
        if cross_agent
        else VisibilityScope.AGENT_PRIVATE
    )

    workspace_id = provider._resolve_workspace_id()
    if workspace_id is None:
        return {
            "ok": False,
            "error": "iso_node_create: no workspace_id resolvable",
        }

    assert_kora_can_perform("cap_write_agent_scratchpad")

    content = _pack_content(node_kind, title, content_summary)
    # KR-8 swap: scratchpad writes route through the Sea MCP tool via
    # the KR-7a-wired IsoKronMCPClient. Substrate-side failures surface
    # as IsoKronMCPInvocationError; we project them into a structured
    # envelope so the model gets an in-band signal rather than an
    # uncaught exception (mirrors the dispatcher's denied-envelope
    # pattern from KR-6).
    from ..mcp_client import IsoKronMCPInvocationError
    from ..scratchpad import write_scratchpad_entry

    assert provider._connection is not None
    try:
        mcp_client = provider._connection.get_mcp_client()
    except Exception as exc:
        return {
            "ok": False,
            "error": f"iso_node_create: MCP client unavailable — {exc}",
        }
    try:
        entry_id = provider._connection.submit_and_wait(
            write_scratchpad_entry(
                workspace_id=workspace_id,
                scratchpad_kind=scratchpad_kind,
                visibility_scope=visibility,
                content=content,
                mcp_client=mcp_client,
            ),
            timeout=10.0,
        )
    except IsoKronMCPInvocationError as exc:
        return {
            "ok": False,
            "substrate_error": True,
            "tool_name": exc.tool_name,
            "message": exc.message,
        }
    return {"ok": True, "entry_id": entry_id}


def _handle_iso_node_read(
    provider: "IsoKronMemoryProvider", args: Dict[str, Any]
) -> Dict[str, Any]:
    entry_id = args.get("entry_id")
    if not isinstance(entry_id, str) or not entry_id:
        return {"ok": False, "error": "iso_node_read: entry_id (string) required"}

    workspace_id = provider._resolve_workspace_id()
    if workspace_id is None:
        return {"ok": False, "error": "iso_node_read: no workspace_id resolvable"}

    assert_kora_can_perform("cap_read_precommit_scratchpad")

    # v0.1: pull own + cross-agent caches; substring-match on entry_id.
    # A dedicated point read lands in KR-3a (single-row fetch by id
    # would need a new SQL constant + the matching RLS path).
    own = provider.read_own_scratchpad()
    cross = provider.read_cross_agent_scratchpad()
    for entry in (*own, *cross):
        if entry.scratchpad_entry_id == entry_id:
            return {"ok": True, "node": _entry_to_dict(entry)}
    return {"ok": False, "error": f"iso_node_read: no entry with id {entry_id}"}


def _handle_iso_node_search(
    provider: "IsoKronMemoryProvider", args: Dict[str, Any]
) -> Dict[str, Any]:
    node_kind_filter = args.get("node_kind")
    text_query_raw = args.get("text_query")
    limit_arg = args.get("limit", 20)
    cross_agent_only = bool(args.get("cross_agent_only", False))

    if node_kind_filter is not None:
        try:
            _validate_node_kind(node_kind_filter)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    try:
        limit = int(limit_arg)
    except (TypeError, ValueError):
        return {"ok": False, "error": "iso_node_search: limit must be an integer"}
    limit = max(1, min(50, limit))

    text_query = (
        text_query_raw.lower() if isinstance(text_query_raw, str) else None
    )

    workspace_id = provider._resolve_workspace_id()
    if workspace_id is None:
        return {"ok": False, "error": "iso_node_search: no workspace_id resolvable"}

    assert_kora_can_perform("cap_read_precommit_scratchpad")

    own = provider.read_own_scratchpad()
    candidates: list[ScratchpadEntry] = list(own)
    if cross_agent_only:
        candidates.extend(provider.read_cross_agent_scratchpad())

    results: list[dict[str, Any]] = []
    for entry in candidates:
        unpacked = _unpack_content(entry.content_inline)
        if node_kind_filter and unpacked["node_kind"] != node_kind_filter:
            continue
        if text_query is not None:
            haystack = " ".join(
                str(v) for v in (unpacked["title"], unpacked["body"]) if v
            ).lower()
            if text_query not in haystack:
                continue
        results.append(_entry_to_dict(entry))
        if len(results) >= limit:
            break

    return {"ok": True, "results": results, "count": len(results)}


def _handle_iso_node_supersede(
    provider: "IsoKronMemoryProvider", args: Dict[str, Any]
) -> Dict[str, Any]:
    superseded_entry_id = args.get("superseded_entry_id")
    new_content_summary = args.get("new_content_summary")
    supersession_reason = args.get("supersession_reason")
    new_title = args.get("new_title", "")

    if not (
        isinstance(superseded_entry_id, str)
        and isinstance(new_content_summary, str)
        and isinstance(supersession_reason, str)
    ):
        return {
            "ok": False,
            "error": (
                "iso_node_supersede requires superseded_entry_id, "
                "new_content_summary, supersession_reason"
            ),
        }

    workspace_id = provider._resolve_workspace_id()
    if workspace_id is None:
        return {"ok": False, "error": "iso_node_supersede: no workspace_id resolvable"}

    assert_kora_can_perform("cap_write_agent_scratchpad")

    # Look up the original to inherit node_kind into the supersession.
    original_read = _handle_iso_node_read(
        provider, {"entry_id": superseded_entry_id}
    )
    if not original_read.get("ok"):
        return {
            "ok": False,
            "error": (
                f"iso_node_supersede: cannot resolve superseded entry "
                f"({superseded_entry_id}) — {original_read.get('error')}"
            ),
        }
    original_node = original_read["node"]
    inherited_kind = original_node.get("node_kind") or "Concept"
    packed = _pack_content(
        inherited_kind,
        new_title or (original_node.get("title") or ""),
        f"{new_content_summary}\n\nSupersedes: {superseded_entry_id}\n"
        f"Reason: {supersession_reason}",
    )

    # KR-8 swap: scratchpad write routes through Sea MCP.
    # KR-7 closed: the supersession chain event also routes through Sea MCP
    # via provider._attempt_chain_event_emit. Both surfaces are live;
    # substrate-side failures surface as IsoKronMCPInvocationError on
    # the write and ERROR logs on the chain emit (lifecycle hook).
    from ..mcp_client import IsoKronMCPInvocationError
    from ..scratchpad import write_scratchpad_entry

    assert provider._connection is not None
    try:
        mcp_client = provider._connection.get_mcp_client()
    except Exception as exc:
        return {
            "ok": False,
            "error": f"iso_node_supersede: MCP client unavailable — {exc}",
        }
    try:
        new_entry_id = provider._connection.submit_and_wait(
            write_scratchpad_entry(
                workspace_id=workspace_id,
                scratchpad_kind=ScratchpadKind.COMPACTED_SUMMARY,
                visibility_scope=VisibilityScope.AGENT_PRIVATE,
                content=packed,
                mcp_client=mcp_client,
            ),
            timeout=10.0,
        )
    except IsoKronMCPInvocationError as exc:
        return {
            "ok": False,
            "substrate_error": True,
            "tool_name": exc.tool_name,
            "message": exc.message,
        }
    # Supersession-event emit (defensive: catches at the lifecycle
    # boundary so emit failure doesn't override the successful write).
    provider._attempt_chain_event_emit(
        workspace_id=workspace_id,
        event_type="kora.node.superseded",
        payload={
            "superseded_entry_id": superseded_entry_id,
            "new_entry_id": new_entry_id,
            "reason": supersession_reason,
        },
        origin="iso_node_supersede",
    )
    return {"ok": True, "entry_id": new_entry_id}


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


_HANDLERS = {
    "iso_node_create": _handle_iso_node_create,
    "iso_node_read": _handle_iso_node_read,
    "iso_node_search": _handle_iso_node_search,
    "iso_node_supersede": _handle_iso_node_supersede,
}


def handle_iso_node_tool_call(
    provider: "IsoKronMemoryProvider",
    tool_name: str,
    args: Dict[str, Any],
) -> str:
    """Route a tool call to the right ``iso_node_*`` handler.

    Returns a JSON string per the ``MemoryProvider.handle_tool_call``
    ABC contract. Catches :class:`CapabilityDeniedError` and surfaces
    it as a structured envelope (``{"ok": false, "denied": true,
    "capability": ..., "reason": ...}``) so the model can reason about
    which capability was denied rather than seeing a runtime crash.
    """
    handler = _HANDLERS.get(tool_name)
    if handler is None:
        raise NotImplementedError(
            f"Provider isokron does not handle tool {tool_name!r}"
        )
    try:
        result = handler(provider, args)
    except CapabilityDeniedError as exc:
        result = {
            "ok": False,
            "denied": True,
            "capability": exc.capability,
            "reason": exc.reason,
        }
    return json.dumps(result, default=str)
