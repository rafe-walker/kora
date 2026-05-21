"""Process-wide accessor for the live ``IsoKronMemoryProvider`` (KR-P2-CLEANUP).

Admin-panel endpoints in ``kora_cli/web_server.py`` need a live, initialized
provider to query for real data (sea-tickets, kora_control, etc.). The
provider is constructed once at gateway startup
(``sea_ticket_poller_lifecycle.build_and_start_sea_ticket_poller``) — this
module exposes that instance to anything that needs it.

# Why module-level singleton

Mirrors the :mod:`agent.operational_state_holder` pattern: one initialized
provider per process, accessed by anything that needs to read substrate
state without going through an agent session. Web-server endpoints in
particular run outside any agent context.

# Why not lazy ``load_memory_provider("isokron")``

``plugins.memory.load_memory_provider`` returns a *fresh* uninitialized
provider on each call (the plugin loader's ``register(ctx)`` constructs a
new ``IsoKronMemoryProvider`` instance). Initializing it per-request
would open new asyncpg pools and MCP transports each time — expensive and
duplicative. The singleton holds the ALREADY-initialized provider so
endpoints can re-use its connection.

# Lifetime

* :func:`set_active_provider` runs ONCE at gateway boot, from the
  poller lifecycle after ``provider.initialize(...)`` succeeds.
* :func:`get_active_provider` returns the cached reference (or ``None``
  if the gateway hasn't initialized one — typical for early-boot or
  isolated test contexts).
* :func:`clear_active_provider` is for tests only.

# Compatibility with existing per-session providers

The per-agent provider constructed in :mod:`agent.agent_init` is unaffected
— each agent session still builds its own (the singleton isn't consulted
there). This module is strictly for cross-cutting endpoints that need
gateway-level read access.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


_active_provider: Optional[Any] = None


def set_active_provider(provider: Any) -> None:
    """Register the gateway-level provider as the process-wide active one.

    Idempotent in spirit: calling twice with the same instance is a no-op;
    calling with a different instance logs a WARNING and replaces (the
    most-recently-set wins; gateway shouldn't replace mid-life).
    """
    global _active_provider
    if _active_provider is provider:
        return
    if _active_provider is not None:
        logger.warning(
            "[active_provider] replacing previously-set active "
            "IsoKronMemoryProvider — this is unexpected outside test "
            "reset paths."
        )
    _active_provider = provider


def get_active_provider() -> Optional[Any]:
    """Return the gateway-level provider, or ``None`` if none registered."""
    return _active_provider


def clear_active_provider() -> None:
    """Test-only: reset the singleton."""
    global _active_provider
    _active_provider = None
