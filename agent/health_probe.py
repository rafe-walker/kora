"""Synthetic ``kora.health.probe`` emitter (KR-P2-L ST2, R4.1 §9.7).

The probe is a periodic chain event that captures the full R4.1 §9.7
health rollup at the moment of emit, giving the cockpit a durable
record of Kora-side health independent of the live BFF read. The
event flows through ``kora__append_event`` (verified vocab literal
in ``foundation/0159_kora_r41_operational_state_event_vocabulary.sql``).

# Payload shape

The payload mirrors the HEALTH-PANEL JSON contract pinned in
``kora_cli/web_server.py:get_health_rollup`` with one extra field:

  - ``overall`` / ``control_plane`` / ``worker`` — top-level enums
  - ``stopped_reason``
  - ``subsignals`` — 8 R4.1 §9.7 axes
  - ``probe_cadence_seconds`` — operator-tuned cadence, so the
    cockpit knows when to expect the next probe (a stale probe is
    itself a degradation signal)

Cockpit consumers index off the probe's chain row + the panel's
live read; the two should agree at the moment of emit.

# Cron registration

The probe registers as a Hermes-fork cron job that fires every
``probe_cadence_seconds`` (default 5 min per R4.1 §9.7). The
SUBSTRATE_HEARTBEAT ``work_class`` field is pending KR-P2-D ST1 —
this module ships the emit function + a CLI entrypoint; the
work_class-bearing job is a follow-on once KR-P2-D lands.

Operators can wire this into any cron of choice in the meantime
by running ``python -m agent.health_probe`` (uses
:func:`get_active_provider` to find the gateway-level provider).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, Optional

from agent.health_rollup_holder import (
    HealthRollupHolder,
    get_health_rollup_holder,
    init_health_rollup_holder,
    rollup_to_panel_payload,
)

logger = logging.getLogger(__name__)


HEALTH_PROBE_EVENT_TYPE = "kora.health.probe"


async def emit_health_probe(
    memory_provider: Any,
    *,
    holder: Optional[HealthRollupHolder] = None,
) -> Optional[str]:
    """Emit ``kora.health.probe`` with the full rollup payload.

    Args:
        memory_provider: An initialized :class:`IsoKronMemoryProvider`
            (typically from :func:`active_provider.get_active_provider`).
            Must expose ``_connection`` (with ``get_mcp_client()``) +
            ``_resolve_workspace_id()``.
        holder: Optional explicit holder. When ``None``, uses
            :func:`agent.health_rollup_holder.get_health_rollup_holder`.
            If still ``None``, the function returns ``None`` and logs
            a warning — the gateway should always initialize the
            holder before the probe cron fires.

    Returns:
        The substrate-assigned ``event_id`` (UUID string) on success;
        ``None`` on any failure path (logged at WARN level — bucket §4
        non-scope rules out retry; degraded observability beats a
        crashed cron job).
    """
    if holder is None:
        holder = get_health_rollup_holder()
    if holder is None:
        logger.warning(
            "[health_probe] HealthRollupHolder not initialized; "
            "skipping probe emit. Initialize via "
            "init_health_rollup_holder() at gateway boot."
        )
        return None

    if memory_provider is None:
        logger.warning(
            "[health_probe] memory_provider is None; skipping probe emit"
        )
        return None

    # Build the payload from a fresh collect.
    try:
        rollup = holder.current()
    except Exception:
        logger.warning(
            "[health_probe] holder.current() raised; skipping probe emit",
            exc_info=True,
        )
        return None

    payload = rollup_to_panel_payload(rollup)
    payload["probe_cadence_seconds"] = holder.probe_cadence_seconds

    # Resolve MCP client + workspace from the provider.
    connection = getattr(memory_provider, "_connection", None)
    if connection is None:
        logger.warning(
            "[health_probe] memory_provider has no _connection; "
            "skipping probe emit"
        )
        return None

    get_mcp = getattr(connection, "get_mcp_client", None)
    mcp_client = None
    if callable(get_mcp):
        try:
            mcp_client = get_mcp()
        except Exception:
            logger.warning(
                "[health_probe] connection.get_mcp_client() raised; "
                "skipping probe emit",
                exc_info=True,
            )
            return None
    if mcp_client is None:
        logger.warning(
            "[health_probe] no MCP client available; skipping probe emit"
        )
        return None

    resolve_ws = getattr(memory_provider, "_resolve_workspace_id", None)
    workspace_id = None
    if callable(resolve_ws):
        try:
            workspace_id = resolve_ws()
        except Exception:
            logger.warning(
                "[health_probe] _resolve_workspace_id() raised; "
                "skipping probe emit",
                exc_info=True,
            )
            return None
    if not workspace_id:
        logger.warning(
            "[health_probe] no workspace_id available; skipping probe emit"
        )
        return None

    # Emit via the existing chain-event helper. Fail-soft: substrate
    # error (CHECK violation, lock failure, etc.) logs WARN + returns
    # None so the cron job doesn't crash.
    try:
        from plugins.memory.isokron.events import emit_kora_event

        event_id = await emit_kora_event(
            workspace_id=workspace_id,
            event_type=HEALTH_PROBE_EVENT_TYPE,
            payload=payload,
            mcp_client=mcp_client,
        )
        logger.info(
            "[health_probe] emitted kora.health.probe (event_id=%s) "
            "overall=%s",
            event_id,
            payload["overall"],
        )
        return event_id
    except Exception:
        logger.warning(
            "[health_probe] emit_kora_event raised; probe NOT emitted",
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# CLI entrypoint — ``python -m agent.health_probe``
# ---------------------------------------------------------------------------


async def _main() -> int:
    """Entrypoint for the cron job + manual operator invocation.

    Looks up the gateway-level provider via
    :func:`plugins.memory.isokron.active_provider.get_active_provider`.
    If no active provider is registered (e.g. the gateway isn't
    running in this process), exits 1 with a clear error.

    Exit codes:
      0 — probe emitted (event_id printed to stdout)
      1 — could not resolve provider or holder
      2 — emit failed (degraded observability; substrate is the
          authoritative record)
    """
    from plugins.memory.isokron.active_provider import get_active_provider

    provider = get_active_provider()
    if provider is None:
        print(
            "[health_probe] no active IsoKronMemoryProvider registered; "
            "run within the gateway process or after "
            "set_active_provider(...)",
            file=sys.stderr,
        )
        return 1

    if get_health_rollup_holder() is None:
        init_health_rollup_holder()

    event_id = await emit_health_probe(provider)
    if event_id is None:
        print(
            "[health_probe] emit failed; see log for details",
            file=sys.stderr,
        )
        return 2
    print(event_id)
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via CLI
    sys.exit(asyncio.run(_main()))
