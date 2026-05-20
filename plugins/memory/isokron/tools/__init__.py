"""Beads-pattern typed-graph tools for Kora's memory surface.

KR-3 ships model-facing MCP tools that replace Hermes' flat ``memory``
tool surface with typed-node + typed-edge operations against the
IsoKron substrate.

- **ST1 (this PR)** — ``iso_node_*`` family (4 tools) against
  ``kronicle.agent_scratchpad_entries`` (Plan 02 typed scratchpad).
- **ST2** — ``iso_link_*`` family (3 tools) against the RelationLink
  substrate (ADR-0033/0034).
- **ST3** — registration polish + Hermes flat memory deprecation +
  system prompt updates.

Spec § 32 calls out a v0.1 simplification: ``iso_node_*`` reads + writes
operate on the scratchpad surface only. Full read-across the 17 entity
tables lands in KR-3a or later when read patterns + permissions firm up.

Tools that need writes go through the Sea MCP tool surface (cap_-gated
+ chain-audited). As of substrate main, two write tools are missing:

- ``kora__write_agent_scratchpad`` — tracked as
  D-kr2-st3-no-scratchpad-write-mcp-tool. Affects ``iso_node_create``
  and ``iso_node_supersede``.
- ``kora__append_event`` — tracked as
  D-kr2-st4-no-chain-emit-mcp-tool. Affects ``iso_node_supersede``'s
  ``kora.node.superseded`` event.

The handlers attempt the writes through the deferred surfaces and
surface a structured ``{"deferred": true, ...}`` payload back to the
model so it can adapt (e.g. skip a follow-up write that would have
keyed off the new entry_id). When the substrate tools ship, the
handlers' bodies stay the same — only ``scratchpad.write_scratchpad_entry``
and ``events.emit_kora_event`` change.

Capability gating (Plan 04 ``actorHasCapability`` Python mirror) ships
in KR-6 — until then ``assert_kora_can_perform`` is a stub that always
allows and logs ``D-kr3-st1-capability-check-deferred``.
"""

from .iso_link import (
    ISO_LINK_TOOL_SCHEMAS,
    handle_iso_link_tool_call,
)
from .iso_node import (
    ISO_NODE_TOOL_SCHEMAS,
    NODE_KINDS,
    assert_kora_can_perform,
    handle_iso_node_tool_call,
)


ISO_TYPED_GRAPH_TOOL_SCHEMAS = ISO_NODE_TOOL_SCHEMAS + ISO_LINK_TOOL_SCHEMAS
"""Combined 4 + 3 = 7 tool schemas for the typed-graph surface."""


__all__ = [
    "ISO_LINK_TOOL_SCHEMAS",
    "ISO_NODE_TOOL_SCHEMAS",
    "ISO_TYPED_GRAPH_TOOL_SCHEMAS",
    "NODE_KINDS",
    "assert_kora_can_perform",
    "handle_iso_link_tool_call",
    "handle_iso_node_tool_call",
]
