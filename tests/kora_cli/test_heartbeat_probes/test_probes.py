"""Per-probe tests (KR-FEAT-HEARTBEAT ST1).

One section per probe. Each section covers:
  - Missing auth env → status=unknown + ZERO httpx call
  - 200 response with healthy shape → status=healthy + details
  - 200 with degraded threshold tripped → status=degraded
  - Non-200 response → status=unhealthy + sanitized error
  - Transport error → status=unknown + sanitized error

httpx is mocked at the AsyncClient class level so probes never
hit the real network. The mock records call counts so we can
verify the auth-missing path makes ZERO outbound calls.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from kora_cli.heartbeat_probes.doppler import (
    DOPPLER_API_TOKEN_ENV,
    DopplerProbe,
)
from kora_cli.heartbeat_probes.fly import (
    FLY_API_TOKEN_ENV,
    FlyProbe,
)
from kora_cli.heartbeat_probes.sentry import (
    SENTRY_API_TOKEN_ENV,
    SENTRY_ORG_ENV,
    SentryProbe,
)
from kora_cli.heartbeat_probes.supabase import (
    SUPABASE_ANON_KEY_ENV,
    SUPABASE_URL_ENV,
    SupabaseProbe,
)
from kora_cli.heartbeat_probes.vercel import (
    VERCEL_API_TOKEN_ENV,
    VercelProbe,
)


def _fake_response(*, status_code: int = 200, json_payload: Any = None) -> MagicMock:
    """Build a MagicMock that quacks like httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_payload if json_payload is not None else {})
    return resp


def _patch_http_client(get_response=None, head_response=None):
    """Patch httpx.AsyncClient so we control responses + can count
    calls. Returns the AsyncMock instance so tests can introspect."""
    fake_client = AsyncMock(spec=httpx.AsyncClient)
    if get_response is not None:
        fake_client.get = AsyncMock(return_value=get_response)
    if head_response is not None:
        fake_client.head = AsyncMock(return_value=head_response)
    # AsyncClient is used as async context manager
    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_client)
    fake_cm.__aexit__ = AsyncMock(return_value=None)
    return patch("httpx.AsyncClient", return_value=fake_cm), fake_client


# =============================================================================
# VercelProbe
# =============================================================================


@pytest.mark.asyncio
async def test_vercel_missing_token_returns_unknown_zero_calls(monkeypatch):
    monkeypatch.delenv(VERCEL_API_TOKEN_ENV, raising=False)
    cm_patch, fake_client = _patch_http_client(get_response=_fake_response())
    with cm_patch:
        snap = await VercelProbe().check()
    assert snap.status == "unknown"
    assert snap.latency_ms is None
    assert VERCEL_API_TOKEN_ENV in snap.error
    # SECURITY: zero outbound calls when auth missing
    fake_client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_vercel_200_healthy(monkeypatch):
    monkeypatch.setenv(VERCEL_API_TOKEN_ENV, "vercel_pat_test")
    payload = {
        "deployments": [
            {"created": _ms_now(), "state": "READY"},
            {"created": _ms_now(), "state": "READY"},
        ]
    }
    cm_patch, fake_client = _patch_http_client(
        get_response=_fake_response(status_code=200, json_payload=payload)
    )
    with cm_patch:
        snap = await VercelProbe().check()
    assert snap.status == "healthy"
    assert snap.latency_ms is not None
    assert snap.details["deployments_last_24h"] == 2
    assert snap.details["error_rate_24h"] == 0.0


@pytest.mark.asyncio
async def test_vercel_200_degraded_when_error_rate_high(monkeypatch):
    monkeypatch.setenv(VERCEL_API_TOKEN_ENV, "vercel_pat_test")
    # 8 deploys, 2 errors → 25% > 10% threshold
    payload = {
        "deployments": [{"created": _ms_now(), "state": "READY"}] * 6
        + [{"created": _ms_now(), "state": "ERROR"}] * 2
    }
    cm_patch, _ = _patch_http_client(
        get_response=_fake_response(status_code=200, json_payload=payload)
    )
    with cm_patch:
        snap = await VercelProbe().check()
    assert snap.status == "degraded"
    assert snap.details["error_rate_24h"] == 0.25


@pytest.mark.asyncio
async def test_vercel_non_200_unhealthy_with_sanitized_error(monkeypatch):
    monkeypatch.setenv(VERCEL_API_TOKEN_ENV, "vercel_pat_test")
    cm_patch, _ = _patch_http_client(get_response=_fake_response(status_code=503))
    with cm_patch:
        snap = await VercelProbe().check()
    assert snap.status == "unhealthy"
    assert "HTTP 503" in snap.error
    assert "vercel_pat_test" not in snap.error


@pytest.mark.asyncio
async def test_vercel_transport_error_with_token_in_message_redacted(monkeypatch):
    """SECURITY: if httpx error message contains the token (URL
    fragment, etc.), the snapshot's error field must redact it."""
    monkeypatch.setenv(VERCEL_API_TOKEN_ENV, "vercel_pat_test")
    fake_client = AsyncMock()
    fake_client.get = AsyncMock(
        side_effect=httpx.ConnectError("upstream rejected token vercel_pat_test"),
    )
    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_client)
    fake_cm.__aexit__ = AsyncMock(return_value=None)
    with patch("httpx.AsyncClient", return_value=fake_cm):
        snap = await VercelProbe().check()
    assert snap.status == "unknown"
    assert "vercel_pat_test" not in snap.error


# =============================================================================
# SentryProbe
# =============================================================================


@pytest.mark.asyncio
async def test_sentry_missing_token_returns_unknown_zero_calls(monkeypatch):
    monkeypatch.delenv(SENTRY_API_TOKEN_ENV, raising=False)
    monkeypatch.setenv(SENTRY_ORG_ENV, "stormhaven")
    cm_patch, fake_client = _patch_http_client(get_response=_fake_response())
    with cm_patch:
        snap = await SentryProbe().check()
    assert snap.status == "unknown"
    assert SENTRY_API_TOKEN_ENV in snap.error
    fake_client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_sentry_missing_org_returns_unknown(monkeypatch):
    monkeypatch.setenv(SENTRY_API_TOKEN_ENV, "sentry_token_test")
    monkeypatch.delenv(SENTRY_ORG_ENV, raising=False)
    cm_patch, fake_client = _patch_http_client(get_response=_fake_response())
    with cm_patch:
        snap = await SentryProbe().check()
    assert snap.status == "unknown"
    assert SENTRY_ORG_ENV in snap.error
    fake_client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_sentry_200_healthy_below_threshold(monkeypatch):
    monkeypatch.setenv(SENTRY_API_TOKEN_ENV, "sentry_token_test")
    monkeypatch.setenv(SENTRY_ORG_ENV, "stormhaven")
    cm_patch, _ = _patch_http_client(
        get_response=_fake_response(status_code=200, json_payload=[{}, {}, {}])
    )
    with cm_patch:
        snap = await SentryProbe().check()
    assert snap.status == "healthy"
    assert snap.details["unresolved_issues"] == 3


@pytest.mark.asyncio
async def test_sentry_degraded_above_threshold(monkeypatch):
    monkeypatch.setenv(SENTRY_API_TOKEN_ENV, "sentry_token_test")
    monkeypatch.setenv(SENTRY_ORG_ENV, "stormhaven")
    cm_patch, _ = _patch_http_client(
        get_response=_fake_response(status_code=200, json_payload=[{}] * 15)
    )
    with cm_patch:
        snap = await SentryProbe().check()
    assert snap.status == "degraded"
    assert snap.details["unresolved_issues"] == 15


@pytest.mark.asyncio
async def test_sentry_non_200_unhealthy(monkeypatch):
    monkeypatch.setenv(SENTRY_API_TOKEN_ENV, "sentry_token_test")
    monkeypatch.setenv(SENTRY_ORG_ENV, "stormhaven")
    cm_patch, _ = _patch_http_client(get_response=_fake_response(status_code=429))
    with cm_patch:
        snap = await SentryProbe().check()
    assert snap.status == "unhealthy"
    assert "HTTP 429" in snap.error
    assert "sentry_token_test" not in snap.error


# =============================================================================
# DopplerProbe
# =============================================================================


@pytest.mark.asyncio
async def test_doppler_missing_token_returns_unknown_zero_calls(monkeypatch):
    monkeypatch.delenv(DOPPLER_API_TOKEN_ENV, raising=False)
    cm_patch, fake_client = _patch_http_client(get_response=_fake_response())
    with cm_patch:
        snap = await DopplerProbe().check()
    assert snap.status == "unknown"
    fake_client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_doppler_200_healthy(monkeypatch):
    monkeypatch.setenv(DOPPLER_API_TOKEN_ENV, "doppler_token_test")
    cm_patch, _ = _patch_http_client(
        get_response=_fake_response(
            status_code=200, json_payload={"workplace": {"name": "stormhaven"}}
        )
    )
    with cm_patch:
        snap = await DopplerProbe().check()
    assert snap.status == "healthy"
    # Workplace endpoint doesn't expose project list — known-unknown
    assert snap.details["projects_total"] == "unknown"
    assert snap.details["oldest_secret_age_days"] == "unknown"


@pytest.mark.asyncio
async def test_doppler_non_200_unhealthy(monkeypatch):
    monkeypatch.setenv(DOPPLER_API_TOKEN_ENV, "doppler_token_test")
    cm_patch, _ = _patch_http_client(get_response=_fake_response(status_code=401))
    with cm_patch:
        snap = await DopplerProbe().check()
    assert snap.status == "unhealthy"
    assert "HTTP 401" in snap.error
    assert "doppler_token_test" not in snap.error


# =============================================================================
# SupabaseProbe
# =============================================================================


@pytest.mark.asyncio
async def test_supabase_missing_envs_returns_unknown_zero_calls(monkeypatch):
    monkeypatch.delenv(SUPABASE_ANON_KEY_ENV, raising=False)
    monkeypatch.delenv(SUPABASE_URL_ENV, raising=False)
    cm_patch, fake_client = _patch_http_client(head_response=_fake_response())
    with cm_patch:
        snap = await SupabaseProbe().check()
    assert snap.status == "unknown"
    assert SUPABASE_ANON_KEY_ENV in snap.error
    assert SUPABASE_URL_ENV in snap.error
    fake_client.head.assert_not_awaited()


@pytest.mark.asyncio
async def test_supabase_200_healthy(monkeypatch):
    monkeypatch.setenv(SUPABASE_ANON_KEY_ENV, "supabase_anon_test")
    monkeypatch.setenv(SUPABASE_URL_ENV, "https://abc123.supabase.co")
    cm_patch, _ = _patch_http_client(head_response=_fake_response(status_code=200))
    with cm_patch:
        snap = await SupabaseProbe().check()
    assert snap.status == "healthy"
    assert snap.details["connections_pct"] == "unknown"


@pytest.mark.asyncio
async def test_supabase_204_also_healthy(monkeypatch):
    monkeypatch.setenv(SUPABASE_ANON_KEY_ENV, "supabase_anon_test")
    monkeypatch.setenv(SUPABASE_URL_ENV, "https://abc123.supabase.co")
    cm_patch, _ = _patch_http_client(head_response=_fake_response(status_code=204))
    with cm_patch:
        snap = await SupabaseProbe().check()
    assert snap.status == "healthy"


@pytest.mark.asyncio
async def test_supabase_500_unhealthy_with_redacted_error(monkeypatch):
    monkeypatch.setenv(SUPABASE_ANON_KEY_ENV, "supabase_anon_test")
    monkeypatch.setenv(SUPABASE_URL_ENV, "https://abc123.supabase.co")
    cm_patch, _ = _patch_http_client(head_response=_fake_response(status_code=500))
    with cm_patch:
        snap = await SupabaseProbe().check()
    assert snap.status == "unhealthy"
    assert "HTTP 500" in snap.error
    assert "supabase_anon_test" not in snap.error


# =============================================================================
# FlyProbe
# =============================================================================


@pytest.mark.asyncio
async def test_fly_missing_token_returns_unknown_zero_calls(monkeypatch):
    monkeypatch.delenv(FLY_API_TOKEN_ENV, raising=False)
    cm_patch, fake_client = _patch_http_client(get_response=_fake_response())
    with cm_patch:
        snap = await FlyProbe().check()
    assert snap.status == "unknown"
    fake_client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_fly_one_app_healthy(monkeypatch):
    monkeypatch.setenv(FLY_API_TOKEN_ENV, "fly_token_test")
    cm_patch, _ = _patch_http_client(
        get_response=_fake_response(
            status_code=200,
            json_payload=[{"state": "started"}, {"state": "started"}],
        )
    )
    with cm_patch:
        snap = await FlyProbe().check()
    assert snap.status == "healthy"
    assert snap.details["apps_running"] == 1


@pytest.mark.asyncio
async def test_fly_app_degraded_when_machines_partial(monkeypatch):
    """Machine partially healthy (1 started + 1 stopped) → degraded."""
    monkeypatch.setenv(FLY_API_TOKEN_ENV, "fly_token_test")
    cm_patch, _ = _patch_http_client(
        get_response=_fake_response(
            status_code=200,
            json_payload=[{"state": "started"}, {"state": "stopped"}],
        )
    )
    with cm_patch:
        snap = await FlyProbe().check()
    assert snap.status == "degraded"


@pytest.mark.asyncio
async def test_fly_all_apps_fail_returns_unhealthy(monkeypatch):
    monkeypatch.setenv(FLY_API_TOKEN_ENV, "fly_token_test")
    cm_patch, _ = _patch_http_client(get_response=_fake_response(status_code=404))
    with cm_patch:
        snap = await FlyProbe().check()
    assert snap.status == "unhealthy"
    assert "404" in snap.error
    assert "fly_token_test" not in snap.error


# =============================================================================
# Cross-probe: 0 calls when ALL auth envs missing
# =============================================================================


@pytest.mark.asyncio
async def test_all_probes_make_zero_calls_when_all_envs_missing(monkeypatch):
    """Defense-in-depth invariant: a fully-unconfigured kora install
    makes ZERO outbound HTTP calls during a probe cycle."""
    for env in (
        VERCEL_API_TOKEN_ENV,
        SENTRY_API_TOKEN_ENV,
        SENTRY_ORG_ENV,
        DOPPLER_API_TOKEN_ENV,
        SUPABASE_ANON_KEY_ENV,
        SUPABASE_URL_ENV,
        FLY_API_TOKEN_ENV,
    ):
        monkeypatch.delenv(env, raising=False)

    fake_client = AsyncMock()
    fake_client.get = AsyncMock()
    fake_client.head = AsyncMock()
    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_client)
    fake_cm.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=fake_cm):
        for probe in (
            VercelProbe(),
            SentryProbe(),
            DopplerProbe(),
            SupabaseProbe(),
            FlyProbe(),
        ):
            snap = await probe.check()
            assert snap.status == "unknown"
            assert snap.latency_ms is None

    fake_client.get.assert_not_awaited()
    fake_client.head.assert_not_awaited()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ms_now() -> int:
    """Vercel uses ms-since-epoch for `created`. Recent → within 24h."""
    return int(datetime.now(timezone.utc).timestamp() * 1000)
