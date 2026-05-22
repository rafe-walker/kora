"""VercelProbe — Phase 2 Feature 2 backend service probe.

Lists recent deployments via the Vercel REST API; healthy if the
endpoint returns 200 with at least one deployment. Degraded when
the deploy error rate exceeds 10% over the last 24 hours.

Auth: ``KORA_VERCEL_API_TOKEN`` Doppler-injected env var.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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


VERCEL_API_TOKEN_ENV = "KORA_VERCEL_API_TOKEN"
VERCEL_API_BASE = "https://api.vercel.com"
DEGRADED_ERROR_RATE_THRESHOLD = 0.10


class VercelProbe:
    name = "vercel"

    async def check(self) -> ServiceHealthSnapshot:
        token = resolve_env(VERCEL_API_TOKEN_ENV)
        if token is None:
            return snapshot_for_auth_missing(
                name=self.name, env_var=VERCEL_API_TOKEN_ENV
            )

        started_ms = now_ms_monotonic()
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
                response = await client.get(
                    f"{VERCEL_API_BASE}/v6/deployments",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"limit": 100},
                )
        except httpx.HTTPError as exc:
            return snapshot_for_unexpected_error(
                name=self.name, exc=exc, auth_tokens=(token,)
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

        deployments_24h, error_rate = _summarize_deployments(response.json())
        status: str = "healthy"
        if deployments_24h == 0:
            # No recent activity isn't an error — surface as healthy
            # with 0 count. The dashboard reads details to decide if
            # the "no activity" reading is noteworthy.
            status = "healthy"
        elif error_rate > DEGRADED_ERROR_RATE_THRESHOLD:
            status = "degraded"

        return ServiceHealthSnapshot(
            name=self.name,
            status=status,
            latency_ms=latency_ms,
            last_check_at=utc_now(),
            details={
                "deployments_last_24h": deployments_24h,
                "error_rate_24h": round(error_rate, 4),
            },
            error=None,
        )


def _summarize_deployments(payload: Any) -> tuple[int, float]:
    """Project Vercel's deployment list to (count_24h, error_rate).

    Defensive against shape drift — unexpected payload returns
    (0, 0.0) rather than raising.
    """
    if not isinstance(payload, dict):
        return (0, 0.0)
    deployments = payload.get("deployments")
    if not isinstance(deployments, list):
        return (0, 0.0)

    cutoff_ms = int(
        (datetime.now(timezone.utc) - timedelta(hours=24)).timestamp() * 1000
    )
    recent_total = 0
    recent_errors = 0
    for entry in deployments:
        if not isinstance(entry, dict):
            continue
        created_at = entry.get("created")
        if not isinstance(created_at, (int, float)):
            continue
        if created_at < cutoff_ms:
            continue
        recent_total += 1
        state = entry.get("state") or entry.get("readyState")
        if isinstance(state, str) and state.upper() in {"ERROR", "FAILED"}:
            recent_errors += 1

    if recent_total == 0:
        return (0, 0.0)
    return (recent_total, recent_errors / recent_total)
