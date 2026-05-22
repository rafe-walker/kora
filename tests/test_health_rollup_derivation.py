"""KR-P2-L ST3 — tests for ``agent/health_rollup_derivation.py``.

Covers:
  - derive_overall_status: STOPPED short-circuit + DEGRADED→outage +
    STALE/MISSING→degraded + all-fresh→healthy
  - derive_control_plane_health: dispatch + auth + escalation_watcher
    subset
  - derive_worker_health: claim + heartbeat + credit_burn + breaker
    + last_write subset
  - escalation_watcher_liveness pending-ack sentinel: MISSING +
    "pending substrate ack" note → excluded from rollup; OTHER
    MISSING signals still count
  - stopped_reason population: non-None only when overall ∈
    {stopped, outage}, per the test_web_server_health_rollup.py
    contract
"""

from __future__ import annotations

import pytest

from agent.health_rollup_derivation import (
    CONTROL_PLANE_SUBSIGNALS,
    WORKER_SUBSIGNALS,
    derive_control_plane_health,
    derive_overall_status,
    derive_worker_health,
)
from agent.health_rollup_holder import (
    ALL_SUBSIGNAL_NAMES,
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


def _fresh(name: str) -> Subsignal:
    return Subsignal(name=name, status=SubsignalStatus.FRESH)


def _stale(name: str) -> Subsignal:
    return Subsignal(name=name, status=SubsignalStatus.STALE)


def _degraded(name: str) -> Subsignal:
    return Subsignal(name=name, status=SubsignalStatus.DEGRADED)


def _missing(name: str, *, note: str = "") -> Subsignal:
    extra = {"note": note} if note else {}
    return Subsignal(name=name, status=SubsignalStatus.MISSING, extra=extra)


def _all_fresh() -> dict[str, Subsignal]:
    return {name: _fresh(name) for name in ALL_SUBSIGNAL_NAMES}


# ---------------------------------------------------------------------------
# derive_overall_status — STOPPED short-circuit
# ---------------------------------------------------------------------------


def test_overall_stopped_when_primary_state_is_stopped():
    overall, reason = derive_overall_status(
        _all_fresh(),
        primary_state=PrimaryState.STOPPED,
        stopped_trigger="operator STOP-KORA L4",
    )
    assert overall is HealthStatus.STOPPED
    assert reason == "operator STOP-KORA L4"


def test_overall_stopped_with_no_trigger_uses_generic_reason():
    overall, reason = derive_overall_status(
        _all_fresh(),
        primary_state=PrimaryState.STOPPED,
        stopped_trigger=None,
    )
    assert overall is HealthStatus.STOPPED
    assert reason is not None
    assert "STOPPED" in reason


def test_overall_stopped_short_circuits_even_with_degraded_subsignals():
    """If runtime is STOPPED, that's the dominant signal — DEGRADED
    subsignals don't elevate to outage."""
    subs = _all_fresh()
    subs[SUBSIGNAL_DISPATCH_REACHABLE] = _degraded(SUBSIGNAL_DISPATCH_REACHABLE)
    overall, reason = derive_overall_status(
        subs, primary_state=PrimaryState.STOPPED, stopped_trigger="t"
    )
    assert overall is HealthStatus.STOPPED


# ---------------------------------------------------------------------------
# derive_overall_status — outage from any DEGRADED
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("degraded_name", list(ALL_SUBSIGNAL_NAMES))
def test_overall_outage_when_any_subsignal_degraded(degraded_name):
    subs = _all_fresh()
    subs[degraded_name] = _degraded(degraded_name)
    overall, reason = derive_overall_status(subs, primary_state=PrimaryState.READY)
    assert overall is HealthStatus.OUTAGE
    assert reason is not None
    assert degraded_name in reason


def test_overall_outage_reason_picks_first_degraded():
    """Iteration order = insertion order; first DEGRADED wins."""
    # Build dict with controlled order: dispatch first, claim second
    subs = {
        SUBSIGNAL_DISPATCH_REACHABLE: _degraded(SUBSIGNAL_DISPATCH_REACHABLE),
        SUBSIGNAL_CLAIM_STATE: _degraded(SUBSIGNAL_CLAIM_STATE),
    }
    for name in ALL_SUBSIGNAL_NAMES:
        subs.setdefault(name, _fresh(name))
    overall, reason = derive_overall_status(subs, primary_state=PrimaryState.READY)
    assert overall is HealthStatus.OUTAGE
    assert SUBSIGNAL_DISPATCH_REACHABLE in reason


# ---------------------------------------------------------------------------
# derive_overall_status — degraded from STALE/MISSING
# ---------------------------------------------------------------------------


def test_overall_degraded_when_subsignal_stale():
    subs = _all_fresh()
    subs[SUBSIGNAL_LAST_HEARTBEAT] = _stale(SUBSIGNAL_LAST_HEARTBEAT)
    overall, reason = derive_overall_status(subs, primary_state=PrimaryState.READY)
    assert overall is HealthStatus.DEGRADED
    assert reason is None  # not stopped/outage → no reason


def test_overall_degraded_when_subsignal_missing():
    subs = _all_fresh()
    subs[SUBSIGNAL_CREDIT_BURN] = _missing(SUBSIGNAL_CREDIT_BURN)
    overall, reason = derive_overall_status(subs, primary_state=PrimaryState.READY)
    assert overall is HealthStatus.DEGRADED
    assert reason is None


def test_overall_healthy_when_all_fresh():
    overall, reason = derive_overall_status(
        _all_fresh(), primary_state=PrimaryState.READY
    )
    assert overall is HealthStatus.HEALTHY
    assert reason is None


def test_overall_healthy_with_no_primary_state():
    """No operational holder available → derivation skips STOPPED
    short-circuit but otherwise proceeds normally."""
    overall, _ = derive_overall_status(_all_fresh(), primary_state=None)
    assert overall is HealthStatus.HEALTHY


# ---------------------------------------------------------------------------
# escalation_watcher_liveness pending-ack sentinel
# ---------------------------------------------------------------------------


def test_pending_substrate_ack_missing_does_not_degrade_overall():
    """The escalation_watcher_liveness MISSING with note 'pending
    substrate ack' is informational — it has its own FE banner and
    must NOT degrade overall."""
    subs = _all_fresh()
    subs[SUBSIGNAL_ESCALATION_WATCHER_LIVENESS] = _missing(
        SUBSIGNAL_ESCALATION_WATCHER_LIVENESS,
        note="watcher liveness signal pending substrate ack",
    )
    overall, reason = derive_overall_status(subs, primary_state=PrimaryState.READY)
    assert overall is HealthStatus.HEALTHY
    assert reason is None


def test_other_missing_signal_DOES_degrade_overall():
    subs = _all_fresh()
    subs[SUBSIGNAL_DISPATCH_REACHABLE] = _missing(SUBSIGNAL_DISPATCH_REACHABLE)
    overall, _ = derive_overall_status(subs, primary_state=PrimaryState.READY)
    assert overall is HealthStatus.DEGRADED


def test_escalation_watcher_stale_DOES_degrade_overall():
    """The 'pending substrate ack' bypass is MISSING-only — a STALE
    watcher signal IS a real degradation."""
    subs = _all_fresh()
    subs[SUBSIGNAL_ESCALATION_WATCHER_LIVENESS] = _stale(
        SUBSIGNAL_ESCALATION_WATCHER_LIVENESS
    )
    overall, _ = derive_overall_status(subs, primary_state=PrimaryState.READY)
    assert overall is HealthStatus.DEGRADED


def test_escalation_watcher_degraded_DOES_outage_overall():
    subs = _all_fresh()
    subs[SUBSIGNAL_ESCALATION_WATCHER_LIVENESS] = _degraded(
        SUBSIGNAL_ESCALATION_WATCHER_LIVENESS
    )
    overall, _ = derive_overall_status(subs, primary_state=PrimaryState.READY)
    assert overall is HealthStatus.OUTAGE


# ---------------------------------------------------------------------------
# derive_control_plane_health
# ---------------------------------------------------------------------------


def test_control_plane_healthy_when_all_relevant_fresh():
    subs = _all_fresh()
    assert derive_control_plane_health(subs) is HealthStatus.HEALTHY


def test_control_plane_only_uses_control_plane_subsignals():
    """Worker subsignals being degraded should NOT affect control_plane."""
    subs = _all_fresh()
    for name in WORKER_SUBSIGNALS:
        subs[name] = _degraded(name)
    assert derive_control_plane_health(subs) is HealthStatus.HEALTHY


def test_control_plane_outage_when_dispatch_degraded():
    subs = _all_fresh()
    subs[SUBSIGNAL_DISPATCH_REACHABLE] = _degraded(SUBSIGNAL_DISPATCH_REACHABLE)
    assert derive_control_plane_health(subs) is HealthStatus.OUTAGE


def test_control_plane_degraded_when_auth_stale():
    subs = _all_fresh()
    subs[SUBSIGNAL_AUTH_VALIDITY_WINDOW] = _stale(SUBSIGNAL_AUTH_VALIDITY_WINDOW)
    assert derive_control_plane_health(subs) is HealthStatus.DEGRADED


def test_control_plane_excludes_pending_ack_escalation_watcher():
    """When escalation_watcher is MISSING with pending-ack note +
    all other control_plane subsignals fresh, plane should be
    HEALTHY (not DEGRADED)."""
    subs = _all_fresh()
    subs[SUBSIGNAL_ESCALATION_WATCHER_LIVENESS] = _missing(
        SUBSIGNAL_ESCALATION_WATCHER_LIVENESS,
        note="watcher liveness signal pending substrate ack",
    )
    assert derive_control_plane_health(subs) is HealthStatus.HEALTHY


# ---------------------------------------------------------------------------
# derive_worker_health
# ---------------------------------------------------------------------------


def test_worker_healthy_when_all_relevant_fresh():
    subs = _all_fresh()
    assert derive_worker_health(subs) is HealthStatus.HEALTHY


def test_worker_only_uses_worker_subsignals():
    """Control-plane subsignals being degraded should NOT affect worker."""
    subs = _all_fresh()
    for name in CONTROL_PLANE_SUBSIGNALS:
        subs[name] = _degraded(name)
    assert derive_worker_health(subs) is HealthStatus.HEALTHY


def test_worker_outage_when_breaker_degraded():
    subs = _all_fresh()
    subs[SUBSIGNAL_BREAKER_STATE] = _degraded(SUBSIGNAL_BREAKER_STATE)
    assert derive_worker_health(subs) is HealthStatus.OUTAGE


def test_worker_degraded_when_credit_burn_stale():
    subs = _all_fresh()
    subs[SUBSIGNAL_CREDIT_BURN] = _stale(SUBSIGNAL_CREDIT_BURN)
    assert derive_worker_health(subs) is HealthStatus.DEGRADED


def test_worker_degraded_when_last_heartbeat_missing():
    subs = _all_fresh()
    subs[SUBSIGNAL_LAST_HEARTBEAT] = _missing(SUBSIGNAL_LAST_HEARTBEAT)
    assert derive_worker_health(subs) is HealthStatus.DEGRADED


# ---------------------------------------------------------------------------
# Plane decomposition completeness
# ---------------------------------------------------------------------------


def test_plane_groupings_cover_all_8_subsignals():
    """Every subsignal belongs to exactly one plane (or is the
    escalation_watcher which sits in control_plane)."""
    union = set(CONTROL_PLANE_SUBSIGNALS) | set(WORKER_SUBSIGNALS)
    assert union == set(ALL_SUBSIGNAL_NAMES)


def test_plane_groupings_are_disjoint():
    overlap = set(CONTROL_PLANE_SUBSIGNALS) & set(WORKER_SUBSIGNALS)
    assert overlap == set()


# ---------------------------------------------------------------------------
# stopped_reason contract pin — non-null only when overall ∈ {stopped, outage}
# ---------------------------------------------------------------------------


def test_stopped_reason_null_when_overall_healthy():
    overall, reason = derive_overall_status(
        _all_fresh(), primary_state=PrimaryState.READY
    )
    assert overall is HealthStatus.HEALTHY
    assert reason is None


def test_stopped_reason_null_when_overall_degraded():
    subs = _all_fresh()
    subs[SUBSIGNAL_CREDIT_BURN] = _stale(SUBSIGNAL_CREDIT_BURN)
    overall, reason = derive_overall_status(subs, primary_state=PrimaryState.READY)
    assert overall is HealthStatus.DEGRADED
    assert reason is None


def test_stopped_reason_non_null_when_overall_outage():
    subs = _all_fresh()
    subs[SUBSIGNAL_DISPATCH_REACHABLE] = _degraded(SUBSIGNAL_DISPATCH_REACHABLE)
    overall, reason = derive_overall_status(subs, primary_state=PrimaryState.READY)
    assert overall is HealthStatus.OUTAGE
    assert reason is not None


def test_stopped_reason_non_null_when_overall_stopped():
    overall, reason = derive_overall_status(
        _all_fresh(),
        primary_state=PrimaryState.STOPPED,
        stopped_trigger="op-stop",
    )
    assert overall is HealthStatus.STOPPED
    assert reason is not None
