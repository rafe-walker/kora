"""``kora__refresh_claim`` heartbeat loop (KR-P2-E ST3).

P2 compliance per R4.1 §9.3: the heartbeat must reflect actual
progress, not a bare timer pulse. Substrate-side the ``kora__refresh_claim``
SECDEF accepts only ``extend_by_seconds`` (no ``progress_signal`` field in
the input schema — verified against ``packages/sea-mcp-server/src/tools/
kora-claim-tools.ts`` on isokron @ 9814763). The runtime enforces the
P2 contract by gating the refresh on progress signals from the agent
loop: streaming-token-event advancement OR tool-call boundary crossing.

# How callers wire this up

1. After ``kora__claim_sea_ticket`` returns ``claimed``, the poller calls
   :func:`start_heartbeat` to spawn a background asyncio task and gets
   back a :class:`HeartbeatHandle`.
2. The handle is threaded into the agent-loop invoker. The invoker
   calls :meth:`HeartbeatHandle.signal_token_progress` and
   :meth:`HeartbeatHandle.signal_tool_boundary` as work proceeds.
3. When the invoker returns (or raises), the poller calls
   :meth:`HeartbeatHandle.cancel`. The task exits cleanly.
4. Between the spawn and the cancel, the task wakes every
   ``heartbeat_interval_seconds``:

   * If progress has been signaled since the last wake → fire
     ``kora__refresh_claim`` to extend the lease.
   * Otherwise → skip the refresh and log. The lease ticks down; if
     the agent is genuinely stuck the lease expires naturally and
     another actor can pick up the ticket.

# Failure mode

If the refresh call raises (network blip) or the substrate response is
non-claimed (e.g. ``contract_version_mismatch`` mid-work), the
heartbeat sets ``HeartbeatHandle.lease_lost = True`` + exits. The
agent loop's invoker is expected to poll ``handle.lease_lost`` and
abort cooperatively. The poller skips the final release when
``lease_lost`` is set (the lease is already gone substrate-side).

# What this module deliberately does NOT do

* **Detect ``sea_ticket.claim_expired`` mid-work.** That requires the
  Kronicle-event-log SSE consumer which is not in this bucket (was
  dropped from KR-P2-F-pre per the runtime-side SSE-consumer
  reasoning).
* **Emit ``kora.sea_ticket.claim_lost_mid_work``.** The spec text
  mentioned this literal, but it's not in the
  ``event_log_event_type_check`` constraint on isokron-prod
  (foundation/0159 doesn't include it). Surfaced for follow-on
  substrate vocab if cockpit needs the explicit signal; substrate's
  own ``sea_ticket.claim_expired`` covers the durable record today.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ``kora__refresh_claim`` response result that means "lease still ours".
_REFRESH_RESULT_CLAIMED = "claimed"


# ---------------------------------------------------------------------------
# ProgressSignal — token + tool-boundary counters
# ---------------------------------------------------------------------------


@dataclass
class ProgressSignal:
    """Tracks agent-loop progress since the last heartbeat snapshot.

    Two independent counters — token advancement vs tool-call
    boundaries — so future telemetry can distinguish "model is
    streaming tokens" from "model is between tool dispatches." Today
    the heartbeat only checks ``has_any_progress()``; the breakdown is
    captured for log readability.
    """

    _token_count: int = 0
    _tool_count: int = 0

    def signal_token_progress(self) -> None:
        """Call from the streaming-token callback. Cheap; safe to call
        many times per second."""
        self._token_count += 1

    def signal_tool_boundary(self) -> None:
        """Call when crossing a tool-call boundary (before dispatch,
        after result). One per logical boundary."""
        self._tool_count += 1

    def snapshot_and_reset(self) -> tuple[bool, int, int]:
        """Atomically read the counters + reset them.

        Returns ``(had_progress, tokens, tools)``. Single-threaded
        within the event loop, so the read+reset is naturally atomic.
        """
        tokens = self._token_count
        tools = self._tool_count
        had_progress = tokens > 0 or tools > 0
        self._token_count = 0
        self._tool_count = 0
        return had_progress, tokens, tools


# ---------------------------------------------------------------------------
# HeartbeatHandle
# ---------------------------------------------------------------------------


@dataclass
class HeartbeatHandle:
    """Handle returned by :func:`start_heartbeat`.

    The agent-loop invoker reads ``lease_lost`` periodically to bail
    out cooperatively on lease loss; it calls the
    ``signal_*_progress`` methods to satisfy the P2 contract.
    """

    progress: ProgressSignal
    task: asyncio.Task[None]
    lease_lost: bool = False
    _cancel_requested: bool = False
    sea_ticket_id: str = ""  # filled by start_heartbeat for logging

    def signal_token_progress(self) -> None:
        self.progress.signal_token_progress()

    def signal_tool_boundary(self) -> None:
        self.progress.signal_tool_boundary()

    async def cancel(self) -> None:
        """Stop the heartbeat task and await its clean shutdown.

        Idempotent — calling twice is safe."""
        self._cancel_requested = True
        if not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception(
                    "[claim_heartbeat] task raised on cancel for "
                    "ticket_id=%s",
                    self.sea_ticket_id,
                )


# ---------------------------------------------------------------------------
# Spawn + loop
# ---------------------------------------------------------------------------


def start_heartbeat(
    *,
    mcp_client: Any,
    workspace_id: str,
    kora_operation_id: str,
    sea_ticket_id: str,
    claim_fence_token: str,
    heartbeat_interval_seconds: int,
    extend_by_seconds: int,
) -> HeartbeatHandle:
    """Spawn the heartbeat task. Returns a :class:`HeartbeatHandle`
    the caller threads into the agent-loop invoker.

    The task is owned by the handle; the handle's lifetime should
    cover the entire claimed work attempt. The caller MUST eventually
    call :meth:`HeartbeatHandle.cancel` (typically in a try/finally
    around the agent loop) so the task doesn't outlive the claim.
    """
    progress = ProgressSignal()
    handle = HeartbeatHandle(
        progress=progress,
        task=asyncio.create_task(asyncio.sleep(0)),  # placeholder; replaced
        sea_ticket_id=sea_ticket_id,
    )
    # Build the actual task with a closure over the handle so the
    # loop can set ``handle.lease_lost``.
    handle.task = asyncio.create_task(
        _heartbeat_loop(
            mcp_client=mcp_client,
            handle=handle,
            workspace_id=workspace_id,
            kora_operation_id=kora_operation_id,
            sea_ticket_id=sea_ticket_id,
            claim_fence_token=claim_fence_token,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            extend_by_seconds=extend_by_seconds,
        )
    )
    return handle


async def _heartbeat_loop(
    *,
    mcp_client: Any,
    handle: HeartbeatHandle,
    workspace_id: str,
    kora_operation_id: str,
    sea_ticket_id: str,
    claim_fence_token: str,
    heartbeat_interval_seconds: int,
    extend_by_seconds: int,
) -> None:
    """The actual heartbeat loop.

    Wakes every ``heartbeat_interval_seconds``. If progress was
    signaled, fires ``kora__refresh_claim``; otherwise skips and
    logs. Exits cleanly on cancel; exits with ``lease_lost=True`` on
    any substrate-side failure.
    """
    try:
        while True:
            try:
                await asyncio.sleep(heartbeat_interval_seconds)
            except asyncio.CancelledError:
                logger.info(
                    "[claim_heartbeat] cancelled for ticket_id=%s",
                    sea_ticket_id,
                )
                return

            if handle._cancel_requested:
                return

            had_progress, tokens, tools = handle.progress.snapshot_and_reset()
            if not had_progress:
                # P2 compliance: no progress signaled, do not refresh.
                # The lease ticks down; if work is genuinely stuck the
                # lease expires + the sweeper picks the ticket up
                # again for another actor.
                logger.info(
                    "[claim_heartbeat] no progress signaled for "
                    "ticket_id=%s in last %ss — skipping refresh "
                    "(P2 compliance)",
                    sea_ticket_id,
                    heartbeat_interval_seconds,
                )
                continue

            logger.debug(
                "[claim_heartbeat] firing refresh for ticket_id=%s "
                "(tokens=%d tools=%d)",
                sea_ticket_id,
                tokens,
                tools,
            )

            try:
                resp = await mcp_client.invoke(
                    "kora__refresh_claim",
                    {
                        "workspace_id": workspace_id,
                        "kora_operation_id": kora_operation_id,
                        "sea_ticket_id": sea_ticket_id,
                        "claim_fence_token": claim_fence_token,
                        "extend_by_seconds": extend_by_seconds,
                    },
                )
            except Exception as exc:
                logger.exception(
                    "[claim_heartbeat] kora__refresh_claim raised for "
                    "ticket_id=%s — aborting heartbeat, lease will "
                    "expire naturally: %r",
                    sea_ticket_id,
                    exc,
                )
                handle.lease_lost = True
                return

            result = resp.get("result") if isinstance(resp, dict) else None
            if result != _REFRESH_RESULT_CLAIMED:
                logger.error(
                    "[claim_heartbeat] substrate rejected refresh for "
                    "ticket_id=%s with result=%r — lease lost",
                    sea_ticket_id,
                    result,
                )
                handle.lease_lost = True
                return
    except asyncio.CancelledError:
        # External cancel during a non-sleep await. Quiet exit.
        return
