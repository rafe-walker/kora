"""Caching sub-plugin — standalone caching hook + ``register()``.

KR-PLUGIN-CACHING (Deliverable B of KR-PLUGIN-EXTRACTIONS-BATCH-2).

# Hook ownership status

The bundled ``pre_api_request_mutable`` handler in
``cost_ladder/plugin.py`` continues to do BOTH cost-ladder model
selection AND caching wrap in a single hook fire — the
intentional v1 bundling documented in that module.

This file provides :func:`caching_hook` as the standalone
caching-only handler that a future split (e.g. when
KR-HERMES-LOCAL-EXT-REISSUE motivates one) can register as a
SECOND ``pre_api_request_mutable`` handler. Today
``register(ctx)`` is **intentionally a no-op**: registering
``caching_hook`` alongside the bundled cost-ladder hook would
double-fire the caching wrap. The clean module boundary +
canonical marker location is the value extraction this PR
ships.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _is_kora_call(route_value: Any) -> bool:
    """Mirror the orchestrator's gate. Lazy import to break a
    potential circle on plugin-discovery load order."""
    try:
        from kora_runtime.plugin import (
            _is_kora_call as _check,
        )

        return _check(route_value)
    except Exception:
        return False


def caching_hook(
    *,
    route: str = "",
    api_kwargs: Optional[dict] = None,
    **kw,
) -> Optional[dict]:
    """Standalone caching ``pre_api_request_mutable`` handler.

    Wraps ``api_kwargs['system']`` (if str) + ``api_kwargs['tools']``
    (if non-empty list) with ``cache_control: ephemeral`` markers
    so Anthropic caches them. Returns ``{"override": {...}}`` with
    the new ``system`` + ``tools`` shape, or ``None`` when nothing
    is wrappable / route isn't Kora.

    NOT currently registered — the bundled cost-ladder hook does
    this work in v1. Available for a future split.
    """
    if not _is_kora_call(route):
        return None
    if not isinstance(api_kwargs, dict):
        return None

    from kora_runtime.caching.markers import (
        _wrap_system_as_cacheable,
        _wrap_tools_as_cacheable,
    )

    override: dict = {}

    existing_system = api_kwargs.get("system")
    if isinstance(existing_system, str) and existing_system:
        override["system"] = _wrap_system_as_cacheable(existing_system)

    existing_tools = api_kwargs.get("tools") or []
    if isinstance(existing_tools, list) and existing_tools:
        override["tools"] = _wrap_tools_as_cacheable(existing_tools)

    if not override:
        return None
    return {"override": override}


def register(ctx) -> None:
    """Sub-register — intentionally a no-op today.

    The bundled cost-ladder handler at ``cost_ladder/plugin.py``
    continues to own the single ``pre_api_request_mutable`` fire
    (with the caching wrap inlined). Registering
    :func:`caching_hook` here would double-fire the cache marker
    application. Kept as a no-op so the orchestrator's
    ``register_caching(ctx)`` call exists for symmetry — the
    boundary is in place, ready for a future split.
    """
    # Intentionally no ctx.register_hook call. See module docstring.
    logger.debug(
        "[kora_hermes.caching] sub-plugin registered (no-op; "
        "bundled with cost_ladder hook in v1)"
    )
