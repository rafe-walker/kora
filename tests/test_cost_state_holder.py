"""Unit tests for ``agent/cost_state_holder.py`` (KR-P2-K ST1).

Covers:
  - Constructor validation (credit_pool > 0, tz-aware billing_period_start)
  - CostRung threshold boundaries (NORMAL / WARN_75 / DOWNSHIFT_90 /
    HARD_STOP_100), including exact boundary values
  - ``record_inference`` accumulates spend; layers on
    ``estimate_usage_cost`` (with monkeypatched pricing)
  - ``record_inference`` no-op when pricing unknown / subscription-included
  - ``record_rate_limit_pulse`` captures the latest snapshot
  - ``burn_rate_usd_per_day`` returns 0 in first 24h, then spent/days
  - ``projected_end_of_period_usd`` extrapolates burn rate × month days
  - ``reconcile_with_anthropic`` happy / within-tolerance / breaker re-trip
  - ``reconcile_with_anthropic`` never rolls spent_to_date BACKWARD
  - Singleton accessor: init / get / reset
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from agent.cost_state_holder import (
    DEFAULT_CREDIT_POOL_USD,
    DEFAULT_RECONCILE_TOLERANCE_PCT,
    DOWNSHIFT_90_THRESHOLD,
    HARD_STOP_100_THRESHOLD,
    WARN_75_THRESHOLD,
    CostRung,
    CostState,
    CostStateHolder,
    RateLimitAxis,
    RateLimitPulse,
    ReconciliationResult,
    _reset_cost_holder_for_tests,
    get_cost_holder,
    init_cost_holder,
)
from agent.usage_pricing import CanonicalUsage, CostResult


@pytest.fixture(autouse=True)
def _reset_singleton():
    _reset_cost_holder_for_tests()
    yield
    _reset_cost_holder_for_tests()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _period_start(year: int = 2026, month: int = 5, day: int = 1) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _make_holder(
    *,
    credit_pool_usd: float = DEFAULT_CREDIT_POOL_USD,
    period_start: datetime | None = None,
) -> CostStateHolder:
    return CostStateHolder(
        billing_period_start=period_start or _period_start(),
        credit_pool_usd=credit_pool_usd,
    )


def _patch_cost(*, amount_usd: float | None, status: str = "estimated"):
    """Patch ``estimate_usage_cost`` to return a canned amount."""
    return patch(
        "agent.cost_state_holder.estimate_usage_cost",
        return_value=CostResult(
            amount_usd=Decimal(str(amount_usd)) if amount_usd is not None else None,
            status=status,  # type: ignore[arg-type]
            source="official_docs_snapshot",  # type: ignore[arg-type]
            label="test",
        ),
    )


def _usage(input_tokens: int = 100, output_tokens: int = 50) -> CanonicalUsage:
    return CanonicalUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


def test_rejects_non_positive_credit_pool():
    with pytest.raises(ValueError, match="credit_pool_usd must be > 0"):
        CostStateHolder(
            billing_period_start=_period_start(),
            credit_pool_usd=0,
        )
    with pytest.raises(ValueError, match="credit_pool_usd must be > 0"):
        CostStateHolder(
            billing_period_start=_period_start(),
            credit_pool_usd=-100,
        )


def test_rejects_naive_billing_period_start():
    naive = datetime(2026, 5, 1)  # no tzinfo
    with pytest.raises(ValueError, match="timezone-aware"):
        CostStateHolder(billing_period_start=naive)


def test_default_credit_pool_is_joshua_locked_200():
    """Sanity guard — the Max 20x pool size is load-bearing for the
    bucket's headline number."""
    assert DEFAULT_CREDIT_POOL_USD == 200.00


def test_initial_state_has_zero_spent_and_no_reconciliation():
    holder = _make_holder()
    state = holder.current
    assert state.spent_to_date_usd == 0.0
    assert state.credit_pool_usd == DEFAULT_CREDIT_POOL_USD
    assert state.last_reconciled_at is None
    assert state.last_reconciled_anthropic_usd is None
    assert state.extra_usage_off is True
    assert state.latest_rate_limit_pulse is None


# ---------------------------------------------------------------------------
# CostRung threshold boundaries
# ---------------------------------------------------------------------------


def test_active_rung_normal_below_75pct():
    holder = _make_holder(credit_pool_usd=100.0)
    with _patch_cost(amount_usd=70.0):
        holder.record_inference(_usage(), model_name="claude-sonnet-4.7")
    assert holder.active_rung() is CostRung.NORMAL


def test_active_rung_warn_75_at_exact_threshold():
    """75% exactly crosses into WARN_75 (boundary is inclusive)."""
    holder = _make_holder(credit_pool_usd=100.0)
    with _patch_cost(amount_usd=75.0):
        holder.record_inference(_usage(), model_name="claude-sonnet-4.7")
    assert holder.active_rung() is CostRung.WARN_75


def test_active_rung_downshift_90_at_exact_threshold():
    holder = _make_holder(credit_pool_usd=100.0)
    with _patch_cost(amount_usd=90.0):
        holder.record_inference(_usage(), model_name="claude-opus-4.7")
    assert holder.active_rung() is CostRung.DOWNSHIFT_90


def test_active_rung_hard_stop_100_at_exact_threshold():
    holder = _make_holder(credit_pool_usd=100.0)
    with _patch_cost(amount_usd=100.0):
        holder.record_inference(_usage(), model_name="claude-opus-4.7")
    assert holder.active_rung() is CostRung.HARD_STOP_100


def test_active_rung_hard_stop_100_when_spent_exceeds_pool():
    """Reconciliation can push spent > pool; rung stays HARD_STOP_100."""
    holder = _make_holder(credit_pool_usd=100.0)
    with _patch_cost(amount_usd=200.0):
        holder.record_inference(_usage(), model_name="claude-opus-4.7")
    assert holder.current_pct_used() == 2.0
    assert holder.active_rung() is CostRung.HARD_STOP_100


def test_thresholds_are_load_bearing_constants():
    """Pin the threshold values — drift would silently change rung
    semantics across the cost-ladder + COST-PANEL surfaces."""
    assert WARN_75_THRESHOLD == 0.75
    assert DOWNSHIFT_90_THRESHOLD == 0.90
    assert HARD_STOP_100_THRESHOLD == 1.00


# ---------------------------------------------------------------------------
# record_inference
# ---------------------------------------------------------------------------


def test_record_inference_accumulates_spend():
    holder = _make_holder()
    with _patch_cost(amount_usd=1.50):
        holder.record_inference(_usage(), model_name="claude-haiku-4.5")
    assert holder.current.spent_to_date_usd == 1.50
    with _patch_cost(amount_usd=3.25):
        holder.record_inference(_usage(), model_name="claude-haiku-4.5")
    assert holder.current.spent_to_date_usd == pytest.approx(4.75)


def test_record_inference_skips_unknown_pricing():
    holder = _make_holder()
    with _patch_cost(amount_usd=None, status="unknown"):
        holder.record_inference(_usage(), model_name="some-mystery-model")
    assert holder.current.spent_to_date_usd == 0.0


def test_record_inference_skips_subscription_included_zero():
    """Codex bundle returns amount=0 status=included — don't accumulate."""
    holder = _make_holder()
    with _patch_cost(amount_usd=0.0, status="included"):
        holder.record_inference(_usage(), model_name="codex-bundle-model")
    assert holder.current.spent_to_date_usd == 0.0


def test_record_inference_passes_through_provider_and_base_url():
    """ST2 wire-in needs provider + base_url forwarded to
    ``estimate_usage_cost`` so multi-provider routing works."""
    holder = _make_holder()
    with patch("agent.cost_state_holder.estimate_usage_cost") as mock_cost:
        mock_cost.return_value = CostResult(
            amount_usd=Decimal("0.50"),
            status="estimated",  # type: ignore[arg-type]
            source="official_docs_snapshot",  # type: ignore[arg-type]
            label="test",
        )
        holder.record_inference(
            _usage(),
            model_name="claude-opus-4.7",
            provider="anthropic",
            base_url="https://api.anthropic.com",
        )
    mock_cost.assert_called_once()
    args, kwargs = mock_cost.call_args
    assert args[0] == "claude-opus-4.7"
    assert kwargs["provider"] == "anthropic"
    assert kwargs["base_url"] == "https://api.anthropic.com"


# ---------------------------------------------------------------------------
# record_rate_limit_pulse
# ---------------------------------------------------------------------------


def test_record_rate_limit_pulse_captures_latest():
    holder = _make_holder()
    now = datetime.now(timezone.utc)
    pulse = RateLimitPulse(
        requests=RateLimitAxis(limit=1000, remaining=800, reset_at=now),
        tokens=RateLimitAxis(limit=10_000_000, remaining=7_000_000, reset_at=now),
        captured_at=now,
    )
    holder.record_rate_limit_pulse(pulse)
    assert holder.current.latest_rate_limit_pulse is pulse


def test_record_rate_limit_pulse_overwrites_previous():
    holder = _make_holder()
    now = datetime.now(timezone.utc)
    older = RateLimitPulse(
        requests=RateLimitAxis(limit=1000, remaining=999, reset_at=now),
        tokens=RateLimitAxis(limit=10, remaining=9, reset_at=now),
        captured_at=now,
    )
    newer = RateLimitPulse(
        requests=RateLimitAxis(limit=1000, remaining=500, reset_at=now),
        tokens=RateLimitAxis(limit=10, remaining=5, reset_at=now),
        captured_at=now,
    )
    holder.record_rate_limit_pulse(older)
    holder.record_rate_limit_pulse(newer)
    assert holder.current.latest_rate_limit_pulse is newer


# ---------------------------------------------------------------------------
# burn_rate + projected_end_of_period
# ---------------------------------------------------------------------------


def test_burn_rate_returns_zero_within_first_day():
    """First 24h doesn't have a meaningful days-elapsed denominator."""
    period_start = datetime.now(timezone.utc) - timedelta(hours=12)
    holder = CostStateHolder(billing_period_start=period_start)
    with _patch_cost(amount_usd=5.0):
        holder.record_inference(_usage(), model_name="claude-haiku-4.5")
    assert holder.burn_rate_usd_per_day() == 0.0


def test_burn_rate_computes_spent_per_day_after_first_day():
    period_start = datetime.now(timezone.utc) - timedelta(days=5)
    holder = CostStateHolder(billing_period_start=period_start)
    with _patch_cost(amount_usd=50.0):
        holder.record_inference(_usage(), model_name="claude-opus-4.7")
    # 50 / 5 = 10 USD/day (approx — small drift acceptable)
    assert holder.burn_rate_usd_per_day() == pytest.approx(10.0, rel=0.01)


def test_projected_end_of_period_extrapolates_burn_to_month_end():
    period_start = datetime.now(timezone.utc) - timedelta(days=5)
    holder = CostStateHolder(billing_period_start=period_start)
    with _patch_cost(amount_usd=50.0):
        holder.record_inference(_usage(), model_name="claude-opus-4.7")
    # burn = $10/day; month length varies (28-31); projection in that range
    projected = holder.projected_end_of_period_usd()
    assert 28 * 10 <= projected <= 31 * 10 + 1


def test_projected_end_of_period_returns_zero_in_first_day():
    period_start = datetime.now(timezone.utc) - timedelta(hours=3)
    holder = CostStateHolder(billing_period_start=period_start)
    with _patch_cost(amount_usd=5.0):
        holder.record_inference(_usage(), model_name="claude-haiku-4.5")
    assert holder.projected_end_of_period_usd() == 0.0


# ---------------------------------------------------------------------------
# reconcile_with_anthropic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_within_tolerance_does_not_re_trip():
    holder = _make_holder(credit_pool_usd=100.0)
    with _patch_cost(amount_usd=50.0):
        holder.record_inference(_usage(), model_name="claude-opus-4.7")
    result = await holder.reconcile_with_anthropic(52.0)  # 4% drift
    assert result.within_tolerance is True
    assert result.breaker_re_tripped is False
    assert holder.current.spent_to_date_usd == 50.0  # unchanged
    assert holder.current.last_reconciled_at is not None
    assert holder.current.last_reconciled_anthropic_usd == 52.0


@pytest.mark.asyncio
async def test_reconcile_under_counted_bumps_to_anthropic():
    """Anthropic > local + outside tolerance → spent_to_date bumps up."""
    holder = _make_holder(credit_pool_usd=100.0)
    with _patch_cost(amount_usd=50.0):
        holder.record_inference(_usage(), model_name="claude-opus-4.7")
    result = await holder.reconcile_with_anthropic(80.0, tolerance_pct=0.10)
    assert result.within_tolerance is False
    assert result.breaker_re_tripped is True
    assert holder.current.spent_to_date_usd == 80.0
    # Rung advanced from NORMAL (50%) to WARN_75 (80%)
    assert holder.active_rung() is CostRung.WARN_75


@pytest.mark.asyncio
async def test_reconcile_over_counted_does_not_roll_backward():
    """Anthropic < local: conservative posture — don't roll back."""
    holder = _make_holder(credit_pool_usd=100.0)
    with _patch_cost(amount_usd=80.0):
        holder.record_inference(_usage(), model_name="claude-opus-4.7")
    result = await holder.reconcile_with_anthropic(30.0, tolerance_pct=0.10)
    assert result.within_tolerance is False
    assert result.breaker_re_tripped is False
    # spent_to_date unchanged — conservative
    assert holder.current.spent_to_date_usd == 80.0


@pytest.mark.asyncio
async def test_reconcile_rejects_negative_anthropic_usd():
    holder = _make_holder()
    with pytest.raises(ValueError, match=">= 0"):
        await holder.reconcile_with_anthropic(-1.0)


@pytest.mark.asyncio
async def test_reconcile_rejects_invalid_tolerance():
    holder = _make_holder()
    with pytest.raises(ValueError, match="tolerance_pct"):
        await holder.reconcile_with_anthropic(50.0, tolerance_pct=-0.1)
    with pytest.raises(ValueError, match="tolerance_pct"):
        await holder.reconcile_with_anthropic(50.0, tolerance_pct=1.5)


@pytest.mark.asyncio
async def test_reconcile_default_tolerance_is_10pct():
    """Pin the default tolerance constant — operator-visible knob."""
    assert DEFAULT_RECONCILE_TOLERANCE_PCT == 0.10


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


def test_init_cost_holder_is_idempotent():
    h1 = init_cost_holder(billing_period_start=_period_start())
    h2 = init_cost_holder(
        billing_period_start=_period_start(year=2030),  # ignored
        credit_pool_usd=999.99,  # ignored
    )
    assert h1 is h2


def test_get_cost_holder_returns_none_before_init():
    assert get_cost_holder() is None


def test_get_cost_holder_returns_initialized_holder():
    init_cost_holder(billing_period_start=_period_start())
    holder = get_cost_holder()
    assert holder is not None
    assert isinstance(holder, CostStateHolder)


def test_reset_cost_holder_for_tests_drops_singleton():
    init_cost_holder(billing_period_start=_period_start())
    assert get_cost_holder() is not None
    _reset_cost_holder_for_tests()
    assert get_cost_holder() is None


# ---------------------------------------------------------------------------
# Value class shape
# ---------------------------------------------------------------------------


def test_cost_rung_enum_string_values():
    """Pinned wire format — COST-PANEL UI + cost_downshift consume these."""
    assert {m.value for m in CostRung} == {
        "normal",
        "warn_75",
        "downshift_90",
        "hard_stop_100",
    }


def test_cost_state_is_frozen():
    import dataclasses

    state = CostState(
        credit_pool_usd=200.0,
        spent_to_date_usd=0.0,
        billing_period_start=_period_start(),
        last_reconciled_at=None,
        last_reconciled_anthropic_usd=None,
        extra_usage_off=True,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.spent_to_date_usd = 1.0  # type: ignore[misc]


def test_reconciliation_result_carries_full_diff_context():
    """ReconciliationResult is the operator-facing record of each
    reconcile. Verify it carries enough for triage."""
    r = ReconciliationResult(
        local_estimate_usd=50.0,
        anthropic_reported_usd=80.0,
        diff_usd=30.0,
        diff_pct=0.6,
        within_tolerance=False,
        breaker_re_tripped=True,
    )
    assert r.diff_usd == 30.0
    assert r.diff_pct == 0.6
    assert r.breaker_re_tripped is True


# ===========================================================================
# KR-PER-TENANT-COST-LADDER-FOUNDATION (#202) — per-tenant holders
# ===========================================================================


def test_default_tenant_backward_compat():
    """Legacy ``init_cost_holder()`` + ``get_cost_holder()`` (no
    tenant_id) bind to the DEFAULT_TENANT_ID tenant. Existing
    callers see no shape change."""
    from agent.cost_state_holder import DEFAULT_TENANT_ID

    h_legacy = init_cost_holder(billing_period_start=_period_start())
    h_explicit = get_cost_holder(tenant_id=DEFAULT_TENANT_ID)
    h_no_arg = get_cost_holder()
    assert h_legacy is h_explicit
    assert h_legacy is h_no_arg


def test_per_tenant_holders_are_independent():
    """Two tenants → two distinct holder instances; mutations on
    one don't bleed into the other."""
    from agent.usage_pricing import CanonicalUsage

    h_default = init_cost_holder(billing_period_start=_period_start())
    h_marvin = init_cost_holder(
        billing_period_start=_period_start(),
        tenant_id="marvin",
    )
    assert h_default is not h_marvin

    # Bill 1 call against default; marvin's spent should stay at 0.
    h_default.record_inference(
        CanonicalUsage(input_tokens=1_000_000, output_tokens=0),
        model_name="claude-opus-4-7",
        provider="anthropic",
    )
    assert h_default._state.spent_to_date_usd > 0
    assert h_marvin._state.spent_to_date_usd == 0.0


def test_init_cost_holder_per_tenant_idempotent():
    """Re-initializing the same tenant returns the existing
    instance; arguments are ignored on subsequent calls."""
    h1 = init_cost_holder(
        billing_period_start=_period_start(),
        tenant_id="t-alpha",
    )
    h2 = init_cost_holder(
        billing_period_start=_period_start(year=2030),  # ignored
        credit_pool_usd=999.99,  # ignored
        tenant_id="t-alpha",
    )
    assert h1 is h2


def test_get_cost_holder_unknown_tenant_returns_none():
    """A tenant whose holder hasn't been initialized returns
    ``None`` — same contract as the legacy singleton accessor."""
    init_cost_holder(billing_period_start=_period_start())  # default only
    assert get_cost_holder(tenant_id="never-initialized") is None


def test_per_tenant_credit_pool_env_override(monkeypatch):
    """``KORA_CREDIT_POOL_USD_<TENANT>`` env override picks up the
    per-tenant pool when no explicit credit_pool_usd is passed."""
    monkeypatch.setenv("KORA_CREDIT_POOL_USD_MARVIN", "750")
    h_marvin = init_cost_holder(
        billing_period_start=_period_start(),
        tenant_id="marvin",
    )
    assert h_marvin._state.credit_pool_usd == 750.0


def test_per_tenant_env_normalizes_special_chars(monkeypatch):
    """Tenant_id with non-alnum chars normalizes to env-safe suffix
    (``ops/main`` → ``KORA_CREDIT_POOL_USD_OPS_MAIN``)."""
    monkeypatch.setenv("KORA_CREDIT_POOL_USD_OPS_MAIN", "300")
    h = init_cost_holder(
        billing_period_start=_period_start(),
        tenant_id="ops/main",
    )
    assert h._state.credit_pool_usd == 300.0


def test_explicit_credit_pool_arg_wins_over_env(monkeypatch):
    """When the caller passes credit_pool_usd explicitly (non-default),
    the env override is ignored. Existing tests + the boot pathway
    that passes the resolved value continue to work."""
    monkeypatch.setenv("KORA_CREDIT_POOL_USD_MARVIN", "750")
    h = init_cost_holder(
        billing_period_start=_period_start(),
        tenant_id="marvin",
        credit_pool_usd=123.45,
    )
    assert h._state.credit_pool_usd == 123.45


def test_list_cost_holder_tenants_returns_sorted_registered():
    """list_cost_holder_tenants returns a sorted tuple of every
    currently-registered tenant_id; useful for the snapshot's
    per-tenant projection."""
    from agent.cost_state_holder import list_cost_holder_tenants

    # Initialize out of alphabetical order
    init_cost_holder(billing_period_start=_period_start(), tenant_id="zeta")
    init_cost_holder(billing_period_start=_period_start())  # default
    init_cost_holder(billing_period_start=_period_start(), tenant_id="alpha")
    assert list_cost_holder_tenants() == ("alpha", "default", "zeta")


def test_reset_cost_holder_clears_all_tenants():
    """The test reset hook drops EVERY tenant's holder so each
    test starts with a clean slate."""
    from agent.cost_state_holder import (
        _reset_cost_holder_for_tests,
        list_cost_holder_tenants,
    )

    init_cost_holder(billing_period_start=_period_start())
    init_cost_holder(billing_period_start=_period_start(), tenant_id="marvin")
    init_cost_holder(billing_period_start=_period_start(), tenant_id="alice")
    assert len(list_cost_holder_tenants()) == 3
    _reset_cost_holder_for_tests()
    assert list_cost_holder_tenants() == ()
    assert get_cost_holder() is None
    assert get_cost_holder("marvin") is None
