"""STOP-KORA level → action mapping (KR-P2-J ST2).

R4.1 §9.3 maps each STOP-KORA escalation level to a runtime action.
This module is **pure policy**: it knows nothing about the substrate,
emits nothing, makes no I/O calls. The reader (ST1) fetches an active
:class:`KoraControlCommand`; this module maps it to a
:class:`STOPKoraVerdict` describing what the caller should do; the
tool-executor wire-in (ST3) and the consumer-loop wire-in (ST5,
deferred) decide how to react.

# Level → action map (R4.1 §9.3)

| Level | Action                      | Meaning                                                 |
|-------|-----------------------------|---------------------------------------------------------|
| 0     | ``RESET_CLEAR_LOWER``       | Operator reset — supersedes lower commands. Issuer-side |
|       |                             | only; the runtime sees the supersession via observed    |
|       |                             | state, not via an action on the reset itself.           |
| 1     | ``BLOCK_NEW_INTAKE``        | Stop accepting new Sea_Ticket claims. In-flight work    |
|       |                             | continues normally.                                     |
| 2     | ``DRAIN_CURRENT_FINISH``    | No new claims AND no new tool calls. Current attempt    |
|       |                             | finishes its already-dispatched operations, then        |
|       |                             | releases.                                               |
| 3     | ``ABORT_RELEASE_CLAIM``     | Immediately release the active claim via                |
|       |                             | ``kora__release_claim`` (KR-P2-E ST5 deferred).         |
| 4     | ``EXTERNAL_KILL``           | Operator-side ``flyctl machine stop``. Runtime cannot   |
|       |                             | self-kill; this is informational from the runtime's     |
|       |                             | side — caller emits an enforced ack and continues       |
|       |                             | (the OS will SIGTERM shortly).                          |
| 5     | ``EXTERNAL_KILL``           | Operator-side Fly stop + Doppler token revoke. Same     |
|       |                             | runtime-side semantics as L4.                           |

# Context-dependent blocking

The :class:`STOPKoraVerdict` returned by :func:`evaluate_stop_kora`
carries the level-derived action verbatim. Whether the caller should
*block* on that action depends on the context:

  - At ``pre_claim`` boundary: block on every action L1+ (including
    ``EXTERNAL_KILL`` — wasted work to start a claim moments before
    SIGTERM). Use :data:`BLOCKING_ACTIONS_PRE_CLAIM`.
  - At ``pre_tool_call`` boundary: block only on ``DRAIN_CURRENT_FINISH``
    and ``ABORT_RELEASE_CLAIM``. ``BLOCK_NEW_INTAKE`` (L1) is informational
    because intake has already happened; ``EXTERNAL_KILL`` (L4/L5) is
    informational because the OS will kill the process shortly and
    blocking the current tool call doesn't change that outcome. Use
    :data:`BLOCKING_ACTIONS_PRE_TOOL_CALL`.

The ``context`` parameter on :func:`evaluate_stop_kora` is currently
recorded for documentation + future extension; today's policy is
expressed by the two ``BLOCKING_ACTIONS_*`` sets so callers can branch
without re-deriving the level → context rules.

# DEGRADED is not handled here

DEGRADED is a separate concept from STOP-KORA (R4.1 §9.1 — DEGRADED
is the presence of ``degradation_reasons``, not a primary state).
DEGRADED-flagged states flip ``claim_permission`` to
``critical_only`` or ``none`` per §9.8; that's the operational-state
machine's responsibility (``agent/operational_state.py``), not this
module.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final, Literal, Mapping, Optional

from plugins.memory.isokron.kora_control_reader import KoraControlCommand


# ---------------------------------------------------------------------------
# Action enum + level → action map
# ---------------------------------------------------------------------------


class STOPKoraAction(Enum):
    """What the runtime does on observing a STOP-KORA command at this level.

    String values are stable contract — referenced by ST3's
    ``_build_stop_kora_block_result`` for the model-facing tool-result
    JSON discriminator, and by future cockpit-BFF / runtime contract
    manifest consumers.
    """

    RESET_CLEAR_LOWER = "reset_clear_lower"           # L0
    BLOCK_NEW_INTAKE = "block_new_intake"             # L1
    DRAIN_CURRENT_FINISH = "drain_current_finish"     # L2
    ABORT_RELEASE_CLAIM = "abort_release_claim"       # L3
    EXTERNAL_KILL = "external_kill"                   # L4 / L5


LEVEL_TO_ACTION: Final[Mapping[int, STOPKoraAction]] = {
    0: STOPKoraAction.RESET_CLEAR_LOWER,
    1: STOPKoraAction.BLOCK_NEW_INTAKE,
    2: STOPKoraAction.DRAIN_CURRENT_FINISH,
    3: STOPKoraAction.ABORT_RELEASE_CLAIM,
    4: STOPKoraAction.EXTERNAL_KILL,
    5: STOPKoraAction.EXTERNAL_KILL,
}


# Context literal — mirrors the cockpit-BFF watcher's vocabulary so
# both layers share one definition.
PreFlightContext = Literal["pre_claim", "pre_tool_call"]


# Actions that block the caller at each context. Caller checks
# ``verdict.action in BLOCKING_ACTIONS_<context>``.
BLOCKING_ACTIONS_PRE_CLAIM: Final[frozenset[STOPKoraAction]] = frozenset({
    STOPKoraAction.BLOCK_NEW_INTAKE,
    STOPKoraAction.DRAIN_CURRENT_FINISH,
    STOPKoraAction.ABORT_RELEASE_CLAIM,
    STOPKoraAction.EXTERNAL_KILL,
})

BLOCKING_ACTIONS_PRE_TOOL_CALL: Final[frozenset[STOPKoraAction]] = frozenset({
    STOPKoraAction.DRAIN_CURRENT_FINISH,
    STOPKoraAction.ABORT_RELEASE_CLAIM,
})


# ---------------------------------------------------------------------------
# Verdict + evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class STOPKoraVerdict:
    """Result of evaluating a tool-call (or pre-claim) against active STOP-KORA.

    - ``action`` is ``None`` when no STOP-KORA command is active OR when
      the active command is a level=0 reset (which has no caller-side
      action; the substrate ``issue_kora_control`` SECDEF handled the
      supersession side-effect at issue time).
    - ``action`` is the :class:`STOPKoraAction` for the command's level
      otherwise; whether the *caller* should block on that action
      depends on the context — see :data:`BLOCKING_ACTIONS_PRE_CLAIM` /
      :data:`BLOCKING_ACTIONS_PRE_TOOL_CALL`.
    - ``command_id`` and ``reason`` are propagated from the active command
      for downstream emit / log purposes.
    - ``context`` is the context the verdict was evaluated under,
      recorded for log + emit attribution.
    """

    action: Optional[STOPKoraAction]
    command_id: Optional[str]
    reason: Optional[str]
    context: Optional[PreFlightContext] = None

    def is_blocking(self) -> bool:
        """Whether the caller should block based on this verdict's
        ``action`` + ``context``.

        Equivalent to ``verdict.action in BLOCKING_ACTIONS_<context>``;
        provided as a method for caller convenience.
        """
        if self.action is None or self.context is None:
            return False
        if self.context == "pre_claim":
            return self.action in BLOCKING_ACTIONS_PRE_CLAIM
        if self.context == "pre_tool_call":
            return self.action in BLOCKING_ACTIONS_PRE_TOOL_CALL
        return False


def evaluate_stop_kora(
    command: Optional[KoraControlCommand],
    *,
    context: PreFlightContext,
) -> STOPKoraVerdict:
    """Map an active :class:`KoraControlCommand` to a :class:`STOPKoraVerdict`.

    Pure function — no I/O, no emit, no side effects.

    Decision order:
      - ``command is None`` → ``action=None`` (no STOP active)
      - ``command.kind == 'reset'`` → ``action=None`` (L0 reset has no
        caller-side action; supersession was the issue-time side-effect)
      - ``command.level`` not in 0-5 → ``action=None`` (defensive;
        substrate ``kora_control_level_check`` CHECK enforces 0-5,
        so unreachable in practice — fall-through is the safe default)
      - Otherwise → ``action = LEVEL_TO_ACTION[command.level]``

    Args:
        command: An active command from
            :meth:`KoraControlReader.get_active_command`, or ``None``.
        context: The caller's context (``pre_claim`` or
            ``pre_tool_call``). Recorded in the verdict for emit /
            log attribution; today's policy maps level → action
            uniformly, but the parameter is required so the call
            site documents which boundary fired the evaluation.

    Returns:
        :class:`STOPKoraVerdict` — see ``is_blocking()`` for caller
        branching against the context-dependent blocking sets.
    """
    if command is None:
        return STOPKoraVerdict(
            action=None, command_id=None, reason=None, context=context
        )

    # L0 reset — issuer-side supersession already happened in
    # ``issue_kora_control``; runtime has no action to take on the
    # reset itself. The reader's SELECT filters out kind='reset' rows,
    # so this branch is defensive (and exercised by direct
    # evaluate_stop_kora callers from tests).
    if command.kind == "reset":
        return STOPKoraVerdict(
            action=None,
            command_id=command.command_id,
            reason=command.reason,
            context=context,
        )

    action = LEVEL_TO_ACTION.get(command.level)
    if action is None:
        # Defensive: substrate CHECK constrains 0-5; if a future schema
        # extension adds a level we don't know about, treat as no-op
        # (fail-open rather than fail-closed — the alternative would
        # block all tool calls on an unrecognized level, which is too
        # aggressive for a future-compat path).
        return STOPKoraVerdict(
            action=None,
            command_id=command.command_id,
            reason=command.reason,
            context=context,
        )

    return STOPKoraVerdict(
        action=action,
        command_id=command.command_id,
        reason=command.reason,
        context=context,
    )
