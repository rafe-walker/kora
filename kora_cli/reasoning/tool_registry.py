"""Reasoning-side tool registry — KR-FEAT-AGENTIC-REASONING ST1.

Hardcoded allowlist of ``kora__*`` tools Kora is allowed to call
FROM WITHIN HER OWN REASONING LOOP. Distinct from the broader
``/mcp`` agent-facing surface in
``kora_cli/listeners/mcp_tools.py`` — that surface accepts calls
from OTHER agents (claude_pm_isokron, drones); this surface
governs what Kora can call against HERSELF mid-reasoning.

# Security boundary

Reasoning-tools are **READ-ONLY only** in v1. Kora cannot
initiate state changes from her own reasoning loop:

  - No ``kora__request_state_transition`` (would let Kora
    pause/resume herself based on her own analysis — circular
    authority).
  - No ``kora__create_sea_ticket`` (Kora reasoning about a
    DM shouldn't create work-items in Joshua's queue without
    Joshua asking).
  - No ``kora__send_webhook_test_event`` (dev-only debug tool;
    not appropriate for reasoning).
  - No ``kora__send_slack_dm`` / ``kora__send_email`` (Kora
    already responds via the Slack DM channel; meta-sends would
    be confusing + Loop-risky).

Mutating tools remain available to OTHER agents via ``/mcp`` (with
capability gating per KR-MCP-RUNTIME-SURFACE ST2). The architectural
distinction: **Kora REASONS in her DM thread; AGENTS DRIVE her via
MCP.** Reasoning-tools are for Kora to LOOK at her own state to
answer Joshua; mutating-tools are how other agents tell her what
to DO.

If the operator later needs a reasoning-mutating tool (e.g. "Kora
can self-pause if she detects she's stuck"), that's a deliberate
allowlist expansion + a separate threat-model review. v1 ships
read-only.

# Schema conversion: MCP camelCase → Anthropic snake_case

``mcp_tools.TOOL_DESCRIPTORS`` ships ``inputSchema`` (camelCase —
MCP JSON-RPC wire format). Anthropic's tools API expects
``input_schema`` (snake_case). The registry renames at extraction
time; we do NOT mutate the source descriptors.

# Dispatch shape

``execute_reasoning_tool(name, input_dict)`` looks up the executor
in ``mcp_tools.TOOL_DISPATCH`` (verified allowlist) and calls it
in-process. NOT via ``/mcp`` HTTP — the reasoning engine is in
the same process; HTTP routing would be ceremony without
isolation. The dispatchers' kwargs come from the Claude-generated
``tool_input`` dict; the executors' Pydantic-modeled returns get
serialized to JSON for the ``tool_result`` content block.

Tool-execution exceptions (Pydantic validation / substrate read
failure / etc.) become ``tool_result`` error blocks rather than
engine-level failures — Kora can recover + reason about the
error.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Allowlist (HARDCODED v1)
# ---------------------------------------------------------------------------

# The 5 read-only tools Kora can call mid-reasoning. Names match
# ``mcp_tools.TOOL_DESCRIPTORS`` entries exactly. Order is the
# advertised order in the Anthropic ``tools`` array (cosmetic; Claude
# picks tools by name not position).
REASONING_TOOL_ALLOWLIST: List[str] = [
    "kora__get_operational_state",
    "kora__get_health_rollup",
    "kora__get_recent_ledger_entries",
    "kora__list_active_sea_tickets",
    "kora__get_recent_chain_events",
]


# ---------------------------------------------------------------------------
# Descriptor extraction — converts MCP shape → Anthropic shape
# ---------------------------------------------------------------------------


def _mcp_to_anthropic_descriptor(mcp_desc: Dict[str, Any]) -> Dict[str, Any]:
    """Project an MCP tool descriptor onto Anthropic's tools-API shape.

    MCP (``mcp_tools.TOOL_DESCRIPTORS``):
        {
          "name": "kora__get_operational_state",
          "description": "...",
          "inputSchema": {"type": "object", ...},
          # MCP-specific extras (requires_cap_gate, dev_only) are
          # NOT relevant to reasoning use; dropped here.
        }

    Anthropic (per ``anthropic==0.86.0`` ``messages.create(tools=)``):
        {
          "name": "kora__get_operational_state",
          "description": "...",
          "input_schema": {"type": "object", ...},
        }

    Only three fields. We never forward MCP-specific extras —
    Claude doesn't need them, and they'd pollute the prompt.
    """
    return {
        "name": mcp_desc["name"],
        "description": mcp_desc["description"],
        "input_schema": mcp_desc["inputSchema"],
    }


def get_reasoning_available_tools() -> List[Dict[str, Any]]:
    """Return the Anthropic tool descriptors for Kora's reasoning loop.

    Lazy lookup against ``mcp_tools.TOOL_DESCRIPTORS`` so the
    reasoning registry stays in sync with the canonical MCP
    surface. Tools not in :data:`REASONING_TOOL_ALLOWLIST` are
    filtered out (security boundary: mutating tools never reach
    Claude's tool list).

    Returns a fresh list per call so the engine's per-respond
    builder can pass it to ``messages.create(tools=...)`` without
    risk of cross-call mutation.
    """
    # Lazy import — keeps non-reasoning paths fast + breaks any
    # circular import risk between mcp_tools and the reasoning
    # engine.
    from kora_cli.listeners.mcp_tools import TOOL_DESCRIPTORS

    by_name = {desc["name"]: desc for desc in TOOL_DESCRIPTORS}

    out: List[Dict[str, Any]] = []
    for name in REASONING_TOOL_ALLOWLIST:
        mcp_desc = by_name.get(name)
        if mcp_desc is None:
            # The allowlist refers to a name not in the MCP
            # surface — config drift. Skip + WARN; the rest of
            # the allowlist still works.
            logger.warning(
                "[kora.reasoning.tool_registry] allowlist references "
                "unknown MCP tool %r — skipping",
                name,
            )
            continue
        out.append(_mcp_to_anthropic_descriptor(mcp_desc))
    return out


# ---------------------------------------------------------------------------
# Tool dispatch — direct in-process function call
# ---------------------------------------------------------------------------


class ReasoningToolNotAllowed(RuntimeError):
    """Claude requested a tool not in the reasoning allowlist.

    The engine catches this + emits a ``tool_result`` error block
    so Claude can recover. Never propagates to the handler.
    """


async def execute_reasoning_tool(
    name: str, tool_input: Dict[str, Any]
) -> Any:
    """Execute a reasoning-allowed tool by name.

    Args:
      name: ``kora__*`` tool name (must be in
        :data:`REASONING_TOOL_ALLOWLIST`).
      tool_input: Claude-generated arguments dict. Forwarded to the
        executor as kwargs (executors accept keyword-only args per
        ``mcp_tools.py``).

    Returns:
      The Pydantic ``BaseModel`` result from the executor. Caller
      (the engine) serializes via ``model_dump_json()`` for the
      ``tool_result`` content block.

    Raises:
      :class:`ReasoningToolNotAllowed`: ``name`` not in allowlist.
        Engine catches + converts to a ``tool_result`` error.

    Tool-execution exceptions (TypeError on bad input keys,
    substrate read failures, Pydantic validation errors)
    propagate up; the engine catches them at the call site and
    builds a ``tool_result`` error block rather than crashing.
    """
    if name not in REASONING_TOOL_ALLOWLIST:
        raise ReasoningToolNotAllowed(
            f"tool {name!r} is not in the reasoning allowlist "
            f"(read-only tools only). Available: "
            f"{REASONING_TOOL_ALLOWLIST}"
        )

    # Resolve the executor via mcp_tools.TOOL_DISPATCH. The
    # dispatcher takes a single ``params`` dict and calls the
    # underlying _execute_<name> function internally. We pass
    # the Claude-generated tool_input straight through.
    from kora_cli.listeners.mcp_tools import TOOL_DISPATCH

    dispatcher = TOOL_DISPATCH.get(name)
    if dispatcher is None:
        # Shouldn't happen — allowlist names match TOOL_DESCRIPTORS
        # which is in lockstep with TOOL_DISPATCH. Defensive.
        raise ReasoningToolNotAllowed(
            f"tool {name!r} is in the reasoning allowlist but has "
            f"no dispatcher — mcp_tools.TOOL_DISPATCH drift"
        )

    return await dispatcher(tool_input)
