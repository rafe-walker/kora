"""KR-P2-INT-TESTS ST7 — cockpit-BFF-down surface (R4.1 §12).

Per the bucket spec:
  1. Configure escalation_watcher_liveness subsignal to return
     status=stale.
  2. GET /api/health-rollup; verify P6 banner-trigger condition in
     the response.

# P6 contract

The FE renders the "control escalation unavailable — use manual L4"
banner when
``response.subsignals.escalation_watcher_liveness.status == "stale"``
(pinned by ``kora_cli/web_server.py:get_health_rollup`` docstring +
KR-P2-HEALTH-PANEL contract test).

# Why the current collector returns MISSING

KR-P2-L §1 verification established that substrate-team hasn't
shipped the escalation_watcher liveness ping yet. The collector
returns ``status=missing`` with note "pending substrate ack"
(intentionally not 'stale' — doesn't trip the banner while ack is
pending). This ST patches the collector to return 'stale' so the
banner-trigger contract is validated; the production fallback
remains 'missing'.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent.health_rollup_holder import (
    SUBSIGNAL_ESCALATION_WATCHER_LIVENESS,
    Subsignal,
    SubsignalStatus,
    _reset_health_rollup_holder_for_tests,
)


pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Mirror the unit test's hermetic config isolation."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("kora_cli.config.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr(
        "kora_cli.config.get_config_path", lambda: tmp_path / "config.yaml"
    )
    monkeypatch.setattr(
        "kora_cli.config.get_env_path", lambda: tmp_path / ".env"
    )
    _reset_health_rollup_holder_for_tests()
    yield
    _reset_health_rollup_holder_for_tests()


@pytest.mark.asyncio
async def test_stale_escalation_watcher_liveness_triggers_p6_banner_condition():
    """When the escalation_watcher_liveness subsignal is 'stale', the
    /api/health-rollup payload satisfies the P6 banner trigger.

    The FE banner check is literally
    ``payload.subsignals.escalation_watcher_liveness.status == 'stale'``.
    This test pins that the live-read path surfaces that condition
    unchanged from collector to endpoint."""

    def _stale_collector(*, now=None, threshold_seconds=15):
        return Subsignal(
            name=SUBSIGNAL_ESCALATION_WATCHER_LIVENESS,
            status=SubsignalStatus.STALE,
            threshold_seconds=threshold_seconds,
            elapsed_seconds=42,
            extra={"value_at": "2026-05-21T22:29:55Z"},
        )

    with patch(
        "agent.health_rollup_holder.collect_escalation_watcher_liveness",
        _stale_collector,
    ):
        from kora_cli import web_server

        payload = await web_server.get_health_rollup()

    # Live branch must be engaged (not the fallback)
    assert payload["stub"] is False
    ewl = payload["subsignals"][SUBSIGNAL_ESCALATION_WATCHER_LIVENESS]
    assert ewl["status"] == "stale"
    # P6 banner trigger condition holds
    assert ewl["status"] == "stale", (
        "escalation_watcher_liveness.status must be exactly 'stale' "
        "for the FE banner trigger; got {!r}".format(ewl["status"])
    )


@pytest.mark.asyncio
async def test_pending_substrate_ack_missing_does_not_trigger_banner():
    """Regression guard for the locked KR-P2-L §1 verification result:
    the default collector returns MISSING with 'pending substrate ack'
    note. That must NOT satisfy the P6 trigger — banner would fire on
    every fresh boot before substrate-team ships the signal."""
    from kora_cli import web_server

    payload = await web_server.get_health_rollup()
    assert payload["stub"] is False
    ewl = payload["subsignals"][SUBSIGNAL_ESCALATION_WATCHER_LIVENESS]
    # MISSING (not 'stale') — banner does NOT trigger
    assert ewl["status"] == "missing"
    assert ewl["status"] != "stale"
    # Note records why
    assert "pending substrate ack" in ewl.get("note", "")


@pytest.mark.asyncio
async def test_stale_watcher_degrades_control_plane():
    """When watcher is STALE (not the pending-ack MISSING sentinel),
    the control_plane derivation flips to DEGRADED per
    `agent.health_rollup_derivation.derive_control_plane_health`.

    This is the secondary signal alongside the FE banner — the
    rollup itself reports control_plane unhealthy, not just the
    subsignal."""

    def _stale_collector(*, now=None, threshold_seconds=15):
        return Subsignal(
            name=SUBSIGNAL_ESCALATION_WATCHER_LIVENESS,
            status=SubsignalStatus.STALE,
            threshold_seconds=threshold_seconds,
            elapsed_seconds=42,
            extra={"value_at": "2026-05-21T22:29:55Z"},
        )

    with patch(
        "agent.health_rollup_holder.collect_escalation_watcher_liveness",
        _stale_collector,
    ):
        from kora_cli import web_server

        payload = await web_server.get_health_rollup()

    assert payload["control_plane"] == "degraded"


@pytest.mark.asyncio
async def test_panel_payload_keys_unchanged_in_bff_down_state():
    """The fallback / live branch surface contract holds even when
    watcher is stale — same 8 subsignals, same top-level shape. The
    FE renders without schema drift."""

    def _stale_collector(*, now=None, threshold_seconds=15):
        return Subsignal(
            name=SUBSIGNAL_ESCALATION_WATCHER_LIVENESS,
            status=SubsignalStatus.STALE,
            threshold_seconds=threshold_seconds,
            elapsed_seconds=42,
            extra={"value_at": "2026-05-21T22:29:55Z"},
        )

    with patch(
        "agent.health_rollup_holder.collect_escalation_watcher_liveness",
        _stale_collector,
    ):
        from kora_cli import web_server

        payload = await web_server.get_health_rollup()

    assert set(payload.keys()) >= {
        "overall",
        "control_plane",
        "worker",
        "stopped_reason",
        "subsignals",
        "stub",
    }
    assert len(payload["subsignals"]) == 8
