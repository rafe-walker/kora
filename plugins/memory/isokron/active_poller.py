"""Process-wide accessor for the live ``SeaTicketPoller`` (KR-P2-L ST1).

Mirrors :mod:`plugins.memory.isokron.active_provider` for the
gateway-level SeaTicketPoller: the
:func:`build_and_start_sea_ticket_poller` lifecycle helper registers
the running instance here, and cross-cutting consumers
(health-rollup holder + admin-panel endpoints) read it.

# Why a separate singleton vs. attaching to the provider

The provider is the long-lived substrate-access surface; the poller
is a service running on top of the provider. Attaching the poller
to the provider would couple two distinct lifecycles. Following the
existing ``active_provider`` pattern keeps each cross-cutting object
in its own accessor module so test-isolation is trivial.

# Lifetime

* :func:`set_active_poller` runs ONCE at gateway boot from the
  poller lifecycle after ``build_and_start_sea_ticket_poller``
  succeeds.
* :func:`get_active_poller` returns the cached reference (or
  ``None`` if no poller is running — typical for early-boot,
  agent-session-only contexts, or isolated tests).
* :func:`clear_active_poller` is for tests only.

# Why ``Any`` typing

To avoid a circular import: ``sea_ticket_poller.py`` already
imports from sibling modules (control reader, ledger, heartbeat),
and the active-provider singleton next door uses ``Any`` for the
same reason. Callers should runtime-check before assuming the
returned object exposes a particular SeaTicketPoller method.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


_active_poller: Optional[Any] = None


def set_active_poller(poller: Any) -> None:
    """Register the gateway-level poller as the process-wide active one.

    Idempotent for the same instance; logs WARNING + replaces if a
    different instance is set (gateway shouldn't replace mid-life).
    """
    global _active_poller
    if _active_poller is poller:
        return
    if _active_poller is not None:
        logger.warning(
            "[active_poller] replacing previously-set active "
            "SeaTicketPoller — this is unexpected outside test reset "
            "paths."
        )
    _active_poller = poller


def get_active_poller() -> Optional[Any]:
    """Return the gateway-level poller, or ``None`` if none registered."""
    return _active_poller


def clear_active_poller() -> None:
    """Test-only: reset the singleton."""
    global _active_poller
    _active_poller = None
