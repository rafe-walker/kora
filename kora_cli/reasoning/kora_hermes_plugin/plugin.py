"""Top-level KoraHermesPlugin — orchestrates sub-plugin registration.

Per KR-PLUGIN-COST-LADDER (#185) the canonical Kora-side home
for plugin orchestration is this file. Each sub-plugin
(``cost_ladder/``, ``audit/``, ``caching/``, ``short_circuit/``,
``state_holders/``) owns its own hook handlers + ``register()``
function; the orchestrator just calls each sub-register so the
top-level Hermes plugin entry
(``plugins/kora_hermes/__init__.py``) is a thin shim.

# Sub-plugin landscape (post KR-PLUGIN-EXTRACTIONS-BATCH-2)

  1. cost_ladder — KR-PLUGIN-COST-LADDER (#185)
     Owns: ``pre_api_request_mutable`` (model selection + caching
     wrap — bundled handler).
  2. audit — KR-PLUGIN-AUDIT (this PR, Deliverable A)
     Owns: ``post_tool_call`` + ``post_llm_call`` (debug-log;
     reasoning-tool audit emit helper ``_emit_tool_called_audit``).
  3. caching — KR-PLUGIN-CACHING (this PR, Deliverable B)
     Owns: ``cache_control: ephemeral`` markers (used by cost-
     ladder's hook). Standalone ``caching_hook`` exists for a
     future split; intentionally not registered today.
  4. short_circuit — KR-PLUGIN-SHORT-CIRCUIT (this PR, Deliverable C)
     Owns: regex + snapshot interpolation phrasebook matcher.
     ``transform_input`` hook stub exists; intentionally not
     registered today (handler-side call path still owns it).
  5. state_holders — KR-PLUGIN-STATE-HOLDERS (this PR, Deliverable D)
     Owns: ``on_session_start`` (debug-log; holder liveness
     registry).
  6. haiku_router — KR-HAIKU-ROUTER-PLUGIN (KR-HERMES-LOCAL-EXT-
     REISSUE-AND-HAIKU-ROUTER-PLUGIN-PAIR — completes Lock R3-2
     Phase C). Owns: ``post_llm_call_can_reissue`` (the new
     local Hermes hook added by Deliverable A of the same
     bucket). Consumes ``should_escalate_post_call`` from the
     cost_ladder sub-plugin to fire parallel-Claude's Haiku-
     as-Opus-context escalation pattern.

# Remaining orchestrator-resident handlers

These hooks await their own extraction buckets:

  - ``pre_tool_list_finalized`` — KR-PLUGIN-TOOL-DESC-TRIM
  - ``pre_tool_call`` — KR-PLUGIN-CONSTITUTION
  - ``pre_tool_call_can_provide_result`` — KR-REASONING-ROUTE-
    THROUGH-GATEWAY-ST2B tool bridge (stays in orchestrator;
    deep integration with reasoning tool registry)
"""

from __future__ import annotations

import logging
from typing import Any, Optional

# Re-exports for backward-compat with downstream consumers (the
# discovery shim at ``plugins/kora_hermes/__init__.py`` + tests
# at ``tests/plugins/test_kora_hermes_plugin*.py`` import these
# names from this module).
from kora_cli.reasoning.kora_hermes_plugin.audit.plugin import (
    _post_llm_call,
    _post_tool_call,
)
from kora_cli.reasoning.kora_hermes_plugin.state_holders.plugin import (
    _on_session_start,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# KORA_ROUTES gate — re-exported from the discovery shim
# ---------------------------------------------------------------------------


# Kora route literals — matches ``kora_cli/telemetry/cost_telemetry.
# KNOWN_ROUTES`` (the existing telemetry vocabulary). When
# ``agent.route`` is in this set we treat the call as Kora-
# originated; else we no-op. Sourced as a literal to keep plugin
# discovery import-light (verified-in-sync by a pin test).
KORA_ROUTES = frozenset(
    {
        "slack_dm",
        "email_inbound",
        "email_outbound_compose",
        "mcp_tool",
        "alert_investigation",
        "probe_investigation",
        "tool_loop_iteration",
        "scheduled_task",
    }
)


def _is_kora_call(route_value: Any) -> bool:
    """Return True when the route kwarg signals a Kora-originated call."""
    if not isinstance(route_value, str) or not route_value:
        return False
    return route_value in KORA_ROUTES


# ---------------------------------------------------------------------------
# Orchestrator-resident hook handlers (not yet extracted)
# ---------------------------------------------------------------------------


def _pre_tool_list_finalized(
    *,
    route: str = "",
    tools: Optional[list] = None,
    **kw,
) -> Optional[dict]:
    """KR-HERMES-LOCAL-EXTENSIONS hook. For Kora calls, filter the
    tool list per-route. Today: no-op; future KR-PLUGIN-TOOL-
    DESC-TRIM will plumb route-specific tool manifests."""
    if not _is_kora_call(route):
        return None
    logger.debug(
        "[kora_hermes] pre_tool_list_finalized fired for route=%s "
        "(no-op; future KR-PLUGIN-TOOL-DESC-TRIM will plumb)",
        route,
    )
    return None


def _pre_tool_call(
    *,
    tool_name: str = "",
    args: Optional[dict] = None,
    **kw,
) -> Optional[dict]:
    """Constitution pre-screen (today lives in Kora-side
    `_execute_single_tool_block` allowlist). Today: no-op;
    KR-PLUGIN-CONSTITUTION will plumb KoraConstitution.evaluate_
    tool_call() once that extraction bucket lands."""
    route = kw.get("route", "") or ""
    if not _is_kora_call(route):
        return None
    logger.debug(
        "[kora_hermes] pre_tool_call fired for tool=%s route=%s "
        "(no-op)",
        tool_name,
        route,
    )
    return None


# ---------------------------------------------------------------------------
# KR-REASONING-ROUTE-THROUGH-GATEWAY-ST2B — tool bridge (stays in
# orchestrator — deep integration with reasoning tool registry)
# ---------------------------------------------------------------------------


def _is_kora_reasoning_tool(tool_name: str) -> bool:
    """True iff ``tool_name`` is one of Kora's reasoning-allowlist
    tools (``kora__*``). Import is lazy so plugin discovery
    doesn't fault when the registry isn't importable in CI."""
    try:
        from kora_cli.reasoning.tool_registry import REASONING_TOOL_ALLOWLIST
    except Exception:
        return False
    return tool_name in REASONING_TOOL_ALLOWLIST


def _tool_bridge_provide_result(
    *,
    tool_name: str = "",
    args: Optional[dict] = None,
    **kw,
) -> Optional[dict]:
    """Bridge handler for ``pre_tool_call_can_provide_result``.

    Intercepts dispatch for Kora's reasoning tools and returns
    the result the kora_cli reasoning code would have produced
    in the bypass loop. Hermes's default ``registry.dispatch``
    would otherwise fail (Kora's tools aren't registered as
    Hermes tools).

    See ``plugins/kora_hermes/__init__.py`` history (pre-
    KR-PLUGIN-COST-LADDER) for the full design + failure-mode
    handling rationale. Behavior preserved verbatim.
    """
    import asyncio
    import json

    if not _is_kora_reasoning_tool(tool_name):
        return None

    try:
        from kora_cli.reasoning.tool_registry import execute_reasoning_tool

        result_model = asyncio.run(
            execute_reasoning_tool(tool_name, args or {})
        )

        if hasattr(result_model, "model_dump_json"):
            result_str = result_model.model_dump_json()
        else:
            result_str = json.dumps(result_model, default=str)

        logger.debug(
            "[kora_hermes.tool_bridge] dispatched %s via Kora registry "
            "(result %d chars)",
            tool_name,
            len(result_str),
        )
        return {"result": result_str}
    except Exception as exc:
        error_msg = (
            f"kora_tool_dispatch_error: {type(exc).__name__}: {exc!s}"
        )
        logger.exception(
            "[kora_hermes.tool_bridge] dispatch raised for %s",
            tool_name,
        )
        return {"result": json.dumps({"error": error_msg})}


def get_kora_tools_for_agent() -> list:
    """Return Kora's reasoning tools in Hermes/OpenAI tool shape
    for ``agent.tools`` population. Behavior preserved verbatim
    by the KR-PLUGIN-COST-LADDER refactor."""
    try:
        from kora_cli.reasoning.tool_registry import (
            get_reasoning_available_tools,
        )

        anthropic_tools = get_reasoning_available_tools() or []
    except Exception as exc:
        logger.warning(
            "[kora_hermes.tool_bridge] tool registry unavailable: %r "
            "— agent.tools stays empty",
            exc,
        )
        return []

    hermes_tools: list = []
    for tool in anthropic_tools:
        try:
            hermes_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema", {}),
                    },
                }
            )
        except Exception as exc:
            logger.warning(
                "[kora_hermes.tool_bridge] tool %r conversion raised "
                "%r — skipping",
                tool.get("name", "<unknown>"),
                exc,
            )
    return hermes_tools


# ---------------------------------------------------------------------------
# Top-level plugin class
# ---------------------------------------------------------------------------


class KoraHermesPlugin:
    """Orchestrator that delegates to sub-plugins.

    Per KR-PLUGIN-EXTRACTIONS-BATCH-2: every Kora hook handler
    lives in a sub-plugin file. The orchestrator's ``register``
    walks each sub-register, then attaches the two remaining
    orchestrator-resident handlers (constitution pre-screen +
    tool-list-finalized stub) and the ST2B tool bridge.
    """

    def register(self, ctx) -> None:
        # --- Sub-plugin registration (each owns its hooks) ---
        # KR-PLUGIN-COST-LADDER (#185).
        from kora_cli.reasoning.kora_hermes_plugin.cost_ladder import (
            register as register_cost_ladder,
        )

        register_cost_ladder(ctx)

        # KR-PLUGIN-AUDIT (BATCH-2 Deliverable A) — owns
        # post_tool_call + post_llm_call.
        from kora_cli.reasoning.kora_hermes_plugin.audit import (
            register as register_audit,
        )

        register_audit(ctx)

        # KR-PLUGIN-CACHING (BATCH-2 Deliverable B) — owns the
        # cache_control markers used by cost-ladder. register()
        # is intentionally a no-op (cost-ladder bundled handler
        # still owns the single pre_api_request_mutable fire).
        from kora_cli.reasoning.kora_hermes_plugin.caching import (
            register as register_caching,
        )

        register_caching(ctx)

        # KR-PLUGIN-SHORT-CIRCUIT (BATCH-2 Deliverable C) — owns
        # the matcher. register() is intentionally a no-op
        # (slack DM handler owns short-circuit call path in v1).
        from kora_cli.reasoning.kora_hermes_plugin.short_circuit import (
            register as register_short_circuit,
        )

        register_short_circuit(ctx)

        # KR-PLUGIN-STATE-HOLDERS (BATCH-2 Deliverable D) — owns
        # on_session_start.
        from kora_cli.reasoning.kora_hermes_plugin.state_holders import (
            register as register_state_holders,
        )

        register_state_holders(ctx)

        # KR-HAIKU-ROUTER-PLUGIN — owns post_llm_call_can_reissue.
        # Consumes should_escalate_post_call from cost_ladder/
        # selector.py to fire post-call Opus escalation per
        # parallel-Claude's pattern. Registers against the new
        # local Hermes hook added by the paired Deliverable A.
        from kora_cli.reasoning.kora_hermes_plugin.haiku_router import (
            register as register_haiku_router,
        )

        register_haiku_router(ctx)

        # --- Handlers still living in the orchestrator (await
        # their own KR-PLUGIN-* extraction buckets) ---
        ctx.register_hook(
            "pre_tool_list_finalized", _pre_tool_list_finalized
        )
        ctx.register_hook("pre_tool_call", _pre_tool_call)
        ctx.register_hook(
            "pre_tool_call_can_provide_result",
            _tool_bridge_provide_result,
        )

        logger.info(
            "[kora_hermes] plugin registered: 6 sub-plugins + 3 "
            "orchestrator-resident hooks against KORA_ROUTES=%s",
            sorted(KORA_ROUTES),
        )


def register(ctx) -> None:
    """Module-level register — what the Hermes discovery shim
    calls. Instantiates the plugin + delegates to its register."""
    KoraHermesPlugin().register(ctx)
