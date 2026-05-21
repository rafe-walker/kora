"""Tests for ``GET /api/kora-control/observed-state`` (KR-P2-CLEANUP ST3).

Covers both branches after the stub → live flip:

  * **Uninitialized branch** — ``get_active_provider()`` returns ``None``.
    The endpoint returns the stub-shape with ``stub: True`` + an
    ``error`` field naming the cause.
  * **Live branch** — active provider registered;
    ``get_observed_state_via_provider`` is called; the endpoint
    returns the three-bucket grouped dict with NO ``stub`` flag.

Maintenance note: the shape contract (top-level keys + per-entry
key set) must stay stable so the cockpit panel renders. The
``stub`` flag triggers the panel's STUB banner; ``error`` is shown
underneath on the uninitialized branch.
"""

import pytest

from plugins.memory.isokron import active_provider


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
    active_provider.clear_active_provider()
    yield
    active_provider.clear_active_provider()


# ---- Uninitialized branch — stub + error ---------------------------------


@pytest.mark.asyncio
async def test_uninitialized_branch_returns_stub_with_error(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_kora_control_observed_state()

    assert result["stub"] is True
    assert "error" in result
    assert isinstance(result["error"], str)
    assert "not yet registered" in result["error"]
    assert set(result.keys()) >= {
        "active",
        "recently_enforced",
        "history",
        "stub",
        "error",
    }


@pytest.mark.asyncio
async def test_uninitialized_stub_entries_match_documented_shape(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_kora_control_observed_state()
    for section in ("active", "recently_enforced", "history"):
        assert isinstance(result[section], list)
        for entry in result[section]:
            assert set(entry.keys()) >= {
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
            assert entry["kind"] in _VALID_KIND
            assert entry["lifecycle_state"] in _VALID_LIFECYCLE
            assert isinstance(entry["level"], int)
            assert 0 <= entry["level"] <= 5


# ---- Live branch — active provider returns real data ---------------------


def _canned_grouped() -> dict:
    return {
        "active": [
            {
                "command_id": "kc-real-001",
                "level": 2,
                "kind": "stop",
                "reason": "Real STOP-KORA L2 — drain mode",
                "issuer": "operator/op_1 (cockpit session real-cs-1)",
                "sequence": 99,
                "created_at": "2026-05-21T19:00:00Z",
                "visible_to_runtime_at": "2026-05-21T19:00:01Z",
                "observed_at": "2026-05-21T19:00:02Z",
                "acknowledged_at": "2026-05-21T19:00:03Z",
                "enforced_at": None,
                "lifecycle_state": "acknowledged",
                "expires_at": None,
                "target_session": None,
            }
        ],
        "recently_enforced": [],
        "history": [],
    }


@pytest.mark.asyncio
async def test_live_branch_returns_real_data_without_stub_flag(
    _isolate_config, monkeypatch
):
    from kora_cli import web_server

    sentinel_provider = object()
    active_provider.set_active_provider(sentinel_provider)

    async def fake_get(*, provider):
        assert provider is sentinel_provider
        return _canned_grouped()

    monkeypatch.setattr(
        "plugins.memory.isokron.observed_kora_control."
        "get_observed_state_via_provider",
        fake_get,
    )

    result = await web_server.get_kora_control_observed_state()

    assert "stub" not in result
    assert "error" not in result
    assert result["active"][0]["command_id"] == "kc-real-001"
    assert result["active"][0]["lifecycle_state"] == "acknowledged"


@pytest.mark.asyncio
async def test_live_branch_falls_back_to_stub_on_read_failure(
    _isolate_config, monkeypatch
):
    from kora_cli import web_server

    sentinel_provider = object()
    active_provider.set_active_provider(sentinel_provider)

    async def failing_get(*, provider):
        return None

    monkeypatch.setattr(
        "plugins.memory.isokron.observed_kora_control."
        "get_observed_state_via_provider",
        failing_get,
    )

    result = await web_server.get_kora_control_observed_state()
    assert result["stub"] is True
    assert "substrate read returned None" in result["error"]


# ---- Cron-regression sanity ----------------------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_kora_control_registered(_isolate_config):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
