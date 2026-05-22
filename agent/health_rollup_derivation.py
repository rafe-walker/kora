"""Health rollup derivation — overall / control-plane / worker (KR-P2-L ST3, R4.1 §9.7).

Maps the 8 subsignals (collected in
:mod:`agent.health_rollup_holder`) onto the three top-level
enums + ``stopped_reason``.

# Plane decomposition (R4.1 §9.7)

The R4.1 spec wants operators to distinguish "control plane is
healthy but worker isn't" (e.g. cost-ladder stopped the worker
but the substrate + auth are fine) from "everything is degraded."
Two plane functions encode the grouping:

  - **control_plane** = ``dispatch_reachable`` +
    ``auth_validity_window`` + ``escalation_watcher_liveness``
  - **worker** = ``claim_state`` + ``last_heartbeat`` +
    ``credit_burn`` + ``breaker_state`` +
    ``last_successful_write``

# Status mapping

For each plane:

  - any subsignal at :attr:`SubsignalStatus.DEGRADED` → plane is
    :attr:`HealthStatus.OUTAGE`
  - else any subsignal at :attr:`SubsignalStatus.STALE` or
    :attr:`SubsignalStatus.MISSING` → plane is
    :attr:`HealthStatus.DEGRADED`
  - else (all FRESH) → :attr:`HealthStatus.HEALTHY`

The escalation-watcher liveness signal carries a stable
``pending substrate ack`` note while substrate-team hasn't shipped
its liveness ping (KR-P2-L §1 verification result). That specific
``status=missing`` is treated as informational — it has its own
FE banner (P6) and does NOT degrade the plane. Other MISSING
signals (e.g. dispatch_reachable when no invokes have happened
yet) DO degrade because they represent real runtime gaps.

# Overall + stopped_reason

  - ``primary_state is PrimaryState.STOPPED`` → ``overall = stopped``;
    ``stopped_reason`` is the trigger that put the state there
    (provided by the caller — the holder doesn't query the
    operational-state history itself)
  - else if any subsignal at DEGRADED → ``outage`` +
    ``stopped_reason`` = first DEGRADED subsignal name
  - else if any subsignal at STALE/MISSING → ``degraded`` +
    ``stopped_reason = None``
  - else → ``healthy`` + ``stopped_reason = None``
"""

from __future__ import annotations

from typing import Optional

from agent.health_rollup_holder import (
    SUBSIGNAL_AUTH_VALIDITY_WINDOW,
    SUBSIGNAL_BREAKER_STATE,
    SUBSIGNAL_CLAIM_STATE,
    SUBSIGNAL_CREDIT_BURN,
    SUBSIGNAL_DISPATCH_REACHABLE,
    SUBSIGNAL_ESCALATION_WATCHER_LIVENESS,
    SUBSIGNAL_LAST_HEARTBEAT,
    SUBSIGNAL_LAST_SUCCESSFUL_WRITE,
    HealthStatus,
    Subsignal,
    SubsignalStatus,
)
from agent.operational_state import PrimaryState


# Plane decomposition per R4.1 §9.7.
CONTROL_PLANE_SUBSIGNALS = (
    SUBSIGNAL_DISPATCH_REACHABLE,
    SUBSIGNAL_AUTH_VALIDITY_WINDOW,
    SUBSIGNAL_ESCALATION_WATCHER_LIVENESS,
)

WORKER_SUBSIGNALS = (
    SUBSIGNAL_CLAIM_STATE,
    SUBSIGNAL_LAST_HEARTBEAT,
    SUBSIGNAL_CREDIT_BURN,
    SUBSIGNAL_BREAKER_STATE,
    SUBSIGNAL_LAST_SUCCESSFUL_WRITE,
)


def _is_pending_substrate_ack(signal: Subsignal) -> bool:
    """Sentinel: escalation_watcher_liveness's known-pending state.

    Carries the ``pending substrate ack`` note set by
    :func:`agent.health_rollup_holder.collect_escalation_watcher_liveness`.
    A MISSING signal with this note is informational (the FE has its
    own P6 banner) and does NOT degrade the plane.
    """
    if signal.name != SUBSIGNAL_ESCALATION_WATCHER_LIVENESS:
        return False
    if signal.status is not SubsignalStatus.MISSING:
        return False
    note = signal.extra.get("note", "") if signal.extra else ""
    return "pending substrate ack" in note


def _plane_status_from_subsignals(
    subsignals: dict[str, Subsignal], plane_names: tuple[str, ...]
) -> HealthStatus:
    """Project plane status from the subsignals belonging to the plane.

    Order of checks matches the docstring rule:
      DEGRADED → outage; STALE/MISSING → degraded; else healthy.
    """
    has_degraded = False
    has_soft = False
    for name in plane_names:
        signal = subsignals.get(name)
        if signal is None:
            # A missing subsignal IS a degradation — surface it.
            has_soft = True
            continue
        if _is_pending_substrate_ack(signal):
            continue
        if signal.status is SubsignalStatus.DEGRADED:
            has_degraded = True
        elif signal.status in (SubsignalStatus.STALE, SubsignalStatus.MISSING):
            has_soft = True
    if has_degraded:
        return HealthStatus.OUTAGE
    if has_soft:
        return HealthStatus.DEGRADED
    return HealthStatus.HEALTHY


def derive_control_plane_health(
    subsignals: dict[str, Subsignal],
) -> HealthStatus:
    """Control plane: dispatch + auth + escalation watcher."""
    return _plane_status_from_subsignals(subsignals, CONTROL_PLANE_SUBSIGNALS)


def derive_worker_health(subsignals: dict[str, Subsignal]) -> HealthStatus:
    """Worker: claim + heartbeat + credit_burn + breaker + last_write."""
    return _plane_status_from_subsignals(subsignals, WORKER_SUBSIGNALS)


def derive_overall_status(
    subsignals: dict[str, Subsignal],
    *,
    primary_state: Optional[PrimaryState] = None,
    stopped_trigger: Optional[str] = None,
) -> tuple[HealthStatus, Optional[str]]:
    """Roll up the 8 subsignals + operational primary state.

    Args:
        subsignals: All 8 subsignals (keyed by name) — typically the
            full :attr:`HealthRollup.subsignals`.
        primary_state: Current operational primary state. ``STOPPED``
            forces ``overall = stopped``.
        stopped_trigger: When ``primary_state == STOPPED``, the
            trigger that put us there — surfaced verbatim as
            ``stopped_reason``. ``None`` falls back to a generic
            label.

    Returns:
        ``(overall, stopped_reason)``. ``stopped_reason`` is non-None
        only when ``overall ∈ {stopped, outage}`` (per R4.1 §9.7
        contract pinned by ``tests/kora_cli/test_web_server_health_rollup.py``).
    """
    if primary_state is PrimaryState.STOPPED:
        return (
            HealthStatus.STOPPED,
            stopped_trigger or "operational state is STOPPED",
        )

    # Find the first DEGRADED subsignal to surface as stopped_reason
    # when overall=outage. Iteration order is insertion order
    # (Python 3.7+ dict guarantee).
    degraded_name: Optional[str] = None
    has_soft = False
    for name, signal in subsignals.items():
        if _is_pending_substrate_ack(signal):
            continue
        if signal.status is SubsignalStatus.DEGRADED:
            if degraded_name is None:
                degraded_name = name
        elif signal.status in (SubsignalStatus.STALE, SubsignalStatus.MISSING):
            has_soft = True

    if degraded_name is not None:
        return (HealthStatus.OUTAGE, f"{degraded_name} degraded")
    if has_soft:
        return (HealthStatus.DEGRADED, None)
    return (HealthStatus.HEALTHY, None)
