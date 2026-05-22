"""Tests for the KR-HB-PANEL stub endpoint.

Bucket §4 scenarios:
  1. GET /api/heartbeat/services returns 200
  2. Top-level shape (services + generated_at + stub:true)
  3. All 5 expected services present
  4. Each service entry has the required keys + valid status enum
  5. status:degraded sample matches the bucket §3 documented stub
  6. Cron-regression sanity
"""

import pytest


_VALID_STATUS = {"healthy", "degraded", "unhealthy"}
_EXPECTED_SERVICES = {"vercel", "sentry", "doppler", "supabase", "fly"}


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


# ---- 1. 200 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_returns_200(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_heartbeat_services()
    assert isinstance(result, dict)


# ---- 2. Top-level shape ----------------------------------------------


@pytest.mark.asyncio
async def test_response_shape_has_required_keys(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_heartbeat_services()
    assert set(result.keys()) == {"services", "generated_at", "stub"}
    assert isinstance(result["services"], list)
    assert isinstance(result["generated_at"], str)
    assert result["stub"] is True


# ---- 3. All 5 expected services present ------------------------------


@pytest.mark.asyncio
async def test_all_five_expected_services_present(_isolate_config):
    """Pin the canonical 5-service list (Vercel / Sentry / Doppler /
    Supabase / Fly). A future stub edit that drops one would silently
    break the dashboard aggregate count test, so catch it here."""
    from kora_cli import web_server

    result = await web_server.get_heartbeat_services()
    names = {s["name"] for s in result["services"]}
    assert names == _EXPECTED_SERVICES


# ---- 4. Per-entry shape + status enum --------------------------------


@pytest.mark.asyncio
async def test_each_service_entry_has_required_keys(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_heartbeat_services()
    for service in result["services"]:
        assert set(service.keys()) == {
            "name",
            "status",
            "last_check_at",
            "latency_ms",
            "details",
        }
        assert isinstance(service["name"], str) and service["name"]
        assert service["status"] in _VALID_STATUS, (
            f"{service['name']}: status={service['status']!r} not in "
            f"{_VALID_STATUS}"
        )
        assert isinstance(service["latency_ms"], int)
        assert service["latency_ms"] >= 0
        assert isinstance(service["last_check_at"], str)
        assert isinstance(service["details"], dict)


# ---- 5. Bucket §3 documented stub values pinned ----------------------


@pytest.mark.asyncio
async def test_sentry_is_degraded_in_stub_per_spec(_isolate_config):
    """The bucket §3 stub pins sentry as the one degraded service (with
    12 unresolved_issues). The dashboard card's "1 degraded" aggregate
    depends on this — pin it so a future stub edit can't silently
    flip the count."""
    from kora_cli import web_server

    result = await web_server.get_heartbeat_services()
    sentry = next(s for s in result["services"] if s["name"] == "sentry")
    assert sentry["status"] == "degraded"
    assert sentry["details"]["unresolved_issues"] == 12


@pytest.mark.asyncio
async def test_other_four_services_healthy_in_stub(_isolate_config):
    """Counterpart to the sentry-degraded pin: the other 4 are healthy
    per bucket §3. Dashboard aggregate: 4 healthy / 1 degraded / 0
    unhealthy."""
    from kora_cli import web_server

    result = await web_server.get_heartbeat_services()
    for service in result["services"]:
        if service["name"] != "sentry":
            assert service["status"] == "healthy", (
                f"{service['name']}: expected healthy in stub, got "
                f"{service['status']!r}"
            )


@pytest.mark.asyncio
async def test_details_payloads_match_documented_shape(_isolate_config):
    """Spot-check each service's documented detail keys are present
    (without pinning exact values — values are stub data that the
    follow-on real-poller PR will overwrite)."""
    from kora_cli import web_server

    expected_keys: dict[str, set[str]] = {
        "vercel": {"deployments_last_24h", "error_rate_24h"},
        "sentry": {"unresolved_issues"},
        "doppler": {"projects_total", "oldest_secret_age_days"},
        "supabase": {"connections_pct"},
        "fly": {"apps_running", "deploys_last_24h"},
    }
    from kora_cli import web_server

    result = await web_server.get_heartbeat_services()
    by_name = {s["name"]: s for s in result["services"]}
    for name, keys in expected_keys.items():
        assert keys <= set(by_name[name]["details"].keys()), (
            f"{name}: missing detail key(s) "
            f"{keys - set(by_name[name]['details'].keys())}"
        )


# ---- 6. Cron-regression sanity --------------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_heartbeat_registered(_isolate_config):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
