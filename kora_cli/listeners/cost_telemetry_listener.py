"""Cost-telemetry persistence + window-reset listener — KR-CHEAP-COST-TELEMETRY.

Registers three periodic tasks with the heartbeat scheduler:

  * ``cost_telemetry.persist`` (default 300s / 5 min) — atomic-
    write the current counters to disk so the cockpit + future
    routing-layer can read them outside of process memory.
  * ``cost_telemetry.rolling_24h_reset`` (default 3600s / 1h tick;
    actual reset gated on UTC midnight) — clears the rolling-24h
    window once per UTC day.
  * ``cost_telemetry.monthly_reset`` (default 3600s tick; gated on
    month rollover) — clears the monthly window once per UTC
    month boundary.

The two reset tasks use a "watch + act" pattern: they fire on a
sub-window cadence and check whether the boundary has crossed
since the last reset. This avoids the trickiness of asyncio
scheduling at exact midnight while still guaranteeing the
boundary is honored within a small window of when it occurs.

# Fail-soft

All three tasks are wrapped — exceptions log + swallow so the
heartbeat scheduler keeps ticking. Persistence failures are
operator-recoverable (manual disk inspection); reset failures
worst-case leave the counter rolling slightly past its boundary
(operator visible via the snapshot's window timestamps).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent.background_daemon_registry import (
    BackgroundDaemonEntry,
    PeriodicTaskSpec,
    background_daemon_registry,
)
from kora_cli.daemon import DEFAULT_SHUTDOWN_TIMEOUT, register_daemon_listener
from kora_cli.listeners.heartbeat import register_periodic_task
from kora_cli.telemetry import (
    WINDOW_MONTHLY,
    WINDOW_ROLLING_24H,
    get_telemetry,
)
from utils import atomic_replace

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


COST_TELEMETRY_PATH_ENV = "KORA_COST_TELEMETRY_PATH"
_COST_TELEMETRY_RELATIVE_PATH = Path("cache") / "cost_telemetry.json"


def cost_telemetry_path() -> Path:
    """Resolve the on-disk telemetry path. Env override first, then
    ``${KORA_HOME}/cache/cost_telemetry.json``.

    Mirrors the snapshot module's path-resolution pattern from
    PR #157 so monkeypatch in tests works without ContextVar
    plumbing.
    """
    override = os.environ.get(COST_TELEMETRY_PATH_ENV, "").strip()
    if override:
        return Path(override)
    from kora_constants import get_kora_home

    return get_kora_home() / _COST_TELEMETRY_RELATIVE_PATH


# ---------------------------------------------------------------------------
# Cadence config
# ---------------------------------------------------------------------------


DEFAULT_PERSIST_INTERVAL_SEC: float = 300.0  # 5 min per spec §2(c)
PERSIST_INTERVAL_ENV: str = "KORA_COST_TELEMETRY_PERSIST_INTERVAL_SEC"

# Reset tasks tick every hour and check whether the boundary has
# crossed. Smaller cadence (≤ boundary granularity) means
# precision; choosing 1h trades sub-hour precision for cheap
# accounting (the boundary lateness within an hour is operator-
# negligible for the 24h / monthly windows).
DEFAULT_RESET_TICK_INTERVAL_SEC: float = 3600.0
RESET_TICK_INTERVAL_ENV: str = "KORA_COST_TELEMETRY_RESET_TICK_SEC"


def _read_positive_interval(env_name: str, default_value: float) -> float:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default_value
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "[kora.cost_telemetry_listener] %s=%r is not numeric; "
            "using default %ss",
            env_name,
            raw,
            default_value,
        )
        return default_value
    if value <= 0:
        logger.warning(
            "[kora.cost_telemetry_listener] %s=%s must be > 0; using "
            "default %ss",
            env_name,
            value,
            default_value,
        )
        return default_value
    return value


def _read_persist_interval() -> float:
    return _read_positive_interval(
        PERSIST_INTERVAL_ENV, DEFAULT_PERSIST_INTERVAL_SEC
    )


def _read_reset_tick_interval() -> float:
    return _read_positive_interval(
        RESET_TICK_INTERVAL_ENV, DEFAULT_RESET_TICK_INTERVAL_SEC
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def write_telemetry_snapshot() -> None:
    """Atomic-write the current telemetry snapshot to disk.

    Same pattern as the daemon snapshot from PR #157: write to a
    sibling tmp file then ``atomic_replace`` to the target. Parent
    dir created if missing.
    """
    target = cost_telemetry_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    snapshot = get_telemetry().snapshot()
    payload = {
        "schema_version": 1,
        "written_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "windows": snapshot,
    }
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=target.parent,
        prefix="cost_telemetry.",
        suffix=".tmp",
    ) as fp:
        json.dump(payload, fp, indent=2, sort_keys=True)
        fp.write("\n")
        tmp_path = fp.name
    atomic_replace(tmp_path, target)


def read_telemetry_snapshot() -> Optional[dict]:
    """Read the on-disk telemetry snapshot. Returns the parsed dict
    or ``None`` when missing / unreadable / malformed.

    Same fail-soft posture as :func:`kora_cli.snapshot.read_snapshot`."""
    target = cost_telemetry_path()
    if not target.is_file():
        return None
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "[kora.cost_telemetry] read failed for %s: %r", target, exc
        )
        return None
    try:
        snapshot = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            "[kora.cost_telemetry] malformed snapshot at %s: %r",
            target,
            exc,
        )
        return None
    if not isinstance(snapshot, dict):
        return None
    return snapshot


# ---------------------------------------------------------------------------
# Window-reset state
# ---------------------------------------------------------------------------


# Track the last UTC date/month on which a reset fired so the
# watch-and-act task fires exactly once per boundary crossing.
# Module-level state is fine — the listener is single-process; if a
# future bucket adds multi-process daemons each one resets its own
# in-memory counters independently and that's the correct shape
# (telemetry is per-process today).
_last_24h_reset_date: Optional[datetime] = None
_last_monthly_reset_month: Optional[tuple] = None  # (year, month)


def _utc_date_now() -> datetime:
    return datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _utc_month_now() -> tuple:
    now = datetime.now(timezone.utc)
    return (now.year, now.month)


# ---------------------------------------------------------------------------
# Periodic-task entry points
# ---------------------------------------------------------------------------


async def run_persist_cycle() -> None:
    """One scheduler-tick of the persistence task. Atomic-writes the
    current counter set to disk. Fail-soft."""
    try:
        write_telemetry_snapshot()
    except Exception as exc:
        logger.warning(
            "[kora.cost_telemetry] persist cycle raised %r — counters "
            "still in memory; will retry next tick",
            exc,
        )


async def run_rolling_24h_reset_check() -> None:
    """Reset the rolling-24h window if a UTC midnight has crossed
    since the last reset.

    First call after process boot stamps the "last reset date" as
    today's UTC date WITHOUT firing a reset (counters at zero
    anyway). Subsequent calls fire a reset only when the UTC date
    changes.
    """
    global _last_24h_reset_date
    today = _utc_date_now()
    if _last_24h_reset_date is None:
        _last_24h_reset_date = today
        return
    if today > _last_24h_reset_date:
        try:
            get_telemetry().reset_window(WINDOW_ROLLING_24H)
            _last_24h_reset_date = today
        except Exception as exc:
            logger.warning(
                "[kora.cost_telemetry] rolling_24h reset raised %r — "
                "will retry next tick",
                exc,
            )


async def run_monthly_reset_check() -> None:
    """Reset the monthly window if a UTC month rollover has crossed
    since the last reset.

    Same first-tick stamping shape as the 24h reset.
    """
    global _last_monthly_reset_month
    current = _utc_month_now()
    if _last_monthly_reset_month is None:
        _last_monthly_reset_month = current
        return
    if current != _last_monthly_reset_month:
        try:
            get_telemetry().reset_window(WINDOW_MONTHLY)
            _last_monthly_reset_month = current
        except Exception as exc:
            logger.warning(
                "[kora.cost_telemetry] monthly reset raised %r — "
                "will retry next tick",
                exc,
            )


def _reset_window_tracking_for_tests() -> None:
    """Test-only: clear the reset-tracking module state. Production
    code MUST NOT call this."""
    global _last_24h_reset_date, _last_monthly_reset_month
    _last_24h_reset_date = None
    _last_monthly_reset_month = None


# ---------------------------------------------------------------------------
# Listener
# ---------------------------------------------------------------------------


class CostTelemetryListener:
    """Stateless lifecycle binding. The three periodic tasks are
    registered at module-import time; this listener just emits
    boot logs so operators can confirm wiring."""

    async def startup(self, coordinator=None) -> None:
        logger.info(
            "[kora.cost_telemetry_listener] periodic tasks registered: "
            "persist cadence=%ss, reset-tick cadence=%ss",
            _read_persist_interval(),
            _read_reset_tick_interval(),
        )

    async def shutdown(self) -> None:
        logger.info("[kora.cost_telemetry_listener] shutdown")


# ---------------------------------------------------------------------------
# Factory + registration (import-time side effect)
# ---------------------------------------------------------------------------


# Process-wide singleton — KR-DAEMON-LISTENERS-VIA-GATEWAY Phase 2.
_listener_singleton = CostTelemetryListener()


def _factory():
    return (
        _listener_singleton.startup,
        _listener_singleton.shutdown,
        DEFAULT_SHUTDOWN_TIMEOUT,
    )


register_daemon_listener("cost_telemetry", _factory)


# 5-min persistence cycle.
register_periodic_task(
    "cost_telemetry.persist",
    interval_seconds=_read_persist_interval(),
    callable=run_persist_cycle,
)
# Hourly watch-and-act resets for the two windowed counters.
register_periodic_task(
    "cost_telemetry.rolling_24h_reset",
    interval_seconds=_read_reset_tick_interval(),
    callable=run_rolling_24h_reset_check,
)
register_periodic_task(
    "cost_telemetry.monthly_reset",
    interval_seconds=_read_reset_tick_interval(),
    callable=run_monthly_reset_check,
)


# ---------------------------------------------------------------------------
# Hermes-side registration (Phase 2; Path B thin-shim same as snapshot #196)
# ---------------------------------------------------------------------------
#
# Multi-task listener: the audit's §4.1 recommendation (option c) says
# "let startup spawn its own asyncio loops" for daemons with more than
# one periodic task. We register the PRIMARY persist task here in the
# BackgroundDaemonEntry's periodic_task field; the two reset-check
# tasks stay on Kora's heartbeat scheduler via the register_periodic_
# task calls above. Phase 4 (scheduler dissolution) will revisit
# whether to extend PeriodicTaskSpec to a list-of-specs.

_hermes_entry = BackgroundDaemonEntry(
    name="cost_telemetry",
    startup=_listener_singleton.startup,
    shutdown=_listener_singleton.shutdown,
    periodic_task=PeriodicTaskSpec(
        interval_seconds=_read_persist_interval(),
        callback=run_persist_cycle,
        name="cost_telemetry.persist",
    ),
    shutdown_timeout=DEFAULT_SHUTDOWN_TIMEOUT,
    plugin_name="kora",
)

try:
    background_daemon_registry().register(_hermes_entry)
except ValueError as _exc:
    logger.debug(
        "[kora.cost_telemetry_listener] hermes registry already had "
        "'cost_telemetry' entry: %s — skipping duplicate registration",
        _exc,
    )
