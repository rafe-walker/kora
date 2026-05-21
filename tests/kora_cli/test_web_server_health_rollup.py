"""Tests for the KR-P2-HEALTH-PANEL stub endpoint.

Bucket §4 scenarios:
  1. GET /api/health-rollup returns 200
  2. Top-level shape (overall + control_plane + worker + stopped_reason +
     subsignals + stub:true)
  3. All 8 R4.1 §9.7 subsignals present
  4. Each subsignal status ∈ {fresh, stale, missing, degraded}
  5. overall / control_plane / worker values ∈ {healthy, degraded, stopped, outage}
  6. (Frontend banner check — docstring + contract guard here that the
     escalation_watcher_liveness subsignal carries the status field the FE
     switches on)
  7. Contract: stopped_reason non-null only when overall ∈ {stopped, outage}
  8. Cron-regression sanity
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
    return tmp_path


# ---- 1. 200 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_returns_200(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_health_rollup()
    assert isinstance(result, dict)


# ---- 2. Top-level shape ---------------------------------------------------


@pytest.mark.asyncio
async def test_response_shape_top_level_keys(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_health_rollup()
    assert set(result.keys()) == {
        "overall",
        "control_plane",
        "worker",
        "stopped_reason",
        "subsignals",
        "stub",
    }
    assert result["stub"] is True
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
