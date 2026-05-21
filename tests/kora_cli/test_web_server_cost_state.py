"""Tests for the KR-P2-COST-PANEL stub endpoint.

Bucket §5 scenarios:
  1. GET /api/cost-state returns 200
  2. Top-level shape (4 keys + stub:true)
  3. current has all required fields
  4. active_rung values ∈ documented set
  5. effective_model_tier values ∈ documented set
  6. deferred_tickets entries have required keys + valid criticality
  7. reconciliation_history entries have required keys + bool tolerance
  8. Contract guard: hard_stop_100 ⇒ current_pct_used >= 100
  9. Credential-leak guard (no token/secret/api_key/password/bearer fields)
 10. Cron-regression sanity
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


# ---- 1. 200 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_returns_200(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_cost_state()
    assert isinstance(result, dict)


# ---- 2. Top-level shape ---------------------------------------------------


@pytest.mark.asyncio
async def test_response_shape_has_required_keys(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_cost_state()
    assert set(result.keys()) == {
        "current",
        "rate_limit_pulse",
        "deferred_tickets",
        "reconciliation_history",
        "stub",
    }
    assert result["stub"] is True


# ---- 3. current shape -----------------------------------------------------


@pytest.mark.asyncio
async def test_current_has_required_fields(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_cost_state()
    required = {
        "billing_period_start",
        "billing_period_end",
        "days_remaining",
        "credit_pool_usd",
        "spent_to_date_usd",
        "burn_rate_usd_per_day",
        "projected_end_of_period_usd",
        "active_rung",
        "active_rung_threshold_pct",
        "current_pct_used",
        "effective_model_tier",
        "downshift_active",
        "downshift_reason",
        "extra_usage_off",
    }
    assert required <= set(result["current"].keys())


# ---- 4. active_rung enum --------------------------------------------------


@pytest.mark.asyncio
async def test_active_rung_in_documented_set(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_cost_state()
    assert result["current"]["active_rung"] in _VALID_RUNG


# ---- 5. effective_model_tier enum ----------------------------------------


@pytest.mark.asyncio
async def test_effective_model_tier_in_documented_set(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_cost_state()
    assert result["current"]["effective_model_tier"] in _VALID_MODEL_TIER


# ---- 6. deferred_tickets shape -------------------------------------------


@pytest.mark.asyncio
async def test_deferred_tickets_have_required_keys(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_cost_state()
    tickets = result["deferred_tickets"]
    assert isinstance(tickets, list)

    for t in tickets:
        assert set(t.keys()) >= {
            "id",
            "title",
            "criticality",
            "state",
            "deferred_at",
            "reason",
        }
        assert t["criticality"] in _VALID_CRITICALITY
        assert t["state"] == "deferred_cost_limit"


# ---- 7. reconciliation_history shape -------------------------------------


@pytest.mark.asyncio
async def test_reconciliation_history_entries_have_required_keys(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_cost_state()
    history = result["reconciliation_history"]
    assert isinstance(history, list)

    for r in history:
        assert set(r.keys()) >= {
            "reconciled_at",
            "local_estimator_usd",
            "anthropic_reported_usd",
            "delta_usd",
            "delta_pct",
            "within_tolerance",
        }
        assert isinstance(r["within_tolerance"], bool)


# ---- 8. Contract guard: hard_stop ⇒ pct >= 100 ---------------------------


@pytest.mark.asyncio
async def test_hard_stop_rung_implies_pct_at_or_above_100(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_cost_state()
    current = result["current"]
    if current["active_rung"] == "hard_stop_100":
        assert current["current_pct_used"] >= 100, (
            f"hard_stop_100 active but only {current['current_pct_used']}% used — "
            "rung classifier and pct disagree"
        )


# Bucket §3 contract: extra_usage_off being TRUE means the Anthropic
# console hard cap is in place. If hard_stop_100 ever shows up with
# extra_usage_off == False, something is lying about the hard cap.
@pytest.mark.asyncio
async def test_hard_stop_with_extra_usage_on_is_inconsistent(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_cost_state()
    current = result["current"]
    if current["active_rung"] == "hard_stop_100":
        # The stub keeps extra_usage_off=True so this is mostly a guard
        # for future real-data drift, but worth pinning the invariant
        # while we have the read on a stable shape.
        assert current["extra_usage_off"] is True, (
            "hard_stop_100 active with extra_usage_off=False means the "
            "Anthropic console hard cap is off — surface this to the "
            "operator, never silently accept it"
        )


# ---- 9. Credential-leak guard --------------------------------------------


_CREDENTIAL_KEY_RE = re.compile(
    r"\b(token|secret|api[_-]?key|password|bearer|authorization)\b",
    re.IGNORECASE,
)


def _walk_keys(obj):
    """Yield every dict key in a nested structure."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_keys(item)


@pytest.mark.asyncio
async def test_no_credential_shaped_keys_in_response(_isolate_config):
    """Bucket §1 + §4: never expose tokens, secrets, OAuth creds, etc.
    The cost surface is dollar amounts + model tier + ticket IDs only.
    Catch any future drift that names a credential-shaped field here.
    """
    from kora_cli import web_server

    result = await web_server.get_cost_state()
    offending = [
        key for key in _walk_keys(result) if _CREDENTIAL_KEY_RE.search(key)
    ]
    assert offending == [], (
        f"cost-state response contains credential-shaped keys: {offending}"
    )


# ---- 10. Cron-regression sanity ------------------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_cost_state_registered(_isolate_config):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
