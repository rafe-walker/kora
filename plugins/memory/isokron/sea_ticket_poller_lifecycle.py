"""SeaTicketPoller startup helper for the gateway (KR-P2-E ST5).

Encapsulates the steps gateway/run.py runs to bring the poller online:

1. Load the IsoKron memory provider via the plugin loader.
2. Check the provider is configured + available.
3. Initialize it with a fixed session_id (gateway-level, not chat-tied).
4. Construct the SeaTicketPoller wired to the provider's MCP client +
   asyncpg connection.
5. Spawn ``poller.run_forever()`` as a background asyncio task and
   return the (poller, task) pair so the caller can ``poller.stop()``
   on gateway shutdown.

# Fail-open honesty

Every failure path returns ``None`` + logs:

* IsoKron provider plugin not discoverable → log + skip
* Provider config missing (no ``plugins.entries.isokron`` block) →
  ``provider.is_available()`` returns False → log + skip
* Provider initialization raises (substrate unreachable, env vars
  missing) → log + skip
* MCP client construction raises → log + skip

In every skip case the gateway continues to serve platform adapters
(Slack, Discord, etc.) — the consumer loop just doesn't run. The
original bucket spec wanted a ``kora.startup.poller_disabled`` chain
event emitted on skip, but that literal is NOT in the substrate
vocab (``packages/db/migrations/foundation/0159…sql`` has no such
row). Surfaced for follow-on substrate vocab; runtime-side
observability today is the WARNING log line.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from plugins.memory.isokron.sea_ticket_poller import (
    AgentLoopInvoker,
    SeaTicketPoller,
)

logger = logging.getLogger(__name__)


_GATEWAY_POLLER_SESSION_ID = "gateway-sea-ticket-poller"


async def build_and_start_sea_ticket_poller(
    agent_loop_invoker: Optional[AgentLoopInvoker] = None,
) -> Optional[tuple[SeaTicketPoller, asyncio.Task]]:
    """Build + start the gateway-level SeaTicketPoller.

    Returns ``(poller, task)`` on success; ``None`` if the provider
    is unavailable or initialization fails. The caller can
    ``poller.stop()`` to break the run loop on shutdown.

    ``agent_loop_invoker``: production callers pass the real
    invoker (reads ticket content, runs Kora's agent, returns a
    SeaTicketResolution). When ``None``, the poller uses its
    built-in ``_placeholder_agent_loop`` which returns
    ``COMPLETED`` unconditionally — useful for an MVP wire-up
    against the substrate before the agent-loop integration lands
    in a follow-on bucket.
    """
    try:
        from plugins.memory import load_memory_provider
    except Exception:
        logger.exception(
            "[sea_ticket_poller_lifecycle] could not import "
            "plugins.memory.load_memory_provider; SeaTicketPoller "
            "will not start."
        )
        return None

    provider = load_memory_provider("isokron")
    if provider is None:
        logger.warning(
            "[sea_ticket_poller_lifecycle] IsoKron memory provider "
            "plugin not discoverable; SeaTicketPoller will not start. "
            "Verify plugins/memory/isokron/ is on the plugin path."
        )
        return None

    if not _is_provider_available(provider):
        logger.warning(
            "[sea_ticket_poller_lifecycle] IsoKron provider reports "
            "is_available() = False (typically: plugins.entries.isokron "
            "config block is missing or env vars unresolved); "
            "SeaTicketPoller will not start."
        )
        return None

    try:
        provider.initialize(session_id=_GATEWAY_POLLER_SESSION_ID)
    except Exception:
        logger.exception(
            "[sea_ticket_poller_lifecycle] IsoKron provider "
            "initialize() raised; SeaTicketPoller will not start."
        )
        return None

    # KR-P2-CLEANUP ST2: register the now-initialized provider as the
    # process-wide active provider so cross-cutting admin-panel
    # endpoints (sea-tickets, kora_control observed state, etc.) can
    # read substrate state without going through an agent session.
    try:
        from plugins.memory.isokron.active_provider import set_active_provider

        set_active_provider(provider)
    except Exception:
        # Best-effort: if the active-provider singleton is unavailable,
        # the poller still starts, just without the cross-cutting read
        # surface. Endpoints will fall back to their stub branches.
        logger.exception(
            "[sea_ticket_poller_lifecycle] could not register "
            "active provider; admin-panel live reads will fall back "
            "to their stub branches."
        )

    try:
        mcp_client = provider._connection.get_mcp_client()
    except Exception:
        logger.exception(
            "[sea_ticket_poller_lifecycle] could not obtain MCP "
            "client from IsoKron connection; SeaTicketPoller will "
            "not start. The provider may need to be reconfigured."
        )
        return None

    kwargs: dict[str, Any] = {
        "mcp_client": mcp_client,
        "memory_provider": provider,
    }
    if agent_loop_invoker is not None:
        kwargs["agent_loop_invoker"] = agent_loop_invoker

    poller = SeaTicketPoller(**kwargs)
    task = asyncio.create_task(poller.run_forever())

    # KR-P2-L ST1: register the now-running poller as the process-wide
    # active poller so the health-rollup holder + future cross-cutting
    # readers (cockpit BFF, admin endpoints) can query its
    # ``current_claim`` without import-time coupling.
    try:
        from plugins.memory.isokron.active_poller import set_active_poller

        set_active_poller(poller)
    except Exception:
        # Best-effort — if the active-poller singleton import fails,
        # the poller still starts; the health-rollup just sees
        # claim_state as "missing".
        logger.exception(
            "[sea_ticket_poller_lifecycle] could not register active "
            "poller; health-rollup claim_state subsignal will fall "
            "back to missing."
        )

    logger.info(
        "[sea_ticket_poller_lifecycle] SeaTicketPoller started as a "
        "background task (session_id=%s)",
        _GATEWAY_POLLER_SESSION_ID,
    )
    return (poller, task)


def _is_provider_available(provider: Any) -> bool:
    """Return ``True`` when ``provider.is_available()`` says so.

    Wrapped in its own helper so a provider with no ``is_available``
    method (deprecated providers, test doubles) doesn't break the
    lifecycle; default to "treat as available" — production providers
    all expose it.
    """
    is_avail = getattr(provider, "is_available", None)
    if not callable(is_avail):
        return True
    try:
        return bool(is_avail())
    except Exception:
        logger.exception(
            "[sea_ticket_poller_lifecycle] provider.is_available() "
            "raised; treating as unavailable"
        )
        return False
