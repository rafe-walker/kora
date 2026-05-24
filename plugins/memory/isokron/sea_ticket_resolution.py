"""Failure classification + ``kora.sea_ticket.resolved`` emit (KR-P2-E ST4).

After the agent loop returns (or raises) for a claimed ticket, this
module:

1. **Classifies** the outcome against the R4.1 §9.4 failure table —
   maps an exception (or the agent's own resolution choice) to a
   :class:`~plugins.memory.isokron.sea_ticket_poller.SeaTicketResolution`
   value (``COMPLETED`` / ``FAILED_TERMINAL`` / ``FAILED_RETRYABLE`` /
   ``RELEASED``) plus a ``next_eligible_offset_seconds`` advisory
   recommended for that resolution.
2. **Emits** ``kora.sea_ticket.resolved`` via the existing
   ``kora__append_event`` MCP tool (PM-verified vocab literal in
   foundation/0159 line 295). The payload is a free-form JSON dict
   carrying ``resolution`` / ``resolution_summary`` / ``model_tier_used``
   plus (advisory) ``next_eligible_offset_seconds``; substrate-team
   imposes no payload CHECK on this literal, but cockpit consumers
   index on the field names captured here so treat them as
   wire-format-stable.

# Substrate-side gap noted for follow-on

The R4.1 §9.4 table mandates that ``failed_retryable`` resolutions set
``tickets.sea_status='failed_retryable'`` + ``tickets.next_eligible_at``
so the next poll respects the backoff. The substrate's current MCP
surface has NO Kora-tier tool that updates either field —
``sea__set_status`` exists but at the ``operator`` tier (Kora's
``actor_kind='kora'`` capability check fails) and accepts no
``next_eligible_at``. The chain event we emit here carries the
runtime's INTENT; the actual ``tickets`` row update is deferred to a
substrate-side follow-on (filed as a vocab-gap-style note in the ST4
PR body). Once the substrate ships a ``kora__resolve_sea_ticket`` tool
or equivalent, the chain emit + the row update can be threaded
together atomically and this module's emit can be extended.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from plugins.memory.isokron.sea_ticket_poller import (
    SeaTicket,
    SeaTicketResolution,
)

logger = logging.getLogger(__name__)


# Substrate vocab literal — verified against
# ``packages/db/migrations/foundation/0159_kora_r41_operational_state_event_vocabulary.sql``
# line 295 (rafe-walker/isokron @ 9814763).
RESOLVED_EVENT = "kora.sea_ticket.resolved"


# ---------------------------------------------------------------------------
# Resolution + next-eligible offset
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClassifiedResolution:
    """The outcome of classifying an agent-loop result.

    ``next_eligible_offset_seconds`` is an advisory the runtime
    surfaces in the chain-event payload; without a substrate-side
    update path it doesn't take effect on ``tickets.next_eligible_at``
    today. Future substrate work can pick it up.
    """

    resolution: SeaTicketResolution
    next_eligible_offset_seconds: Optional[int]
    reason: str  # human-readable, e.g. "constitution pre-screen FAIL"


# R4.1 §9.4 — failure-cause → (resolution, retry offset).
_OFFSET_CONSTITUTION_INCONCLUSIVE = 5 * 60  # 5 min
_OFFSET_AGENT_LOOP_TIMEOUT = 10 * 60  # 10 min
_OFFSET_NETWORK_DISPATCH = 1 * 60  # 1 min


# ---------------------------------------------------------------------------
# Per-tool classification table
# ---------------------------------------------------------------------------

# Conservative starting set — only tools where the "logical vs
# transient" distinction is unambiguous get a non-default mapping. The
# default for any unmapped tool exception is ``FAILED_RETRYABLE`` with
# the network/dispatch retry offset.
#
# Operator tuning lives here — adding rows is a one-line PR. ST4 ships
# the structure; expansion is operational.
_PER_TOOL_CLASSIFICATION: dict[str, SeaTicketResolution] = {
    # Constitution-tier policy denies are logical, not transient — no
    # retry helps because the policy is the gate. Caller surfaces these
    # via ConstitutionAuditEmitError or a similar typed signal; if a
    # bare tool-name tag arrives here, treat as terminal.
    "kora__claim_sea_ticket": SeaTicketResolution.FAILED_TERMINAL,
    # MCP plumbing failures — substrate down or auth flap. Transient
    # by default; the substrate-side retry budget at higher tiers
    # decides terminal escalation.
    "kora__append_event": SeaTicketResolution.FAILED_RETRYABLE,
    "kora__release_claim": SeaTicketResolution.FAILED_RETRYABLE,
    "kora__refresh_claim": SeaTicketResolution.FAILED_RETRYABLE,
}


# ---------------------------------------------------------------------------
# Sentinel exception types the agent loop can use to signal specific
# failure causes. The classifier branches on these BEFORE falling back
# to the per-tool table.
# ---------------------------------------------------------------------------


class ConstitutionPreScreenFailError(Exception):
    """Constitution pre-screen returned FAIL (policy denies the tool
    use). Maps to FAILED_TERMINAL — retrying with the same policy
    state buys nothing. The agent-loop layer also emits
    ``kora.constitution.disagreement_raised`` (KR-P2-A ST3); this
    classifier doesn't re-emit."""


class ConstitutionPreScreenInconclusiveError(Exception):
    """Pre-screen returned INCONCLUSIVE — policy can't decide,
    typically because Critic/Oracle disagreed or context is
    insufficient. Retryable after a 5-minute backoff per R4.1 §9.4."""


class AgentLoopTimeoutError(Exception):
    """Agent loop exceeded its wall-clock budget. Retryable with a
    10-minute backoff."""


class NetworkDispatchFailureError(Exception):
    """Substrate-tier dispatch failed for transient reasons (Sea MCP
    transport, network blip). Retryable with a 1-minute backoff and
    NO retry-count attribution (counts toward failed_terminal only
    when the LOGICAL class trips it, per §9.4)."""


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def classify_failure(
    exc: BaseException, *, tool_name: Optional[str] = None
) -> ClassifiedResolution:
    """Map an exception (or fall-through tool failure) to a
    :class:`ClassifiedResolution`.

    Priority:

    1. Sentinel exception types (`ConstitutionPreScreen*`,
       `AgentLoopTimeoutError`, `NetworkDispatchFailureError`) — exact
       isinstance match, R4.1 §9.4 mapping.
    2. ``tool_name`` lookup in the per-tool classification table —
       caller passes the tool name when the failure was raised by
       a specific tool dispatch.
    3. Conservative default: ``FAILED_RETRYABLE`` with the
       network/dispatch offset. Unknown failures get the benefit of
       the doubt — substrate-side retry budget catches genuinely
       terminal ones.
    """
    # Constitution table (R4.1 §9.4 rows 1 + 2).
    if isinstance(exc, ConstitutionPreScreenFailError):
        return ClassifiedResolution(
            resolution=SeaTicketResolution.FAILED_TERMINAL,
            next_eligible_offset_seconds=None,
            reason="constitution pre-screen FAIL (policy denies)",
        )
    if isinstance(exc, ConstitutionPreScreenInconclusiveError):
        return ClassifiedResolution(
            resolution=SeaTicketResolution.FAILED_RETRYABLE,
            next_eligible_offset_seconds=_OFFSET_CONSTITUTION_INCONCLUSIVE,
            reason="constitution pre-screen INCONCLUSIVE",
        )
    # Agent-loop wall-clock budget.
    if isinstance(exc, AgentLoopTimeoutError):
        return ClassifiedResolution(
            resolution=SeaTicketResolution.FAILED_RETRYABLE,
            next_eligible_offset_seconds=_OFFSET_AGENT_LOOP_TIMEOUT,
            reason="agent-loop timeout",
        )
    # Network / substrate dispatch transient.
    if isinstance(exc, NetworkDispatchFailureError):
        return ClassifiedResolution(
            resolution=SeaTicketResolution.RELEASED,
            next_eligible_offset_seconds=_OFFSET_NETWORK_DISPATCH,
            reason="network / dispatch transient",
        )

    # Per-tool table lookup.
    if tool_name is not None and tool_name in _PER_TOOL_CLASSIFICATION:
        resolution = _PER_TOOL_CLASSIFICATION[tool_name]
        offset = (
            None
            if resolution is SeaTicketResolution.FAILED_TERMINAL
            else _OFFSET_NETWORK_DISPATCH
        )
        return ClassifiedResolution(
            resolution=resolution,
            next_eligible_offset_seconds=offset,
            reason=f"per-tool classification: {tool_name}",
        )

    # Conservative default.
    return ClassifiedResolution(
        resolution=SeaTicketResolution.FAILED_RETRYABLE,
        next_eligible_offset_seconds=_OFFSET_NETWORK_DISPATCH,
        reason=f"unknown failure ({type(exc).__name__}): {exc!s}",
    )


# ---------------------------------------------------------------------------
# Chain-event emit
# ---------------------------------------------------------------------------


async def emit_sea_ticket_resolved(
    *,
    provider: Any,
    ticket: SeaTicket,
    resolution: SeaTicketResolution,
    resolution_summary: str,
    model_tier_used: Optional[str] = None,
    next_eligible_offset_seconds: Optional[int] = None,
) -> Optional[str]:
    """Emit ``kora.sea_ticket.resolved`` via ``kora__append_event``.

    Returns the substrate-assigned ``event_id`` on success; ``None``
    if emit failed (logged loudly but NOT raised — release happens
    after this and shouldn't be blocked by an emit hiccup; the chain-
    event log losing the resolution is a degraded-observability
    outcome, not a correctness one).

    Payload shape (cockpit-stable, even though substrate doesn't
    CHECK it):

    .. code-block:: json

        {
          "sea_ticket_id": "<uuid>",
          "resolution": "completed",
          "resolution_summary": "ticket finished cleanly",
          "model_tier_used": "tier_2",
          "next_eligible_offset_seconds": null,
          "workspace_id": "org_xyz"
        }
    """
    payload: dict[str, Any] = {
        "sea_ticket_id": ticket.ticket_id,
        "workspace_id": ticket.workspace_id,
        "resolution": resolution.value,
        "resolution_summary": resolution_summary,
    }
    if model_tier_used is not None:
        payload["model_tier_used"] = model_tier_used
    if next_eligible_offset_seconds is not None:
        payload["next_eligible_offset_seconds"] = next_eligible_offset_seconds

    connection = getattr(provider, "_connection", None)
    if connection is None:
        logger.error(
            "[sea_ticket_resolution] provider has no _connection; "
            "cannot emit %s for ticket_id=%s",
            RESOLVED_EVENT,
            ticket.ticket_id,
        )
        return None

    try:
        from isokron_client.events import emit_kora_event

        mcp_client = connection.get_mcp_client()
        import asyncio

        future = connection._submit_async(
            emit_kora_event(
                workspace_id=ticket.workspace_id,
                event_type=RESOLVED_EVENT,
                payload=payload,
                mcp_client=mcp_client,
            )
        )
        event_id = await asyncio.wrap_future(future)
        logger.info(
            "[sea_ticket_resolution] emitted %s for ticket_id=%s "
            "(resolution=%s) → event_id=%s",
            RESOLVED_EVENT,
            ticket.ticket_id,
            resolution.value,
            event_id,
        )
        return event_id
    except Exception:
        logger.exception(
            "[sea_ticket_resolution] %s emit raised for ticket_id=%s "
            "(resolution=%s) — degraded observability, release will "
            "still fire",
            RESOLVED_EVENT,
            ticket.ticket_id,
            resolution.value,
        )
        return None
