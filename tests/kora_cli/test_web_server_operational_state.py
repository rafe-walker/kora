"""Tests for the KR-P2-OPS-PANEL stub endpoint.

Bucket §5 scenarios:
  1. GET /api/operational-state returns 200
  2. Response shape matches documented stub exactly
  3. transition_history has the required keys
  4. valid_next_states entries point at valid PrimaryState values
  5. Cron-regression sanity: /api/cron/jobs still works
"""

import pytest


_VALID_PRIMARY_STATES = {"booting", "ready", "active", "paused", "stopped"}
_VALID_CLAIM_PERMISSIONS = {"none", "critical_only", "normal"}
_VALID_DEGRADATION_REASONS = {
    "cost",
    "auth",
    "dispatch",
    "substrate",
    "migration",
    "operator",
    "token_expiring",
    "retry_ceiling",
}


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
async def test_get_operational_state_returns_200(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_operational_state()
    assert isinstance(result, dict)


# ---- 2. Shape matches stub spec -------------------------------------------


@pytest.mark.asyncio
async def test_stub_shape_matches_spec_exactly(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_operational_state()

    assert result["primary_state"] == "ready"
    assert result["claim_permission"] == "normal"
    assert result["is_degraded"] is False
    assert result["degradation_reasons"] == []
    assert result["stub"] is True


@pytest.mark.asyncio
async def test_stub_response_has_all_required_top_level_keys(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_operational_state()

    required = {
        "primary_state",
        "claim_permission",
        "degradation_reasons",
        "is_degraded",
        "transition_history",
        "valid_next_states",
        "stub",
    }
    assert required.issubset(result.keys())


# ---- 3. transition_history shape -----------------------------------------


@pytest.mark.asyncio
async def test_transition_history_has_required_keys(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_operational_state()
    history = result["transition_history"]

    assert isinstance(history, list)
    assert len(history) >= 1

    for entry in history:
        assert set(entry.keys()) >= {
            "timestamp",
            "from_state",
            "to_state",
            "trigger",
        }
        assert entry["from_state"] in _VALID_PRIMARY_STATES
        assert entry["to_state"] in _VALID_PRIMARY_STATES
        assert isinstance(entry["trigger"], str) and entry["trigger"]


# ---- 4. valid_next_states shape -------------------------------------------


@pytest.mark.asyncio
async def test_valid_next_states_point_at_valid_primary_states(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_operational_state()
    next_states = result["valid_next_states"]

    assert isinstance(next_states, list)
    assert len(next_states) >= 1

    for entry in next_states:
        assert set(entry.keys()) >= {"to_state", "trigger"}
        assert entry["to_state"] in _VALID_PRIMARY_STATES
        assert isinstance(entry["trigger"], str) and entry["trigger"]


# Bucket §6 fixes the stub's READY-state transitions; codify that we
# advertise the three exits the bucket lists so a future "tighten the
# stub" PR doesn't silently regress what operators have memorised.
@pytest.mark.asyncio
async def test_ready_state_advertises_three_known_next_states(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_operational_state()
    next_targets = {n["to_state"] for n in result["valid_next_states"]}

    assert next_targets == {"active", "paused", "stopped"}


# ---- 5. Cron-regression sanity --------------------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_ops_state_registered(_isolate_config):
    """Catch import-time regressions when the new endpoint is registered."""
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)


# ---- Bonus: enum value subset check (catches typos in stub) ---------------


@pytest.mark.asyncio
async def test_stub_uses_only_valid_enum_values(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_operational_state()

    assert result["primary_state"] in _VALID_PRIMARY_STATES
    assert result["claim_permission"] in _VALID_CLAIM_PERMISSIONS
    for reason in result["degradation_reasons"]:
        assert reason in _VALID_DEGRADATION_REASONS
