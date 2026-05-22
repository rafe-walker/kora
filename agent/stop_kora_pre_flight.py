"""STOP-KORA pre-flight helpers for the tool-executor wire-in (KR-P2-J ST3).

Mirrors the shape of :mod:`agent.constitution_audit` (KR-P2-A ST3):
sync helper functions that the synchronous ``execute_tool_calls_*``
loops in :mod:`agent.tool_executor` call from their pre-flight path,
bridging to async :mod:`plugins.memory.isokron.kora_control_reader`
methods via ``IsoKronConnection.submit_and_wait`` (the same sync→async
bridge the provider uses for chain emit + scratchpad write).

# Flow per pre-tool-call boundary

1. ``run_stop_kora_pre_flight(agent)`` reads the active
   ``kora_control`` command via :class:`KoraControlReader`.
2. :func:`evaluate_stop_kora` maps it to a :class:`STOPKoraVerdict`.
3. If the verdict ``is_blocking()`` at ``pre_tool_call`` context
   (DRAIN_CURRENT_FINISH or ABORT_RELEASE_CLAIM), the wire-in builds a
   block_result and short-circuits guardrails + tool execution.
4. If blocking AND Kora's actor UUID is resolvable, the helper advances
   the substrate lifecycle: ``acknowledged → enforcing → enforced``.
   The substrate ``transition_kora_control`` SECDEF emits
   ``kora_control.enforced`` internally on the final transition.
5. If blocking but actor UUID is NOT resolvable (no isokron config,
   no kora actor in ``actor_registry``, query failed), the helper
   blocks anyway and WARN-logs the missed advance — the cockpit-BFF
   SLA watcher will auto-escalate to L4 on no-progress timeout.

# No per-stage chain emit (Gap-2 ruling)

Per the verification round before ST1, the substrate's design is
terminal-only chain emit (the table itself records intermediate state;
the chain trace is audit-class events only). This helper calls the
SECDEF for each lifecycle transition — the SECDEF advances the table
and emits ``kora_control.enforced`` / ``kora_control.failed``
internally at terminal. No runtime-side
``kronicle._emit_chain_event(... 'kora_control.acknowledged' ...)``
calls — that literal doesn't exist in
``foundation/0159_kora_r41_operational_state_event_vocabulary.sql``.

# DEGRADED + operational-state PAUSED transition

This helper does NOT directly transition operational state. The
KR-P2-I-integration listener observes the substrate-emitted
``kora_control.enforced`` chain event and transitions the operational
state machine to PAUSED. Clean separation.
"""

from __future__ import annotations

import logging
from typing import Any, Final, Optional

from agent.stop_kora_handler import (
    STOPKoraAction,
    STOPKoraVerdict,
    evaluate_stop_kora,
)
from plugins.memory.isokron.kora_control_reader import (
    KoraControlCommand,
    KoraControlReader,
)

logger = logging.getLogger(__name__)


# Sentinel UUID returned by the actor-uuid lookup when no kora actor is
# registered for the workspace. Used to instantiate ``KoraControlReader``
# in degraded mode (the reader's transitions will fail with a substrate
# foreign-key error if attempted; the helper guards against this by
# skipping mark_* calls when the sentinel is returned).
_UNRESOLVED_ACTOR_UUID: Final[str] = "00000000-0000-0000-0000-000000000000"


# JSON discriminator pinned in the model-facing block_result. Mirrors
# the KR-P2-A ``block_kind`` discriminator convention
# (``constitution_reject`` / ``constitution_escalate``).
STOP_KORA_BLOCK_KIND: Final[str] = "stop_kora"


def run_stop_kora_pre_flight(agent: Any) -> Optional[STOPKoraVerdict]:
    """Synchronous pre-flight wire-in entry point.

    Returns ``None`` when no Constitution-class memory layer is loaded
    (``_memory_manager is None`` — CLI/test ``skip_memory=True`` path).
    Otherwise returns a :class:`STOPKoraVerdict` describing whether
    the caller should block.

    Best-effort lifecycle advance: if the verdict is blocking AND a
    kora actor UUID is resolvable for the workspace, advances the
    substrate row through ``acknowledged → enforcing → enforced``.
    Failure to advance is logged at WARN and does NOT short-circuit
    the block — the cockpit-BFF SLA watcher escalates on no-progress
    timeout regardless.

    The function never raises. Reader / provider exceptions are caught
    locally and surfaced as WARN logs.

    Fail-CLOSED on substrate read error (KR-P2-FAIL-SAFETIES ST2 fix):
    if ``get_active_command`` raises, the verdict is
    ``action=DRAIN_CURRENT_FINISH`` (blocks new tool calls at
    pre_tool_call context) with ``reason="substrate read failed: ..."``.
    A no-action verdict on read failure was the pre-fix behavior;
    per the locked rule
    ``feedback_fail_closed_by_default_security_infra``, security
    infrastructure must not silently allow when verification is
    impossible.
    """
    memory_manager = getattr(agent, "_memory_manager", None)
    if memory_manager is None:
        return None

    provider = memory_manager.get_provider("isokron")
    if provider is None:
        return None

    connection = getattr(provider, "_connection", None)
    if connection is None:
        return None

    actor_uuid = _resolve_kora_actor_uuid(agent)

    reader = KoraControlReader(
        provider, actor_uuid or _UNRESOLVED_ACTOR_UUID
    )

    try:
        command = connection.submit_and_wait(
            reader.get_active_command(),
            timeout=5.0,
        )
    except Exception as exc:
        # KR-P2-FAIL-SAFETIES ST2 — fix LEAK identified in §1 audit
        # (`feedback_fail_closed_by_default_security_infra` locked
        # rule). Substrate read failure must NOT silently allow tool
        # execution. Returns a BLOCKING verdict at pre_tool_call
        # context (DRAIN_CURRENT_FINISH — finish in-flight work,
        # block new tool calls until substrate is reachable again).
        #
        # The previous behavior returned action=None ("treating as
        # no STOP-KORA active. Tool call proceeds.") which was
        # silent-allow on a security-infrastructure read failure.
        logger.warning(
            "[kora.control.pre_flight] get_active_command raised: %r — "
            "cannot verify STOP-KORA state. Blocking tool call "
            "(DRAIN_CURRENT_FINISH) until substrate read recovers.",
            exc,
        )
        return STOPKoraVerdict(
            action=STOPKoraAction.DRAIN_CURRENT_FINISH,
            command_id=None,
            reason=f"substrate read failed: {type(exc).__name__}",
            context="pre_tool_call",
        )

    verdict = evaluate_stop_kora(command, context="pre_tool_call")
    if not verdict.is_blocking():
        return verdict

    # Blocking. Try to advance lifecycle. Best-effort.
    if actor_uuid is None:
        logger.warning(
            "[kora.control.pre_flight] STOP-KORA active "
            "(command_id=%s level=%s) but kora actor UUID is unresolvable "
            "for the workspace; blocking tool call without advancing "
            "substrate lifecycle. Cockpit-BFF SLA watcher will escalate "
            "on no-progress timeout.",
            verdict.command_id,
            command.level if command is not None else None,
        )
        return verdict

    _advance_lifecycle_best_effort(
        connection, reader, verdict, command
    )
    return verdict


def build_stop_kora_block_result(verdict: STOPKoraVerdict) -> str:
    """JSON-encode the model-facing block_result for a STOP-KORA-blocked tool call.

    The ``block_kind`` discriminator (``"stop_kora"``) lets downstream
    consumers (chain-event listeners, cockpit UI) distinguish this from
    Constitution / guardrail / plugin blocks. ``stop_kora_action`` and
    ``command_id`` are included for traceability.
    """
    import json

    action_str = verdict.action.value if verdict.action is not None else None
    return json.dumps(
        {
            "error": (
                f"STOP-KORA active (action={action_str or 'none'}, "
                f"command_id={verdict.command_id or 'n/a'}): "
                f"{verdict.reason or 'no reason provided'}"
            ),
            "block_kind": STOP_KORA_BLOCK_KIND,
            "stop_kora_action": action_str,
            "command_id": verdict.command_id,
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve_kora_actor_uuid(agent: Any) -> Optional[str]:
    """Look up Kora's ``actor_id`` UUID for the active workspace.

    Cached on ``agent._cached_kora_actor_uuid`` after first successful
    lookup (the value doesn't change for the agent's lifetime). Returns
    ``None`` on any failure — the caller treats that as the degraded
    "block-only, don't advance lifecycle" path.

    ``actor_registry`` has no RLS policy (per
    ``0057_actor_registry.sql`` — workspace_id is a column filter, not
    an RLS GUC), so no ``set_config`` call is needed.
    """
    cached = getattr(agent, "_cached_kora_actor_uuid", None)
    if cached is not None:
        return cached

    memory_manager = getattr(agent, "_memory_manager", None)
    if memory_manager is None:
        return None
    provider = memory_manager.get_provider("isokron")
    if provider is None:
        return None

    try:
        workspace_id = provider._resolve_workspace_id()
    except Exception:
        return None
    if not workspace_id:
        return None

    connection = getattr(provider, "_connection", None)
    if connection is None:
        return None

    try:
        actor_uuid = connection.submit_and_wait(
            _query_kora_actor_uuid(connection, workspace_id),
            timeout=5.0,
        )
    except Exception as exc:
        logger.warning(
            "[kora.control.pre_flight] kora actor UUID lookup failed for "
            "workspace=%s: %r",
            workspace_id,
            exc,
        )
        return None

    if actor_uuid is not None:
        # Cache on agent so subsequent pre-flights skip the query.
        try:
            agent._cached_kora_actor_uuid = actor_uuid
        except Exception:  # pragma: no cover — defensive against frozen agents
            pass
    return actor_uuid


async def _query_kora_actor_uuid(
    connection: Any, workspace_id: str
) -> Optional[str]:
    """Async SELECT — looks up the kora actor UUID for ``workspace_id``."""
    pool = connection.get_pg_pool()
    if pool is None:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT actor_id::text AS actor_id
              FROM public.actor_registry
             WHERE workspace_id = $1
               AND actor_kind = 'kora'
               AND deactivated_at IS NULL
             LIMIT 1
            """,
            workspace_id,
        )
    return row["actor_id"] if row is not None else None


def _advance_lifecycle_best_effort(
    connection: Any,
    reader: KoraControlReader,
    verdict: STOPKoraVerdict,
    command: Optional[KoraControlCommand],
) -> None:
    """Advance ``acknowledged → enforcing → enforced`` for the blocked command.

    On any step failure: log WARN + call ``mark_failed`` (best-effort —
    if that also fails, log ERROR). Substrate emits
    ``kora_control.enforced`` internally on success and
    ``kora_control.failed`` internally on the failure path.

    Never raises. Wire-in's block_result is independent of advance
    success — operator triage correlates the chain event by command_id.
    """
    if verdict.command_id is None:
        return  # defensive — blocking verdict always has command_id
    command_id = verdict.command_id

    try:
        connection.submit_and_wait(
            reader.mark_acknowledged(command_id), timeout=5.0
        )
        connection.submit_and_wait(
            reader.mark_enforcing(command_id), timeout=5.0
        )
        connection.submit_and_wait(
            reader.mark_enforced(command_id), timeout=5.0
        )
        logger.info(
            "[kora.control.enforced] command_id=%s level=%s — "
            "lifecycle advanced acknowledged → enforcing → enforced; "
            "substrate emitted kora_control.enforced.",
            command_id,
            command.level if command is not None else None,
        )
    except Exception as advance_exc:
        logger.warning(
            "[kora.control.pre_flight] lifecycle advance failed for "
            "command_id=%s: %r — marking failed.",
            command_id,
            advance_exc,
        )
        try:
            connection.submit_and_wait(
                reader.mark_failed(
                    command_id,
                    reason=f"lifecycle advance error: {advance_exc!r}",
                ),
                timeout=5.0,
            )
        except Exception as failed_exc:
            logger.error(
                "[kora.control.pre_flight] mark_failed ALSO failed for "
                "command_id=%s: %r. Substrate row stays at the last "
                "transitioned state; cockpit-BFF SLA watcher will "
                "escalate on no-progress timeout.",
                command_id,
                failed_exc,
            )
