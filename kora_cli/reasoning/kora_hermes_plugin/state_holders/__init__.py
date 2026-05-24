"""State-holders sub-plugin — KR-PLUGIN-STATE-HOLDERS.

Owns the ``on_session_start`` Hermes hook handler that the
orchestrator previously hosted. The state-holder modules
themselves (``agent/cost_state_holder.py``,
``agent/operational_state_holder.py``) stay at their canonical
locations — they're cross-cutting daemon state, not Kora-plugin-
specific, and DaemonCoordinator owns the actual ``init_*_holder``
call sequence at process boot.

This sub-plugin owns:
  - The ``on_session_start`` hook handler (today: no-op debug
    log; future: per-session liveness check / re-init plumbing)
  - The :data:`STATE_HOLDER_ACCESSORS` registry — declarative
    map of which holders the plugin layer cares about, so
    future wiring has a single source of truth.
"""

from kora_cli.reasoning.kora_hermes_plugin.state_holders.plugin import (
    _on_session_start,
    register,
)
from kora_cli.reasoning.kora_hermes_plugin.state_holders.registry import (
    STATE_HOLDER_ACCESSORS,
    holder_liveness,
)

__all__ = [
    "STATE_HOLDER_ACCESSORS",
    "_on_session_start",
    "holder_liveness",
    "register",
]
