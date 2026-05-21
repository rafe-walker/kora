"""Tests for ``GET /api/sea-tickets/kora-assigned`` (KR-P2-CLEANUP ST2).

Covers both branches of the endpoint after the stub → live flip:

  * **Uninitialized branch** — ``get_active_provider()`` returns
    ``None`` (early-boot or isolated-test context). The endpoint
    returns the stub-shape with ``stub: True`` + an ``error`` field
    naming the cause. Cockpit panel renders a distinct banner.
  * **Live branch** — active provider registered; ``get_assigned_sea_tickets_via_provider``
    runs against the substrate. The endpoint returns the four-bucket
    grouped dict with NO ``stub`` flag.

When updating these tests in lockstep with the endpoint:
  - The shape contract (top-level keys + per-entry key set) MUST
    stay constant — the cockpit panel's renderer is built against
    it.
  - The ``stub`` flag is the panel's STUB-banner trigger; the
    ``error`` field appears alongside it on the uninitialized branch.
  - The live branch drops both fields.
"""

from typing import Any

import pytest

from plugins.memory.isokron import active_provider


_VALID_CRITICALITY = {"low", "normal", "high", "frontier"}
_VALID_MODEL_TIER = {"haiku", "sonnet", "opus", None}  # None on live branch
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


@pytest.fixture(autouse=True)
def _reset_active_provider():
    """Each test starts with no active provider — explicit set if a test
    wants the live branch."""
    active_provider.clear_active_provider()
    yield
    active_provider.clear_active_provider()


# ---- Uninitialized branch — stub + error ---------------------------------


@pytest.mark.asyncio
async def test_uninitialized_branch_returns_stub_with_error(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_kora_assigned_sea_tickets()

    assert result["stub"] is True
    assert "error" in result
    assert isinstance(result["error"], str)
    assert "not yet registered" in result["error"]
    # Stub shape still passes panel-render expectations.
    assert set(result.keys()) >= {
        "in_progress",
        "queued",
        "recently_resolved",
        "failed_or_blocked",
        "stub",
        "error",
    }


@pytest.mark.asyncio
async def test_uninitialized_stub_entries_match_documented_shape(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_kora_assigned_sea_tickets()
    for entry in result["in_progress"]:
        assert set(entry.keys()) >= {
            "id", "title", "criticality", "claimed_at",
            "claim_count", "work_attempt_count",
        }
    for entry in result["queued"]:
        assert set(entry.keys()) >= {
            "id", "title", "criticality", "assigned_at", "next_eligible_at",
        }
    for entry in result["recently_resolved"]:
        assert set(entry.keys()) >= {
            "id", "title", "criticality", "resolved_at",
            "resolution", "model_tier_used",
        }
    for entry in result["failed_or_blocked"]:
        assert set(entry.keys()) >= {
            "id", "title", "criticality", "state", "failure_count_by_reason",
        }


@pytest.mark.asyncio
async def test_uninitialized_stub_resolution_values_in_documented_set(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_kora_assigned_sea_tickets()
    for entry in result["recently_resolved"]:
        assert entry["resolution"] in _VALID_RESOLUTION


# ---- Live branch — active provider returns real data ---------------------


def _grouped_canned(**overrides: Any) -> dict[str, list[dict[str, Any]]]:
    """A canned grouped-tickets dict the fake provider returns. Shape
    matches what the live read produces."""
    default = {
        "in_progress": [
            {
                "id": "real-ticket-1",
                "title": "Real ticket — in progress",
                "criticality": "normal",
                "claimed_at": "2026-05-21T17:00:00Z",
                "claim_count": 2,
                "work_attempt_count": 3,
            },
        ],
        "queued": [
            {
                "id": "real-ticket-2",
                "title": "Real ticket — queued",
                "criticality": "high",
                "assigned_at": "2026-05-21T16:55:00Z",
                "next_eligible_at": None,
            },
        ],
        "recently_resolved": [],
        "failed_or_blocked": [],
    }
    default.update(overrides)
    return default


@pytest.mark.asyncio
async def test_live_branch_returns_real_data_without_stub_flag(
    _isolate_config, monkeypatch
):
    from kora_cli import web_server

    # Mark an active provider sentinel; we don't care about its shape
    # because we monkeypatch the read helper to return canned data.
    sentinel_provider = object()
    active_provider.set_active_provider(sentinel_provider)

    async def fake_get(*, provider, actor_id=None):
        assert provider is sentinel_provider
        return _grouped_canned()

    monkeypatch.setattr(
        "plugins.memory.isokron.assigned_sea_tickets."
        "get_assigned_sea_tickets_via_provider",
        fake_get,
    )

    result = await web_server.get_kora_assigned_sea_tickets()

    # No stub flag, no error field on the live path.
    assert "stub" not in result
    assert "error" not in result
    # Real data passed through.
    assert result["in_progress"][0]["id"] == "real-ticket-1"
    assert result["queued"][0]["criticality"] == "high"


@pytest.mark.asyncio
async def test_live_branch_falls_back_to_stub_on_read_failure(
    _isolate_config, monkeypatch
):
    from kora_cli import web_server

    sentinel_provider = object()
    active_provider.set_active_provider(sentinel_provider)

    async def failing_get(*, provider, actor_id=None):
        return None  # signals "read failed; fall back to stub with error"

    monkeypatch.setattr(
        "plugins.memory.isokron.assigned_sea_tickets."
        "get_assigned_sea_tickets_via_provider",
        failing_get,
    )

    result = await web_server.get_kora_assigned_sea_tickets()

    assert result["stub"] is True
    assert "error" in result
    assert "substrate read returned None" in result["error"]


# ---- Bucket §4 non-scope guard ------------------------------------------


@pytest.mark.asyncio
async def test_claim_fence_token_never_appears_in_response(_isolate_config):
    """No internal idempotency token is ever exposed — neither in the
    stub nor any future live path. Catches drift in either branch."""
    from kora_cli import web_server

    result = await web_server.get_kora_assigned_sea_tickets()
    payload_str = repr(result)
    assert "claim_fence_token" not in payload_str
    assert "fence_token" not in payload_str


# ---- Cron-regression sanity ----------------------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_sea_tickets_registered(_isolate_config):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)


# ---- active_provider singleton sanity ------------------------------------


def test_active_provider_set_and_clear_round_trips():
    sentinel = object()
    active_provider.set_active_provider(sentinel)
    assert active_provider.get_active_provider() is sentinel
    active_provider.clear_active_provider()
    assert active_provider.get_active_provider() is None
