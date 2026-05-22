"""SupabaseProbe — HEADs the PostgREST endpoint of IsoKron's DB.

Auth: ``KORA_SUPABASE_ANON_KEY`` + ``KORA_SUPABASE_URL`` env vars.
The anon key is the right surface for a heartbeat — it's expected
in client-side SDKs + carries no privileged access.

Healthy: HEAD returns 200/204. Degraded: connections_pct > 80
(when surface available; "unknown" otherwise).
"""

from __future__ import annotations

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


SUPABASE_ANON_KEY_ENV = "KORA_SUPABASE_ANON_KEY"
SUPABASE_URL_ENV = "KORA_SUPABASE_URL"
DEGRADED_CONNECTIONS_PCT_THRESHOLD = 80.0


class SupabaseProbe:
    name = "supabase"

    async def check(self) -> ServiceHealthSnapshot:
        anon_key = resolve_env(SUPABASE_ANON_KEY_ENV)
        url = resolve_env(SUPABASE_URL_ENV)
        if anon_key is None or url is None:
            return snapshot_for_auth_missing(
                name=self.name,
                env_var=SUPABASE_ANON_KEY_ENV,
                extra_envs=(SUPABASE_URL_ENV,),
            )

        # PostgREST endpoint — the canonical reachability check
        # against Supabase. HEAD on the root returns 200 when the
        # API is up. Strip trailing slash to avoid double-slash.
        rest_url = f"{url.rstrip('/')}/rest/v1/"

        started_ms = now_ms_monotonic()
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
                response = await client.head(
                    rest_url,
                    headers={
                        "apikey": anon_key,
                        "Authorization": f"Bearer {anon_key}",
                    },
                )
        except Exception as exc:
            return snapshot_for_unexpected_error(
                name=self.name, exc=exc, auth_tokens=(anon_key,)
            )

        latency_ms = int(now_ms_monotonic() - started_ms)
        if response.status_code not in (200, 204):
            return ServiceHealthSnapshot(
                name=self.name,
                status="unhealthy",
                latency_ms=latency_ms,
                last_check_at=utc_now(),
                details={},
                error=sanitize_error(
                    f"HTTP {response.status_code}", anon_key
                ),
            )

        # connections_pct is "unknown" — pulling it requires the
        # Supabase Management API + a project ref, which is a
        # different auth surface. Surfaced as a known-unknown so
        # operator sees the gap; doesn't fail the probe.
        return ServiceHealthSnapshot(
            name=self.name,
            status="healthy",
            latency_ms=latency_ms,
            last_check_at=utc_now(),
            details={"connections_pct": "unknown"},
            error=None,
        )
