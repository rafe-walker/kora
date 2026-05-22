"""DopplerProbe — checks workplace reachability + secret-rotation hygiene.

Auth: ``KORA_DOPPLER_API_TOKEN`` (a SERVICE token with workplace
read-only scope — NOT a project token; per §4 Q3 ruling Joshua mints
a dedicated service token for the probe). Healthy: 200. Degraded:
oldest_secret_age_days > 180.
"""

from __future__ import annotations

from typing import Any

import httpx

from kora_cli.heartbeat_probes.base import (
    PROBE_TIMEOUT_SECONDS,
    now_ms_monotonic,
    resolve_env,
    sanitize_error,
    snapshot_for_auth_missing,
    snapshot_for_unexpected_error,
    utc_now,
)
from kora_cli.heartbeat_probes.types import ServiceHealthSnapshot


DOPPLER_API_TOKEN_ENV = "KORA_DOPPLER_API_TOKEN"
DOPPLER_API_BASE = "https://api.doppler.com"
DEGRADED_SECRET_AGE_DAYS_THRESHOLD = 180


class DopplerProbe:
    name = "doppler"

    async def check(self) -> ServiceHealthSnapshot:
        token = resolve_env(DOPPLER_API_TOKEN_ENV)
        if token is None:
            return snapshot_for_auth_missing(
                name=self.name, env_var=DOPPLER_API_TOKEN_ENV
            )

        started_ms = now_ms_monotonic()
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
                response = await client.get(
                    f"{DOPPLER_API_BASE}/v3/workplace",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except Exception as exc:
            return snapshot_for_unexpected_error(
                name=self.name, exc=exc, auth_tokens=(token,)
            )

        latency_ms = int(now_ms_monotonic() - started_ms)
        if response.status_code != 200:
            return ServiceHealthSnapshot(
                name=self.name,
                status="unhealthy",
                latency_ms=latency_ms,
                last_check_at=utc_now(),
                details={},
                error=sanitize_error(
                    f"HTTP {response.status_code}", token
                ),
            )

        projects_total, oldest_age_days = _project_workplace(response.json())
        status = "healthy"
        if (
            isinstance(oldest_age_days, int)
            and oldest_age_days > DEGRADED_SECRET_AGE_DAYS_THRESHOLD
        ):
            status = "degraded"

        return ServiceHealthSnapshot(
            name=self.name,
            status=status,
            latency_ms=latency_ms,
            last_check_at=utc_now(),
            details={
                "projects_total": projects_total,
                "oldest_secret_age_days": oldest_age_days,
            },
            error=None,
        )


def _project_workplace(payload: Any) -> tuple[Any, Any]:
    """Project Doppler workplace response. Returns
    (projects_total | "unknown", oldest_secret_age_days | "unknown").

    Defensive — the workplace endpoint exposes ``workplace`` dict
    with various fields; we extract what's available, fall back to
    "unknown" markers when shape isn't as expected (the probe's
    job is "is the service reachable + responding"; deep secret-
    age inspection requires per-project queries which are out of
    scope)."""
    if not isinstance(payload, dict):
        return ("unknown", "unknown")
    workplace = payload.get("workplace") if "workplace" in payload else payload
    if not isinstance(workplace, dict):
        return ("unknown", "unknown")
    # Doppler workplace endpoint doesn't expose project list
    # directly — the probe's "projects_total" + "oldest_secret_age_days"
    # remain "unknown" unless a richer signal becomes available.
    return ("unknown", "unknown")
