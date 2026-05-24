"""Probe runner + snapshot cache (KR-FEAT-HEARTBEAT ST1).

Iterates the 5 default probes; isolates per-probe failures so one
slow / broken probe doesn't block the others. Writes results to a
module-level cache exposed via :func:`current_service_snapshots`.

The cache is process-shared (singleton dict). The daemon listener
at :mod:`kora_cli.listeners.heartbeat_probes_listener` registers a
:func:`run_all_probes` task with the heartbeat scheduler; first
cycle populates the cache. Until then, callers see an empty dict
(ST2 endpoint surfaces this as ``cache_warming: true``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Sequence

from kora_cli.heartbeat_probes.base import (
    ServiceProbe,
    snapshot_for_unexpected_error,
    with_timeout,
)
from kora_cli.heartbeat_probes.doppler import DopplerProbe
from kora_cli.heartbeat_probes.fly import FlyProbe
from kora_cli.heartbeat_probes.sentry import SentryProbe
from kora_cli.heartbeat_probes.supabase import SupabaseProbe
from kora_cli.heartbeat_probes.types import ServiceHealthSnapshot
from kora_cli.heartbeat_probes.vercel import VercelProbe

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default probe set
# ---------------------------------------------------------------------------


def default_probes() -> tuple[ServiceProbe, ...]:
    """Return a fresh tuple of the 5 default probes.

    Each call returns NEW instances — probes are cheap to construct
    and stateless beyond their httpx clients (created per-check).
    """
    return (
        VercelProbe(),
        SentryProbe(),
        DopplerProbe(),
        SupabaseProbe(),
        FlyProbe(),
    )


# ---------------------------------------------------------------------------
# Snapshot cache + accessor
# ---------------------------------------------------------------------------


_snapshot_cache: dict[str, ServiceHealthSnapshot] = {}


def current_service_snapshots() -> dict[str, ServiceHealthSnapshot]:
    """Return a defensive copy of the snapshot cache.

    Read by ``/api/heartbeat/services`` (KR-FEAT-HEARTBEAT ST2).
    Mutations on the returned dict don't leak into the cache.
    """
    return dict(_snapshot_cache)


def _clear_snapshot_cache() -> None:
    """Test hook + listener-shutdown helper."""
    global _snapshot_cache
    _snapshot_cache = {}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def run_all_probes(
    probes: Sequence[ServiceProbe] | None = None,
) -> dict[str, ServiceHealthSnapshot]:
    """Run every probe + populate the snapshot cache.

    Each probe runs under a 10s wall-clock timeout (per
    :func:`with_timeout`). A probe that raises an unhandled
    exception (anything not caught by its own ``check()``) is
    caught here + recorded via :func:`snapshot_for_unexpected_error`
    — guarantees one snapshot per probe per cycle.

    Probes run SERIALLY: per-cycle latency stays bounded
    (5 × 10s worst case = 50s, well under the 5-min cadence), and
    keeping probes serial avoids accidental concurrent-token-use
    pressure on rate-limited upstream APIs.

    Returns the snapshot map for the cycle (also written to the
    module cache).
    """
    if probes is None:
        probes = default_probes()
    results: dict[str, ServiceHealthSnapshot] = {}
    for probe in probes:
        try:
            snapshot = await with_timeout(probe.check(), name=probe.name)
        except Exception as exc:
            # Defense in depth: probe's check() should never raise
            # past with_timeout's catch — this guards against
            # construction-time errors or unexpected exit paths.
            logger.warning(
                "[kora.heartbeat_probes] %s.check() raised unexpectedly: %r",
                probe.name,
                exc,
            )
            snapshot = snapshot_for_unexpected_error(
                name=probe.name, exc=exc
            )
        results[probe.name] = snapshot
        _snapshot_cache[probe.name] = snapshot

    # KR-PROBE-AUDIT-AND-CONVERT — post-cycle issue-detection hook.
    # Classifies each snapshot into Issue objects + emits one
    # ``probe.wake_requested`` audit row per Issue. Routine probing
    # remains $0 LLM cost; the audit emission is in-memory + JSONL
    # append. Fail-soft per the wake emitter's own contract — any
    # exception here logs + continues.
    try:
        from kora_cli.probes import detect_issues, emit_wake_event

        for issue in detect_issues(results.values()):
            emit_wake_event(issue)
    except Exception as exc:
        logger.warning(
            "[kora.heartbeat_probes] post-cycle issue detection raised "
            "%r — wake events not emitted this cycle",
            exc,
        )

    return results


# ---------------------------------------------------------------------------
# Cancellable runner for the scheduler
# ---------------------------------------------------------------------------


async def run_all_probes_scheduled() -> None:
    """Scheduler-callable: zero-arg, returns None.

    Wraps :func:`run_all_probes` so it matches
    :data:`kora_cli.listeners.heartbeat.PeriodicCallable` type. A
    scheduler-cancelled cycle still leaves the cache in whatever
    state it was in (snapshots from completed probes); next cycle
    overwrites."""
    try:
        await run_all_probes()
    except asyncio.CancelledError:
        # Scheduler shutdown — propagate so the wrapping task exits
        raise
    except Exception as exc:
        # Unknown error in run_all_probes itself (not per-probe;
        # those are caught above). Surface so the scheduler logs +
        # keeps firing on cadence.
        logger.exception(
            "[kora.heartbeat_probes] run_all_probes raised: %r", exc
        )
