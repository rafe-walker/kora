"""R4.1 §9.1 operational state machine — enums + transition table.

**KR-P2-I-skeleton bucket — code-only, no emit, no wire-in.** Chain-event
emission on transition lands in KR-P2-I-integration once substrate-round
Bucket C adds the operational-state event-type literals
(``kora.boot.ready``, ``kora.boot.failed``, etc.) to the
``event_log_event_type_check`` constraint set. The agent-loop integration
also waits on KR-P2-H (boot gates module).

Why ship the skeleton now: CC#2's Operational State admin panel imports
:class:`PrimaryState`, :class:`DegradationReason`, and
:class:`ClaimPermission` for type safety. Decoupling that frontend work
from the substrate-round shortens the critical path.

# What's here

- :class:`PrimaryState` — 5-member primary-state enum
  (``BOOTING``/``READY``/``ACTIVE``/``PAUSED``/``STOPPED``).
- :class:`DegradationReason` — 8-member reason enum; multiple may be
  active simultaneously (set membership in
  :attr:`OperationalState.degradation_reasons`).
- :class:`ClaimPermission` — 3-member permission enum
  (``none``/``critical_only``/``normal``).
- :class:`OperationalState` — frozen dataclass snapshot of the runtime
  posture (primary_state + degradation_reasons + claim_permission).
- :class:`StateTransition` — frozen dataclass for one transition-table
  row.
- :data:`TRANSITION_TABLE` — tuple of allowed transitions per R4.1 §9.1,
  with the doc's ``any → X`` shorthand expanded into one row per
  concrete ``from_state``.
- :func:`is_valid_transition`, :func:`transitions_from`,
  :func:`transitions_to` — read-only query helpers.

# DEGRADED is not a primary_state

Per R4.1 §9.1: *"DEGRADED is the presence of ``degradation_reasons``,
not a ``primary_state``."* It is modeled here as the derived flag
:meth:`OperationalState.is_degraded`, never as an enum member.

# What's deliberately NOT here

- No append-event MCP calls (emit on transition lands in
  KR-P2-I-integration after substrate-round Bucket C).
- No agent-loop wire-in (also KR-P2-I-integration).
- No startup hooks anywhere (``gateway/run.py``, ``kora_cli/__main__.py``,
  etc.).
- No IsoKron memory-provider integration or actor-loop coupling.
- No ``OperationalStateManager`` singleton / service-locator.
- No persistence — in-memory only.

Enum string values are part of the public contract: CC#2's admin panel
and the future substrate-side chain-event payloads consume them
verbatim. Treat ``Enum.value`` strings as load-bearing wire format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet


class PrimaryState(Enum):
    """R4.1 §9.1 primary-state alphabet.

    Five members; values are the lower-case strings used in the chain
    event payloads and the admin-panel API.
    """

    BOOTING = "booting"
    READY = "ready"
    ACTIVE = "active"
    PAUSED = "paused"
    STOPPED = "stopped"


class DegradationReason(Enum):
    """Reasons the runtime is degraded.

    Multiple may be active simultaneously — modeled as set membership in
    :attr:`OperationalState.degradation_reasons`. Per R4.1 §9.1 the
    ``DEGRADED`` flag is the *presence* of any reason, not its own
    primary_state.
    """

    COST = "cost"
    AUTH = "auth"
    DISPATCH = "dispatch"
    SUBSTRATE = "substrate"
    MIGRATION = "migration"
    OPERATOR = "operator"
    TOKEN_EXPIRING = "token_expiring"
    RETRY_CEILING = "retry_ceiling"


class ClaimPermission(Enum):
    """Whether the consumer loop may mint new claims.

    STOP-KORA L1 (per R4.1 §9.1) flips this to ``NONE`` while a held
    claim is allowed to finish; DEGRADED-flagged states can flip to
    ``CRITICAL_ONLY`` per the dispatch/substrate/auth check rules in
    §9.8.
    """

    NONE = "none"
    CRITICAL_ONLY = "critical_only"
    NORMAL = "normal"


class HeartbeatProgress(Enum):
    """R4.1 §9.3 P2 — what counts as "heartbeat progress" for the
    cockpit-BFF SLA-exception watcher.

    Pinned per the verification-5 ruling on KR-P2-J: this is a
    **documentation/type artifact** for the Runtime Contract, NOT a
    chain-emit literal. The two members enumerate the exact signals
    that satisfy the SLA-exception clause:

    - ``STREAMING_TOKEN_ADVANCED`` — the SDK streaming-token-event
      counter advanced for an in-flight inference call. Observable
      out-of-band (SDK telemetry / structured logs); not a substrate
      artifact.

    - ``TOOL_CALL_BOUNDARY`` — the agent loop crossed a tool-call
      boundary (next dispatch started). Observable from
      ``public.kora_operation_ledger.updated_at`` — the ledger row
      ``updated_at`` is auto-touched by the
      ``_kora_operation_ledger_touch_updated_at`` trigger on every
      status transition, so the cockpit-BFF watcher's ≤5s poll sees
      the advancement without a dedicated chain emit.

    A bare liveness pulse (e.g. an HTTP /alive ping that always
    returns 200 regardless of agent state) does NOT satisfy the
    exception. Listed here so the runtime + cockpit BFF share one
    definition.

    # Why no emit function

    There is no ``kora.heartbeat.*`` literal in
    ``foundation/0159_kora_r41_operational_state_event_vocabulary.sql``
    — adding a per-tool-call emit would be the audit-class flood the
    substrate's terminal-only-emit design avoids. The cockpit-BFF
    watcher reads the progress condition from substrate table
    side-effects (``kora_operation_ledger.updated_at``,
    ``kora_control.acknowledged_at``), not from chain events.

    Future helper that may read this enum: a ``is_progress_signal_recent``
    query helper for KR-P2-I-integration's drain-state logic. Not
    needed for KR-P2-J ST3.
    """

    STREAMING_TOKEN_ADVANCED = "streaming_token_advanced"
    TOOL_CALL_BOUNDARY = "tool_call_boundary"


@dataclass(frozen=True, slots=True)
class OperationalState:
    """Snapshot of Kora's runtime operational state.

    Immutable; use the :meth:`with_*` factory methods to produce derived
    states. No emit logic here — chain event emission on transition
    lives in the KR-P2-I-integration follow-on bucket.
    """

    primary_state: PrimaryState
    degradation_reasons: FrozenSet[DegradationReason] = field(default_factory=frozenset)
    claim_permission: ClaimPermission = ClaimPermission.NORMAL

    def is_degraded(self) -> bool:
        """True iff at least one degradation reason is active.

        Per R4.1 §9.1: DEGRADED is *the presence of*
        ``degradation_reasons``, not a primary_state.
        """
        return bool(self.degradation_reasons)

    def with_added_reason(
        self, reason: DegradationReason
    ) -> "OperationalState":
        """Return a derived state with ``reason`` added to the set."""
        return OperationalState(
            primary_state=self.primary_state,
            degradation_reasons=self.degradation_reasons | {reason},
            claim_permission=self.claim_permission,
        )

    def with_removed_reason(
        self, reason: DegradationReason
    ) -> "OperationalState":
        """Return a derived state with ``reason`` removed from the set.

        No-op (returns an equal-shape instance) if ``reason`` is not
        currently present — matches the set-difference semantics.
        """
        return OperationalState(
            primary_state=self.primary_state,
            degradation_reasons=self.degradation_reasons - {reason},
            claim_permission=self.claim_permission,
        )

    def with_primary_state(
        self, new_state: PrimaryState
    ) -> "OperationalState":
        """Return a derived state with the primary_state replaced."""
        return OperationalState(
            primary_state=new_state,
            degradation_reasons=self.degradation_reasons,
            claim_permission=self.claim_permission,
        )

    def with_claim_permission(
        self, new_permission: ClaimPermission
    ) -> "OperationalState":
        """Return a derived state with the claim_permission replaced."""
        return OperationalState(
            primary_state=self.primary_state,
            degradation_reasons=self.degradation_reasons,
            claim_permission=new_permission,
        )


@dataclass(frozen=True, slots=True)
class StateTransition:
    """One row of the R4.1 §9.1 transition table.

    ``trigger`` / ``guard`` / ``recovery_owner`` are human-readable
    strings matching the R4.1 column wording. They are not parsed —
    they document operator-facing semantics and surface in admin-panel
    "why this transition" tooltips. An empty string means the R4.1
    cell was a dash (no guard / no specific recovery owner).
    """

    from_state: PrimaryState
    to_state: PrimaryState
    trigger: str
    guard: str
    recovery_owner: str


# R4.1 §9.1 expanded transition table. The R4.1 doc uses ``any → PAUSED``
# and ``any → STOPPED`` shorthand; we expand each into one row per
# concrete ``from_state`` so :func:`is_valid_transition` can do a single
# scan. PAUSED → PAUSED via STOP-KORA L1–3 is intentionally omitted
# (PAUSED is already paused — no observable change).
#
# Duplicate ``(from_state, to_state)`` pairs are expected and represent
# the same arrow reached through different triggers (e.g.
# BOOTING → STOPPED via "invariant gate failure" vs "STOP-KORA L4/L5";
# BOOTING → PAUSED via "gate 3b epoch mismatch" vs "STOP-KORA L1–3").
TRANSITION_TABLE: tuple[StateTransition, ...] = (
    # ── Cold boot (§9.2) ────────────────────────────────────────────
    StateTransition(
        from_state=PrimaryState.BOOTING,
        to_state=PrimaryState.READY,
        trigger="all §9.2 gates pass",
        guard="",
        recovery_owner="",
    ),
    StateTransition(
        from_state=PrimaryState.BOOTING,
        to_state=PrimaryState.BOOTING,
        trigger="transient gate failure",
        guard="retry budget not exhausted",
        recovery_owner="self (backoff)",
    ),
    StateTransition(
        from_state=PrimaryState.BOOTING,
        to_state=PrimaryState.STOPPED,
        trigger="invariant gate failure, or retry budget exhausted",
        guard="",
        recovery_owner="operator",
    ),
    StateTransition(
        from_state=PrimaryState.BOOTING,
        to_state=PrimaryState.PAUSED,
        trigger="gate 3b epoch mismatch (§9.8)",
        guard="",
        recovery_owner="operator",
    ),
    # ── READY ↔ ACTIVE (claim acquire/release) ───────────────────────
    StateTransition(
        from_state=PrimaryState.READY,
        to_state=PrimaryState.ACTIVE,
        trigger="claim acquired",
        guard="",
        recovery_owner="self",
    ),
    StateTransition(
        from_state=PrimaryState.ACTIVE,
        to_state=PrimaryState.READY,
        trigger="claim released",
        guard="",
        recovery_owner="self",
    ),
    # ── any → PAUSED (STOP-KORA L1–3, cost 100%, operator) ──────────
    StateTransition(
        from_state=PrimaryState.READY,
        to_state=PrimaryState.PAUSED,
        trigger="STOP-KORA L1–3, cost 100%, operator",
        guard="",
        recovery_owner="per reason",
    ),
    StateTransition(
        from_state=PrimaryState.ACTIVE,
        to_state=PrimaryState.PAUSED,
        trigger="STOP-KORA L1–3, cost 100%, operator",
        guard="",
        recovery_owner="per reason",
    ),
    StateTransition(
        from_state=PrimaryState.BOOTING,
        to_state=PrimaryState.PAUSED,
        trigger="STOP-KORA L1–3, cost 100%, operator",
        guard="",
        recovery_owner="per reason",
    ),
    # ── PAUSED → READY (cost-clear / operator-clear) ────────────────
    StateTransition(
        from_state=PrimaryState.PAUSED,
        to_state=PrimaryState.READY,
        trigger="monthly credit refresh confirmed",
        guard=(
            "reconciled spend < threshold; "
            "claim already safe-released (§9.6)"
        ),
        recovery_owner="self (ramped, §9.6)",
    ),
    StateTransition(
        from_state=PrimaryState.PAUSED,
        to_state=PrimaryState.READY,
        trigger="operator clears via kora_control reset",
        guard="",
        recovery_owner="operator",
    ),
    # ── any → STOPPED (STOP-KORA L4/L5) ─────────────────────────────
    StateTransition(
        from_state=PrimaryState.READY,
        to_state=PrimaryState.STOPPED,
        trigger="STOP-KORA L4/L5",
        guard="",
        recovery_owner="operator (new boot)",
    ),
    StateTransition(
        from_state=PrimaryState.ACTIVE,
        to_state=PrimaryState.STOPPED,
        trigger="STOP-KORA L4/L5",
        guard="",
        recovery_owner="operator (new boot)",
    ),
    StateTransition(
        from_state=PrimaryState.PAUSED,
        to_state=PrimaryState.STOPPED,
        trigger="STOP-KORA L4/L5",
        guard="",
        recovery_owner="operator (new boot)",
    ),
    StateTransition(
        from_state=PrimaryState.BOOTING,
        to_state=PrimaryState.STOPPED,
        trigger="STOP-KORA L4/L5",
        guard="",
        recovery_owner="operator (new boot)",
    ),
)


def is_valid_transition(
    from_state: PrimaryState, to_state: PrimaryState
) -> bool:
    """Return ``True`` iff at least one :data:`TRANSITION_TABLE` row
    matches ``(from_state, to_state)``.

    Multiple rows may match the same pair (same arrow, different
    triggers); a single match suffices.
    """
    return any(
        row.from_state is from_state and row.to_state is to_state
        for row in TRANSITION_TABLE
    )


def transitions_from(
    state: PrimaryState,
) -> tuple[StateTransition, ...]:
    """All :data:`TRANSITION_TABLE` rows originating at ``state``."""
    return tuple(row for row in TRANSITION_TABLE if row.from_state is state)


def transitions_to(
    state: PrimaryState,
) -> tuple[StateTransition, ...]:
    """All :data:`TRANSITION_TABLE` rows arriving at ``state``."""
    return tuple(row for row in TRANSITION_TABLE if row.to_state is state)
