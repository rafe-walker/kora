"""Audit sub-plugin — hook handlers + ``register()``.

KR-PLUGIN-AUDIT (Deliverable A of KR-PLUGIN-EXTRACTIONS-BATCH-2).

Hosts the ``post_tool_call`` + ``post_llm_call`` Hermes hook
handlers that previously lived in
``kora_hermes_plugin/plugin.py``. The handlers themselves remain
no-op/debug-only — the actual reasoning-tool audit emit happens
inside ``anthropic_engine._execute_single_tool_block`` via the
moved :func:`_emit_tool_called_audit` (see ``writer.py``). This
sub-plugin's purpose is establishing the clean plugin boundary
so future wiring (e.g. routing the engine's audit emit through
the Hermes hook layer) has a canonical home.

Per the bucket spec: **no behavior change**.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _is_kora_call(route_value: Any) -> bool:
    """Lazy gate that mirrors the orchestrator's check. Imported
    lazily to avoid a circular import (orchestrator imports this
    module via ``register_audit``)."""
    try:
        from kora_cli.reasoning.kora_hermes_plugin.plugin import (
            _is_kora_call as _check,
        )

        return _check(route_value)
    except Exception:
        return False


def _post_tool_call(
    *,
    tool_name: str = "",
    result: Any = None,
    **kw,
) -> None:
    """Audit emit per tool call. Today: no-op debug-log only
    (Kora's existing audit runs inside
    ``_execute_single_tool_block`` in the bypass loop and via
    ``_emit_tool_called_audit`` in :mod:`writer`); future wiring
    will route engine audit through this hook."""
    route = kw.get("route", "") or ""
    if not _is_kora_call(route):
        return
    logger.debug(
        "[kora_hermes.audit] post_tool_call fired for tool=%s "
        "route=%s (no-op; engine emit path remains canonical)",
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
    ``_record_inference_to_cost_ladder``).

    Note: ``post_llm_call`` fires per CONVERSATION END (not per
    API roundtrip)."""
    if not _is_kora_call(route):
        return
    logger.info(
        "[kora.gateway.post_llm_call] route=%s model=%s",
        route,
        model or "<unknown>",
    )


def register(ctx) -> None:
    """Sub-register. The orchestrator calls this so the audit
    handlers attach to the Hermes plugin context."""
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("post_llm_call", _post_llm_call)
