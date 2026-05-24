"""Tests for KR-P2-COST-FLIP (/api/cost-state — live read + stub fallback).

Original bucket §5 contract guards (carried over from KR-P2-COST-PANEL #49):
  - top-level shape (current + rate_limit_pulse + deferred_tickets +
    reconciliation_history + stub)
  - current has all required fields
  - active_rung enum validation
  - effective_model_tier enum validation
  - deferred_tickets shape + criticality validation
  - reconciliation_history shape + bool tolerance
  - hard_stop_100 ⇒ current_pct_used >= 100
  - credential-leak guard (no token/secret/api_key/password/bearer)
  - cron-regression sanity

KR-P2-COST-FLIP additions:
  - Uninit path: no CostStateHolder → stub:true + error
  - Live path: mocked holder returns shape with stub:false + no error
  - No-rate-limit-pulse edge: holder with latest_rate_limit_pulse=None
    → response carries rate_limit_pulse: null (not absent, not stub)
  - Error path: summary raises → stub:true + error includes message
  - Reconciliation synthesis: holder with last_reconciled_at set
    → single-entry history with synthesized: True
"""

import re

import pytest


_VALID_RUNG = {"normal", "warn_75", "downshift_90", "hard_stop_100"}
_VALID_MODEL_TIER = {"opus", "sonnet", "haiku"}
_VALID_CRITICALITY = {"low", "normal", "high", "frontier"}


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("kora_cli.config.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr(
        "kora_cli.config.get_config_path", lambda: tmp_path / "config.yaml"
    )
    monkeypatch.setattr(
        "kora_cli.config.get_env_path", lambda: tmp_path / ".env"
    )
    return tmp_path


@pytest.fixture
def _no_holder(monkeypatch):
    """Force the CostStateHolder getter to return None — happens in CI /
    dev where init_cost_holder hasn't run.

    KR-PER-TENANT-COST-LADDER-FOUNDATION (#202): the singleton
    ``_HOLDER`` was replaced with ``_HOLDERS_BY_TENANT``. Use the
    canonical reset hook instead of poking the private attribute.
    """
    import agent.cost_state_holder as holder_mod

    monkeypatch.setattr(holder_mod, "_HOLDERS_BY_TENANT", {}, raising=False)
    monkeypatch.setattr(
        holder_mod, "get_cost_holder", lambda tenant_id=None: None
    )
    return None


def _make_live_holder(
    *,
    spent: float = 50.0,
    pool: float = 200.0,
    extra_usage_off: bool = True,
    rate_limit_pulse=None,
    last_reconciled_at=None,
    last_reconciled_anthropic_usd=None,
):
    """Build a CostStateHolder seeded with the given spent + (optional)
    reconciliation snapshot. Tests use this rather than init_cost_holder
    so we don't perturb the module-level singleton across tests."""
    from datetime import datetime, timezone
    from agent.cost_state_holder import CostStateHolder, CostState
    from dataclasses import replace

    h = CostStateHolder(
        billing_period_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        credit_pool_usd=pool,
        extra_usage_off=extra_usage_off,
    )
    new_state = CostState(
        credit_pool_usd=pool,
        spent_to_date_usd=spent,
        billing_period_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        last_reconciled_at=last_reconciled_at,
        last_reconciled_anthropic_usd=last_reconciled_anthropic_usd,
        extra_usage_off=extra_usage_off,
        latest_rate_limit_pulse=rate_limit_pulse,
    )
    h._state = new_state
    return h


def _install_holder(monkeypatch, holder):
    # KR-PER-TENANT-COST-LADDER-FOUNDATION (#202): get_cost_holder
    # now accepts an optional tenant_id kwarg. Adapt the test stub
    # to match the new signature so call sites passing tenant_id
    # don't TypeError.
    import agent.cost_state_holder as holder_mod

    monkeypatch.setattr(
        holder_mod, "get_cost_holder", lambda tenant_id=None: holder
    )


def _install_no_provider(monkeypatch):
    """Force the IsoKron provider getter to return None — deferred_tickets
    degrades to []."""
    import plugins.memory.isokron as isokron_pkg

    monkeypatch.setattr(isokron_pkg, "_last_active_provider", None)


# ---- 1. 200 + shape (no-holder branch) ---------------------------------


@pytest.mark.asyncio
async def test_endpoint_returns_200(_isolate_config, _no_holder, monkeypatch):
    _install_no_provider(monkeypatch)
    from kora_cli import web_server

    result = await web_server.get_cost_state()
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_response_shape_has_required_keys(_isolate_config, _no_holder, monkeypatch):
    _install_no_provider(monkeypatch)
    from kora_cli import web_server

    result = await web_server.get_cost_state()
    # Live branch returns 5 keys; error branch adds "error". Superset.
    assert {
        "current",
        "rate_limit_pulse",
        "deferred_tickets",
        "reconciliation_history",
        "stub",
    } <= set(result.keys())


# ---- 2. Uninit path (no holder) → stub + error ------------------------


@pytest.mark.asyncio
async def test_no_holder_returns_stub_true_with_error(_isolate_config, _no_holder, monkeypatch):
    _install_no_provider(monkeypatch)
    from kora_cli import web_server

    result = await web_server.get_cost_state()
    assert result["stub"] is True
    assert "error" in result
    assert "CostStateHolder" in result["error"]
    assert "not initialised" in result["error"].lower() or "not initialized" in result["error"].lower()
    # Fallback shape preserved
    assert result["deferred_tickets"] == []
    assert result["reconciliation_history"] == []
    assert result["rate_limit_pulse"] is None


# ---- 3. Live path with mocked holder ----------------------------------


@pytest.mark.asyncio
async def test_live_path_drops_stub_flag(_isolate_config, monkeypatch):
    _install_holder(monkeypatch, _make_live_holder(spent=50.0, pool=200.0))
    _install_no_provider(monkeypatch)
    from kora_cli import web_server

    result = await web_server.get_cost_state()
    assert result["stub"] is False
    assert "error" not in result
    assert result["current"]["credit_pool_usd"] == 200.0
    assert result["current"]["spent_to_date_usd"] == 50.0


@pytest.mark.asyncio
async def test_live_path_normal_rung_at_low_pct(_isolate_config, monkeypatch):
    """50/200 = 25% → NORMAL rung; no downshift."""
    _install_holder(monkeypatch, _make_live_holder(spent=50.0, pool=200.0))
    _install_no_provider(monkeypatch)
    from kora_cli import web_server

    result = await web_server.get_cost_state()
    assert result["current"]["active_rung"] == "normal"
    assert result["current"]["downshift_active"] is False
    assert result["current"]["effective_model_tier"] == "opus"


@pytest.mark.asyncio
async def test_live_path_warn_rung_at_75pct(_isolate_config, monkeypatch):
    """160/200 = 80% → WARN_75 rung; downshift_eligible tickets step down."""
    _install_holder(monkeypatch, _make_live_holder(spent=160.0, pool=200.0))
    _install_no_provider(monkeypatch)
    from kora_cli import web_server

    result = await web_server.get_cost_state()
    assert result["current"]["active_rung"] == "warn_75"
    assert result["current"]["downshift_active"] is True
    # Effective tier for downshift_eligible tickets at WARN_75 is sonnet
    assert result["current"]["effective_model_tier"] == "sonnet"


# ---- 4. No-rate-limit-pulse edge → canonical null --------------------


@pytest.mark.asyncio
async def test_no_rate_limit_pulse_returns_null(_isolate_config, monkeypatch):
    """OpenAI-compat-only recent traffic leaves latest_rate_limit_pulse
    as None. The response carries rate_limit_pulse: null (not absent,
    not stub) — the FE renders the pulse-card empty state cleanly."""
    _install_holder(monkeypatch, _make_live_holder(rate_limit_pulse=None))
    _install_no_provider(monkeypatch)
    from kora_cli import web_server

    result = await web_server.get_cost_state()
    assert result["stub"] is False
    assert "rate_limit_pulse" in result
    assert result["rate_limit_pulse"] is None


@pytest.mark.asyncio
async def test_rate_limit_pulse_present_projects_correctly(_isolate_config, monkeypatch):
    """When the holder has a captured pulse, the projection mirrors
    the documented shape (captured_at + requests + tokens axes)."""
    from datetime import datetime, timezone
    from agent.cost_state_holder import RateLimitAxis, RateLimitPulse

    pulse = RateLimitPulse(
        requests=RateLimitAxis(
            limit=4000,
            remaining=3712,
            reset_at=datetime(2026, 5, 21, 22, 5, tzinfo=timezone.utc),
        ),
        tokens=RateLimitAxis(
            limit=400_000,
            remaining=312_000,
            reset_at=datetime(2026, 5, 21, 22, 5, tzinfo=timezone.utc),
        ),
        captured_at=datetime(2026, 5, 21, 22, 0, tzinfo=timezone.utc),
    )
    _install_holder(monkeypatch, _make_live_holder(rate_limit_pulse=pulse))
    _install_no_provider(monkeypatch)
    from kora_cli import web_server

    result = await web_server.get_cost_state()
    assert result["rate_limit_pulse"] is not None
    assert result["rate_limit_pulse"]["requests"]["limit"] == 4000
    assert result["rate_limit_pulse"]["tokens"]["remaining"] == 312_000
    assert result["rate_limit_pulse"]["captured_at"].endswith("Z")


# ---- 5. Reconciliation synthesis --------------------------------------


@pytest.mark.asyncio
async def test_reconciliation_history_empty_when_never_reconciled(_isolate_config, monkeypatch):
    """Pre-first-reconcile state: holder has no last_reconciled_at, so
    the synthesis returns []."""
    _install_holder(monkeypatch, _make_live_holder(spent=50.0))
    _install_no_provider(monkeypatch)
    from kora_cli import web_server

    result = await web_server.get_cost_state()
    assert result["reconciliation_history"] == []


@pytest.mark.asyncio
async def test_reconciliation_history_single_entry_when_reconciled(_isolate_config, monkeypatch):
    """When holder has reconciliation data, synthesize one entry with
    synthesized:True — same pattern as DR-FLIP's epoch_history."""
    from datetime import datetime, timezone

    _install_holder(
        monkeypatch,
        _make_live_holder(
            spent=87.43,
            last_reconciled_at=datetime(2026, 5, 21, 18, 0, tzinfo=timezone.utc),
            last_reconciled_anthropic_usd=88.0,
        ),
    )
    _install_no_provider(monkeypatch)
    from kora_cli import web_server

    result = await web_server.get_cost_state()
    history = result["reconciliation_history"]
    assert len(history) == 1
    entry = history[0]
    assert entry["synthesized"] is True
    assert entry["local_estimator_usd"] == 87.43
    assert entry["anthropic_reported_usd"] == 88.0
    assert isinstance(entry["within_tolerance"], bool)


# ---- 6. Error path → stub + error -------------------------------------


@pytest.mark.asyncio
async def test_summary_failure_returns_stub_with_error(_isolate_config, monkeypatch):
    """Any exception from the summary fetcher falls through to the
    stub-fallback branch with stub:true + error carrying type + message."""
    _install_holder(monkeypatch, _make_live_holder())
    _install_no_provider(monkeypatch)

    import agent.cost_state_summary as summary_mod

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated holder snapshot read failed")

    monkeypatch.setattr(summary_mod, "get_cost_state_summary", _boom)

    from kora_cli import web_server

    result = await web_server.get_cost_state()
    assert result["stub"] is True
    assert "error" in result
    assert "RuntimeError" in result["error"]
    assert "simulated holder snapshot read failed" in result["error"]


# ---- 7. Carried-over contract guards (from #49) ----------------------


@pytest.mark.asyncio
async def test_active_rung_in_documented_set(_isolate_config, _no_holder, monkeypatch):
    _install_no_provider(monkeypatch)
    from kora_cli import web_server

    result = await web_server.get_cost_state()
    assert result["current"]["active_rung"] in _VALID_RUNG


@pytest.mark.asyncio
async def test_effective_model_tier_in_documented_set(_isolate_config, _no_holder, monkeypatch):
    _install_no_provider(monkeypatch)
    from kora_cli import web_server

    result = await web_server.get_cost_state()
    assert result["current"]["effective_model_tier"] in _VALID_MODEL_TIER


@pytest.mark.asyncio
async def test_hard_stop_rung_implies_pct_at_or_above_100(_isolate_config, monkeypatch):
    """200/200 = 100% → HARD_STOP_100; pct must be >= 100."""
    _install_holder(monkeypatch, _make_live_holder(spent=200.0, pool=200.0))
    _install_no_provider(monkeypatch)
    from kora_cli import web_server

    result = await web_server.get_cost_state()
    if result["current"]["active_rung"] == "hard_stop_100":
        assert result["current"]["current_pct_used"] >= 100


_CREDENTIAL_KEY_RE = re.compile(
    r"\b(token|secret|api[_-]?key|password|bearer|authorization)\b",
    re.IGNORECASE,
)


def _walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_keys(item)


@pytest.mark.asyncio
async def test_no_credential_shaped_keys_in_response(_isolate_config, _no_holder, monkeypatch):
    _install_no_provider(monkeypatch)
    from kora_cli import web_server

    result = await web_server.get_cost_state()
    offending = [
        key for key in _walk_keys(result) if _CREDENTIAL_KEY_RE.search(key)
    ]
    assert offending == [], (
        f"cost-state response contains credential-shaped keys: {offending}"
    )


# ---- 8. Cron-regression sanity ----------------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_cost_state_registered(_isolate_config):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
