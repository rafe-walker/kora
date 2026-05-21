"""Tests for the KR-P2-BOOT-PANEL stub endpoint.

Bucket §5 scenarios:
  1. GET /api/boot-status returns 200
  2. Top-level shape (current + history + stub:true)
  3. current.gates is a list with required keys
  4. boot outcome ∈ {booting, ready, failed}
  5. gate_class ∈ {transient, invariant}
  6. per-gate outcome ∈ {pass, fail}
  7. Contract guard: outcome==ready ⇒ every gate passed
  8. Cron-regression sanity
"""

import pytest


_VALID_BOOT_OUTCOME = {"booting", "ready", "failed"}
_VALID_GATE_OUTCOME = {"pass", "fail"}
_VALID_GATE_CLASS = {"transient", "invariant"}


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

    result = await web_server.get_boot_status()
    assert isinstance(result, dict)


# ---- 2. Top-level shape ---------------------------------------------------


@pytest.mark.asyncio
async def test_response_shape_has_required_keys(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_boot_status()
    assert set(result.keys()) == {"current", "history", "stub"}
    assert result["stub"] is True
    assert isinstance(result["current"], dict)
    assert isinstance(result["history"], list)


# ---- 3. current.gates shape ----------------------------------------------


@pytest.mark.asyncio
async def test_current_gates_is_list_with_required_keys(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_boot_status()
    gates = result["current"]["gates"]
    assert isinstance(gates, list)
    assert len(gates) >= 1

    for gate in gates:
        assert set(gate.keys()) >= {
            "gate_id",
            "title",
            "gate_class",
            "outcome",
            "elapsed_ms",
            "detail",
        }
        assert isinstance(gate["elapsed_ms"], int)


@pytest.mark.asyncio
async def test_current_boot_has_top_level_metadata(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_boot_status()
    current = result["current"]
    assert set(current.keys()) >= {
        "boot_id",
        "primary_state",
        "started_at",
        "completed_at",
        "elapsed_ms",
        "outcome",
        "gates",
    }


# ---- 4. boot outcome enum -------------------------------------------------


@pytest.mark.asyncio
async def test_boot_outcomes_in_documented_set(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_boot_status()
    assert result["current"]["outcome"] in _VALID_BOOT_OUTCOME
    for entry in result["history"]:
        assert entry["outcome"] in _VALID_BOOT_OUTCOME


# ---- 5. gate_class enum --------------------------------------------------


@pytest.mark.asyncio
async def test_gate_class_values_in_documented_set(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_boot_status()
    for gate in result["current"]["gates"]:
        assert gate["gate_class"] in _VALID_GATE_CLASS


# ---- 6. per-gate outcome enum --------------------------------------------


@pytest.mark.asyncio
async def test_gate_outcomes_in_documented_set(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_boot_status()
    for gate in result["current"]["gates"]:
        assert gate["outcome"] in _VALID_GATE_OUTCOME


# ---- 7. Contract guard: ready ⇒ all-pass ---------------------------------


@pytest.mark.asyncio
async def test_ready_outcome_implies_all_gates_passed(_isolate_config):
    """A ``ready`` boot must have ``outcome=="pass"`` on every gate it ran.
    If we ever see ready-with-fail, something is lying about state."""
    from kora_cli import web_server

    result = await web_server.get_boot_status()
    if result["current"]["outcome"] == "ready":
        for gate in result["current"]["gates"]:
            assert gate["outcome"] == "pass", (
                f"ready boot has failing gate {gate['gate_id']}: {gate['detail']}"
            )


# Bucket §3: a failed history entry should carry a failed_gate_id +
# failed_gate_title; codify so the panel always has data to display.
@pytest.mark.asyncio
async def test_failed_history_entries_carry_failure_metadata(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_boot_status()
    for entry in result["history"]:
        if entry["outcome"] == "failed":
            assert "failed_gate_id" in entry
            assert "failed_gate_title" in entry
            assert isinstance(entry["failed_gate_id"], str)
            assert entry["failed_gate_id"]


# ---- 8. Cron-regression sanity -------------------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_boot_status_registered(_isolate_config):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
