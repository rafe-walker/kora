"""KR-P2-L ST1 — unit tests for ``agent/health_rollup_holder.py``.

Covers:
  - 8 collector functions, each across MISSING / FRESH / STALE /
    DEGRADED status branches where applicable.
  - HealthRollupHolder.current() wires the collectors against live
    singletons; uninitialized deps still produce a complete rollup
    shape with the right MISSING placeholders.
  - rollup_to_panel_payload projects to the JSON shape pinned by the
    HEALTH-PANEL stub in ``kora_cli/web_server.py``.
  - Probe cadence env override + invalid-value fallback.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from agent.health_rollup_holder import (
    ALL_SUBSIGNAL_NAMES,
    DEFAULT_AUTH_VALIDITY_THRESHOLD_DAYS,
    DEFAULT_DISPATCH_REACHABLE_THRESHOLD_SECONDS,
    DEFAULT_ESCALATION_WATCHER_THRESHOLD_SECONDS,
    DEFAULT_LAST_HEARTBEAT_THRESHOLD_SECONDS,
    DEFAULT_LAST_SUCCESSFUL_WRITE_THRESHOLD_SECONDS,
    DEFAULT_PROBE_CADENCE_SECONDS,
    ENV_PROBE_CADENCE_SECONDS,
    HealthRollupHolder,
    HealthStatus,
    SUBSIGNAL_AUTH_VALIDITY_WINDOW,
    SUBSIGNAL_BREAKER_STATE,
    SUBSIGNAL_CLAIM_STATE,
    SUBSIGNAL_CREDIT_BURN,
    SUBSIGNAL_DISPATCH_REACHABLE,
    SUBSIGNAL_ESCALATION_WATCHER_LIVENESS,
    SUBSIGNAL_LAST_HEARTBEAT,
    SUBSIGNAL_LAST_SUCCESSFUL_WRITE,
    Subsignal,
    SubsignalStatus,
    _reset_health_rollup_holder_for_tests,
    collect_auth_validity_window,
    collect_breaker_state,
    collect_claim_state,
    collect_credit_burn,
    collect_dispatch_reachable,
    collect_escalation_watcher_liveness,
    collect_last_heartbeat,
    collect_last_successful_write,
    init_health_rollup_holder,
    rollup_to_panel_payload,
)


@pytest.fixture(autouse=True)
def _reset():
    _reset_health_rollup_holder_for_tests()
    yield
    _reset_health_rollup_holder_for_tests()


_NOW = datetime(2026, 5, 21, 22, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# collect_last_successful_write
# ---------------------------------------------------------------------------


def test_last_successful_write_missing_when_none():
    sig = collect_last_successful_write(last_write_at=None, now=_NOW)
    assert sig.name == SUBSIGNAL_LAST_SUCCESSFUL_WRITE
    assert sig.status is SubsignalStatus.MISSING
    assert sig.elapsed_seconds is None
    assert sig.threshold_seconds == DEFAULT_LAST_SUCCESSFUL_WRITE_THRESHOLD_SECONDS


def test_last_successful_write_fresh_when_within_threshold():
    last = _NOW - timedelta(seconds=120)
    sig = collect_last_successful_write(last_write_at=last, now=_NOW)
    assert sig.status is SubsignalStatus.FRESH
    assert sig.elapsed_seconds == 120
    assert sig.extra["value_at"] == "2026-05-21T22:28:00Z"


def test_last_successful_write_stale_past_threshold():
    last = _NOW - timedelta(seconds=350)  # 300s threshold
    sig = collect_last_successful_write(last_write_at=last, now=_NOW)
    assert sig.status is SubsignalStatus.STALE
    assert sig.elapsed_seconds == 350


# ---------------------------------------------------------------------------
# collect_claim_state
# ---------------------------------------------------------------------------


def test_claim_state_idle_when_no_active_claim():
    sig = collect_claim_state(current_claim=None)
    assert sig.name == SUBSIGNAL_CLAIM_STATE
    assert sig.status is SubsignalStatus.FRESH  # always fresh; value carries posture
    assert sig.extra["value"] == "idle"
    assert sig.extra["claim_id"] is None


def test_claim_state_active_when_claim_held():
    claim = SimpleNamespace(
        ticket_id="t-1",
        workspace_id="org_test",
        work_attempt_id="wa-1",
        claim_fence_token="tok-1",
        claimed_at=_NOW,
    )
    sig = collect_claim_state(current_claim=claim)
    assert sig.status is SubsignalStatus.FRESH
    assert sig.extra["value"] == "active"
    assert sig.extra["claim_id"] == "wa-1"
    assert sig.extra["ticket_id"] == "t-1"
    assert sig.last_seen == _NOW


# ---------------------------------------------------------------------------
# collect_credit_burn
# ---------------------------------------------------------------------------


class _FakeCostHolder:
    def __init__(self, pct: float, rung_value: str = "normal") -> None:
        self._pct = pct
        self._rung_value = rung_value

    def current_pct_used(self) -> float:
        return self._pct  # fraction 0..1

    def active_rung(self):
        return SimpleNamespace(value=self._rung_value)


def test_credit_burn_missing_when_holder_none():
    sig = collect_credit_burn(cost_holder=None)
    assert sig.name == SUBSIGNAL_CREDIT_BURN
    assert sig.status is SubsignalStatus.MISSING
    assert sig.extra["value_pct"] is None
    assert sig.extra["rung"] is None


def test_credit_burn_fresh_below_threshold():
    sig = collect_credit_burn(
        cost_holder=_FakeCostHolder(pct=0.50, rung_value="normal")
    )
    assert sig.status is SubsignalStatus.FRESH
    assert sig.extra["value_pct"] == 50.0
    assert sig.extra["rung"] == "normal"


def test_credit_burn_degraded_at_threshold():
    sig = collect_credit_burn(
        cost_holder=_FakeCostHolder(pct=0.92, rung_value="downshift_90")
    )
    assert sig.status is SubsignalStatus.DEGRADED


def test_credit_burn_degraded_at_hard_stop():
    sig = collect_credit_burn(
        cost_holder=_FakeCostHolder(pct=1.05, rung_value="hard_stop_100")
    )
    assert sig.status is SubsignalStatus.DEGRADED
    assert sig.extra["rung"] == "hard_stop_100"


def test_credit_burn_missing_when_holder_raises():
    class _Boom:
        def current_pct_used(self):
            raise RuntimeError("boom")

        def active_rung(self):
            raise RuntimeError("boom")

    sig = collect_credit_burn(cost_holder=_Boom())
    assert sig.status is SubsignalStatus.MISSING


# ---------------------------------------------------------------------------
# collect_breaker_state
# ---------------------------------------------------------------------------


def test_breaker_state_missing_when_holder_none():
    sig = collect_breaker_state(cost_holder=None)
    assert sig.name == SUBSIGNAL_BREAKER_STATE
    assert sig.status is SubsignalStatus.MISSING
    assert sig.extra["value"] == "unknown"


def test_breaker_state_closed_when_below_hard_stop():
    from agent.cost_state_holder import CostRung

    class _Holder:
        def active_rung(self):
            return CostRung.WARN_75

    sig = collect_breaker_state(cost_holder=_Holder())
    assert sig.status is SubsignalStatus.FRESH
    assert sig.extra["value"] == "closed"


def test_breaker_state_open_at_hard_stop():
    from agent.cost_state_holder import CostRung

    class _Holder:
        def active_rung(self):
            return CostRung.HARD_STOP_100

    sig = collect_breaker_state(cost_holder=_Holder())
    assert sig.status is SubsignalStatus.DEGRADED
    assert sig.extra["value"] == "open"


# ---------------------------------------------------------------------------
# collect_auth_validity_window
# ---------------------------------------------------------------------------


def test_auth_validity_missing_when_no_credentials():
    sig = collect_auth_validity_window(credentials=None, now=_NOW)
    assert sig.name == SUBSIGNAL_AUTH_VALIDITY_WINDOW
    assert sig.status is SubsignalStatus.MISSING
    assert sig.extra["days_remaining"] is None


def test_auth_validity_missing_when_no_expires_at():
    sig = collect_auth_validity_window(
        credentials={"accessToken": "cc-token"}, now=_NOW
    )
    assert sig.status is SubsignalStatus.MISSING


def test_auth_validity_fresh_well_before_expiry():
    # expiresAt is 90 days out (well past threshold 30)
    expires_at = _NOW + timedelta(days=90)
    sig = collect_auth_validity_window(
        credentials={"expiresAt": int(expires_at.timestamp() * 1000)},
        now=_NOW,
    )
    assert sig.status is SubsignalStatus.FRESH
    assert sig.extra["days_remaining"] >= 89
    assert sig.extra["threshold_days"] == DEFAULT_AUTH_VALIDITY_THRESHOLD_DAYS


def test_auth_validity_stale_within_threshold():
    expires_at = _NOW + timedelta(days=15)
    sig = collect_auth_validity_window(
        credentials={"expiresAt": int(expires_at.timestamp() * 1000)},
        now=_NOW,
    )
    assert sig.status is SubsignalStatus.STALE


def test_auth_validity_degraded_past_expiry():
    expires_at = _NOW - timedelta(days=1)
    sig = collect_auth_validity_window(
        credentials={"expiresAt": int(expires_at.timestamp() * 1000)},
        now=_NOW,
    )
    assert sig.status is SubsignalStatus.DEGRADED
    assert sig.extra["days_remaining"] < 0


def test_auth_validity_invalid_expires_at_value():
    """Malformed expiresAt (way past year 9999) falls back to MISSING."""
    sig = collect_auth_validity_window(
        credentials={"expiresAt": 10**18}, now=_NOW
    )
    assert sig.status is SubsignalStatus.MISSING


# ---------------------------------------------------------------------------
# collect_dispatch_reachable
# ---------------------------------------------------------------------------


def test_dispatch_reachable_missing_when_no_invoke():
    sig = collect_dispatch_reachable(last_invoke_at=None, now=_NOW)
    assert sig.status is SubsignalStatus.MISSING
    assert sig.threshold_seconds == DEFAULT_DISPATCH_REACHABLE_THRESHOLD_SECONDS


def test_dispatch_reachable_fresh_recent():
    sig = collect_dispatch_reachable(
        last_invoke_at=_NOW - timedelta(seconds=5), now=_NOW
    )
    assert sig.status is SubsignalStatus.FRESH
    assert sig.elapsed_seconds == 5


def test_dispatch_reachable_stale_past_60s():
    sig = collect_dispatch_reachable(
        last_invoke_at=_NOW - timedelta(seconds=120), now=_NOW
    )
    assert sig.status is SubsignalStatus.STALE


# ---------------------------------------------------------------------------
# collect_last_heartbeat
# ---------------------------------------------------------------------------


def test_last_heartbeat_missing_when_no_refresh():
    sig = collect_last_heartbeat(last_heartbeat_at=None, now=_NOW)
    assert sig.status is SubsignalStatus.MISSING
    assert sig.threshold_seconds == DEFAULT_LAST_HEARTBEAT_THRESHOLD_SECONDS


def test_last_heartbeat_fresh_within_90s():
    sig = collect_last_heartbeat(
        last_heartbeat_at=_NOW - timedelta(seconds=60), now=_NOW
    )
    assert sig.status is SubsignalStatus.FRESH


def test_last_heartbeat_stale_past_90s():
    sig = collect_last_heartbeat(
        last_heartbeat_at=_NOW - timedelta(seconds=120), now=_NOW
    )
    assert sig.status is SubsignalStatus.STALE


# ---------------------------------------------------------------------------
# collect_escalation_watcher_liveness
# ---------------------------------------------------------------------------


def test_escalation_watcher_always_missing_until_substrate_ack():
    sig = collect_escalation_watcher_liveness(now=_NOW)
    assert sig.name == SUBSIGNAL_ESCALATION_WATCHER_LIVENESS
    assert sig.status is SubsignalStatus.MISSING
    assert sig.threshold_seconds == DEFAULT_ESCALATION_WATCHER_THRESHOLD_SECONDS
    assert "pending substrate ack" in sig.extra["note"]


# ---------------------------------------------------------------------------
# HealthRollupHolder.current()
# ---------------------------------------------------------------------------


def test_holder_current_returns_all_8_subsignals_with_no_deps():
    """Uninitialized cost holder + no active provider + no poller +
    no credentials → all subsignals MISSING (except claim_state which
    reports idle), overall = degraded."""
    holder = HealthRollupHolder()
    rollup = holder.current()
    assert set(rollup.subsignals.keys()) == set(ALL_SUBSIGNAL_NAMES)
    # All 8 names present (R4.1 §9.7 contract).
    assert rollup.overall is HealthStatus.DEGRADED
    # Control plane / worker placeholder for ST1
    assert rollup.control_plane is HealthStatus.HEALTHY
    assert rollup.worker is HealthStatus.HEALTHY
    assert rollup.stopped_reason is None


def test_holder_current_overall_healthy_only_when_all_fresh(monkeypatch):
    """Forcing every collector to return FRESH should make overall=healthy."""

    def _fresh(name: str) -> Subsignal:
        return Subsignal(name=name, status=SubsignalStatus.FRESH)

    fake_subs = {name: _fresh(name) for name in ALL_SUBSIGNAL_NAMES}

    monkeypatch.setattr(
        HealthRollupHolder,
        "_collect_all_subsignals",
        lambda self, *, now: fake_subs,
    )

    holder = HealthRollupHolder()
    rollup = holder.current()
    assert rollup.overall is HealthStatus.HEALTHY


def test_holder_current_wires_cost_holder():
    """When cost holder is initialized, credit_burn + breaker_state
    pull from it."""
    from agent.cost_state_holder import (
        _reset_cost_holder_for_tests,
        init_cost_holder,
    )

    _reset_cost_holder_for_tests()
    try:
        init_cost_holder(
            billing_period_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            credit_pool_usd=200.00,
        )
        holder = HealthRollupHolder()
        rollup = holder.current()
        credit_burn = rollup.subsignals[SUBSIGNAL_CREDIT_BURN]
        # Fresh holder = 0 spent → 0% used
        assert credit_burn.status is SubsignalStatus.FRESH
        assert credit_burn.extra["value_pct"] == 0.0
        assert credit_burn.extra["rung"] == "normal"
        breaker = rollup.subsignals[SUBSIGNAL_BREAKER_STATE]
        assert breaker.extra["value"] == "closed"
    finally:
        _reset_cost_holder_for_tests()


def test_holder_current_wires_active_poller():
    """When active_poller is registered, claim_state pulls from it."""
    from plugins.memory.isokron.active_poller import (
        clear_active_poller,
        set_active_poller,
    )

    clear_active_poller()
    try:
        claim = SimpleNamespace(
            ticket_id="t-99",
            workspace_id="org_test",
            work_attempt_id="wa-99",
            claim_fence_token="tok",
            claimed_at=_NOW,
        )
        fake_poller = SimpleNamespace(current_claim=claim)
        set_active_poller(fake_poller)

        holder = HealthRollupHolder()
        rollup = holder.current()
        claim_state = rollup.subsignals[SUBSIGNAL_CLAIM_STATE]
        assert claim_state.extra["value"] == "active"
        assert claim_state.extra["claim_id"] == "wa-99"
    finally:
        clear_active_poller()


def test_holder_current_wires_active_provider_mcp_client():
    """When active_provider is registered with an MCP client carrying
    last_invoke_at etc., the time-based subsignals pull from it."""
    from plugins.memory.isokron.active_provider import (
        clear_active_provider,
        set_active_provider,
    )

    clear_active_provider()
    try:
        mcp_client = SimpleNamespace(
            last_invoke_at=_NOW - timedelta(seconds=10),
            last_successful_append_event_at=_NOW - timedelta(seconds=45),
            last_successful_refresh_claim_at=_NOW - timedelta(seconds=20),
        )
        connection = SimpleNamespace(get_mcp_client=lambda: mcp_client)
        provider = SimpleNamespace(_connection=connection)
        set_active_provider(provider)

        holder = HealthRollupHolder()
        with patch(
            "agent.health_rollup_holder._now", return_value=_NOW
        ):
            rollup = holder.current()
        dispatch = rollup.subsignals[SUBSIGNAL_DISPATCH_REACHABLE]
        assert dispatch.status is SubsignalStatus.FRESH
        write = rollup.subsignals[SUBSIGNAL_LAST_SUCCESSFUL_WRITE]
        assert write.status is SubsignalStatus.FRESH
        heartbeat = rollup.subsignals[SUBSIGNAL_LAST_HEARTBEAT]
        assert heartbeat.status is SubsignalStatus.FRESH
    finally:
        clear_active_provider()


# ---------------------------------------------------------------------------
# rollup_to_panel_payload — JSON shape projection
# ---------------------------------------------------------------------------


def test_panel_payload_keys_match_stub_contract():
    """Stub at kora_cli/web_server.py:get_health_rollup pins the shape.
    The projection must produce the same top-level keys + subsignal
    names so ST4 is a literal swap."""
    holder = HealthRollupHolder()
    payload = rollup_to_panel_payload(holder.current())
    assert set(payload.keys()) == {
        "overall",
        "control_plane",
        "worker",
        "stopped_reason",
        "subsignals",
    }
    assert set(payload["subsignals"].keys()) == set(ALL_SUBSIGNAL_NAMES)


def test_panel_payload_subsignal_values_in_documented_enums():
    holder = HealthRollupHolder()
    payload = rollup_to_panel_payload(holder.current())
    valid_top = {"healthy", "degraded", "stopped", "outage"}
    valid_sub = {"fresh", "stale", "missing", "degraded"}
    for field in ("overall", "control_plane", "worker"):
        assert payload[field] in valid_top
    for sub_name, sub_body in payload["subsignals"].items():
        assert sub_body["status"] in valid_sub, (
            f"{sub_name} has unexpected status {sub_body['status']!r}"
        )


def test_panel_payload_extra_fields_passthrough():
    """value_at / threshold_seconds / extras flatten into the subsignal
    dict at the same level as ``status`` — matches stub shape."""
    holder = HealthRollupHolder()
    payload = rollup_to_panel_payload(holder.current())
    # escalation_watcher_liveness carries note in extra
    ewl = payload["subsignals"][SUBSIGNAL_ESCALATION_WATCHER_LIVENESS]
    assert "note" in ewl
    assert "threshold_seconds" in ewl


# ---------------------------------------------------------------------------
# Probe cadence env override
# ---------------------------------------------------------------------------


def test_probe_cadence_defaults_to_300_seconds():
    if ENV_PROBE_CADENCE_SECONDS in os.environ:
        del os.environ[ENV_PROBE_CADENCE_SECONDS]
    holder = HealthRollupHolder()
    assert holder.probe_cadence_seconds == DEFAULT_PROBE_CADENCE_SECONDS


def test_probe_cadence_env_override(monkeypatch):
    monkeypatch.setenv(ENV_PROBE_CADENCE_SECONDS, "120")
    holder = HealthRollupHolder()
    assert holder.probe_cadence_seconds == 120


def test_probe_cadence_invalid_value_falls_back(monkeypatch, caplog):
    import logging as _logging

    monkeypatch.setenv(ENV_PROBE_CADENCE_SECONDS, "nope")
    with caplog.at_level(
        _logging.WARNING, logger="agent.health_rollup_holder"
    ):
        holder = HealthRollupHolder()
    assert holder.probe_cadence_seconds == DEFAULT_PROBE_CADENCE_SECONDS
    assert any("not an int" in r.message for r in caplog.records)


def test_probe_cadence_non_positive_falls_back(monkeypatch, caplog):
    import logging as _logging

    monkeypatch.setenv(ENV_PROBE_CADENCE_SECONDS, "0")
    with caplog.at_level(
        _logging.WARNING, logger="agent.health_rollup_holder"
    ):
        holder = HealthRollupHolder()
    assert holder.probe_cadence_seconds == DEFAULT_PROBE_CADENCE_SECONDS


def test_probe_cadence_explicit_kwarg_overrides_env(monkeypatch):
    monkeypatch.setenv(ENV_PROBE_CADENCE_SECONDS, "120")
    holder = HealthRollupHolder(probe_cadence_seconds=600)
    assert holder.probe_cadence_seconds == 600


# ---------------------------------------------------------------------------
# Singleton accessors
# ---------------------------------------------------------------------------


def test_init_health_rollup_holder_returns_singleton():
    h1 = init_health_rollup_holder(probe_cadence_seconds=100)
    h2 = init_health_rollup_holder(probe_cadence_seconds=999)
    assert h1 is h2
    # First call's cadence wins; second's argument is ignored.
    assert h1.probe_cadence_seconds == 100


def test_get_health_rollup_holder_none_before_init():
    from agent.health_rollup_holder import get_health_rollup_holder

    _reset_health_rollup_holder_for_tests()
    assert get_health_rollup_holder() is None
    init_health_rollup_holder()
    assert get_health_rollup_holder() is not None
