"""Cost-ladder plugin module — hook handler + sub-register.

The ``pre_api_request_mutable`` hook handler that drives Kora's
default-Haiku-with-earned-Opus model selection + the prompt-
caching ``cache_control: ephemeral`` markers on system + last
tool. Per KR-PLUGIN-COST-LADDER, this is the first plugin
extraction sub-module; the same pattern applies to follow-on
KR-PLUGIN-* buckets.

# Bundled responsibility (transitional)

Today's handler bundles cost-ladder model selection AND prompt-
caching cache_control wrapping. KR-PLUGIN-CACHING will extract
the caching half into its own ``caching/`` sub-plugin file.
Until then, both behaviors live here. The bundling is
intentional for v1: both fire at the same hook site
(``pre_api_request_mutable``), both build the same
``{"override": {...}}`` return shape, and splitting now would
add a second hook fire without behavior benefit.

# Activation gate

The handler short-circuits to ``None`` (no override) when the
call isn't a Kora-tagged route — protects Hermes-fork users who
load the plugin from inadvertently running Kora logic on their
sessions. Uses the same ``_is_kora_call`` predicate as the
top-level plugin's other hook handlers.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _is_kora_call(route_value: Any) -> bool:
    """Mirror of the top-level plugin's KORA_ROUTES gate. Imported
    locally to avoid a circular import from
    ``plugins.kora_hermes`` (the top-level Hermes-discovery
    entry point); the route literal set is small + stable so the
    duplication cost is low + the import isolation is worth it."""
    # Lazy import to break a potential circle.
    from plugins.kora_hermes import KORA_ROUTES

    if not isinstance(route_value, str) or not route_value:
        return False
    return route_value in KORA_ROUTES


# ---------------------------------------------------------------------------
# Hook handler: pre_api_request_mutable
# ---------------------------------------------------------------------------


def cost_ladder_and_caching_hook(
    *,
    route: str = "",
    api_kwargs: Optional[dict] = None,
    api_call_count: int = 0,
    user_message: str = "",
    **kw,
) -> Optional[dict]:
    """``pre_api_request_mutable`` handler.

    For Kora-tagged calls:
      1. Call the cost-router (``select_model_pre_call``) to
         pick Haiku-default-or-Opus-earned based on iteration +
         decision-language + cost rung + force-Opus env signals.
      2. Wrap ``system`` + ``tools`` with
         ``cache_control: ephemeral`` markers so Anthropic
         caches them (KR-CHEAP-PROMPT-CACHING semantic via the
         hook layer rather than the bypass loop's inline wrap).

    Returns ``{"override": {...}}`` with the keys to replace in
    api_kwargs. ``None`` (no-op) for non-Kora calls.
    """
    if not _is_kora_call(route):
        return None

    if not isinstance(api_kwargs, dict):
        return None

    override: dict = {}

    # --- Cost-ladder model selection ---
    # Use the (now-extracted) selector. Hermes's
    # ``api_call_count`` is 1-indexed per-call; matches Kora's
    # iteration count from the bypass loop. cost_rung comes from
    # the process-global CostStateHolder.
    try:
        from kora_runtime.cost_ladder.selector import (
            select_model_pre_call,
        )

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
            "[kora_hermes.cost_ladder] select_model_pre_call raised "
            "%r — leaving api_kwargs['model'] unchanged",
            exc,
        )

    # --- Caching: wrap system + tools with cache_control markers ---
    # KR-PLUGIN-EXTRACTIONS-BATCH-2 (Deliverable B) — markers now
    # live at the canonical caching sub-plugin location, NOT in
    # anthropic_engine.py. Cleans the v1 cross-dep.
    try:
        from kora_runtime.caching.markers import (
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
            "[kora_hermes.cost_ladder] caching wrap raised %r — "
            "leaving api_kwargs unchanged",
            exc,
        )

    if not override:
        return None
    return {"override": override}


# ---------------------------------------------------------------------------
# Sub-register
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Sub-plugin register. Called by the top-level
    ``KoraHermesPlugin.register`` to wire the cost-ladder hook.

    The top-level plugin owns plugin-context ownership; this
    sub-register is just a thin function that calls
    ``ctx.register_hook`` for the one hook this sub-plugin
    owns. Keeps the file structure clean for the eventual
    upstream PR (each sub-plugin is independently packageable).
    """
    ctx.register_hook(
        "pre_api_request_mutable", cost_ladder_and_caching_hook
    )
    logger.debug(
        "[kora_hermes.cost_ladder] sub-plugin registered"
    )
