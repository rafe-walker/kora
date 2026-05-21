"""Tests for the KR-P2-SEA-PANEL stub endpoint.

Bucket §5 scenarios:
  1. GET /api/sea-tickets/kora-assigned returns 200
  2. Response shape matches documented stub (4 grouping keys + stub:true)
  3. Each section is a list; sample-field type-check per section
  4. criticality values ∈ {low, normal, high, frontier}
  5. resolution values ∈ documented union
  6. Cron-regression sanity (other endpoints still register)
"""

import pytest


_VALID_CRITICALITY = {"low", "normal", "high", "frontier"}
_VALID_MODEL_TIER = {"haiku", "sonnet", "opus"}
_VALID_RESOLUTION = {
    "completed",
    "released",
    "failed_retryable",
    "failed_terminal",
    "blocked_needs_operator",
    "deferred_cost_limit",
}
_VALID_FAILED_STATE = {"failed_terminal", "blocked_needs_operator"}


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

    result = await web_server.get_kora_assigned_sea_tickets()
    assert isinstance(result, dict)


# ---- 2. Top-level shape ---------------------------------------------------


@pytest.mark.asyncio
async def test_response_shape_has_all_required_grouping_keys(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_kora_assigned_sea_tickets()

    assert set(result.keys()) == {
        "in_progress",
        "queued",
        "recently_resolved",
        "failed_or_blocked",
        "stub",
    }
    assert result["stub"] is True


# ---- 3. Per-section shape -------------------------------------------------


@pytest.mark.asyncio
async def test_in_progress_entries_have_required_keys(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_kora_assigned_sea_tickets()
    in_progress = result["in_progress"]
    assert isinstance(in_progress, list)

    for entry in in_progress:
        assert set(entry.keys()) >= {
            "id",
            "title",
            "criticality",
            "claimed_at",
            "claim_count",
            "work_attempt_count",
        }
        assert isinstance(entry["claim_count"], int)
        assert isinstance(entry["work_attempt_count"], int)


@pytest.mark.asyncio
async def test_queued_entries_have_required_keys(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_kora_assigned_sea_tickets()
    queued = result["queued"]
    assert isinstance(queued, list)

    for entry in queued:
        assert set(entry.keys()) >= {
            "id",
            "title",
            "criticality",
            "assigned_at",
            "next_eligible_at",
        }
        # next_eligible_at is either an ISO string or None
        assert entry["next_eligible_at"] is None or isinstance(
            entry["next_eligible_at"], str
        )


@pytest.mark.asyncio
async def test_recently_resolved_entries_have_required_keys(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_kora_assigned_sea_tickets()
    resolved = result["recently_resolved"]
    assert isinstance(resolved, list)

    for entry in resolved:
        assert set(entry.keys()) >= {
            "id",
            "title",
            "criticality",
            "resolved_at",
            "resolution",
            "model_tier_used",
        }
        assert entry["model_tier_used"] in _VALID_MODEL_TIER


@pytest.mark.asyncio
async def test_failed_or_blocked_entries_have_required_keys(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_kora_assigned_sea_tickets()
    failed = result["failed_or_blocked"]
    assert isinstance(failed, list)

    for entry in failed:
        assert set(entry.keys()) >= {
            "id",
            "title",
            "criticality",
            "state",
            "failure_count_by_reason",
        }
        assert entry["state"] in _VALID_FAILED_STATE
        assert isinstance(entry["failure_count_by_reason"], dict)


# ---- 4. criticality enum values ------------------------------------------


@pytest.mark.asyncio
async def test_all_criticality_values_are_in_documented_set(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_kora_assigned_sea_tickets()

    for section_name in (
        "in_progress",
        "queued",
        "recently_resolved",
        "failed_or_blocked",
    ):
        for entry in result[section_name]:
            assert entry["criticality"] in _VALID_CRITICALITY, (
                f"{section_name}: {entry['id']} has unknown criticality "
                f"{entry['criticality']!r}"
            )


# ---- 5. resolution enum values -------------------------------------------


@pytest.mark.asyncio
async def test_all_resolution_values_are_in_documented_set(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_kora_assigned_sea_tickets()
    for entry in result["recently_resolved"]:
        assert entry["resolution"] in _VALID_RESOLUTION


# ---- §4 non-scope guard: claim_fence_token never leaks ----------------


@pytest.mark.asyncio
async def test_claim_fence_token_never_appears_in_response(_isolate_config):
    """Bucket §4 explicitly forbids exposing the internal idempotency
    token. Catch any future drift that introduces it into the API shape.
    """
    from kora_cli import web_server

    result = await web_server.get_kora_assigned_sea_tickets()
    payload_str = repr(result)
    assert "claim_fence_token" not in payload_str
    assert "fence_token" not in payload_str


# ---- 6. Cron-regression sanity --------------------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_sea_tickets_registered(_isolate_config):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
