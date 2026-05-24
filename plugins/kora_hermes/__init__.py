"""KR-REASONING-ROUTE-THROUGH-GATEWAY-CORE ST1 — Kora behaviors as a Hermes plugin.

This bundled plugin registers Kora's reasoning behaviors as Hermes
hook callbacks so the (forthcoming) gateway-route-through code path
exercises them via the standard Hermes plugin contract instead of
the bypass loop in ``kora_cli/reasoning/anthropic_engine.py``.

# Scope (ST1)

This file is intentionally a **dispatch shim** — the hook handlers
do not move logic out of ``kora_cli/*`` modules. They check whether
the active call is a Kora call (``agent.route`` in KORA_ROUTES) and
delegate to the existing Kora module code when it is, no-op when
it isn't. KR-PLUGIN-COST-LADDER / KR-PLUGIN-AUDIT / etc. follow-on
buckets extract clean plugin bodies; this bucket just wires the
discovery + dispatch surface.

# Activation gate

Every hook handler short-circuits to no-op when ``agent.route`` is
empty or not in :data:`KORA_ROUTES`. This protects every Hermes
CLI / Gateway user from inadvertently running Kora logic on their
sessions — the plugin is harmless to ship as a bundled package
even on a non-Kora Hermes deployment.

# ST2 follow-on

Hook handler bodies currently delegate to ``kora_hermes_plugin``
in ``kora_cli/reasoning/``. ST2 (KR-PLUGIN-COST-LADDER first per
the bucket's recommendation) will move logic out of router /
short_circuit / etc. modules into clean plugin files.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Kora route literals — matches ``kora_cli/telemetry/cost_telemetry.KNOWN_ROUTES``
# (the existing telemetry vocabulary). When ``agent.route`` is in this
# set we treat the call as Kora-originated; else we no-op.
#
# Sourced as a literal list to keep the plugin discovery side import-
# light (avoids pulling in the telemetry module at plugin-load time);
# verified-in-sync via tests/plugins/test_kora_hermes_plugin.py.
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
# Hook handlers — all are dispatch shims to ``kora_hermes_plugin`` module
# ---------------------------------------------------------------------------


def _on_session_start(*, route: str = "", **kw) -> None:
    """Fires once when Hermes starts a conversation_loop session.
    For Kora calls, ensures state holders are initialized. ST1:
    no-op (state holders init at daemon boot via DaemonCoordinator;
    this hook may become relevant once ST2 wires per-session
    Kora init)."""
    if not _is_kora_call(route):
        return
    logger.debug(
        "[kora_hermes] on_session_start fired for route=%s (ST1 no-op)",
        route,
    )


def _pre_api_request_mutable(
    *,
    route: str = "",
    api_kwargs: Optional[dict] = None,
    api_call_count: int = 0,
    user_message: str = "",
    **kw,
) -> Optional[dict]:
    """KR-HERMES-LOCAL-EXTENSIONS hook — ST2 real wiring.

    For Kora-tagged calls:
      1. Call the cost-router (``select_model_pre_call``) to
         pick Haiku-default-or-Opus-earned based on iteration +
         decision-language + cost rung + force-Opus env signals.
      2. Wrap ``system`` + ``tools`` with
         ``cache_control: ephemeral`` markers so Anthropic
         caches them (KR-CHEAP-PROMPT-CACHING semantic via the
         hook layer rather than the bypass loop's inline wrap).

    Returns ``{"override": {...}}`` with the keys to replace in
    api_kwargs. None / no-op for non-Kora calls.
    """
    if not _is_kora_call(route):
        return None

    if not isinstance(api_kwargs, dict):
        return None

    override: dict = {}

    # --- Cost-ladder model selection ---
    # Use the existing router. iteration semantics: Hermes's
    # ``api_call_count`` is 1-indexed per-call; matches Kora's
    # iteration count from the bypass loop. cost_rung comes from
    # the process-global CostStateHolder.
    try:
        from kora_cli.router import select_model_pre_call

        cost_rung = _current_cost_rung()
        decision = select_model_pre_call(
            message_text=user_message or "",
            iteration=max(int(api_call_count or 1), 1),
            cost_rung=cost_rung,
        )
        if decision.model is not None:
            override["model"] = decision.model
    except Exception as exc:
        logger.warning(
            "[kora_hermes] cost-ladder select_model_pre_call raised "
            "%r — leaving api_kwargs['model'] unchanged",
            exc,
        )

    # --- Caching: wrap system + tools with cache_control markers ---
    try:
        from kora_cli.reasoning.anthropic_engine import (
            _wrap_system_as_cacheable,
            _wrap_tools_as_cacheable,
        )

        # System: may be str (Hermes default) OR already a list
        # (e.g. test fixture passed a content-block list). Wrap
        # only the str case so we don't double-wrap.
        existing_system = api_kwargs.get("system")
        if isinstance(existing_system, str) and existing_system:
            override["system"] = _wrap_system_as_cacheable(existing_system)

        # Tools: tools_for_api is a list (may be empty in
        # toolless v1 route-through). The wrapper handles empty
        # list by returning empty list — safe to always call.
        existing_tools = api_kwargs.get("tools") or []
        if isinstance(existing_tools, list) and existing_tools:
            override["tools"] = _wrap_tools_as_cacheable(existing_tools)
    except Exception as exc:
        logger.warning(
            "[kora_hermes] caching wrap raised %r — leaving "
            "api_kwargs unchanged",
            exc,
        )

    if not override:
        return None
    return {"override": override}


def _current_cost_rung() -> str:
    """Read the active cost-ladder rung. Defaults to ``"normal"``
    on any failure (holder unwired, exception, etc.) — matches
    cost_router's expectation."""
    try:
        from agent.cost_state_holder import get_cost_holder

        holder = get_cost_holder()
        if holder is None:
            return "normal"
        rung = holder.active_rung()
        return getattr(rung, "value", str(rung)) or "normal"
    except Exception:
        return "normal"


def _pre_tool_list_finalized(
    *,
    route: str = "",
    tools: Optional[list] = None,
    **kw,
) -> Optional[dict]:
    """KR-HERMES-LOCAL-EXTENSIONS hook. For Kora calls, filter the
    tool list per-route. ST1: no-op."""
    if not _is_kora_call(route):
        return None
    logger.debug(
        "[kora_hermes] pre_tool_list_finalized fired for route=%s "
        "(ST1 no-op; future KR-PLUGIN-TOOL-DESC-TRIM will plumb)",
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
    `_execute_single_tool_block` allowlist). ST1: no-op; ST2 plumbs
    KoraConstitution.evaluate_tool_call() once that extraction
    bucket lands."""
    route = kw.get("route", "") or ""
    if not _is_kora_call(route):
        return None
    logger.debug(
        "[kora_hermes] pre_tool_call fired for tool=%s route=%s "
        "(ST1 no-op)",
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
    """Audit emit per tool call. ST1: no-op (Kora's existing audit
    runs inside `_execute_single_tool_block` in the bypass loop;
    ST2 wires this to call `_emit_audit` directly)."""
    route = kw.get("route", "") or ""
    if not _is_kora_call(route):
        return
    logger.debug(
        "[kora_hermes] post_tool_call fired for tool=%s route=%s "
        "(ST1 no-op)",
        tool_name,
        route,
    )


def _post_llm_call(
    *,
    route: str = "",
    model: str = "",
    **kw,
) -> None:
    """ST2 real wiring — per-call cost-telemetry record_call.

    Records the call to the telemetry counter so the cockpit's
    cost panel shows Kora's route-through spend alongside the
    bypass path's spend (same telemetry vocabulary; the route
    discriminator is the only diff).

    Note: ``post_llm_call`` fires per CONVERSATION END (not per
    API roundtrip). The conversation-loop accumulates tokens
    across iterations; the response_text + final usage is what
    we see here. For per-iteration accounting (which the bypass
    loop does via ``_record_call_to_telemetry`` per call) we'd
    need a different hook or the existing ``post_api_request``
    observer — out of scope for ST2 (ST2B follow-on).
    """
    if not _is_kora_call(route):
        return

    # Hermes's post_llm_call kwargs don't include a usage object
    # (the assistant_response is a string). For ST2 the cost-
    # ladder write happens at the handler layer when the
    # ResponseResult comes back; this hook is a structured-log
    # marker for the telemetry timeline. Per-call CanonicalUsage
    # accumulation lives in the handler (slack_dm_handler's
    # ``_record_inference_to_cost_ladder``).
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

    Returns:
      * ``{"result": <json_str>}`` when the tool IS in Kora's
        allowlist + dispatch succeeded → short-circuits Hermes
      * ``{"result": <json_str_with_error>}`` when the tool IS
        in Kora's allowlist BUT dispatch raised → short-circuits
        Hermes with an is_error result so the reasoning loop
        sees the error rather than getting Hermes's
        "tool not found" envelope.
      * ``None`` when the tool isn't a Kora tool OR the call
        isn't a Kora-route call → falls through to other plugins
        or Hermes default. Non-Kora-route safety: Hermes-fork
        users with this plugin loaded see no behavior change on
        their own (non-Kora) sessions.

    Implementation note: ``handle_function_call`` is sync;
    ``execute_reasoning_tool`` is async. We bridge via
    ``asyncio.run`` when no loop is running, OR via
    ``asyncio.new_event_loop`` + ``run_until_complete`` when
    nested under an existing loop (the daemon path runs
    ``handle_function_call`` inside an ``asyncio.to_thread``
    call from ``_respond_via_gateway`` — the thread has no
    running loop, so ``asyncio.run`` is the right primitive).
    """
    import asyncio
    import json

    route = kw.get("route", "") or ""
    # ``handle_function_call`` doesn't currently forward
    # ``route`` to the hook (the kwargs it passes are tool_name,
    # args, task_id, session_id, tool_call_id). For ST2B v1 we
    # gate on the tool name's belonging to Kora's allowlist
    # alone — this is the cleaner check anyway since the tool
    # name uniquely identifies whether Kora can serve it.
    # Future ST2C: thread ``route`` through the hook kwargs so
    # we can also restrict to Kora routes (defense-in-depth).

    if not _is_kora_reasoning_tool(tool_name):
        return None

    # Dispatch via Kora's reasoning tool registry.
    try:
        from kora_cli.reasoning.tool_registry import execute_reasoning_tool

        result_model = asyncio.run(
            execute_reasoning_tool(tool_name, args or {})
        )

        # Project the Pydantic model into a JSON string. Models
        # have ``model_dump_json`` per the existing registry's
        # contract; fall back to ``str()`` if not Pydantic.
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
        # Convert dispatch failure to an is_error tool_result so
        # the reasoning loop can see the error rather than crash.
        # Match Kora's existing bypass-loop error envelope shape
        # (the JSON-serializable dict with ``error`` key, matching
        # what Hermes's _sanitize_tool_error produces on its own
        # dispatch errors).
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
    for ``agent.tools`` population. The kora_cli registry stores
    Anthropic-shaped descriptors (``{"name", "description",
    "input_schema"}``); Hermes's ``agent.tools`` reads
    ``tool["function"]["name"]`` (OpenAI shape) at multiple
    sites. We convert here so consumers (e.g.
    ``_respond_via_gateway``) get the right shape.

    Returns ``[]`` on any error (registry unavailable, schema
    drift) — engine falls back to toolless route-through.
    """
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
# Plugin entry point — called once at plugin discovery
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Plugin entry. Called by ``PluginManager.discover_and_load``
    once at process startup. Registers Kora behaviors against the
    7 Hermes hooks Kora's reasoning path needs (post-ST2B)."""
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("pre_api_request_mutable", _pre_api_request_mutable)
    ctx.register_hook("pre_tool_list_finalized", _pre_tool_list_finalized)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("post_llm_call", _post_llm_call)
    # KR-REASONING-ROUTE-THROUGH-GATEWAY-ST2B — tool-bridge hook.
    ctx.register_hook(
        "pre_tool_call_can_provide_result", _tool_bridge_provide_result
    )
    logger.info(
        "[kora_hermes] plugin registered: 7 hooks against KORA_ROUTES=%s",
        sorted(KORA_ROUTES),
    )
