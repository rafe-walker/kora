"""Per-route cost telemetry — KR-CHEAP-COST-TELEMETRY (R3-4 #10).

Tags every ``record_inference`` call with a route label; accumulates
per-route counters; surfaces for cockpit + tuning decisions.

Zero LLM cost on the accounting itself. The decision layer for any
future tuning (escalation-rate, classifier, route-shape) reads
from this telemetry to evaluate "is cheap-substrate work actually
saving what we expect?"

# Route taxonomy (v1 — spec §2)

| Route | When |
|---|---|
| ``slack_dm`` | DM-equivalent traffic the slack handler bills |
| ``email_inbound`` | Inbound email reasoning (reserved — handler doesn't yet write a bill) |
| ``email_outbound_compose`` | Reasoning invoked to draft an outbound email (reserved) |
| ``mcp_tool`` | An MCP-driven invocation reaches reasoning (reserved) |
| ``alert_investigation`` | Alert wakes Kora; investigation reasoning (Lock R3-8 (d); reserved) |
| ``probe_investigation`` | Probe escalates an issue; Kora investigates (Lock R3-8 (b); reserved) |
| ``tool_loop_iteration`` | Tool-use loop iteration 2+ (first iteration attributed to the parent route) |
| ``scheduled_task`` | Cron-fired scheduled task invokes reasoning (reserved) |
| ``unknown`` | route metadata absent / unrecognized (fail-soft; never raises) |

Routes accepted whether or not a current consumer exists. Reserving
the literal in the taxonomy lets a future bucket wire a new consumer
without touching this module.

# Concurrency

A single :class:`threading.RLock` protects all counter mutations +
snapshot reads. ``record_call`` is called from:

  * Daemon asyncio loop (handlers, reasoning, periodic tasks)
  * Cron-driven periodic snapshot task (same loop today)
  * Possibly future background threads

Lock-per-update is cheap; the alternative (lock-free dict
mutations under GIL) is technically safe for individual key
mutations but the snapshot/aggregation read is multi-key and a
race could surface an inconsistent picture. Lock is the
conservative choice.

# Windows

Three independent counter windows maintained:

  * ``process_lifetime`` — reset on process boot only
  * ``rolling_24h`` — reset at midnight UTC (cron-driven)
  * ``monthly`` — reset at month rollover (cron-driven)

The cost-ladder holder already tracks the monthly billing window
for budget-cap purposes; this telemetry's ``monthly`` window
aligns to the same UTC month boundary so operator can correlate
per-route burn to monthly burn cap.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical route taxonomy (spec §2)
# ---------------------------------------------------------------------------


ROUTE_SLACK_DM = "slack_dm"
ROUTE_EMAIL_INBOUND = "email_inbound"
ROUTE_EMAIL_OUTBOUND_COMPOSE = "email_outbound_compose"
ROUTE_MCP_TOOL = "mcp_tool"
ROUTE_ALERT_INVESTIGATION = "alert_investigation"
ROUTE_PROBE_INVESTIGATION = "probe_investigation"
ROUTE_TOOL_LOOP_ITERATION = "tool_loop_iteration"
ROUTE_SCHEDULED_TASK = "scheduled_task"
ROUTE_UNKNOWN = "unknown"

KNOWN_ROUTES = frozenset(
    {
        ROUTE_SLACK_DM,
        ROUTE_EMAIL_INBOUND,
        ROUTE_EMAIL_OUTBOUND_COMPOSE,
        ROUTE_MCP_TOOL,
        ROUTE_ALERT_INVESTIGATION,
        ROUTE_PROBE_INVESTIGATION,
        ROUTE_TOOL_LOOP_ITERATION,
        ROUTE_SCHEDULED_TASK,
        ROUTE_UNKNOWN,
    }
)


# Window names; document inline for snapshot / endpoint consumers.
WINDOW_PROCESS_LIFETIME = "process_lifetime"
WINDOW_ROLLING_24H = "rolling_24h"
WINDOW_MONTHLY = "monthly"

KNOWN_WINDOWS = (WINDOW_PROCESS_LIFETIME, WINDOW_ROLLING_24H, WINDOW_MONTHLY)


# ---------------------------------------------------------------------------
# Counter shape
# ---------------------------------------------------------------------------


@dataclass
class _RouteCounters:
    """Mutable counters for one (window, route) pair.

    Not frozen — mutated in-place under the singleton's lock. The
    ``snapshot()`` method produces a JSON-serializable dict copy
    safe to hand out to readers.
    """

    calls_count: int = 0
    input_tokens_total: int = 0
    output_tokens_total: int = 0
    cache_read_tokens_total: int = 0
    cache_creation_tokens_total: int = 0
    cost_estimate_usd_total: float = 0.0
    escalation_count: int = 0
    model_breakdown: Dict[str, int] = field(default_factory=dict)
    # KR-CC3-CLEANUP — escalation reason breakdown. Increments
    # only when ``escalated_to_opus=True`` AND a reason was
    # supplied. Reasons are free-form strings sourced by the
    # caller (today: ``low_confidence_marker`` /
    # ``short_response_for_long_input`` from the post-call
    # haiku_router escalator). Lets cockpit panels show "X% of
    # escalations were low-confidence-marker, Y% were short-
    # response-heuristic, Z% were untagged".
    escalation_reason_breakdown: Dict[str, int] = field(
        default_factory=dict
    )

    def add(
        self,
        *,
        canonical_usage: Any,
        cost_estimate_usd: Optional[float],
        model: str,
        escalated_to_opus: bool,
        escalation_reason: Optional[str] = None,
    ) -> None:
        """Increment counters from one call's worth of usage."""
        self.calls_count += 1
        # ``canonical_usage`` is a duck-typed CanonicalUsage — read
        # the 4 token-count fields defensively (each may be missing
        # on a future shape variant; tolerate via getattr with 0
        # default). The pricing module's canonical struct uses
        # ``cache_write_tokens`` for the per-call cost write; we
        # surface it under ``cache_creation_tokens_total`` per the
        # spec §2 counter naming.
        self.input_tokens_total += int(
            getattr(canonical_usage, "input_tokens", 0) or 0
        )
        self.output_tokens_total += int(
            getattr(canonical_usage, "output_tokens", 0) or 0
        )
        self.cache_read_tokens_total += int(
            getattr(canonical_usage, "cache_read_tokens", 0) or 0
        )
        self.cache_creation_tokens_total += int(
            getattr(canonical_usage, "cache_write_tokens", 0) or 0
        )
        if cost_estimate_usd is not None:
            try:
                self.cost_estimate_usd_total += float(cost_estimate_usd)
            except (TypeError, ValueError):
                pass
        if escalated_to_opus:
            self.escalation_count += 1
            # Reason breakdown bumps only when both signal and
            # reason are present — untagged escalations (legacy
            # callers) just contribute to escalation_count.
            if escalation_reason:
                self.escalation_reason_breakdown[escalation_reason] = (
                    self.escalation_reason_breakdown.get(
                        escalation_reason, 0
                    )
                    + 1
                )
        if model:
            self.model_breakdown[model] = (
                self.model_breakdown.get(model, 0) + 1
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "calls_count": self.calls_count,
            "input_tokens_total": self.input_tokens_total,
            "output_tokens_total": self.output_tokens_total,
            "cache_read_tokens_total": self.cache_read_tokens_total,
            "cache_creation_tokens_total": self.cache_creation_tokens_total,
            "cost_estimate_usd_total": round(
                self.cost_estimate_usd_total, 6
            ),
            "escalation_count": self.escalation_count,
            "model_breakdown": dict(self.model_breakdown),
            "escalation_reason_breakdown": dict(
                self.escalation_reason_breakdown
            ),
        }


def _empty_window() -> Dict[str, _RouteCounters]:
    """Build a fresh per-route counter dict for one window.

    Pre-populated with every known route so the snapshot shape is
    stable from process boot — consumers don't have to handle
    "route absent" vs "route at zero" distinctly.
    """
    return {route: _RouteCounters() for route in KNOWN_ROUTES}


# ---------------------------------------------------------------------------
# CostRouteTelemetry — singleton accumulator
# ---------------------------------------------------------------------------


class CostRouteTelemetry:
    """Per-route counter accumulator. Singleton via :func:`get_telemetry`.

    Counters reset at process boot (``process_lifetime`` window).
    Persistent windowed counters (``rolling_24h``, ``monthly``) are
    written to disk every 5 min by the snapshot listener; window-
    reset periodic tasks clear them at the appropriate boundaries.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._windows: Dict[str, Dict[str, _RouteCounters]] = {
            window: _empty_window() for window in KNOWN_WINDOWS
        }

    def record_call(
        self,
        *,
        route: str,
        model: str,
        canonical_usage: Any,
        cost_estimate_usd: Optional[float],
        escalated_to_opus: bool = False,
        escalation_reason: Optional[str] = None,
    ) -> None:
        """Increment counters for this route across all live windows.

        Fail-soft: any exception inside is caught + logged so the
        caller (a hot-path inference completion handler) never sees
        a telemetry failure. Unknown routes silently bucket to
        ``"unknown"`` rather than raising.

        ``escalation_reason`` (optional) is a free-form tag — when
        present alongside ``escalated_to_opus=True`` it increments
        the per-reason breakdown counter so cockpit panels can
        differentiate "low-confidence Haiku" escalations from
        "short-response-heuristic" or future variants.
        """
        try:
            self._record_call_inner(
                route=route,
                model=model or "",
                canonical_usage=canonical_usage,
                cost_estimate_usd=cost_estimate_usd,
                escalated_to_opus=bool(escalated_to_opus),
                escalation_reason=(
                    escalation_reason
                    if isinstance(escalation_reason, str) and escalation_reason
                    else None
                ),
            )
        except Exception as exc:
            logger.warning(
                "[kora.cost_telemetry] record_call raised %r — counters "
                "not updated for route=%s",
                exc,
                route,
            )

    def _record_call_inner(
        self,
        *,
        route: str,
        model: str,
        canonical_usage: Any,
        cost_estimate_usd: Optional[float],
        escalated_to_opus: bool,
        escalation_reason: Optional[str] = None,
    ) -> None:
        normalized_route = (
            route
            if isinstance(route, str) and route in KNOWN_ROUTES
            else ROUTE_UNKNOWN
        )
        with self._lock:
            for window in KNOWN_WINDOWS:
                self._windows[window][normalized_route].add(
                    canonical_usage=canonical_usage,
                    cost_estimate_usd=cost_estimate_usd,
                    model=model,
                    escalated_to_opus=escalated_to_opus,
                    escalation_reason=escalation_reason,
                )

    def snapshot(self) -> Dict[str, Any]:
        """Snapshot the current counters for cockpit / on-disk write.

        Returns a JSON-serializable dict shaped:

        .. code-block:: python

            {
                "process_lifetime": {<route>: {...}, ...},
                "rolling_24h":      {<route>: {...}, ...},
                "monthly":          {<route>: {...}, ...},
            }

        Each per-route dict matches :meth:`_RouteCounters.to_dict`.
        Snapshot read happens under the lock so a concurrent
        ``record_call`` can't surface a half-updated counter.
        """
        with self._lock:
            return {
                window: {
                    route: counters.to_dict()
                    for route, counters in routes.items()
                }
                for window, routes in self._windows.items()
            }

    def reset_window(self, window: str) -> None:
        """Reset one named window's counters to zero.

        Used by the rolling-24h and monthly reset periodic tasks
        when the respective boundary rolls. The
        ``process_lifetime`` window MAY be reset via this surface
        (test fixtures use it) but production code shouldn't.
        """
        if window not in KNOWN_WINDOWS:
            logger.warning(
                "[kora.cost_telemetry] reset_window: unknown window=%r "
                "(known: %s) — ignoring",
                window,
                KNOWN_WINDOWS,
            )
            return
        with self._lock:
            self._windows[window] = _empty_window()
        logger.info(
            "[kora.cost_telemetry] window=%s counters reset", window
        )

    def reset_all_for_tests(self) -> None:
        """Test-only: reset every window to zero. Production code
        uses :meth:`reset_window`."""
        with self._lock:
            for window in KNOWN_WINDOWS:
                self._windows[window] = _empty_window()


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


_telemetry_singleton: Optional[CostRouteTelemetry] = None
_singleton_lock = threading.Lock()


def get_telemetry() -> CostRouteTelemetry:
    """Process-global accessor for :class:`CostRouteTelemetry`.

    Lazily constructs the singleton on first call. Thread-safe
    construction via double-checked locking. Subsequent calls
    return the same instance.
    """
    global _telemetry_singleton
    if _telemetry_singleton is not None:
        return _telemetry_singleton
    with _singleton_lock:
        if _telemetry_singleton is None:
            _telemetry_singleton = CostRouteTelemetry()
        return _telemetry_singleton


def _reset_singleton_for_tests() -> None:
    """Drop the singleton so the next ``get_telemetry()`` call returns
    a fresh instance. Used by test fixtures to isolate state.

    Production code MUST NOT call this — it would zero all live
    counters mid-process.
    """
    global _telemetry_singleton
    with _singleton_lock:
        _telemetry_singleton = None
