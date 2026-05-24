"""Top-level KoraHermesPlugin — orchestrates sub-plugin registration.

Per KR-PLUGIN-COST-LADDER: this is the canonical Kora-side home
for the orchestration logic. Each sub-plugin (cost_ladder/,
future audit/, future caching/, etc.) owns its own hook
handlers + ``register()`` function; the orchestrator just
calls each sub-register so the top-level Hermes plugin entry
(``plugins/kora_hermes/__init__.py``) is a thin shim.

Sub-plugin extraction order (follow-on buckets):
  1. cost_ladder — **THIS BUCKET** (KR-PLUGIN-COST-LADDER, #182)
  2. audit — KR-PLUGIN-AUDIT (recommended next per CC#3 ST1)
  3. caching — KR-PLUGIN-CACHING (split caching from cost_ladder)
  4. short_circuit — KR-PLUGIN-SHORT-CIRCUIT
  5. state_holders — KR-PLUGIN-STATE-HOLDERS

Until each sub-plugin extraction lands, the corresponding hook
handler lives in this orchestrator file (the
``_on_session_start`` / ``_pre_tool_list_finalized`` /
``_pre_tool_call`` / ``_post_tool_call`` / ``_post_llm_call`` +
the ST2B tool-bridge ``_tool_bridge_provide_result``).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

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
# Hook handlers that haven't been extracted to their own sub-plugin yet
# ---------------------------------------------------------------------------


def _on_session_start(*, route: str = "", **kw) -> None:
    """Fires once when Hermes starts a conversation_loop session.
    For Kora calls, ensures state holders are initialized. Today:
    no-op (state holders init at daemon boot via DaemonCoordinator;
    this hook may become relevant once KR-PLUGIN-STATE-HOLDERS
    wires per-session Kora init)."""
    if not _is_kora_call(route):
        return
    logger.debug(
        "[kora_hermes] on_session_start fired for route=%s (no-op)",
        route,
    )


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


def _post_tool_call(
    *,
    tool_name: str = "",
    result: Any = None,
    **kw,
) -> None:
    """Audit emit per tool call. Today: no-op (Kora's existing
    audit runs inside `_execute_single_tool_block` in the bypass
    loop; KR-PLUGIN-AUDIT will wire this to call ``_emit_audit``
    directly)."""
    route = kw.get("route", "") or ""
    if not _is_kora_call(route):
        return
    logger.debug(
        "[kora_hermes] post_tool_call fired for tool=%s route=%s "
        "(no-op)",
        tool_name,
        route,
    )


def _post_llm_call(
    *,
    route: str = "",
    model: str = "",
    **kw,
) -> None:
    """Structured-log marker for the per-call cost-telemetry
    timeline. Per-call ``CanonicalUsage`` accumulation lives at
    the handler layer (slack_dm_handler's
    ``_record_inference_to_cost_ladder``); KR-PLUGIN-AUDIT will
    wire this hook to emit the audit JSONL row from the plugin
    instead.

    Note: ``post_llm_call`` fires per CONVERSATION END (not per
    API roundtrip).
    """
    if not _is_kora_call(route):
        return
    logger.info(
        "[kora.gateway.post_llm_call] route=%s model=%s",
        route,
        model or "<unknown>",
    )


# ---------------------------------------------------------------------------
# KR-REASONING-ROUTE-THROUGH-GATEWAY-ST2B — tool bridge
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
    handling rationale. Behavior preserved verbatim by this
    refactor.
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

    Per KR-PLUGIN-COST-LADDER: each sub-plugin owns its hook
    callbacks + sub-register. The orchestrator calls each sub-
    register, then registers any remaining (not-yet-extracted)
    handlers itself. Future extractions move handlers OUT of
    this orchestrator INTO their own sub-plugin files.
    """

    def register(self, ctx) -> None:
        # --- Sub-plugin registration (each owns its hooks) ---
        # KR-PLUGIN-COST-LADDER (first extraction).
        from kora_cli.reasoning.kora_hermes_plugin.cost_ladder import (
            register as register_cost_ladder,
        )

        register_cost_ladder(ctx)

        # --- Handlers still living in the orchestrator (await
        # their own KR-PLUGIN-* extraction buckets) ---
        ctx.register_hook("on_session_start", _on_session_start)
        ctx.register_hook(
            "pre_tool_list_finalized", _pre_tool_list_finalized
        )
        ctx.register_hook("pre_tool_call", _pre_tool_call)
        ctx.register_hook("post_tool_call", _post_tool_call)
        ctx.register_hook("post_llm_call", _post_llm_call)
        ctx.register_hook(
            "pre_tool_call_can_provide_result",
            _tool_bridge_provide_result,
        )

        logger.info(
            "[kora_hermes] plugin registered: cost_ladder sub-plugin + "
            "6 orchestrator-resident hooks against KORA_ROUTES=%s",
            sorted(KORA_ROUTES),
        )


def register(ctx) -> None:
    """Module-level register — what the Hermes discovery shim
    calls. Instantiates the plugin + delegates to its register."""
    KoraHermesPlugin().register(ctx)
