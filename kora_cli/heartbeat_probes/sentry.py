"""SentryProbe — lists unresolved issues for the configured org.

Auth: ``KORA_SENTRY_API_TOKEN`` + ``KORA_SENTRY_ORG`` env vars.
Healthy: 200 + ≤10 unresolved issues. Degraded: 200 with >10.
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


SENTRY_API_TOKEN_ENV = "KORA_SENTRY_API_TOKEN"
SENTRY_ORG_ENV = "KORA_SENTRY_ORG"
SENTRY_API_BASE = "https://sentry.io"
DEGRADED_UNRESOLVED_THRESHOLD = 10


class SentryProbe:
    name = "sentry"

    async def check(self) -> ServiceHealthSnapshot:
        token = resolve_env(SENTRY_API_TOKEN_ENV)
        org = resolve_env(SENTRY_ORG_ENV)
        if token is None or org is None:
            return snapshot_for_auth_missing(
                name=self.name,
                env_var=SENTRY_API_TOKEN_ENV,
                extra_envs=(SENTRY_ORG_ENV,),
            )

        started_ms = now_ms_monotonic()
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
                response = await client.get(
                    f"{SENTRY_API_BASE}/api/0/organizations/{org}/issues/",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"query": "is:unresolved", "limit": 100},
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

        unresolved = _count_unresolved(response.json())
        status = (
            "degraded"
            if unresolved > DEGRADED_UNRESOLVED_THRESHOLD
            else "healthy"
        )
        return ServiceHealthSnapshot(
            name=self.name,
            status=status,
            latency_ms=latency_ms,
            last_check_at=utc_now(),
            details={"unresolved_issues": unresolved},
            error=None,
        )


def _count_unresolved(payload: Any) -> int:
    """Count entries in the response list. Defensive against
    shape drift — returns 0 if payload isn't a list."""
    if not isinstance(payload, list):
        return 0
    return len(payload)
