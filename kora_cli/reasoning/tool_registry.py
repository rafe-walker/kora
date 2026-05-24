"""Reasoning-side tool registry — KR-FEAT-AGENTIC-REASONING ST1.

Hardcoded allowlist of ``kora__*`` tools Kora is allowed to call
FROM WITHIN HER OWN REASONING LOOP. Distinct from the broader
``/mcp`` agent-facing surface in
``kora_cli/listeners/mcp_tools.py`` — that surface accepts calls
from OTHER agents (claude_pm_isokron, drones); this surface
governs what Kora can call against HERSELF mid-reasoning.

# Security boundary

Reasoning-tools are **READ-ONLY** by default. Kora cannot
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
    be confusing + Loop-risky, AND the caller-controlled
    recipient list creates a mass-send risk vector).

# Deliberate scope expansion: kora__send_email_to_operator

KR-EMAIL-OUTBOUND-COMPOSE-TOOL adds ONE mutating tool to the
reasoning allowlist: ``kora__send_email_to_operator``. The
exclusion above for ``kora__send_email`` was driven by two
concerns — Loop-risk + mass-send-risk-from-caller-controlled-
recipient. ``kora__send_email_to_operator`` neutralizes the mass-
send concern by **pinning the recipient** to
``KORA_EMAIL_JOSHUA_ADDRESS`` in the executor itself; the caller
cannot specify other recipients. Loop-risk is addressed by the
tool's own hourly-cap default (``KORA_EMAIL_OUTBOUND_HOURLY_CAP``
= 5; configurable) plus operator R3 Q8a explicitly asking for
this surface ("Kora, email me that pdf").

This is the deliberate expansion the original docstring
anticipated: "If the operator later needs a reasoning-mutating
tool... that's a deliberate allowlist expansion + a separate
threat-model review." The R3 walkthrough was the review.

# Deliberate scope expansion #2: kora__attempt_probe_autofix

KR-PROBE-AUTOFIX-EXECUTION adds a second mutating tool
``kora__attempt_probe_autofix`` per
``feedback-kora-is-unified-operator-interface`` ("Kora
investigates + attempts fix where safe + DMs you with what
happened, what was tried, what's left for you to decide"). The
blast-radius concern (mass-send analog) is handled by THREE
fail-CLOSED gates: (1) per-probe env gate
``KORA_PROBE_AUTOFIX_<PROBE>_ENABLED`` defaults OFF; (2) the
envelope's action whitelist (only ``fly + restart_machine``
exists in v1); (3) the per-probe executor verifies the target_id
resolves to a real unhealthy resource (Fly machine in
state != "started") before any API call. Loop-risk is bounded by
the probe-wake cadence (Kora only sees the envelope offer when a
probe wake actually fired) + the env gate's fail-CLOSED default.

Other mutating tools remain available to OTHER agents via
``/mcp`` (with capability gating per KR-MCP-RUNTIME-SURFACE ST2).
The architectural distinction holds: **Kora REASONS in her DM
thread; AGENTS DRIVE her via MCP.** The two reasoning-side
mutating tools are narrow exceptions where the scope is bound by
non-Kora canonical truth (Joshua's verified email address; the
operator-set envelope env). General-purpose mutating tools (e.g.
``kora__send_email``, ``kora__create_sea_ticket``,
``kora__request_state_transition``) stay excluded.

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

# Read-only tools Kora can call mid-reasoning + one operator-pinned
# mutating tool (kora__send_email_to_operator, KR-EMAIL-OUTBOUND-
# COMPOSE-TOOL — see module docstring for the scope-expansion
# rationale). Names match ``mcp_tools.TOOL_DESCRIPTORS`` /
# ``ST2_TOOL_DESCRIPTORS`` entries exactly. Order is the advertised
# order in the Anthropic ``tools`` array (cosmetic; Claude picks
# tools by name not position).
REASONING_TOOL_ALLOWLIST: List[str] = [
    "kora__get_operational_state",
    "kora__get_health_rollup",
    "kora__get_recent_ledger_entries",
    "kora__list_active_sea_tickets",
    "kora__get_recent_chain_events",
    # KR-EMAIL-OUTBOUND-COMPOSE-TOOL — operator-pinned email send.
    # Mutating but recipient-locked; see module docstring.
    "kora__send_email_to_operator",
    # KR-PROBE-AUTOFIX-EXECUTION — envelope-gated probe autofix.
    # Mutating but bound by per-probe env gate (default OFF,
    # fail-CLOSED) + envelope action whitelist + per-probe
    # executor target verification. See module docstring.
    "kora__attempt_probe_autofix",
]


# Tools in the allowlist that are MUTATING + take the (params,
# caller) ST2 dispatcher signature. Engine-side reasoning calls
# get a synthetic Caller below; the dispatcher's own audit
# emission attributes the call to that synthetic actor_kind.
_REASONING_MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        "kora__send_email_to_operator",
        "kora__attempt_probe_autofix",
    }
)

# Synthetic actor_kind used when the reasoning engine invokes a
# mutating tool from the allowlist. Distinct from "anonymous" so
# the audit trail attributes the action correctly, and distinct
# from any real MCP caller in mcp_callers.yaml so external
# callers can't impersonate it.
_REASONING_SELF_ACTOR_KIND = "kora_reasoning_self"


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
    # engine. Pull from BOTH descriptor lists (ST1 read-only +
    # ST2 mutating) so the operator-pinned email tool is
    # advertised to Claude alongside the read tools.
    from kora_cli.listeners.mcp_tools import (
        ST2_TOOL_DESCRIPTORS,
        TOOL_DESCRIPTORS,
    )

    by_name = {
        desc["name"]: desc
        for desc in (*TOOL_DESCRIPTORS, *ST2_TOOL_DESCRIPTORS)
    }

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
            f"tool {name!r} is not in the reasoning allowlist. "
            f"Available: {REASONING_TOOL_ALLOWLIST}"
        )

    # Mutating-tool path (KR-EMAIL-OUTBOUND-COMPOSE-TOOL): route
    # through ST2_TOOL_DISPATCH with a synthetic Caller. The
    # synthetic caller's allowed_caps contains only the single
    # tool being invoked, so even if a future executor adds a
    # ``caller.allows(other_tool)`` check it'll fail closed.
    if name in _REASONING_MUTATING_TOOLS:
        from kora_cli.listeners.mcp_caller_auth import Caller
        from kora_cli.listeners.mcp_tools import ST2_TOOL_DISPATCH

        st2_dispatcher = ST2_TOOL_DISPATCH.get(name)
        if st2_dispatcher is None:
            raise ReasoningToolNotAllowed(
                f"tool {name!r} is in the reasoning mutating "
                f"allowlist but has no ST2 dispatcher — "
                f"mcp_tools.ST2_TOOL_DISPATCH drift"
            )
        synthetic = Caller(
            actor_kind=_REASONING_SELF_ACTOR_KIND,
            allowed_caps=frozenset({name}),
        )
        return await st2_dispatcher(tool_input, synthetic)

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
