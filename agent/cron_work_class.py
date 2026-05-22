"""CronWorkClass — declared interaction surface for cron jobs (KR-P2-D ST1).

Locked design intent (``feedback_cron_is_wakeup_substrate_via_sea``): cron is
wake-up only; substantive substrate state mutation routes via Sea_Tickets.
Cron jobs CAN do local-machine ops, outbound messaging, one-shot heartbeat
chain events directly. Cron jobs that involve **substantive agent reasoning
that mutates substrate state** MUST author a Sea_Ticket assigned to Kora,
which the consumer loop (KR-P2-E SeaTicketPoller) then claims + works.

# Four work-class values

  * ``LOCAL_ONLY``          — log rotation / sessions GC / local file ops.
                              NO substrate touch.
  * ``OUTBOUND_MSG``        — daily Slack briefing / email digest. NO
                              substrate touch (the inbound message-handler
                              is what eventually authors substrate work).
  * ``SUBSTRATE_HEARTBEAT`` — one-shot ``kora__append_event`` write. Audit
                              trail only — "the watchdog is alive at time T".
  * ``SUBSTRATE_MUTATION``  — substantive agent work. MUST author a
                              Sea_Ticket; cron job returns immediately,
                              consumer loop picks it up.

# What ST1 ships vs blocks

  * Enum + dispatcher + ``classify_dispatch`` helper — shipped.
  * Operator-facing registration callsites (``cronjob`` MCP tool,
    ``cron_create`` CLI, ``_handle_create_job`` web) declare work_class
    as a REQUIRED parameter. Fail-CLOSED when undeclared.
  * Runtime ``dispatch_for_work_class`` raises :class:`NotImplementedError`
    on ``SUBSTRATE_HEARTBEAT`` and ``SUBSTRATE_MUTATION``:
      - ``SUBSTRATE_HEARTBEAT`` blocked on ``kora.cron.tick_fired`` event
        vocab (not in foundation/0159 on isokron @ ce5f3df at bucket-
        dispatch time).
      - ``SUBSTRATE_MUTATION`` blocked on ``sea__create_ticket`` MCP runtime
        (registered as ``notImplementedHandler`` stub in sea-mcp-server;
        per ``operator-sea-ticket-tools.ts:54`` "runtime in Sea-S3").
    When substrate ships both, ST3 + ST4 replace these stubs.

The default work_class for the internal ``cron.jobs.create_job`` helper is
``LOCAL_ONLY`` (back-compat for test fixtures + internal callers).
Operator-facing callsites overshadow that default by passing work_class
explicitly, and surface a validation error to the operator if missing.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Optional, Union

logger = logging.getLogger(__name__)


class CronWorkClass(Enum):
    """How a cron job interacts with substrate (see module docstring)."""

    LOCAL_ONLY = "local_only"
    OUTBOUND_MSG = "outbound_msg"
    SUBSTRATE_HEARTBEAT = "substrate_heartbeat"
    SUBSTRATE_MUTATION = "substrate_mutation"


class CronWorkClassError(ValueError):
    """Raised when an operator-facing registration callsite omits the
    ``work_class`` declaration, or supplies a value that isn't a member
    of :class:`CronWorkClass`."""


def coerce_work_class(
    value: Union[None, str, CronWorkClass],
    *,
    operator_facing: bool,
    surface: str,
) -> CronWorkClass:
    """Normalize ``value`` to a :class:`CronWorkClass` member.

    * ``None`` is rejected when ``operator_facing=True`` (fail-CLOSED at
      every cronjob-tool / CLI / web registration callsite per spec ST1).
      Non-operator (internal) callers may pass ``None`` and receive
      ``LOCAL_ONLY`` back — that's the back-compat default for test
      fixtures + helpers.
    * Plain strings (``"local_only"`` / ``"outbound_msg"`` / etc.) map to
      the enum member of matching ``.value``. Unknown strings raise
      :class:`CronWorkClassError`.
    * Already-:class:`CronWorkClass` instances pass through.
    * ``surface`` is the operator-readable name of the call site (e.g.
      ``"cronjob tool"`` / ``"cron create CLI"``) — surfaced in the
      exception so the operator can grep the error to the relevant
      registration path.
    """
    if isinstance(value, CronWorkClass):
        return value

    if value is None:
        if operator_facing:
            raise CronWorkClassError(
                f"{surface}: work_class is required. One of "
                f"{sorted(c.value for c in CronWorkClass)}. Cron is "
                "wake-up-only for substrate writes; explicit declaration "
                "is the fail-CLOSED gate (KR-P2-D ST1)."
            )
        return CronWorkClass.LOCAL_ONLY

    if isinstance(value, str):
        try:
            return CronWorkClass(value)
        except ValueError as exc:
            raise CronWorkClassError(
                f"{surface}: work_class={value!r} is not a recognized "
                f"value. Expected one of "
                f"{sorted(c.value for c in CronWorkClass)}."
            ) from exc

    raise CronWorkClassError(
        f"{surface}: work_class must be a CronWorkClass / str / None; "
        f"got {type(value).__name__}"
    )


# ---------------------------------------------------------------------------
# Runtime dispatcher — called by the cron scheduler before running each job
# ---------------------------------------------------------------------------


# Stable substrate-blocked exception class for SUBSTRATE_* paths. ST3 + ST4
# will replace the raises with the real emit / ticket-authoring flows once
# substrate ships the missing pieces. The class is also greppable so
# operator runbooks can pattern-match on it in logs.
class CronSubstratePathNotYetImplemented(NotImplementedError):
    """Raised when a cron job is registered with a work_class whose
    runtime branch is blocked on substrate-team work shipping.

    SUBSTRATE_HEARTBEAT → blocked on the ``kora.cron.tick_fired`` event-type
    vocab (per the §1 verification report: literal absent from
    foundation/0159 on isokron at bucket-dispatch time).

    SUBSTRATE_MUTATION  → blocked on the ``sea__create_ticket`` MCP
    runtime (registered as ``notImplementedHandler`` stub in
    sea-mcp-server; ``operator-sea-ticket-tools.ts:54`` says "runtime in
    Sea-S3").

    Operator action: don't register cron jobs with these classes yet.
    Once substrate ships both pieces, KR-P2-D ST3 + ST4 land and the
    raises here turn into real emits + Sea_Ticket-authoring.
    """


def dispatch_for_work_class(
    work_class: CronWorkClass, *, job_id: str
) -> "DispatchDecision":
    """Decide what the scheduler should do with a job of this work_class.

    Returns a :class:`DispatchDecision` that the scheduler interprets:

      * ``DispatchDecision.RUN_LOCAL_FLOW`` — proceed with the existing
        scheduler flow (LOCAL_ONLY / OUTBOUND_MSG). No-op for ST1.
      * ``DispatchDecision.BLOCKED_PENDING_SUBSTRATE`` — the
        SUBSTRATE_HEARTBEAT / SUBSTRATE_MUTATION code paths raise
        :class:`CronSubstratePathNotYetImplemented` (ST3/ST4 unblock).

    The scheduler's responsibility on a BLOCKED decision is to:
      1. raise so the job is recorded as failed (not silently swallowed)
      2. include the work_class + substrate-blocker reason in the error
         so operator logs grep cleanly.
    """
    if work_class in (CronWorkClass.LOCAL_ONLY, CronWorkClass.OUTBOUND_MSG):
        return DispatchDecision.RUN_LOCAL_FLOW
    # SUBSTRATE_HEARTBEAT + SUBSTRATE_MUTATION fall through to a raise; the
    # caller invokes ``raise_substrate_blocked`` to surface the right
    # blocker message per class.
    logger.warning(
        "[cron.work_class] job_id=%s registered with %s but the runtime "
        "path is blocked on substrate-team work — see "
        "CronSubstratePathNotYetImplemented for the unblock conditions",
        job_id,
        work_class.value,
    )
    return DispatchDecision.BLOCKED_PENDING_SUBSTRATE


class DispatchDecision(Enum):
    RUN_LOCAL_FLOW = "run_local_flow"
    BLOCKED_PENDING_SUBSTRATE = "blocked_pending_substrate"


def raise_substrate_blocked(
    work_class: CronWorkClass, *, job_id: str
) -> None:
    """Raise :class:`CronSubstratePathNotYetImplemented` with a
    work_class-specific message naming the substrate blocker. Called by
    the scheduler when :func:`dispatch_for_work_class` returns
    ``BLOCKED_PENDING_SUBSTRATE``.
    """
    if work_class is CronWorkClass.SUBSTRATE_HEARTBEAT:
        raise CronSubstratePathNotYetImplemented(
            f"job_id={job_id} work_class=substrate_heartbeat: KR-P2-D ST3 "
            "is blocked on the `kora.cron.tick_fired` event-type vocab "
            "literal landing in substrate's "
            "foundation/0159_kora_r41_operational_state_event_vocabulary "
            "(verified absent at bucket-dispatch time). Don't register "
            "cron jobs with this work_class until ST3 lands."
        )
    if work_class is CronWorkClass.SUBSTRATE_MUTATION:
        raise CronSubstratePathNotYetImplemented(
            f"job_id={job_id} work_class=substrate_mutation: KR-P2-D ST4 "
            "is blocked on the `sea__create_ticket` MCP runtime shipping "
            "(registered as a notImplementedHandler stub in "
            "packages/sea-mcp-server/src/tools/operator-sea-ticket-tools.ts; "
            "the comment there says 'runtime in Sea-S3'). Don't register "
            "cron jobs with this work_class until ST4 lands."
        )
    # Defensive — unreachable today; future work_class additions would
    # land here if they're also substrate-blocked.
    raise CronSubstratePathNotYetImplemented(
        f"job_id={job_id} work_class={work_class.value}: no dispatch path"
    )
