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
    **kw,
) -> Optional[dict]:
    """KR-HERMES-LOCAL-EXTENSIONS hook. For Kora calls, delegate
    to the router for model selection + caching markers.

    ST1: returns None (no override). ST2 will delegate to
    ``kora_hermes_plugin.cost_ladder_and_caching`` which calls
    ``cost_router.select_model_pre_call(...)`` + adds
    ``cache_control: ephemeral`` to system + last tool.
    """
    if not _is_kora_call(route):
        return None
    logger.debug(
        "[kora_hermes] pre_api_request_mutable fired for route=%s "
        "(ST1: no override; ST2 will plumb cost-router + caching)",
        route,
    )
    return None


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
    **kw,
) -> None:
    """Per-call cost-ladder write + telemetry record_call. ST1:
    no-op (Kora's handler today calls _record_inference_to_cost_
    ladder directly; ST2 wires this to the same code)."""
    if not _is_kora_call(route):
        return
    logger.debug(
        "[kora_hermes] post_llm_call fired for route=%s (ST1 no-op)",
        route,
    )


# ---------------------------------------------------------------------------
# Plugin entry point — called once at plugin discovery
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Plugin entry. Called by ``PluginManager.discover_and_load``
    once at process startup. Registers Kora behaviors against the 7
    Hermes hooks Kora's reasoning path needs."""
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("pre_api_request_mutable", _pre_api_request_mutable)
    ctx.register_hook("pre_tool_list_finalized", _pre_tool_list_finalized)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("post_llm_call", _post_llm_call)
    logger.info(
        "[kora_hermes] plugin registered: 6 hooks against KORA_ROUTES=%s",
        sorted(KORA_ROUTES),
    )
