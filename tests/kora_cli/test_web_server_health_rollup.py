"""Tests for the KR-P2-HEALTH-PANEL endpoint (KR-P2-L ST4 flip).

KR-P2-L ST4 flipped the endpoint from stub to live read; this file
covers BOTH branches:

  - Live branch (holder reachable): ``stub: False``, full rollup
    projected from ``HealthRollupHolder.current()``
  - Fallback branch (holder raises): ``stub: True`` + ``error``
    field, fallback shape so FE renders unchanged

Scenarios:
  1. GET /api/health-rollup returns 200
  2. Top-level shape (overall + control_plane + worker + stopped_reason +
     subsignals + stub key)
  3. All 8 R4.1 §9.7 subsignals present
  4. Each subsignal status ∈ {fresh, stale, missing, degraded}
  5. overall / control_plane / worker values ∈ {healthy, degraded, stopped, outage}
  6. Frontend banner check — escalation_watcher_liveness carries
     status field the FE switches on
  7. Contract: stopped_reason non-null only when overall ∈ {stopped, outage}
  8. Two-branch flip: live + fallback both surface the same shape;
     only ``stub`` + ``error`` differ
"""

import pytest


_VALID_HEALTH = {"healthy", "degraded", "stopped", "outage"}
_VALID_SUBSIGNAL = {"fresh", "stale", "missing", "degraded"}
_EXPECTED_SUBSIGNALS = {
    "last_successful_write",
    "claim_state",
    "credit_burn",
    "breaker_state",
    "auth_validity_window",
    "dispatch_reachable",
    "last_heartbeat",
    "escalation_watcher_liveness",
}


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("kora_cli.config.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr(
        "kora_cli.config.get_config_path", lambda: tmp_path / "config.yaml"
    )
    monkeypatch.setattr(
        "kora_cli.config.get_env_path", lambda: tmp_path / ".env"
    )
    # Reset the holder singleton so each test starts clean.
    from agent.health_rollup_holder import _reset_health_rollup_holder_for_tests

    _reset_health_rollup_holder_for_tests()
    yield tmp_path
    _reset_health_rollup_holder_for_tests()


# ---- 1. 200 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_returns_200(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_health_rollup()
    assert isinstance(result, dict)


# ---- 2. Top-level shape ---------------------------------------------------


@pytest.mark.asyncio
async def test_response_shape_top_level_keys(_isolate_config):
    """Live branch — keys present and stub flag is False."""
    from kora_cli import web_server

    result = await web_server.get_health_rollup()
    # Live branch keys: top-level + stub (no error)
    assert {
        "overall",
        "control_plane",
        "worker",
        "stopped_reason",
        "subsignals",
        "stub",
    }.issubset(set(result.keys()))
    # Post-flip: live read path returns stub=False
    assert result["stub"] is False
    assert "error" not in result
    assert isinstance(result["subsignals"], dict)


# ---- 3. All 8 R4.1 §9.7 subsignals present ------------------------------


@pytest.mark.asyncio
async def test_all_eight_subsignals_present(_isolate_config):
    """R4.1 §9.7 pins exactly these 8 subsignals. If a future runtime
    flip drops one, the FE will silently fall back to a 'missing'
    placeholder card — but the contract is still 8 keys, so catch the
    drop at the endpoint layer."""
    from kora_cli import web_server

    result = await web_server.get_health_rollup()
    assert set(result["subsignals"].keys()) == _EXPECTED_SUBSIGNALS


# ---- 4. Subsignal status enum -------------------------------------------


@pytest.mark.asyncio
async def test_subsignal_statuses_in_documented_set(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_health_rollup()
    for name, signal in result["subsignals"].items():
        assert "status" in signal, f"subsignal {name!r} missing status field"
        assert signal["status"] in _VALID_SUBSIGNAL, (
            f"subsignal {name!r} has unknown status {signal['status']!r}"
        )


# ---- 5. Top-level health enum -------------------------------------------


@pytest.mark.asyncio
async def test_top_level_health_values_in_documented_set(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_health_rollup()
    for field in ("overall", "control_plane", "worker"):
        assert result[field] in _VALID_HEALTH, (
            f"{field} has unknown value {result[field]!r}"
        )


# ---- 6. P6 surface contract -------------------------------------------


@pytest.mark.asyncio
async def test_escalation_watcher_liveness_carries_status_field(_isolate_config):
    """The frontend P6 banner switches on
    ``subsignals.escalation_watcher_liveness.status == 'stale'``. Pin
    that the field is present so a future schema drift can't silently
    suppress the banner — the operator must see when control-plane
    escalation is unavailable and they have to use manual L4."""
    from kora_cli import web_server

    result = await web_server.get_health_rollup()
    ewl = result["subsignals"].get("escalation_watcher_liveness")
    assert ewl is not None
    assert "status" in ewl
    assert ewl["status"] in _VALID_SUBSIGNAL


# ---- 7. stopped_reason contract ----------------------------------------


@pytest.mark.asyncio
async def test_stopped_reason_non_null_only_when_overall_is_stopped_or_outage(_isolate_config):
    """R4.1 §9.7: stopped_reason exists to disambiguate 'intentionally
    stopped' from 'outage'. It MUST be null when overall is healthy or
    degraded (no reason to give one), and MAY be non-null when overall
    is stopped/outage (operator needs the reason). Pin the strong
    direction (healthy/degraded ⇒ null)."""
    from kora_cli import web_server

    result = await web_server.get_health_rollup()
    if result["overall"] in {"healthy", "degraded"}:
        assert result["stopped_reason"] is None, (
            f"overall={result['overall']!r} but stopped_reason is "
            f"{result['stopped_reason']!r} — should be None"
        )


# Bucket §4 also asks for the converse direction — when overall is
# stopped or outage, we'd want a reason. Surfacing the lack of a reason
# is the FE's job (it shows a warning); on the API side we don't make
# stopped_reason mandatory because we don't want to invent data when
# the runtime probe couldn't establish one. So this test asserts the
# weaker invariant: when set, it's a non-empty string.
@pytest.mark.asyncio
async def test_stopped_reason_when_present_is_non_empty_string(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_health_rollup()
    if result["stopped_reason"] is not None:
        assert isinstance(result["stopped_reason"], str)
        assert result["stopped_reason"].strip(), "stopped_reason is empty/whitespace"


# ---- 8. Cron-regression sanity -----------------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_health_rollup_registered(_isolate_config):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)


# ---- 9. Two-branch flip — fallback when live read raises ---------------


@pytest.mark.asyncio
async def test_fallback_branch_returns_stub_true_with_error_when_holder_raises(
    _isolate_config, monkeypatch
):
    """If ``holder.current()`` raises, the endpoint returns the
    fallback shape with ``stub: True`` + ``error`` field — mirrors
    the KR-P2-DR-FLIP two-branch pattern."""
    from agent import health_rollup_holder
    from kora_cli import web_server

    class _BoomHolder:
        probe_cadence_seconds = 300

        def current(self):
            raise RuntimeError("collect boom")

    monkeypatch.setattr(
        health_rollup_holder, "init_health_rollup_holder", lambda **kw: _BoomHolder()
    )
    monkeypatch.setattr(
        health_rollup_holder, "get_health_rollup_holder", lambda: _BoomHolder()
    )

    result = await web_server.get_health_rollup()
    assert result["stub"] is True
    assert "error" in result
    assert "collect boom" in result["error"]
    # Fallback shape preserves all keys + 8 subsignals
    assert set(result["subsignals"].keys()) == _EXPECTED_SUBSIGNALS


@pytest.mark.asyncio
async def test_fallback_branch_returns_stub_true_when_import_raises(
    _isolate_config, monkeypatch
):
    """If even the holder import fails, the endpoint still returns the
    fallback shape rather than 500."""
    import sys
    from kora_cli import web_server

    # Sabotage the module so its import inside the endpoint raises
    bad_module_name = "agent.health_rollup_holder"
    original = sys.modules.get(bad_module_name)
    sys.modules[bad_module_name] = None  # type: ignore[assignment]
    try:
        result = await web_server.get_health_rollup()
    finally:
        if original is not None:
            sys.modules[bad_module_name] = original
        else:
            sys.modules.pop(bad_module_name, None)

    assert result["stub"] is True
    assert "error" in result
    assert isinstance(result["subsignals"], dict)


# ---- 10. Live branch with deps wired —---------------------------------


@pytest.mark.asyncio
async def test_live_branch_with_cost_holder_initialized(_isolate_config):
    """When cost holder is initialized, credit_burn + breaker_state
    surface live values (not stub placeholders)."""
    from agent.cost_state_holder import (
        _reset_cost_holder_for_tests,
        init_cost_holder,
    )
    from datetime import datetime, timezone
    from kora_cli import web_server

    _reset_cost_holder_for_tests()
    try:
        init_cost_holder(
            billing_period_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            credit_pool_usd=200.00,
        )
        result = await web_server.get_health_rollup()
        assert result["stub"] is False
        # Fresh holder → 0% spent → fresh credit_burn
        credit_burn = result["subsignals"]["credit_burn"]
        assert credit_burn["status"] == "fresh"
        assert credit_burn["value_pct"] == 0.0
        # Closed breaker
        breaker = result["subsignals"]["breaker_state"]
        assert breaker["value"] == "closed"
    finally:
        _reset_cost_holder_for_tests()
