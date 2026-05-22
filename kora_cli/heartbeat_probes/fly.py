"""FlyProbe — checks Kora's deployed Fly apps.

Auth: ``KORA_FLY_API_TOKEN``. Apps probed: ``kora-runtime`` always +
``kora-runtime-staging`` when ``KORA_FLY_STAGING_APP_NAME`` env is
set (matches the existing two-app deploy pattern).

Healthy: response 200 + ≥1 machine running. Degraded:
machines.healthy_count < machines.total_count.
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


FLY_API_TOKEN_ENV = "KORA_FLY_API_TOKEN"
FLY_STAGING_APP_NAME_ENV = "KORA_FLY_STAGING_APP_NAME"
FLY_API_BASE = "https://api.machines.dev"
DEFAULT_PROD_APP = "kora-runtime"


class FlyProbe:
    name = "fly"

    async def check(self) -> ServiceHealthSnapshot:
        token = resolve_env(FLY_API_TOKEN_ENV)
        if token is None:
            return snapshot_for_auth_missing(
                name=self.name, env_var=FLY_API_TOKEN_ENV
            )

        apps = [DEFAULT_PROD_APP]
        staging = resolve_env(FLY_STAGING_APP_NAME_ENV)
        if staging:
            apps.append(staging)

        started_ms = now_ms_monotonic()
        apps_running = 0
        apps_total = 0
        any_degraded = False
        first_error_status: int | None = None

        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
                for app_name in apps:
                    machines_response = await client.get(
                        f"{FLY_API_BASE}/v1/apps/{app_name}/machines",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    if machines_response.status_code != 200:
                        if first_error_status is None:
                            first_error_status = machines_response.status_code
                        continue
                    healthy, total = _count_machines(machines_response.json())
                    apps_total += 1
                    if healthy >= 1:
                        apps_running += 1
                    if healthy < total:
                        any_degraded = True
        except Exception as exc:
            return snapshot_for_unexpected_error(
                name=self.name, exc=exc, auth_tokens=(token,)
            )

        latency_ms = int(now_ms_monotonic() - started_ms)

        # All app calls failed → unhealthy
        if apps_total == 0:
            return ServiceHealthSnapshot(
                name=self.name,
                status="unhealthy",
                latency_ms=latency_ms,
                last_check_at=utc_now(),
                details={"apps_running": 0, "deploys_last_24h": 0},
                error=sanitize_error(
                    f"HTTP {first_error_status}"
                    if first_error_status
                    else "no successful app responses",
                    token,
                ),
            )

        status = (
            "degraded"
            if any_degraded or apps_running < len(apps)
            else "healthy"
        )

        return ServiceHealthSnapshot(
            name=self.name,
            status=status,
            latency_ms=latency_ms,
            last_check_at=utc_now(),
            details={
                "apps_running": apps_running,
                # deploys_last_24h is a Releases API query; out of
                # scope for the heartbeat-cycle. Operator panel
                # surfaces "unknown" if needed via the FE renderer.
                "deploys_last_24h": "unknown",
            },
            error=None,
        )


def _count_machines(payload: Any) -> tuple[int, int]:
    """Return (healthy_count, total_count) from /machines response.

    A machine is "healthy" when state == "started"; everything else
    (stopped, suspended, replacing, destroyed) counts toward total
    but not healthy. Defensive against shape drift."""
    if not isinstance(payload, list):
        return (0, 0)
    total = len(payload)
    healthy = 0
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        state = entry.get("state")
        if isinstance(state, str) and state == "started":
            healthy += 1
    return (healthy, total)
