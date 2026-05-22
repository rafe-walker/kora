"""Tests for the /api/heartbeat/services endpoint.

Originally landed by CC#2's KR-HB-PANEL (PR #103) as a hardcoded
stub. CC#1's KR-FEAT-HEARTBEAT ST2 swaps the body for a live read
from ``kora_cli.heartbeat_probes.current_service_snapshots()``.

Scenarios:
  1. GET /api/heartbeat/services returns 200
  2. Top-level shape — services + generated_at + stub + cache_warming
  3. Empty cache → cache_warming=True + services=[]
  4. Populated cache → services projected in canonical order
  5. Each service entry has the required keys + valid status enum
     (4-value: healthy/degraded/unhealthy/unknown)
  6. Latency / last_check_at / error fields nullable per the
     TS contract extension
  7. Cron-regression sanity
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest


_VALID_STATUS = {"healthy", "degraded", "unhealthy", "unknown"}
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
    # Reset the probe snapshot cache between tests
    from kora_cli.heartbeat_probes.runner import _clear_snapshot_cache

    _clear_snapshot_cache()
    yield tmp_path
    _clear_snapshot_cache()


def _seed_snapshot(
    name: str,
    *,
    status: str = "healthy",
    latency_ms: int | None = 50,
    details: dict | None = None,
    error: str | None = None,
    last_check_at: datetime | None = None,
) -> None:
    from kora_cli.heartbeat_probes.runner import _snapshot_cache
    from kora_cli.heartbeat_probes.types import ServiceHealthSnapshot

    _snapshot_cache[name] = ServiceHealthSnapshot(
        name=name,
        status=status,
        latency_ms=latency_ms,
        last_check_at=last_check_at or datetime.now(timezone.utc),
        details=details or {},
        error=error,
    )


def _seed_five_healthy_services() -> None:
    _seed_snapshot(
        "vercel",
        status="healthy",
        latency_ms=140,
        details={"deployments_last_24h": 8, "error_rate_24h": 0.0},
    )
    _seed_snapshot(
        "sentry",
        status="degraded",
        latency_ms=230,
        details={"unresolved_issues": 12},
    )
    _seed_snapshot(
        "doppler",
        status="healthy",
        latency_ms=95,
        details={
            "projects_total": "unknown",
            "oldest_secret_age_days": "unknown",
        },
    )
    _seed_snapshot(
        "supabase",
        status="healthy",
        latency_ms=38,
        details={"connections_pct": "unknown"},
    )
    _seed_snapshot(
        "fly",
        status="healthy",
        latency_ms=88,
        details={"apps_running": 1, "deploys_last_24h": "unknown"},
    )


# ---- 1. 200 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_returns_200(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_heartbeat_services()
    assert isinstance(result, dict)


# ---- 2. Top-level shape ----------------------------------------------


@pytest.mark.asyncio
async def test_response_shape_has_required_keys_when_warming(_isolate_config):
    """Empty cache (just-started daemon) → warming branch. Same
    top-level keys whether warming or not — FE renders the same
    schema."""
    from kora_cli import web_server

    result = await web_server.get_heartbeat_services()
    assert set(result.keys()) == {
        "services",
        "generated_at",
        "stub",
        "cache_warming",
    }
    assert result["stub"] is False
    assert result["cache_warming"] is True
    assert result["services"] == []
    assert isinstance(result["generated_at"], str)


@pytest.mark.asyncio
async def test_response_shape_has_required_keys_when_populated(_isolate_config):
    _seed_five_healthy_services()
    from kora_cli import web_server

    result = await web_server.get_heartbeat_services()
    assert set(result.keys()) == {
        "services",
        "generated_at",
        "stub",
        "cache_warming",
    }
    assert result["stub"] is False
    assert result["cache_warming"] is False
    assert len(result["services"]) == 5


# ---- 3. All 5 expected services present (post-flip from snapshots) -----


@pytest.mark.asyncio
async def test_all_five_expected_services_present_when_populated(_isolate_config):
    _seed_five_healthy_services()
    from kora_cli import web_server

    result = await web_server.get_heartbeat_services()
    names = {s["name"] for s in result["services"]}
    assert names == _EXPECTED_SERVICES


@pytest.mark.asyncio
async def test_canonical_order_preserved(_isolate_config):
    """Services render in the registration order (vercel → sentry →
    doppler → supabase → fly) regardless of insertion order. FE
    cards stay stable between refreshes."""
    # Seed in reverse to verify ordering is enforced server-side
    _seed_snapshot("fly", status="healthy")
    _seed_snapshot("supabase", status="healthy")
    _seed_snapshot("doppler", status="healthy")
    _seed_snapshot("sentry", status="healthy")
    _seed_snapshot("vercel", status="healthy")

    from kora_cli import web_server

    result = await web_server.get_heartbeat_services()
    order = [s["name"] for s in result["services"]]
    assert order == ["vercel", "sentry", "doppler", "supabase", "fly"]


# ---- 4. Per-entry shape + status enum --------------------------------


@pytest.mark.asyncio
async def test_each_service_entry_has_required_keys(_isolate_config):
    _seed_five_healthy_services()
    from kora_cli import web_server

    required = {
        "name",
        "status",
        "last_check_at",
        "latency_ms",
        "details",
        # KR-FEAT-HEARTBEAT ST2 additive
        "error",
    }
    result = await web_server.get_heartbeat_services()
    for service in result["services"]:
        assert set(service.keys()) == required
        assert isinstance(service["name"], str) and service["name"]
        assert service["status"] in _VALID_STATUS
        assert isinstance(service["details"], dict)


@pytest.mark.asyncio
async def test_unknown_status_in_valid_set(_isolate_config):
    """Auth-missing / probe-timeout snapshots surface as
    ``status="unknown"``. FE renders distinct from "unhealthy"."""
    _seed_snapshot(
        "vercel",
        status="unknown",
        latency_ms=None,
        error="auth env unset or empty: 'KORA_VERCEL_API_TOKEN'",
    )
    from kora_cli import web_server

    result = await web_server.get_heartbeat_services()
    vercel = next(s for s in result["services"] if s["name"] == "vercel")
    assert vercel["status"] == "unknown"
    assert vercel["status"] in _VALID_STATUS


@pytest.mark.asyncio
async def test_latency_ms_nullable_on_unknown(_isolate_config):
    """An unknown probe has no roundtrip → latency_ms is null."""
    _seed_snapshot("vercel", status="unknown", latency_ms=None)
    from kora_cli import web_server

    result = await web_server.get_heartbeat_services()
    vercel = next(s for s in result["services"] if s["name"] == "vercel")
    assert vercel["latency_ms"] is None


@pytest.mark.asyncio
async def test_error_field_populated_on_unhealthy(_isolate_config):
    _seed_snapshot(
        "vercel", status="unhealthy", error="HTTP 503", latency_ms=140
    )
    from kora_cli import web_server

    result = await web_server.get_heartbeat_services()
    vercel = next(s for s in result["services"] if s["name"] == "vercel")
    assert vercel["error"] == "HTTP 503"


@pytest.mark.asyncio
async def test_error_field_null_on_healthy(_isolate_config):
    _seed_snapshot("vercel", status="healthy", error=None)
    from kora_cli import web_server

    result = await web_server.get_heartbeat_services()
    vercel = next(s for s in result["services"] if s["name"] == "vercel")
    assert vercel["error"] is None


# ---- 5. cache_warming branch contract ---------------------------------


@pytest.mark.asyncio
async def test_cache_warming_returns_empty_services_list(_isolate_config):
    """Pin the warming-branch shape: services=[] + cache_warming=True
    + stub=False. FE renders "Probes warming up..."."""
    from kora_cli import web_server

    result = await web_server.get_heartbeat_services()
    assert result["services"] == []
    assert result["cache_warming"] is True
    assert result["stub"] is False


@pytest.mark.asyncio
async def test_cache_warming_false_when_any_snapshot_present(_isolate_config):
    """A single snapshot is enough to flip cache_warming=False —
    operator sees partial data rather than the warming placeholder."""
    _seed_snapshot("vercel", status="healthy")
    from kora_cli import web_server

    result = await web_server.get_heartbeat_services()
    assert result["cache_warming"] is False
    assert len(result["services"]) == 1


# ---- 6. Sanitization spot-check (security carry-forward) --------------


@pytest.mark.asyncio
async def test_error_string_passthrough_from_snapshot(_isolate_config):
    """The endpoint trusts the snapshot's error field — sanitization
    is the probe's job (sanitize_error in heartbeat_probes/base.py).
    Pin that the endpoint doesn't accidentally inject token values
    of its own."""
    snapshot_error = "auth env unset or empty: 'KORA_VERCEL_API_TOKEN'"
    _seed_snapshot("vercel", status="unknown", latency_ms=None, error=snapshot_error)
    from kora_cli import web_server

    result = await web_server.get_heartbeat_services()
    vercel = next(s for s in result["services"] if s["name"] == "vercel")
    # No token-value-shape characters added by endpoint projection
    assert vercel["error"] == snapshot_error
    # Defense in depth — pin that response payload has no Bearer / ghp_
    # prefixes anywhere (would indicate token leak via dependency).
    import json

    serialized = json.dumps(result)
    assert "Bearer " not in serialized
    assert "ghp_" not in serialized


# ---- 7. Cron-regression sanity ---------------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_heartbeat_registered(_isolate_config):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
