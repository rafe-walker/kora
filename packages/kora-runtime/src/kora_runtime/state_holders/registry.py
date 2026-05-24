"""Registry of state-holders the kora_hermes plugin observes.

# Source of truth, not source of init

The DaemonCoordinator (``kora_cli/daemon/coordinator.py``) is
the canonical owner of ``init_*_holder`` calls at process boot.
This registry documents which holders the Hermes plugin layer
**reads from** so future hook wiring (e.g. a per-session
liveness probe) has a single declarative list to walk.

Adding a holder here does NOT initialize it — initialization
sequencing remains DaemonCoordinator's responsibility.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)


def _import_cost_holder_getter() -> Optional[Callable]:
    try:
        from agent.cost_state_holder import get_cost_holder

        return get_cost_holder
    except Exception:
        return None


def _import_operational_state_holder_getter() -> Optional[Callable]:
    try:
        from agent.operational_state_holder import (
            get_operational_state_holder,
        )

        return get_operational_state_holder
    except Exception:
        return None


# Declarative registry of holder accessors. Keys are stable
# operator-facing names (used in observability output). Values
# are lazy getters that return the live holder OR ``None`` when
# the holder hasn't been initialized yet (pre-boot / test
# context).
STATE_HOLDER_ACCESSORS: Dict[str, Callable] = {
    "cost_state_holder": _import_cost_holder_getter() or (lambda: None),
    "operational_state_holder": (
        _import_operational_state_holder_getter() or (lambda: None)
    ),
}


def holder_liveness() -> Dict[str, bool]:
    """Probe each registered holder. Returns ``{name: alive}``
    where ``alive`` is True iff ``getter()`` returns non-None.

    Safe to call any time; used by the plugin's on-session-start
    hook for an info-log of holder state at session boot.
    """
    out: Dict[str, bool] = {}
    for name, getter in STATE_HOLDER_ACCESSORS.items():
        try:
            out[name] = getter() is not None
        except Exception as exc:
            logger.debug(
                "[kora_hermes.state_holders] %s getter raised %r — "
                "marking not-live",
                name,
                exc,
            )
            out[name] = False
    return out
