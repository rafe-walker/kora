"""Health-rollup state holder + subsignal collectors (KR-P2-L ST1, R4.1 §9.7).

R4.1 §9.7 defines a Kora-side health rollup that combines 8 subsignals
into a coherent operator-readable shape:

  - ``last_successful_write`` — last successful ``kora__append_event``
    invocation (chain-emit liveness)
  - ``claim_state`` — current SeaTicket claim status (active / idle)
  - ``credit_burn`` — cost-ladder ``current_pct_used``
  - ``breaker_state`` — cost-ladder HARD_STOP_100 indicator
    (closed / open)
  - ``auth_validity_window`` — Claude Code OAuth token expiry
    (days remaining)
  - ``dispatch_reachable`` — last successful MCP invoke (substrate
    reachability)
  - ``last_heartbeat`` — last successful ``kora__refresh_claim`` call
  - ``escalation_watcher_liveness`` — cockpit-BFF watcher's substrate-
    visible liveness ping (PENDING substrate ack; reported as
    ``missing`` until the substrate exposes this signal)

The rollup is rolled up cockpit-side via :func:`HealthRollupHolder.current`
which returns a :class:`HealthRollup` with each subsignal + the three
top-level enums (``overall`` / ``control_plane`` / ``worker``) +
``stopped_reason``.

# Overall / plane derivation

ST1 ships a SIMPLE derivation: any non-``fresh`` subsignal degrades
``overall`` to ``degraded``; ``control_plane`` and ``worker`` default
to ``healthy``. ST3 replaces this with R4.1 §9.7's subsignal-grouped
derivation (control-plane = dispatch_reachable + escalation_watcher
+ auth_validity_window; worker = claim_state + last_heartbeat +
credit_burn + breaker_state + last_successful_write).

The placeholder is deliberately conservative — flips happen on the
ST3 PR alongside the test that asserts the new contract, so the FE
sees a useful (if coarse) signal in the meantime.

# Probe cadence storage

The probe-emit cron's cadence is operator-tunable via the
``KORA_HEALTH_PROBE_CADENCE_SECONDS`` env var (default 300 = 5 min
per R4.1 §9.7). Read at holder-init; runtime mutation is not
supported (a cadence change requires a restart).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums + constants
# ---------------------------------------------------------------------------


class HealthStatus(str, Enum):
    """Top-level health enum per R4.1 §9.7.

    String values pinned for cockpit consumers.
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STOPPED = "stopped"
    OUTAGE = "outage"


class SubsignalStatus(str, Enum):
    """Per-subsignal status enum per R4.1 §9.7."""

    FRESH = "fresh"
    STALE = "stale"
    MISSING = "missing"
    DEGRADED = "degraded"


# 8 subsignal names — keys in the HEALTH-PANEL payload + stable
# identifiers cockpit consumers index on.
SUBSIGNAL_LAST_SUCCESSFUL_WRITE = "last_successful_write"
SUBSIGNAL_CLAIM_STATE = "claim_state"
SUBSIGNAL_CREDIT_BURN = "credit_burn"
SUBSIGNAL_BREAKER_STATE = "breaker_state"
SUBSIGNAL_AUTH_VALIDITY_WINDOW = "auth_validity_window"
SUBSIGNAL_DISPATCH_REACHABLE = "dispatch_reachable"
SUBSIGNAL_LAST_HEARTBEAT = "last_heartbeat"
SUBSIGNAL_ESCALATION_WATCHER_LIVENESS = "escalation_watcher_liveness"

ALL_SUBSIGNAL_NAMES = (
    SUBSIGNAL_LAST_SUCCESSFUL_WRITE,
    SUBSIGNAL_CLAIM_STATE,
    SUBSIGNAL_CREDIT_BURN,
    SUBSIGNAL_BREAKER_STATE,
    SUBSIGNAL_AUTH_VALIDITY_WINDOW,
    SUBSIGNAL_DISPATCH_REACHABLE,
    SUBSIGNAL_LAST_HEARTBEAT,
    SUBSIGNAL_ESCALATION_WATCHER_LIVENESS,
)


# R4.1 §9.7 freshness thresholds. Match the values pinned by the
# HEALTH-PANEL stub in ``kora_cli/web_server.py`` so the ST4 flip is
# a literal swap without FE-contract drift.
DEFAULT_LAST_SUCCESSFUL_WRITE_THRESHOLD_SECONDS = 300
DEFAULT_DISPATCH_REACHABLE_THRESHOLD_SECONDS = 60
DEFAULT_LAST_HEARTBEAT_THRESHOLD_SECONDS = 90
DEFAULT_ESCALATION_WATCHER_THRESHOLD_SECONDS = 15
DEFAULT_AUTH_VALIDITY_THRESHOLD_DAYS = 30
DEFAULT_CREDIT_BURN_THRESHOLD_PCT = 90.0

DEFAULT_PROBE_CADENCE_SECONDS = 300  # 5 min per R4.1 §9.7

# Env override for the probe cadence. Operator override; read once
# at holder init.
ENV_PROBE_CADENCE_SECONDS = "KORA_HEALTH_PROBE_CADENCE_SECONDS"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Subsignal:
    """One R4.1 §9.7 subsignal: name + freshness + raw value bag.

    Attributes:
        name: Stable identifier — one of the eight constants in
            :data:`ALL_SUBSIGNAL_NAMES`.
        status: Freshness classification per
            :class:`SubsignalStatus`.
        threshold_seconds: For time-based subsignals, the freshness
            window after which ``status`` flips to ``stale``.
            ``None`` for axis-specific signals (credit_burn,
            breaker_state, auth_validity_window — those use
            ``threshold_pct`` / ``threshold_days`` carried in
            ``extra``).
        elapsed_seconds: Wall-clock since ``last_seen`` was emitted.
            ``None`` when the subsignal has no observation yet.
        last_seen: When the underlying signal was last observed.
            For ``escalation_watcher_liveness`` this currently
            reflects the most recent ``collect_now`` call (the
            substrate-side ping isn't wired yet, so the value is
            informational only).
        extra: Subsignal-specific extra fields (e.g.
            ``value_pct``, ``rung``, ``expires_at``, ``claim_id``).
            Shape pinned by the HEALTH-PANEL JSON contract in
            ``kora_cli/web_server.py``.
    """

    name: str
    status: SubsignalStatus
    threshold_seconds: Optional[int] = None
    elapsed_seconds: Optional[int] = None
    last_seen: Optional[datetime] = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HealthRollup:
    """Snapshot of the R4.1 §9.7 health rollup.

    Carries the three top-level enums + ``stopped_reason`` + the 8
    subsignals keyed by name.
    """

    overall: HealthStatus
    control_plane: HealthStatus
    worker: HealthStatus
    stopped_reason: Optional[str]
    subsignals: dict[str, Subsignal]
    collected_at: datetime


# ---------------------------------------------------------------------------
# Collector helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _elapsed_seconds(reference: datetime, now: Optional[datetime] = None) -> int:
    now = now or _now()
    delta = (now - reference).total_seconds()
    return max(0, int(delta))


def _classify_time_freshness(
    last_seen: Optional[datetime],
    *,
    threshold_seconds: int,
    now: Optional[datetime] = None,
) -> tuple[SubsignalStatus, Optional[int]]:
    """Map a (last_seen, threshold) pair to a ``SubsignalStatus``.

    Returns the status + elapsed_seconds (None when no observation).

    Semantics:
      - ``last_seen is None`` → ``MISSING``
      - elapsed ≤ threshold → ``FRESH``
      - elapsed > threshold → ``STALE``
    """
    if last_seen is None:
        return SubsignalStatus.MISSING, None
    elapsed = _elapsed_seconds(last_seen, now=now)
    if elapsed <= threshold_seconds:
        return SubsignalStatus.FRESH, elapsed
    return SubsignalStatus.STALE, elapsed


# ---------------------------------------------------------------------------
# Subsignal collectors — one per axis. Each is a pure function: takes
# its dependencies as kwargs, returns a Subsignal. Holder.current()
# wires them all up against the live singletons.
# ---------------------------------------------------------------------------


def collect_last_successful_write(
    *,
    last_write_at: Optional[datetime],
    threshold_seconds: int = DEFAULT_LAST_SUCCESSFUL_WRITE_THRESHOLD_SECONDS,
    now: Optional[datetime] = None,
) -> Subsignal:
    status, elapsed = _classify_time_freshness(
        last_write_at, threshold_seconds=threshold_seconds, now=now
    )
    extra: dict[str, Any] = {}
    if last_write_at is not None:
        extra["value_at"] = last_write_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    return Subsignal(
        name=SUBSIGNAL_LAST_SUCCESSFUL_WRITE,
        status=status,
        threshold_seconds=threshold_seconds,
        elapsed_seconds=elapsed,
        last_seen=last_write_at,
        extra=extra,
    )


def collect_claim_state(*, current_claim: Optional[Any]) -> Subsignal:
    """Snapshot the SeaTicketPoller's current_claim attribute.

    ``current_claim`` is an ``ActiveClaim`` (frozen dataclass with
    ``ticket_id`` / ``work_attempt_id`` / ``claimed_at`` etc.) or
    ``None`` when idle. Status is always ``fresh`` for the
    claim_state signal — the cockpit cares about the value not its
    freshness; the operational-state holder is the authoritative
    "is this stale?" signal elsewhere.
    """
    if current_claim is None:
        return Subsignal(
            name=SUBSIGNAL_CLAIM_STATE,
            status=SubsignalStatus.FRESH,
            extra={"value": "idle", "claim_id": None},
        )
    return Subsignal(
        name=SUBSIGNAL_CLAIM_STATE,
        status=SubsignalStatus.FRESH,
        last_seen=getattr(current_claim, "claimed_at", None),
        extra={
            "value": "active",
            "claim_id": getattr(current_claim, "work_attempt_id", None),
            "ticket_id": getattr(current_claim, "ticket_id", None),
        },
    )


def collect_credit_burn(
    *,
    cost_holder: Optional[Any],
    threshold_pct: float = DEFAULT_CREDIT_BURN_THRESHOLD_PCT,
) -> Subsignal:
    """Pulls ``CostStateHolder.current_pct_used()`` and the active rung.

    Status:
      - ``cost_holder is None`` → ``MISSING`` (cost ladder not
        initialized yet — early-boot window)
      - pct < 75 → ``FRESH``
      - 75 ≤ pct < 90 → ``DEGRADED`` (WARN_75 rung)
      - 90 ≤ pct < 100 → ``DEGRADED`` (DOWNSHIFT_90 rung)
      - pct ≥ 100 → ``DEGRADED`` (HARD_STOP_100 rung)
    """
    if cost_holder is None:
        return Subsignal(
            name=SUBSIGNAL_CREDIT_BURN,
            status=SubsignalStatus.MISSING,
            extra={"value_pct": None, "threshold_pct": threshold_pct, "rung": None},
        )
    try:
        pct = float(cost_holder.current_pct_used()) * 100.0
        rung = cost_holder.active_rung().value
    except Exception:
        logger.debug(
            "[health_rollup] cost_holder current_pct_used/active_rung raised",
            exc_info=True,
        )
        return Subsignal(
            name=SUBSIGNAL_CREDIT_BURN,
            status=SubsignalStatus.MISSING,
            extra={"value_pct": None, "threshold_pct": threshold_pct, "rung": None},
        )
    # Below the 75% rung the budget is healthy; above 75% the
    # cockpit wants visibility (degraded). The PANEL stub uses
    # ``"fresh"`` even at warn_75 — that's coarser than R4.1 §9.7
    # intends. For ST1 we mirror the stub's pattern (status=fresh
    # below threshold_pct=90, degraded at/above) so the panel
    # rendering stays consistent during the flip.
    if pct >= threshold_pct:
        status = SubsignalStatus.DEGRADED
    else:
        status = SubsignalStatus.FRESH
    return Subsignal(
        name=SUBSIGNAL_CREDIT_BURN,
        status=status,
        extra={
            "value_pct": round(pct, 2),
            "threshold_pct": threshold_pct,
            "rung": rung,
        },
    )


def collect_breaker_state(*, cost_holder: Optional[Any]) -> Subsignal:
    """Maps cost-ladder HARD_STOP_100 to a closed/open breaker signal."""
    if cost_holder is None:
        return Subsignal(
            name=SUBSIGNAL_BREAKER_STATE,
            status=SubsignalStatus.MISSING,
            extra={"value": "unknown"},
        )
    try:
        from agent.cost_state_holder import CostRung

        is_open = cost_holder.active_rung() is CostRung.HARD_STOP_100
    except Exception:
        logger.debug(
            "[health_rollup] cost_holder breaker check raised", exc_info=True
        )
        return Subsignal(
            name=SUBSIGNAL_BREAKER_STATE,
            status=SubsignalStatus.MISSING,
            extra={"value": "unknown"},
        )
    return Subsignal(
        name=SUBSIGNAL_BREAKER_STATE,
        # An open breaker is the runtime in safety mode — degraded.
        status=SubsignalStatus.DEGRADED if is_open else SubsignalStatus.FRESH,
        extra={"value": "open" if is_open else "closed"},
    )


def collect_auth_validity_window(
    *,
    credentials: Optional[dict[str, Any]],
    threshold_days: int = DEFAULT_AUTH_VALIDITY_THRESHOLD_DAYS,
    now: Optional[datetime] = None,
) -> Subsignal:
    """Decode the Claude Code OAuth ``expiresAt`` (milliseconds since
    epoch) into a days-remaining window.

    Status:
      - no creds OR no ``expiresAt`` → ``MISSING`` (env-only or
        managed-key flow — expiry unknown)
      - days_remaining < 0 → ``DEGRADED`` (token already expired)
      - days_remaining < threshold → ``STALE`` (approaching expiry)
      - days_remaining ≥ threshold → ``FRESH``
    """
    now = now or _now()
    if not credentials:
        return Subsignal(
            name=SUBSIGNAL_AUTH_VALIDITY_WINDOW,
            status=SubsignalStatus.MISSING,
            extra={
                "expires_at": None,
                "threshold_days": threshold_days,
                "days_remaining": None,
            },
        )
    expires_at_ms = credentials.get("expiresAt")
    if not expires_at_ms:
        return Subsignal(
            name=SUBSIGNAL_AUTH_VALIDITY_WINDOW,
            status=SubsignalStatus.MISSING,
            extra={
                "expires_at": None,
                "threshold_days": threshold_days,
                "days_remaining": None,
            },
        )
    try:
        expires_at = datetime.fromtimestamp(
            expires_at_ms / 1000.0, tz=timezone.utc
        )
    except (ValueError, OverflowError, TypeError, OSError):
        return Subsignal(
            name=SUBSIGNAL_AUTH_VALIDITY_WINDOW,
            status=SubsignalStatus.MISSING,
            extra={
                "expires_at": None,
                "threshold_days": threshold_days,
                "days_remaining": None,
            },
        )
    days_remaining = (expires_at - now).total_seconds() / 86400.0
    if days_remaining < 0:
        status = SubsignalStatus.DEGRADED
    elif days_remaining < threshold_days:
        status = SubsignalStatus.STALE
    else:
        status = SubsignalStatus.FRESH
    return Subsignal(
        name=SUBSIGNAL_AUTH_VALIDITY_WINDOW,
        status=status,
        last_seen=now,
        extra={
            "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "threshold_days": threshold_days,
            "days_remaining": int(days_remaining),
        },
    )


def collect_dispatch_reachable(
    *,
    last_invoke_at: Optional[datetime],
    threshold_seconds: int = DEFAULT_DISPATCH_REACHABLE_THRESHOLD_SECONDS,
    now: Optional[datetime] = None,
) -> Subsignal:
    status, elapsed = _classify_time_freshness(
        last_invoke_at, threshold_seconds=threshold_seconds, now=now
    )
    extra: dict[str, Any] = {}
    if last_invoke_at is not None:
        extra["value_at"] = last_invoke_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    return Subsignal(
        name=SUBSIGNAL_DISPATCH_REACHABLE,
        status=status,
        threshold_seconds=threshold_seconds,
        elapsed_seconds=elapsed,
        last_seen=last_invoke_at,
        extra=extra,
    )


def collect_last_heartbeat(
    *,
    last_heartbeat_at: Optional[datetime],
    threshold_seconds: int = DEFAULT_LAST_HEARTBEAT_THRESHOLD_SECONDS,
    now: Optional[datetime] = None,
) -> Subsignal:
    status, elapsed = _classify_time_freshness(
        last_heartbeat_at, threshold_seconds=threshold_seconds, now=now
    )
    extra: dict[str, Any] = {}
    if last_heartbeat_at is not None:
        extra["value_at"] = last_heartbeat_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    return Subsignal(
        name=SUBSIGNAL_LAST_HEARTBEAT,
        status=status,
        threshold_seconds=threshold_seconds,
        elapsed_seconds=elapsed,
        last_seen=last_heartbeat_at,
        extra=extra,
    )


def collect_escalation_watcher_liveness(
    *,
    threshold_seconds: int = DEFAULT_ESCALATION_WATCHER_THRESHOLD_SECONDS,
    now: Optional[datetime] = None,
) -> Subsignal:
    """KR-P2-L ST1 — escalation_watcher_liveness fallback.

    R4.1 §9.7 P6 expects a substrate-side liveness ping from the
    cockpit-BFF's escalation watcher. Substrate-team hasn't shipped
    that signal yet (verified absent during §1 verifications — no
    matching event-vocabulary or table). Until that lands the
    collector returns ``status=missing`` with an ``extra.note``
    explaining the gap. The HEALTH-PANEL P6 banner switches on
    ``status == 'stale'``; ``missing`` deliberately doesn't trip the
    banner (no false positives while substrate ack is pending).
    """
    return Subsignal(
        name=SUBSIGNAL_ESCALATION_WATCHER_LIVENESS,
        status=SubsignalStatus.MISSING,
        threshold_seconds=threshold_seconds,
        elapsed_seconds=None,
        last_seen=None,
        extra={
            "value_at": None,
            "note": "watcher liveness signal pending substrate ack",
        },
    )


# ---------------------------------------------------------------------------
# Holder
# ---------------------------------------------------------------------------


def _read_probe_cadence_from_env(default_seconds: int) -> int:
    """Read ``KORA_HEALTH_PROBE_CADENCE_SECONDS`` if set; else default.

    Invalid values (non-int, ≤ 0) log WARN and fall back to default.
    """
    raw = os.environ.get(ENV_PROBE_CADENCE_SECONDS)
    if not raw:
        return default_seconds
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "[health_rollup] %s=%r not an int; using default %ds",
            ENV_PROBE_CADENCE_SECONDS,
            raw,
            default_seconds,
        )
        return default_seconds
    if value <= 0:
        logger.warning(
            "[health_rollup] %s=%d must be > 0; using default %ds",
            ENV_PROBE_CADENCE_SECONDS,
            value,
            default_seconds,
        )
        return default_seconds
    return value


class HealthRollupHolder:
    """Singleton aggregating R4.1 §9.7 health subsignals into a roll-up.

    Stateless apart from the operator-tuned probe cadence — every
    :meth:`current` call collects all 8 subsignals fresh from the
    live dependencies. There is no caching layer; the underlying
    holders / clients already hold the source-of-truth state.

    Construct via :func:`init_health_rollup_holder`; access via
    :func:`get_health_rollup_holder`.
    """

    def __init__(
        self,
        *,
        probe_cadence_seconds: Optional[int] = None,
    ) -> None:
        self._probe_cadence_seconds = (
            probe_cadence_seconds
            if probe_cadence_seconds is not None
            else _read_probe_cadence_from_env(DEFAULT_PROBE_CADENCE_SECONDS)
        )

    @property
    def probe_cadence_seconds(self) -> int:
        return self._probe_cadence_seconds

    def current(self) -> HealthRollup:
        """Collect all 8 subsignals fresh + derive overall/plane.

        Uses :mod:`agent.health_rollup_derivation` to map subsignals
        onto the three top-level enums:

          - ``overall`` — full rollup including operational primary
            state (STOPPED short-circuits to ``stopped``)
          - ``control_plane`` — dispatch + auth + escalation watcher
          - ``worker`` — claim + heartbeat + credit_burn + breaker +
            last_write
        """
        now = _now()
        subsignals = self._collect_all_subsignals(now=now)

        primary_state, stopped_trigger = self._snapshot_operational_state()

        from agent.health_rollup_derivation import (
            derive_control_plane_health,
            derive_overall_status,
            derive_worker_health,
        )

        overall, stopped_reason = derive_overall_status(
            subsignals,
            primary_state=primary_state,
            stopped_trigger=stopped_trigger,
        )
        return HealthRollup(
            overall=overall,
            control_plane=derive_control_plane_health(subsignals),
            worker=derive_worker_health(subsignals),
            stopped_reason=stopped_reason,
            subsignals=subsignals,
            collected_at=now,
        )

    @staticmethod
    def _snapshot_operational_state() -> tuple[Any, Optional[str]]:
        """Return ``(PrimaryState, stopped_trigger)`` or ``(None, None)``.

        Fail-soft: if the operational holder isn't initialized
        (early-boot windows / agent-session-only contexts), returns
        ``(None, None)`` so the derivation falls back to subsignal-
        only rollup.
        """
        try:
            from agent.operational_state_holder import get_holder

            holder = get_holder()
        except Exception:
            logger.debug(
                "[health_rollup] get_holder import raised", exc_info=True
            )
            return None, None
        if holder is None:
            return None, None
        try:
            primary_state = holder.current.primary_state
        except Exception:
            logger.debug(
                "[health_rollup] holder.current raised", exc_info=True
            )
            return None, None
        # ``stopped_trigger``: read the most recent transition trigger
        # from the holder's history ring. Fail-soft if history isn't
        # available.
        stopped_trigger: Optional[str] = None
        try:
            history = holder.history(limit=1)
            if history:
                stopped_trigger = history[-1].get("trigger")
        except Exception:
            logger.debug(
                "[health_rollup] holder.history raised", exc_info=True
            )
        return primary_state, stopped_trigger

    def _collect_all_subsignals(
        self, *, now: datetime
    ) -> dict[str, Subsignal]:
        """Wire each collector against the live singletons.

        Each collector handles missing deps gracefully (status=missing)
        so a partially-booted gateway still produces a complete
        rollup shape.
        """
        # Cost-ladder deps
        try:
            from agent.cost_state_holder import get_cost_holder

            cost_holder = get_cost_holder()
        except Exception:
            logger.debug(
                "[health_rollup] get_cost_holder import raised", exc_info=True
            )
            cost_holder = None

        # MCP-client deps (via the active provider)
        mcp_client = self._get_active_mcp_client()
        last_invoke_at = (
            getattr(mcp_client, "last_invoke_at", None) if mcp_client else None
        )
        last_write_at = (
            getattr(mcp_client, "last_successful_append_event_at", None)
            if mcp_client
            else None
        )
        last_heartbeat_at = (
            getattr(mcp_client, "last_successful_refresh_claim_at", None)
            if mcp_client
            else None
        )

        # Poller deps
        current_claim = self._get_current_claim()

        # OAuth credentials
        credentials = self._read_credentials_fail_soft()

        return {
            SUBSIGNAL_LAST_SUCCESSFUL_WRITE: collect_last_successful_write(
                last_write_at=last_write_at, now=now
            ),
            SUBSIGNAL_CLAIM_STATE: collect_claim_state(current_claim=current_claim),
            SUBSIGNAL_CREDIT_BURN: collect_credit_burn(cost_holder=cost_holder),
            SUBSIGNAL_BREAKER_STATE: collect_breaker_state(cost_holder=cost_holder),
            SUBSIGNAL_AUTH_VALIDITY_WINDOW: collect_auth_validity_window(
                credentials=credentials, now=now
            ),
            SUBSIGNAL_DISPATCH_REACHABLE: collect_dispatch_reachable(
                last_invoke_at=last_invoke_at, now=now
            ),
            SUBSIGNAL_LAST_HEARTBEAT: collect_last_heartbeat(
                last_heartbeat_at=last_heartbeat_at, now=now
            ),
            SUBSIGNAL_ESCALATION_WATCHER_LIVENESS: (
                collect_escalation_watcher_liveness(now=now)
            ),
        }

    def _get_active_mcp_client(self) -> Optional[Any]:
        """Return the gateway-level MCP client via the active provider."""
        try:
            from plugins.memory.isokron.active_provider import (
                get_active_provider,
            )

            provider = get_active_provider()
        except Exception:
            logger.debug(
                "[health_rollup] get_active_provider raised", exc_info=True
            )
            return None
        if provider is None:
            return None
        connection = getattr(provider, "_connection", None)
        if connection is None:
            return None
        get_mcp = getattr(connection, "get_mcp_client", None)
        if not callable(get_mcp):
            return None
        try:
            return get_mcp()
        except Exception:
            logger.debug(
                "[health_rollup] connection.get_mcp_client raised",
                exc_info=True,
            )
            return None

    def _get_current_claim(self) -> Optional[Any]:
        """Return the active poller's ``current_claim`` (or None)."""
        try:
            from plugins.memory.isokron.active_poller import get_active_poller

            poller = get_active_poller()
        except Exception:
            logger.debug(
                "[health_rollup] get_active_poller raised", exc_info=True
            )
            return None
        if poller is None:
            return None
        return getattr(poller, "current_claim", None)

    def _read_credentials_fail_soft(self) -> Optional[dict[str, Any]]:
        """Return Claude Code OAuth credentials dict or None.

        Wraps :func:`agent.anthropic_adapter.read_claude_code_credentials`
        — heavy import is lazy so unit tests don't pay for it.
        """
        try:
            from agent.anthropic_adapter import read_claude_code_credentials

            return read_claude_code_credentials()
        except Exception:
            logger.debug(
                "[health_rollup] read_claude_code_credentials raised",
                exc_info=True,
            )
            return None

# ---------------------------------------------------------------------------
# Singleton + accessors
# ---------------------------------------------------------------------------


_HOLDER: Optional[HealthRollupHolder] = None


def init_health_rollup_holder(
    *,
    probe_cadence_seconds: Optional[int] = None,
) -> HealthRollupHolder:
    """Initialize the process-wide holder. Idempotent."""
    global _HOLDER
    if _HOLDER is None:
        _HOLDER = HealthRollupHolder(probe_cadence_seconds=probe_cadence_seconds)
    return _HOLDER


def get_health_rollup_holder() -> Optional[HealthRollupHolder]:
    return _HOLDER


def _reset_health_rollup_holder_for_tests() -> None:
    global _HOLDER
    _HOLDER = None


# ---------------------------------------------------------------------------
# Cockpit projection — maps HealthRollup to the JSON dict the existing
# HEALTH-PANEL stub returns. ST4 uses this verbatim.
# ---------------------------------------------------------------------------


def rollup_to_panel_payload(rollup: HealthRollup) -> dict[str, Any]:
    """Project a :class:`HealthRollup` to the panel JSON shape.

    The shape is pinned by the existing stub in
    ``kora_cli/web_server.py:get_health_rollup``. ST4 swaps the stub
    body for ``rollup_to_panel_payload(holder.current())``.
    """
    return {
        "overall": rollup.overall.value,
        "control_plane": rollup.control_plane.value,
        "worker": rollup.worker.value,
        "stopped_reason": rollup.stopped_reason,
        "subsignals": {
            name: _subsignal_to_dict(signal)
            for name, signal in rollup.subsignals.items()
        },
    }


def _subsignal_to_dict(signal: Subsignal) -> dict[str, Any]:
    body: dict[str, Any] = {"status": signal.status.value}
    if signal.threshold_seconds is not None:
        body["threshold_seconds"] = signal.threshold_seconds
    if signal.elapsed_seconds is not None:
        body["elapsed_seconds"] = signal.elapsed_seconds
    body.update(signal.extra)
    return body
