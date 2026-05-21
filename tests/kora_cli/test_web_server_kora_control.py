"""Tests for the KR-P2-CONTROL-PANEL stub endpoint.

Bucket §5 scenarios:
  1. GET /api/kora-control/observed-state returns 200
  2. Response shape (3 grouping keys + stub:true)
  3. Per-section list type + sample-field check
  4. level values ∈ {0..5}
  5. kind values ∈ {stop, reset}
  6. lifecycle_state values ∈ documented union
  7. Cron-regression sanity
"""

import pytest


_VALID_KIND = {"stop", "reset"}
_VALID_LIFECYCLE = {
    "created",
    "visible_to_runtime",
    "acknowledged",
    "enforcing",
    "enforced",
    "superseded",
    "expired",
    "failed",
    "escalated",
}
_VALID_LEVEL = {0, 1, 2, 3, 4, 5}


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

    result = await web_server.get_kora_control_observed_state()
    assert isinstance(result, dict)


# ---- 2. Top-level shape ---------------------------------------------------


@pytest.mark.asyncio
async def test_response_shape_has_required_grouping_keys(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_kora_control_observed_state()

    assert set(result.keys()) == {
        "active",
        "recently_enforced",
        "history",
        "stub",
    }
    assert result["stub"] is True


# ---- 3. Per-section shape -------------------------------------------------


def _required_command_keys() -> set[str]:
    return {
        "command_id",
        "level",
        "kind",
        "reason",
        "issuer",
        "sequence",
        "created_at",
        "visible_to_runtime_at",
        "observed_at",
        "acknowledged_at",
        "enforced_at",
        "lifecycle_state",
        "expires_at",
        "target_session",
    }


@pytest.mark.asyncio
async def test_all_sections_are_lists_with_required_keys(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_kora_control_observed_state()
    required = _required_command_keys()

    for section_name in ("active", "recently_enforced", "history"):
        section = result[section_name]
        assert isinstance(section, list)
        for entry in section:
            assert required <= set(entry.keys()), (
                f"{section_name}: {entry.get('command_id')} missing keys "
                f"{required - set(entry.keys())}"
            )
            assert isinstance(entry["level"], int)
            assert isinstance(entry["sequence"], int)


# ---- 4. level enum --------------------------------------------------------


@pytest.mark.asyncio
async def test_all_level_values_are_in_range(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_kora_control_observed_state()
    for section_name in ("active", "recently_enforced", "history"):
        for entry in result[section_name]:
            assert entry["level"] in _VALID_LEVEL, (
                f"{section_name}: {entry['command_id']} has level "
                f"{entry['level']!r} outside 0..5"
            )


# ---- 5. kind enum ---------------------------------------------------------


@pytest.mark.asyncio
async def test_all_kind_values_in_documented_set(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_kora_control_observed_state()
    for section_name in ("active", "recently_enforced", "history"):
        for entry in result[section_name]:
            assert entry["kind"] in _VALID_KIND


# ---- 6. lifecycle_state enum ---------------------------------------------


@pytest.mark.asyncio
async def test_all_lifecycle_state_values_in_documented_set(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_kora_control_observed_state()
    for section_name in ("active", "recently_enforced", "history"):
        for entry in result[section_name]:
            assert entry["lifecycle_state"] in _VALID_LIFECYCLE


# Bucket §3 says ``recently_enforced`` entries are necessarily in the
# ``enforced`` lifecycle state. Codify that contract so a future stub or
# real-data drift can't silently put an active-state entry into the
# enforced bucket without us noticing.
@pytest.mark.asyncio
async def test_recently_enforced_entries_are_in_enforced_lifecycle(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_kora_control_observed_state()
    for entry in result["recently_enforced"]:
        assert entry["lifecycle_state"] == "enforced"
        assert entry["enforced_at"] is not None


# ---- 7. Cron-regression sanity --------------------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_kora_control_registered(_isolate_config):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
