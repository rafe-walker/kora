"""R4.1 §9.6 cost ladder runtime — :class:`CostStateHolder` + rung
threshold logic + reconciliation (KR-P2-K ST1).

Tracks the $200/mo Anthropic Agent SDK credit pool burn-down (Max 20x
subscription per Joshua's billing memo 2026-05-21). The estimator
layers on :mod:`agent.usage_pricing` (existing infrastructure):

  - Per-call: :meth:`record_inference` accepts a
    :class:`~agent.usage_pricing.CanonicalUsage` snapshot + model
    identifiers; delegates to
    :func:`~agent.usage_pricing.estimate_usage_cost` for the
    multi-source pricing lookup (provider-cost-api,
    provider-generation-api, official-docs-snapshot, OpenRouter,
    user-override, custom-contract).

  - Per-rung: thresholds at 75% / 90% / 100% (operator-tunable
    constants below). :meth:`active_rung` returns the current
    :class:`CostRung` for ST3's downshift selector + ST4's
    hard-stop transition + the COST-PANEL admin UI.

  - Rate-limit pulse (secondary signal): :meth:`record_rate_limit_pulse`
    captures Anthropic SDK ``anthropic-ratelimit-*`` response
    headers. **Best-effort**: only the 2 direct-Anthropic dispatch
    sites in this codebase surface those headers; the OpenAI-compat
    path doesn't. ST2 wires the capture at both eligible sites.

  - Reconciliation: :meth:`reconcile_with_anthropic` compares the
    local estimator against operator-supplied Anthropic-console
    reported usage. On a tolerance breach where the console value
    exceeds the local estimate, bumps ``spent_to_date_usd`` up so
    the breaker trips against ground truth.

# Subscription / billing-mode nuance

``estimate_usage_cost`` returns ZERO for ``billing_mode == "subscription_included"``
— but that route is only taken for the **OpenAI Codex bundle**
(``provider == "openai-codex"``). The Anthropic Max 20x subscription
that funds the $200 pool routes to ``billing_mode == "official_docs_snapshot"``
(metered), so per-token costs accumulate correctly. Codex bundle
traffic doesn't burn the Anthropic $200 pool, so a zero-cost return
on that route is also correct for this holder.

# Concurrency posture

ST1 assumes single-threaded inference dispatch (the agent loop is
sequential). If multi-threaded inference becomes a thing,
:meth:`record_inference` + :meth:`record_rate_limit_pulse` need a
``threading.Lock`` around the ``replace`` calls. Not added pre-need.

# What this module deliberately does NOT do

  - Wire ``record_inference`` into the dispatch sites — ST2.
  - Downshift selection based on ``active_rung`` — ST3.
  - Hard-stop safe-release + PAUSED transition — ST4.
  - Monthly billing-period refresh + ramped resume — ST5.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Final, Optional

from agent.usage_pricing import CanonicalUsage, estimate_usage_cost

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Joshua-locked $200/mo Max 20x Agent SDK pool (Slack 2026-05-21).
# Operator may override via init_cost_holder(credit_pool_usd=…).
DEFAULT_CREDIT_POOL_USD: Final[float] = 200.00


# R4.1 §9.6 rung thresholds (fraction of credit pool). Operator-tunable
# in a future iteration; pinned constants for ST1.
WARN_75_THRESHOLD: Final[float] = 0.75
DOWNSHIFT_90_THRESHOLD: Final[float] = 0.90
HARD_STOP_100_THRESHOLD: Final[float] = 1.00


# Default tolerance for :meth:`reconcile_with_anthropic`. A 10% drift
# between local estimator + Anthropic-console value is the bar for
# "re-trip the breaker." Per R4.1 §9.6, tighter tolerance is safer
# but more expensive operationally; 10% balances both.
DEFAULT_RECONCILE_TOLERANCE_PCT: Final[float] = 0.10


# ---------------------------------------------------------------------------
# Value classes
# ---------------------------------------------------------------------------


class CostRung(Enum):
    """Active rung in the R4.1 §9.6 cost ladder.

    String values are stable wire format — COST-PANEL admin UI + the
    cost-downshift selector both consume them.

    Boundary semantics (``pct = spent_to_date_usd / credit_pool_usd``):

      - ``pct < 0.75`` → ``NORMAL``
      - ``0.75 ≤ pct < 0.90`` → ``WARN_75``
      - ``0.90 ≤ pct < 1.00`` → ``DOWNSHIFT_90``
      - ``pct ≥ 1.00`` → ``HARD_STOP_100``
    """

    NORMAL = "normal"
    WARN_75 = "warn_75"
    DOWNSHIFT_90 = "downshift_90"
    HARD_STOP_100 = "hard_stop_100"


@dataclass(frozen=True, slots=True)
class RateLimitAxis:
    """One axis (requests OR tokens) of the Anthropic SDK
    ``anthropic-ratelimit-{axis}-{limit,remaining,reset}`` headers."""

    limit: int
    remaining: int
    reset_at: datetime


@dataclass(frozen=True, slots=True)
class RateLimitPulse:
    """Snapshot of the request + token rate-limit axes from a direct-
    Anthropic SDK response. Captured best-effort (only the 2 native
    Anthropic dispatch sites surface these headers; the OpenAI-compat
    path doesn't)."""

    requests: RateLimitAxis
    tokens: RateLimitAxis
    captured_at: datetime


@dataclass(frozen=True, slots=True)
class CostState:
    """Snapshot of the cost-ladder state.

    Immutable; :class:`CostStateHolder` swaps a new instance per
    mutation via :func:`dataclasses.replace`.
    """

    credit_pool_usd: float
    spent_to_date_usd: float
    billing_period_start: datetime
    last_reconciled_at: Optional[datetime]
    last_reconciled_anthropic_usd: Optional[float]
    extra_usage_off: bool
    latest_rate_limit_pulse: Optional[RateLimitPulse] = field(default=None)


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """Result of :meth:`CostStateHolder.reconcile_with_anthropic`.

    ``breaker_re_tripped`` is ``True`` when the Anthropic console
    value exceeded the local estimate beyond ``tolerance_pct``, and
    the holder bumped ``spent_to_date_usd`` up to match. A bumped
    holder may immediately enter a higher rung — ST4's hard-stop
    listener fires on the next ``active_rung()`` check.
    """

    local_estimate_usd: float
    anthropic_reported_usd: float
    diff_usd: float
    diff_pct: float
    within_tolerance: bool
    breaker_re_tripped: bool


# ---------------------------------------------------------------------------
# Holder
# ---------------------------------------------------------------------------


class CostStateHolder:
    """Singleton tracking the cost-ladder :class:`CostState`.

    Per the spec, the holder is process-wide. Construct via
    :func:`init_cost_holder`; access via :func:`get_cost_holder`.
    Direct instantiation is reserved for tests.
    """

    def __init__(
        self,
        *,
        billing_period_start: datetime,
        credit_pool_usd: float = DEFAULT_CREDIT_POOL_USD,
        extra_usage_off: bool = True,
    ) -> None:
        if credit_pool_usd <= 0:
            raise ValueError(
                f"credit_pool_usd must be > 0; got {credit_pool_usd}"
            )
        if billing_period_start.tzinfo is None:
            raise ValueError(
                "billing_period_start must be timezone-aware "
                "(use datetime(..., tzinfo=timezone.utc))"
            )
        self._state: CostState = CostState(
            credit_pool_usd=credit_pool_usd,
            spent_to_date_usd=0.0,
            billing_period_start=billing_period_start,
            last_reconciled_at=None,
            last_reconciled_anthropic_usd=None,
            extra_usage_off=extra_usage_off,
            latest_rate_limit_pulse=None,
        )

    # -- Read surface --------------------------------------------------------

    @property
    def current(self) -> CostState:
        """Current state snapshot. Frozen value-class, safe to share."""
        return self._state

    def current_pct_used(self) -> float:
        """Fraction of the credit pool consumed (``spent / pool``).

        May exceed 1.0 if reconciliation revealed under-counting; the
        rung is still ``HARD_STOP_100`` in that case.
        """
        return self._state.spent_to_date_usd / self._state.credit_pool_usd

    def burn_rate_usd_per_day(self) -> float:
        """Average daily burn since billing-period start.

        Returns 0.0 in the first 24 hours of a period (no full day
        elapsed yet). For longer periods, computes
        ``spent_to_date_usd / days_elapsed``.
        """
        now = datetime.now(timezone.utc)
        elapsed = now - self._state.billing_period_start
        days = elapsed.total_seconds() / 86400.0
        if days < 1.0:
            return 0.0
        return self._state.spent_to_date_usd / days

    def projected_end_of_period_usd(self) -> float:
        """Linear projection: ``burn_rate * days_in_billing_period``.

        Useful for the COST-PANEL admin UI to surface "at current
        burn, we'll hit $X by month end." Returns 0.0 inside the
        first day of a period (when burn_rate isn't meaningful yet).
        """
        burn = self.burn_rate_usd_per_day()
        if burn == 0.0:
            return 0.0
        days_in_period = _days_in_billing_period(self._state.billing_period_start)
        return burn * days_in_period

    def active_rung(self) -> CostRung:
        """Map ``current_pct_used`` to a :class:`CostRung`."""
        pct = self.current_pct_used()
        if pct >= HARD_STOP_100_THRESHOLD:
            return CostRung.HARD_STOP_100
        if pct >= DOWNSHIFT_90_THRESHOLD:
            return CostRung.DOWNSHIFT_90
        if pct >= WARN_75_THRESHOLD:
            return CostRung.WARN_75
        return CostRung.NORMAL

    # -- Write surface -------------------------------------------------------

    def record_inference(
        self,
        canonical_usage: CanonicalUsage,
        *,
        model_name: str,
        provider: Optional[str] = None,
        base_url: Optional[str] = None,
        route: str = "unknown",
        escalated_to_opus: bool = False,
        escalation_reason: Optional[str] = None,
    ) -> None:
        """Per-call estimator update.

        Layers on :func:`agent.usage_pricing.estimate_usage_cost` so
        cache reads / cache writes / reasoning tokens / cross-provider
        pricing are all handled by the existing multi-source resolver.

        Fail-soft: any pricing-lookup miss (status ``"unknown"``) or
        subscription-included route returns silently without
        accumulating spend. The cost ladder accumulates only when the
        per-call cost is computable; absent data is logged at DEBUG
        for operator diagnostics but does NOT raise.

        Args:
            canonical_usage: Token counts (input + output + cache +
                reasoning) extracted from the SDK response. Caller
                builds this via
                :func:`agent.usage_pricing.normalize_usage` (ST2
                wire-in pattern).
            model_name: Model identifier as the SDK reported it
                (``response.model`` from Anthropic SDK or
                ``response.model`` from OpenAI-compat). Used to
                resolve the billing route.
            provider: Optional provider name override; matches
                :func:`agent.usage_pricing.resolve_billing_route` (e.g.
                ``"anthropic"``, ``"openrouter"``, ``"openai"``).
            base_url: Optional base URL override; used when the SDK
                is configured against a custom endpoint
                (provider-fronted Anthropic, etc.).
            route: KR-CHEAP-COST-TELEMETRY route label per
                ``kora_cli.telemetry.cost_telemetry`` taxonomy
                (``slack_dm``, ``email_inbound``, ``mcp_tool``,
                ``alert_investigation``, ``probe_investigation``,
                ``tool_loop_iteration``, ``scheduled_task``,
                ``email_outbound_compose``, or ``unknown``).
                Defaults to ``"unknown"`` so existing callers keep
                working unchanged; they bucket into the unknown
                route until tagged explicitly. Telemetry is a
                READ-side observer of pricing; it does NOT affect
                whether/how much is billed.
            escalated_to_opus: Per-call escalation signal — Lock R3-3
                tunable. When True, the telemetry counters increment
                ``escalation_count`` for this route in addition to
                the normal call+token counters.
            escalation_reason: Optional reason tag — used together
                with ``escalated_to_opus=True`` to populate the
                per-reason breakdown in cost telemetry. KR-CC3-
                CLEANUP follow-up to #189: today the post-call
                haiku_router escalator passes ``low_confidence_marker``
                / ``short_response_for_long_input``; pre-call paths
                (force_opus_env / opus_prefix / decision_language /
                tool_loop_iteration) pass their corresponding
                ``RoutingDecision.reason``. Free-form tag — telemetry
                buckets it under whatever string is supplied.
        """
        cost_result = estimate_usage_cost(
            model_name,
            canonical_usage,
            provider=provider,
            base_url=base_url,
        )

        # KR-CHEAP-COST-TELEMETRY: tag the per-route counters. This
        # is read-only telemetry — does NOT affect billing accumulation
        # below. Fail-soft import + record so an unwired test path or
        # an import-cycle scenario can't crash the inference handler.
        cost_estimate_for_telemetry: Optional[float]
        if cost_result.amount_usd is None:
            cost_estimate_for_telemetry = None
        else:
            try:
                cost_estimate_for_telemetry = float(cost_result.amount_usd)
            except (TypeError, ValueError):
                cost_estimate_for_telemetry = None
        try:
            from kora_cli.telemetry import get_telemetry

            get_telemetry().record_call(
                route=route,
                model=model_name,
                canonical_usage=canonical_usage,
                cost_estimate_usd=cost_estimate_for_telemetry,
                escalated_to_opus=escalated_to_opus,
                escalation_reason=escalation_reason,
            )
        except Exception as exc:
            logger.debug(
                "[kora.cost_ladder] telemetry record_call failed: %r — "
                "billing accumulation continues",
                exc,
            )

        if cost_result.amount_usd is None:
            logger.debug(
                "[kora.cost_ladder] no pricing for model=%s provider=%s "
                "(status=%s) — inference not counted",
                model_name,
                provider,
                cost_result.status,
            )
            return
        # status='included' returns amount_usd=ZERO for the OpenAI Codex
        # bundle (per resolve_billing_route's only subscription_included
        # branch). Anthropic Max 20x routes to 'official_docs_snapshot'
        # (metered), so per-token costs land correctly. The zero-add
        # is a no-op here.
        amount = float(cost_result.amount_usd)
        if amount == 0.0:
            return
        new_spent = self._state.spent_to_date_usd + amount
        self._state = replace(self._state, spent_to_date_usd=new_spent)

    def record_rate_limit_pulse(self, pulse: RateLimitPulse) -> None:
        """Capture the latest Anthropic SDK rate-limit-header snapshot.

        Secondary signal per R4.1 §9.6: catches rate-limiting
        independent of the credit-burn gauge. Best-effort — only the
        2 direct-Anthropic dispatch sites surface these headers in
        this codebase; the OpenAI-compat path doesn't see them. ST2
        wires the eligible sites.
        """
        self._state = replace(self._state, latest_rate_limit_pulse=pulse)

    async def reconcile_with_anthropic(
        self,
        anthropic_reported_usd: float,
        *,
        tolerance_pct: float = DEFAULT_RECONCILE_TOLERANCE_PCT,
    ) -> ReconciliationResult:
        """Compare local estimator against Anthropic-console reported
        usage; on a tolerance breach, bump ``spent_to_date_usd`` to
        match.

        Caller fetches ``anthropic_reported_usd`` out-of-band (the
        operator may pull it from the Anthropic console at a periodic
        cadence — Anthropic's admin API surface is operator-managed).
        This method does the local reconciliation logic only.

        Bump-up semantics: we only roll ``spent_to_date_usd`` FORWARD,
        never backward. If Anthropic reports lower usage than the
        local estimate (which would be unusual — our estimator can
        legitimately over-count cache-token billing nuances), we
        leave ``spent_to_date_usd`` at the higher local value as the
        conservative posture.
        """
        if anthropic_reported_usd < 0:
            raise ValueError(
                f"anthropic_reported_usd must be >= 0; got {anthropic_reported_usd}"
            )
        if tolerance_pct < 0 or tolerance_pct > 1.0:
            raise ValueError(
                f"tolerance_pct must be in [0, 1]; got {tolerance_pct}"
            )

        local = self._state.spent_to_date_usd
        diff_usd = anthropic_reported_usd - local
        # Use the larger of (local, anthropic) as the denominator for
        # the % calculation so we get a stable signal even when local
        # is near zero (first hour of a billing period).
        denom = max(local, anthropic_reported_usd, 0.01)
        diff_pct = abs(diff_usd) / denom
        within_tolerance = diff_pct <= tolerance_pct
        breaker_re_tripped = False

        now = datetime.now(timezone.utc)
        if not within_tolerance and diff_usd > 0:
            # Under-counted locally; bump to ground truth.
            self._state = replace(
                self._state,
                spent_to_date_usd=anthropic_reported_usd,
                last_reconciled_at=now,
                last_reconciled_anthropic_usd=anthropic_reported_usd,
            )
            breaker_re_tripped = True
            logger.warning(
                "[kora.cost_ladder] reconciliation re-tripped breaker: "
                "local=%.4f anthropic=%.4f diff_pct=%.2f%% (tol=%.2f%%). "
                "spent_to_date bumped to ground truth; ST4's hard-stop "
                "listener checks active_rung on next inference.",
                local,
                anthropic_reported_usd,
                diff_pct * 100,
                tolerance_pct * 100,
            )
        else:
            self._state = replace(
                self._state,
                last_reconciled_at=now,
                last_reconciled_anthropic_usd=anthropic_reported_usd,
            )

        return ReconciliationResult(
            local_estimate_usd=local,
            anthropic_reported_usd=anthropic_reported_usd,
            diff_usd=diff_usd,
            diff_pct=diff_pct,
            within_tolerance=within_tolerance,
            breaker_re_tripped=breaker_re_tripped,
        )

    def refresh_billing_period(self, new_period_start: datetime) -> None:
        """KR-P2-K ST5 — refresh at month boundary.

        Resets ``spent_to_date_usd`` to 0 and rolls
        ``billing_period_start`` to ``new_period_start``. Also clears
        the reconciliation state (``last_reconciled_at`` /
        ``last_reconciled_anthropic_usd``) so the first reconciliation
        of the new period sets fresh ground truth.

        Preserves ``latest_rate_limit_pulse`` (the Anthropic
        rate-limit window doesn't align with the billing period
        reset; the most recent pulse remains operator-informative)
        and ``credit_pool_usd`` / ``extra_usage_off`` (config).

        Args:
            new_period_start: Tz-aware start of the new billing period.
                Must be strictly forward of the current
                ``billing_period_start`` (refusing a same-period
                refresh prevents accidental loss of within-period
                spend).

        Raises:
            ValueError: if ``new_period_start`` is naive or not
                strictly forward of the current period start.

        Note: the caller is responsible for the cross-holder
        side-effect of clearing PAUSED{COST} on the operational-state
        holder and starting the SeaTicketPoller's ramped resume.
        See :mod:`agent.cost_ladder_refresh` for the coordinator.
        """
        if new_period_start.tzinfo is None:
            raise ValueError(
                "new_period_start must be timezone-aware "
                "(use datetime(..., tzinfo=timezone.utc))"
            )
        if new_period_start <= self._state.billing_period_start:
            raise ValueError(
                f"new_period_start ({new_period_start.isoformat()}) "
                f"must be strictly forward of current period start "
                f"({self._state.billing_period_start.isoformat()})"
            )
        logger.info(
            "[kora.cost_ladder] refreshing billing period: %s -> %s "
            "(spent_to_date was %.4f USD; resetting to 0)",
            self._state.billing_period_start.isoformat(),
            new_period_start.isoformat(),
            self._state.spent_to_date_usd,
        )
        self._state = replace(
            self._state,
            spent_to_date_usd=0.0,
            billing_period_start=new_period_start,
            last_reconciled_at=None,
            last_reconciled_anthropic_usd=None,
        )


# ---------------------------------------------------------------------------
# Singleton + accessors
# ---------------------------------------------------------------------------


_HOLDER: Optional[CostStateHolder] = None


def init_cost_holder(
    *,
    billing_period_start: datetime,
    credit_pool_usd: float = DEFAULT_CREDIT_POOL_USD,
    extra_usage_off: bool = True,
) -> CostStateHolder:
    """Initialize the process-wide holder. Idempotent: subsequent
    calls return the existing instance and ignore arguments.

    Typical first call sits in agent boot after the operational-state
    holder is initialized — the cost holder is independent of the
    operational state machine but ST4 will wire a transition listener
    that consumes ``active_rung()``.
    """
    global _HOLDER
    if _HOLDER is None:
        _HOLDER = CostStateHolder(
            billing_period_start=billing_period_start,
            credit_pool_usd=credit_pool_usd,
            extra_usage_off=extra_usage_off,
        )
    return _HOLDER


def get_cost_holder() -> Optional[CostStateHolder]:
    """Return the process-wide holder, or ``None`` if uninitialized.

    ST2's wire-in checks for ``None`` before calling ``record_inference``
    so an uninitialized holder doesn't cascade into the inference
    response handler.
    """
    return _HOLDER


def _reset_cost_holder_for_tests() -> None:
    """Test escape hatch — drop the singleton so each test starts fresh."""
    global _HOLDER
    _HOLDER = None


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _days_in_billing_period(period_start: datetime) -> int:
    """Number of days in the billing period beginning at ``period_start``.

    Assumes monthly periods aligned to the first of the month. Uses
    :func:`calendar.monthrange` for accurate (28/29/30/31)-day counts.
    """
    import calendar

    return calendar.monthrange(period_start.year, period_start.month)[1]
