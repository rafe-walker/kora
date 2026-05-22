"""Backend service heartbeat probes (KR-FEAT-HEARTBEAT ST1).

5 probes for Joshua's backend stack: vercel, sentry, doppler,
supabase, fly. Each implements :class:`ServiceProbe` (Protocol) and
returns a :class:`ServiceHealthSnapshot`.

Probe-wide rules:

  - 10s per-probe timeout (matches §4 Q1 default cadence; one slow
    probe doesn't stall the others).
  - Missing auth env → status ``unknown`` + ``error`` carrying the
    env-var name (NEVER the value) + ZERO outbound call.
  - All error strings sanitized — never leaks the auth token, even
    on partial-auth failures.
  - Probe-failure isolation: a probe raising an unhandled exception
    is caught by the runner; siblings keep running.

The :func:`run_all_probes` helper iterates :func:`default_probes`
and writes results into the module-level snapshot cache exposed
via :func:`current_service_snapshots`. The daemon listener at
:mod:`kora_cli.listeners.heartbeat_probes_listener` registers a
heartbeat-scheduler task at 5-min cadence (operator override via
``KORA_HEARTBEAT_PROBE_INTERVAL_SEC``).
"""

from kora_cli.heartbeat_probes.base import (
    PROBE_TIMEOUT_SECONDS,
    ServiceProbe,
    snapshot_for_auth_missing,
    snapshot_for_timeout,
    snapshot_for_unexpected_error,
)
from kora_cli.heartbeat_probes.doppler import DopplerProbe
from kora_cli.heartbeat_probes.fly import FlyProbe
from kora_cli.heartbeat_probes.runner import (
    current_service_snapshots,
    default_probes,
    run_all_probes,
)
from kora_cli.heartbeat_probes.sentry import SentryProbe
from kora_cli.heartbeat_probes.supabase import SupabaseProbe
from kora_cli.heartbeat_probes.types import (
    SERVICE_STATUSES,
    ServiceHealthSnapshot,
    ServiceStatus,
)
from kora_cli.heartbeat_probes.vercel import VercelProbe

__all__ = [
    "PROBE_TIMEOUT_SECONDS",
    "SERVICE_STATUSES",
    "DopplerProbe",
    "FlyProbe",
    "SentryProbe",
    "ServiceHealthSnapshot",
    "ServiceProbe",
    "ServiceStatus",
    "SupabaseProbe",
    "VercelProbe",
    "current_service_snapshots",
    "default_probes",
    "run_all_probes",
    "snapshot_for_auth_missing",
    "snapshot_for_timeout",
    "snapshot_for_unexpected_error",
]
