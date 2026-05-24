"""``iso_link_*`` typed-edge tool family (KR-3 ST2).

Three model-facing MCP tools over the ``relationlink`` substrate
(ADR-0033 / ADR-0034 typed edges):

* ``iso_link_create``  — declare a typed edge (deferred; see
  ``D-kr3-st2-no-relationlink-write-mcp-tool``).
* ``iso_link_traverse`` — walk edges from a start node, bounded by
  ``max_depth`` (≤ 3) and a list of allowed ``link_type`` values.
* ``iso_link_list_for_node`` — list all active edges where the given
  node is either source or target.

Reads (`traverse`, `list_for_node`) go direct to Postgres via the
provider's pool — ``relationlink`` has no RLS (foundation/0058);
``workspace_id`` filter in WHERE is the application-layer isolation.

The write surface defers behind
:class:`RelationLinkWriteNotAvailableError`. Three blockers in current
substrate state (see ``BUILD_DEVIATIONS.md``):

1. ``created_by_actor_kind`` CHECK on ``relationlink`` lacks ``'kora'``
   — substrate migration required.
2. No Sea MCP write tool exposes the path.
3. ``chain_event_id NOT NULL`` requires substrate-side emit + bind in
   one SECDEF.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Dict, List

from isokron_client.relationlink import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    MAX_TRAVERSE_DEPTH,
    RELATIONLINK_VALIDITY_STATES,
    RelationLinkRow,
    ReachableNode,
    V1_LINK_TYPES,
    create_relationlink,
    read_relationlink_for_node,
    traverse_relationlink,
)
from isokron_client.capability_check import CapabilityDeniedError, assert_kora_can_perform
from .iso_node import NODE_KINDS

if TYPE_CHECKING:  # pragma: no cover
    from ..provider import IsoKronMemoryProvider


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Capability map (mirrors the iso_node._TOOL_CAPABILITIES pattern)
# ---------------------------------------------------------------------------

_TOOL_CAPABILITIES: Dict[str, str] = {
    "iso_link_create": "cap_sea_link_authoring",
    "iso_link_traverse": "cap_read_unfiltered_relationlink",
    "iso_link_list_for_node": "cap_read_unfiltered_relationlink",
}


# ---------------------------------------------------------------------------
# Tool schemas — OpenAI function-call format
# ---------------------------------------------------------------------------


ISO_LINK_CREATE_SCHEMA: Dict[str, Any] = {
    "name": "iso_link_create",
    "description": (
        "Declare a typed relationship between two entities. Use this "
        "to record connections you discover: 'X supersedes Y', 'A "
        "derived_from B', 'C conflicts_with D'. link_type is one of "
        "the 21 V1 vocabulary (ADR-0033)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "from_entity_id": {"type": "string", "format": "uuid"},
            "from_entity_kind": {
                "type": "string",
                "enum": list(NODE_KINDS),
                "description": "One of the 18 canonical entity kinds.",
            },
            "to_entity_id": {"type": "string", "format": "uuid"},
            "to_entity_kind": {
                "type": "string",
                "enum": list(NODE_KINDS),
                "description": "One of the 18 canonical entity kinds.",
            },
            "link_type": {
                "type": "string",
                "enum": list(V1_LINK_TYPES),
                "description": (
                    "ADR-0033 V1 vocabulary: parent_of / relates_to / "
                    "inspired_by / responds_to / conflicts_with / "
                    "supersedes / blocks / depends_on / condenses_into / "
                    "branches_from / references / derived_from / "
                    "applies_to / validates / grounds_in / documented_in / "
                    "implements / duplicates / caused_by / part_of / covers."
                ),
            },
            "rationale": {
                "type": "string",
                "description": "Why this link exists, in Kora's words.",
            },
        },
        "required": [
            "from_entity_id",
            "from_entity_kind",
            "to_entity_id",
            "to_entity_kind",
            "link_type",
        ],
    },
}

ISO_LINK_TRAVERSE_SCHEMA: Dict[str, Any] = {
    "name": "iso_link_traverse",
    "description": (
        "Walk typed edges from a starting node. Returns nodes "
        "reachable via the specified link_types. Bounded depth "
        "(max 3 per Tenet 2 — keep results flat)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "from_entity_id": {"type": "string", "format": "uuid"},
            "link_types": {
                "type": "array",
                "items": {"type": "string", "enum": list(V1_LINK_TYPES)},
                "minItems": 1,
                "description": (
                    "Which link_types to follow. Required to keep walks "
                    "scoped — an empty list would sprawl through every edge."
                ),
            },
            "direction": {
                "type": "string",
                "enum": ["outgoing", "incoming", "both"],
                "default": "outgoing",
            },
            "max_depth": {
                "type": "integer",
                "default": 2,
                "minimum": 1,
                "maximum": MAX_TRAVERSE_DEPTH,
            },
        },
        "required": ["from_entity_id", "link_types"],
    },
}

ISO_LINK_LIST_FOR_NODE_SCHEMA: Dict[str, Any] = {
    "name": "iso_link_list_for_node",
    "description": (
        "List all active typed edges where the given node is either "
        "source or target. Useful for 'what does Kora know about this "
        "entity?' queries."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "entity_id": {"type": "string", "format": "uuid"},
            "limit": {
                "type": "integer",
                "default": DEFAULT_LIST_LIMIT,
                "maximum": MAX_LIST_LIMIT,
                "minimum": 1,
            },
        },
        "required": ["entity_id"],
    },
}


ISO_LINK_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    ISO_LINK_CREATE_SCHEMA,
    ISO_LINK_TRAVERSE_SCHEMA,
    ISO_LINK_LIST_FOR_NODE_SCHEMA,
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _link_to_dict(link: RelationLinkRow) -> dict[str, Any]:
    return {
        "link_id": link.link_id,
        "from_entity_kind": link.from_entity_kind,
        "from_entity_id": link.from_entity_id,
        "to_entity_kind": link.to_entity_kind,
        "to_entity_id": link.to_entity_id,
        "link_type": link.link_type,
        "link_weight": link.link_weight,
        "validity_state": link.validity_state,
        "created_at": link.created_at,
    }


def _node_to_dict(node: ReachableNode) -> dict[str, Any]:
    return {
        "entity_id": node.entity_id,
        "entity_kind": node.entity_kind,
        "via_link_type": node.via_link_type,
        "depth_from_start": node.depth_from_start,
    }


def _validate_link_type(link_type: str) -> None:
    if link_type not in V1_LINK_TYPES:
        raise ValueError(
            f"iso_link: link_type must be one of the 21 V1 vocabulary "
            f"(ADR-0033); got {link_type!r}"
        )


def _validate_node_kind(node_kind: str) -> None:
    if node_kind not in NODE_KINDS:
        raise ValueError(
            f"iso_link: entity_kind must be one of the 18 canonical "
            f"kinds; got {node_kind!r}"
        )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _handle_iso_link_create(
    provider: "IsoKronMemoryProvider", args: Dict[str, Any]
) -> Dict[str, Any]:
    from_entity_id = args.get("from_entity_id")
    from_entity_kind = args.get("from_entity_kind")
    to_entity_id = args.get("to_entity_id")
    to_entity_kind = args.get("to_entity_kind")
    link_type = args.get("link_type")
    rationale = args.get("rationale")

    if not (
        isinstance(from_entity_id, str)
        and isinstance(from_entity_kind, str)
        and isinstance(to_entity_id, str)
        and isinstance(to_entity_kind, str)
        and isinstance(link_type, str)
    ):
        return {
            "ok": False,
            "error": (
                "iso_link_create requires from_entity_id, from_entity_kind, "
                "to_entity_id, to_entity_kind, link_type"
            ),
        }

    try:
        _validate_node_kind(from_entity_kind)
        _validate_node_kind(to_entity_kind)
        _validate_link_type(link_type)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    workspace_id = provider._resolve_workspace_id()
    if workspace_id is None:
        return {"ok": False, "error": "iso_link_create: no workspace_id resolvable"}

    assert_kora_can_perform("cap_sea_link_authoring")

    # KR-9 swap: relationlink writes route through the Sea MCP tool via
    # the KR-7a-wired IsoKronMCPClient. Substrate-side failures surface
    # as IsoKronMCPInvocationError; project them into a structured
    # envelope so the model gets an in-band signal rather than an
    # uncaught exception (same pattern as iso_node_create / KR-8).
    from ..mcp_client import IsoKronMCPInvocationError

    assert provider._connection is not None
    try:
        mcp_client = provider._connection.get_mcp_client()
    except Exception as exc:
        return {
            "ok": False,
            "error": f"iso_link_create: MCP client unavailable — {exc}",
        }
    try:
        link_id = provider._connection.submit_and_wait(
            create_relationlink(
                workspace_id=workspace_id,
                from_entity_kind=from_entity_kind,
                from_entity_id=from_entity_id,
                to_entity_kind=to_entity_kind,
                to_entity_id=to_entity_id,
                link_type=link_type,
                rationale=rationale,
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
    return {"ok": True, "link_id": link_id}


def _handle_iso_link_traverse(
    provider: "IsoKronMemoryProvider", args: Dict[str, Any]
) -> Dict[str, Any]:
    from_entity_id = args.get("from_entity_id")
    link_types = args.get("link_types")
    direction = args.get("direction", "outgoing")
    max_depth_raw = args.get("max_depth", 2)

    if not isinstance(from_entity_id, str) or not from_entity_id:
        return {"ok": False, "error": "iso_link_traverse: from_entity_id required"}
    if not isinstance(link_types, list) or not link_types:
        return {
            "ok": False,
            "error": "iso_link_traverse: link_types must be a non-empty list",
        }
    invalid = [lt for lt in link_types if lt not in V1_LINK_TYPES]
    if invalid:
        return {
            "ok": False,
            "error": (
                f"iso_link_traverse: link_types contains invalid V1 "
                f"vocabulary entries: {invalid}"
            ),
        }
    if direction not in ("outgoing", "incoming", "both"):
        return {
            "ok": False,
            "error": f"iso_link_traverse: direction must be outgoing / incoming / both; got {direction!r}",
        }
    try:
        max_depth = int(max_depth_raw)
    except (TypeError, ValueError):
        return {"ok": False, "error": "iso_link_traverse: max_depth must be an integer"}
    max_depth = max(1, min(MAX_TRAVERSE_DEPTH, max_depth))

    workspace_id = provider._resolve_workspace_id()
    if workspace_id is None:
        return {"ok": False, "error": "iso_link_traverse: no workspace_id resolvable"}

    assert_kora_can_perform("cap_read_unfiltered_relationlink")

    assert provider._connection is not None
    pool = provider._connection.get_pg_pool()
    reachable = provider._connection.submit_and_wait(
        traverse_relationlink(
            workspace_id,
            from_entity_id,
            list(link_types),
            pool,
            direction=direction,
            max_depth=max_depth,
        ),
        timeout=15.0,
    )
    return {
        "ok": True,
        "results": [_node_to_dict(n) for n in reachable],
        "count": len(reachable),
    }


def _handle_iso_link_list_for_node(
    provider: "IsoKronMemoryProvider", args: Dict[str, Any]
) -> Dict[str, Any]:
    entity_id = args.get("entity_id")
    limit_raw = args.get("limit", DEFAULT_LIST_LIMIT)

    if not isinstance(entity_id, str) or not entity_id:
        return {"ok": False, "error": "iso_link_list_for_node: entity_id required"}
    try:
        limit = int(limit_raw)
    except (TypeError, ValueError):
        return {"ok": False, "error": "iso_link_list_for_node: limit must be an integer"}

    workspace_id = provider._resolve_workspace_id()
    if workspace_id is None:
        return {
            "ok": False,
            "error": "iso_link_list_for_node: no workspace_id resolvable",
        }

    assert_kora_can_perform("cap_read_unfiltered_relationlink")

    assert provider._connection is not None
    pool = provider._connection.get_pg_pool()
    rows = provider._connection.submit_and_wait(
        read_relationlink_for_node(workspace_id, entity_id, pool, limit=limit),
        timeout=10.0,
    )
    return {
        "ok": True,
        "results": [_link_to_dict(r) for r in rows],
        "count": len(rows),
    }


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


_HANDLERS = {
    "iso_link_create": _handle_iso_link_create,
    "iso_link_traverse": _handle_iso_link_traverse,
    "iso_link_list_for_node": _handle_iso_link_list_for_node,
}


def handle_iso_link_tool_call(
    provider: "IsoKronMemoryProvider",
    tool_name: str,
    args: Dict[str, Any],
) -> str:
    """Route a tool call to the right ``iso_link_*`` handler.

    Mirrors the ``iso_node`` dispatcher's CapabilityDeniedError catch
    so a denied capability surfaces as a structured envelope rather
    than a runtime crash.
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
