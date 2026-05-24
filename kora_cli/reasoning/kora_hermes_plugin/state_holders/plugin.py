"""State-holders sub-plugin — ``on_session_start`` hook + ``register()``.

KR-PLUGIN-STATE-HOLDERS (Deliverable D of
KR-PLUGIN-EXTRACTIONS-BATCH-2).

Hosts the ``on_session_start`` handler that previously lived in
``kora_hermes_plugin/plugin.py``. The handler remains a debug-
log marker (no behavior change per the batch spec); the value
of this extraction is establishing the clean plugin boundary so
future per-session work (liveness probes, lazy-init flows) has
a canonical home.

# Holder ownership

The holders themselves (``agent/cost_state_holder.py`` +
``agent/operational_state_holder.py``) are NOT moved.
DaemonCoordinator owns ``init_cost_holder`` / ``init_operational_
state_holder`` at process boot; this plugin only READS them via
the :func:`...state_holders.registry.holder_liveness` probe.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _is_kora_call(route_value: Any) -> bool:
    """Mirror the orchestrator's gate. Lazy import to avoid a
    circular import (orchestrator imports this module via
    ``register_state_holders``)."""
    try:
        from kora_cli.reasoning.kora_hermes_plugin.plugin import (
            _is_kora_call as _check,
        )

        return _check(route_value)
    except Exception:
        return False


def _on_session_start(*, route: str = "", **kw) -> None:
    """Fires once when Hermes starts a conversation_loop session.
    For Kora calls, ensures state holders are initialized.

    Today: debug-log marker only. State holders init at daemon
    boot via DaemonCoordinator; this hook may become relevant
    once per-session Kora init is needed (e.g. lazy re-init when
    a holder becomes None mid-process).
    """
    if not _is_kora_call(route):
        return
    logger.debug(
        "[kora_hermes.state_holders] on_session_start fired for "
        "route=%s (no-op; DaemonCoordinator owns init)",
        route,
    )


def register(ctx) -> None:
    """Sub-register. The orchestrator calls this so the
    on_session_start handler attaches to the Hermes plugin
    context."""
    ctx.register_hook("on_session_start", _on_session_start)
