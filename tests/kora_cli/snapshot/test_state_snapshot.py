"""Tests for KR-CHEAP-PRE-WARMED-SNAPSHOT.

Bucket §2 scenarios:

  Shape / schema:
   1. compute_snapshot returns dict with all required keys
   2. schema_version present + matches module constant
   3. computed_at is ISO 8601 UTC Z-suffixed
   4. operational_state has primary / paused / pause_reason
   5. alerts has active_count / by_severity / by_category
   6. cost_ladder has current_tier / monthly_budget_pct_used /
      model_default
   7. service_health has all 5 expected probe names

  Population vs degradation:
   8. operational_state holder unavailable → primary="unknown",
      paused=False, pause_reason=None
   9. cost holder unavailable → all fields "unknown"
  10. heartbeat probe import failure → all 5 probes "unknown"
  11. alerts aggregator raises → active_count=0 + empty by_*
  12. ALL accessors raise simultaneously → snapshot still computes,
      every section degraded, never raises

  Atomic write:
  13. write_snapshot creates target file with full contents
  14. write_snapshot creates parent directory
  15. Write is atomic (target appears in one rename op)

  Read + freshness:
  16. read_snapshot returns None when file missing
  17. read_snapshot returns parsed dict when file fresh
  18. read_snapshot returns None when file stale (>10 min old)
  19. read_snapshot returns None on malformed JSON
  20. is_snapshot_fresh True for now-ish computed_at
  21. is_snapshot_fresh False for >10 min old
  22. is_snapshot_fresh False for malformed computed_at

  Periodic-task entry:
  23. run_snapshot_cycle writes the file end-to-end
  24. run_snapshot_cycle handles compute failure cleanly
  25. run_snapshot_cycle handles write failure cleanly

  Public-surface convenience:
  26. get_snapshot_for_routing returns fresh snapshot OR None
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kora_cli.snapshot import (
    SCHEMA_VERSION,
    compute_snapshot,
    get_snapshot_for_routing,
    is_snapshot_fresh,
    read_snapshot,
    run_snapshot_cycle,
    snapshot_path,
    write_snapshot,
)
from kora_cli.snapshot.state_snapshot import (
    SNAPSHOT_FRESH_THRESHOLD_SECONDS,
    SNAPSHOT_PATH_ENV,
    _collect_alerts,
    _collect_cost_ladder,
    _collect_operational_state,
    _collect_service_health,
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated KORA_HOME + snapshot path override + fresh-import
    state so no test pollutes another."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setattr("kora_constants.get_kora_home", lambda: tmp_path)
    monkeypatch.delenv(SNAPSHOT_PATH_ENV, raising=False)
    return tmp_path


# ===========================================================================
# Shape / schema
# ===========================================================================


def test_compute_snapshot_has_all_required_top_level_keys(env):
    snap = compute_snapshot()
    assert set(snap.keys()) == {
        "schema_version",
        "computed_at",
        "operational_state",
        "alerts",
        "cost_ladder",
        "tasks",
        "service_health",
    }


def test_compute_snapshot_schema_version_matches_constant(env):
    snap = compute_snapshot()
    assert snap["schema_version"] == SCHEMA_VERSION


def test_compute_snapshot_computed_at_is_iso_utc(env):
    snap = compute_snapshot()
    ts = snap["computed_at"]
    assert isinstance(ts, str)
    assert ts.endswith("Z")
    # Roundtrip parses cleanly.
    parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


def test_operational_state_section_shape(env):
    snap = compute_snapshot()
    op = snap["operational_state"]
    assert set(op.keys()) == {"primary", "paused", "pause_reason"}
    assert isinstance(op["paused"], bool)


def test_alerts_section_shape(env):
    snap = compute_snapshot()
    alerts = snap["alerts"]
    assert set(alerts.keys()) == {
        "active_count",
        "by_severity",
        "by_category",
    }
    assert set(alerts["by_severity"].keys()) >= {
        "critical",
        "warning",
        "info",
    }


def test_cost_ladder_section_shape(env):
    snap = compute_snapshot()
    cl = snap["cost_ladder"]
    assert set(cl.keys()) == {
        "current_tier",
        "monthly_budget_pct_used",
        "model_default",
    }


def test_service_health_includes_all_5_expected_probes(env):
    snap = compute_snapshot()
    sh = snap["service_health"]
    assert set(sh.keys()) == {"vercel", "sentry", "doppler", "supabase", "fly"}


# ===========================================================================
# Degradation — each per-source collector independently
# ===========================================================================


def test_operational_state_no_holder_degrades_to_unknown(env, monkeypatch):
    monkeypatch.setattr(
        "agent.operational_state_holder.get_holder", lambda: None
    )
    op = _collect_operational_state()
    assert op == {
        "primary": "unknown",
        "paused": False,
        "pause_reason": None,
    }


def test_operational_state_holder_raises_degrades(env, monkeypatch):
    bad = MagicMock()
    # Make .current raise.
    type(bad).current = property(
        lambda self: (_ for _ in ()).throw(RuntimeError("kaboom"))
    )
    monkeypatch.setattr(
        "agent.operational_state_holder.get_holder", lambda: bad
    )
    op = _collect_operational_state()
    assert op["primary"] == "unknown"


def test_cost_ladder_no_holder_degrades_to_unknown(env, monkeypatch):
    monkeypatch.setattr(
        "agent.cost_state_holder.get_cost_holder", lambda: None
    )
    cl = _collect_cost_ladder()
    assert cl["current_tier"] == "unknown"
    assert cl["monthly_budget_pct_used"] is None
    assert cl["model_default"] == "unknown"


def test_cost_ladder_active_rung_raises_partial_degrade(env, monkeypatch):
    fake_holder = MagicMock()
    fake_holder.active_rung.side_effect = RuntimeError("rung kaboom")
    fake_holder.current_pct_used.return_value = 0.42
    monkeypatch.setattr(
        "agent.cost_state_holder.get_cost_holder",
        lambda: fake_holder,
    )
    cl = _collect_cost_ladder()
    # Tier degraded, pct still populated.
    assert cl["current_tier"] == "unknown"
    assert cl["monthly_budget_pct_used"] == 42.0


def test_service_health_no_probes_all_unknown(env, monkeypatch):
    monkeypatch.setattr(
        "kora_cli.heartbeat_probes.runner.current_service_snapshots",
        lambda: {},
    )
    sh = _collect_service_health()
    assert sh == {
        "vercel": "unknown",
        "sentry": "unknown",
        "doppler": "unknown",
        "supabase": "unknown",
        "fly": "unknown",
    }


def test_service_health_partial_probes_others_unknown(env, monkeypatch):
    fake_snap = MagicMock()
    fake_snap.status = "healthy"
    monkeypatch.setattr(
        "kora_cli.heartbeat_probes.runner.current_service_snapshots",
        lambda: {"vercel": fake_snap},
    )
    sh = _collect_service_health()
    assert sh["vercel"] == "healthy"
    assert sh["sentry"] == "unknown"


def test_service_health_accessor_raises_all_unknown(env, monkeypatch):
    def boom():
        raise RuntimeError("probes dead")

    monkeypatch.setattr(
        "kora_cli.heartbeat_probes.runner.current_service_snapshots", boom
    )
    sh = _collect_service_health()
    assert all(v == "unknown" for v in sh.values())


def test_alerts_aggregator_raises_returns_zero(env, monkeypatch):
    def boom():
        raise RuntimeError("agg dead")

    monkeypatch.setattr(
        "kora_cli.alerts.aggregator.compute_active_alerts", boom
    )
    alerts = _collect_alerts()
    assert alerts["active_count"] == 0
    assert alerts["by_severity"] == {"critical": 0, "warning": 0, "info": 0}
    assert alerts["by_category"] == {}


def test_compute_snapshot_full_degrade_does_not_raise(env, monkeypatch):
    """Every accessor blown up simultaneously → snapshot still
    computes, fields degraded, never raises."""
    def boom():
        raise RuntimeError("dead")

    monkeypatch.setattr(
        "agent.operational_state_holder.get_holder", boom
    )
    monkeypatch.setattr(
        "agent.cost_state_holder.get_cost_holder", boom
    )
    monkeypatch.setattr(
        "kora_cli.alerts.aggregator.compute_active_alerts", boom
    )
    monkeypatch.setattr(
        "kora_cli.heartbeat_probes.runner.current_service_snapshots", boom
    )

    snap = compute_snapshot()
    assert snap["operational_state"]["primary"] == "unknown"
    assert snap["cost_ladder"]["current_tier"] == "unknown"
    assert snap["alerts"]["active_count"] == 0
    assert all(v == "unknown" for v in snap["service_health"].values())


# ===========================================================================
# Population — when accessors are live
# ===========================================================================


def test_alerts_populated_when_aggregator_returns_alerts(env, monkeypatch):
    from kora_cli.alerts.aggregator import Alert

    fake_alerts = [
        Alert(
            id="a", severity="critical", category="cost_ladder",
            title="t", detail="d", source_panel="cost",
            source_panel_route="/cost-state", first_seen_at="2026-05-23T10:00:00Z",
        ),
        Alert(
            id="b", severity="warning", category="cost_ladder",
            title="t2", detail="d2", source_panel="cost",
            source_panel_route="/cost-state", first_seen_at="2026-05-23T10:00:00Z",
        ),
        Alert(
            id="c", severity="info", category="operational_state",
            title="t3", detail="d3", source_panel="ops",
            source_panel_route="/operational-state", first_seen_at="2026-05-23T10:00:00Z",
        ),
    ]
    monkeypatch.setattr(
        "kora_cli.alerts.aggregator.compute_active_alerts",
        lambda: fake_alerts,
    )
    out = _collect_alerts()
    assert out["active_count"] == 3
    assert out["by_severity"]["critical"] == 1
    assert out["by_severity"]["warning"] == 1
    assert out["by_severity"]["info"] == 1
    assert out["by_category"]["cost_ladder"] == 2
    assert out["by_category"]["operational_state"] == 1


def test_cost_ladder_populated_from_holder(env, monkeypatch):
    from agent.cost_state_holder import CostRung

    fake_holder = MagicMock()
    fake_holder.active_rung.return_value = CostRung.WARN_75
    fake_holder.current_pct_used.return_value = 0.82
    monkeypatch.setattr(
        "agent.cost_state_holder.get_cost_holder",
        lambda: fake_holder,
    )
    cl = _collect_cost_ladder()
    assert cl["current_tier"] == "WARN_75"
    assert cl["monthly_budget_pct_used"] == 82.0


def test_operational_state_populated_when_holder_live(env, monkeypatch):
    from agent.operational_state import PrimaryState

    fake_state = MagicMock()
    fake_state.primary_state = PrimaryState.PAUSED
    fake_state.degradation_reasons = frozenset()
    fake_holder = MagicMock()
    fake_holder.current = fake_state
    monkeypatch.setattr(
        "agent.operational_state_holder.get_holder", lambda: fake_holder
    )
    op = _collect_operational_state()
    assert op["primary"] == "paused"
    assert op["paused"] is True


# ===========================================================================
# Tasks section (deferred per spec §4)
# ===========================================================================


def test_tasks_section_degraded_in_v1(env):
    """Spec §4: substrate MCP from 5-min cron deferred. Shape stays
    stable + values are 'unknown' placeholders."""
    snap = compute_snapshot()
    assert snap["tasks"] == {
        "open_count": "unknown",
        "in_progress_count": "unknown",
    }


# ===========================================================================
# Atomic write
# ===========================================================================


def test_write_snapshot_creates_target_file_and_parent_dir(env):
    snap = compute_snapshot()
    write_snapshot(snap)
    target = snapshot_path()
    assert target.exists()
    assert target.parent.name == "cache"
    loaded = json.loads(target.read_text())
    assert loaded == snap


def test_snapshot_path_respects_env_override(env, monkeypatch, tmp_path):
    override = tmp_path / "custom_snapshot.json"
    monkeypatch.setenv(SNAPSHOT_PATH_ENV, str(override))
    assert snapshot_path() == override


def test_snapshot_path_resolves_default_under_kora_home(env, tmp_path):
    p = snapshot_path()
    assert p == tmp_path / "cache" / "daemon_snapshot.json"


def test_write_snapshot_overwrites_atomically(env):
    """A second write replaces the first without leaving any
    intermediate state."""
    write_snapshot({"first": True, "schema_version": 1, "computed_at": "x"})
    write_snapshot({"second": True, "schema_version": 1, "computed_at": "y"})
    loaded = json.loads(snapshot_path().read_text())
    assert loaded.get("second") is True
    assert "first" not in loaded


# ===========================================================================
# Read + freshness
# ===========================================================================


def test_read_snapshot_returns_none_when_missing(env):
    assert read_snapshot() is None


def test_read_snapshot_returns_parsed_dict_when_fresh(env):
    write_snapshot(compute_snapshot())
    snap = read_snapshot()
    assert snap is not None
    assert snap["schema_version"] == SCHEMA_VERSION


def test_read_snapshot_returns_none_when_stale(env):
    # Hand-build a stale snapshot (computed_at 1 hour ago).
    stale = compute_snapshot()
    stale_ts = datetime.now(timezone.utc) - timedelta(hours=1)
    stale["computed_at"] = stale_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    write_snapshot(stale)
    assert read_snapshot() is None


def test_read_snapshot_returns_none_on_malformed_json(env):
    target = snapshot_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json")
    assert read_snapshot() is None


def test_read_snapshot_returns_none_on_non_dict_json(env):
    target = snapshot_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("[1, 2, 3]")
    assert read_snapshot() is None


def test_is_snapshot_fresh_recent_true():
    snap = {"computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    assert is_snapshot_fresh(snap) is True


def test_is_snapshot_fresh_old_false():
    old = datetime.now(timezone.utc) - timedelta(
        seconds=SNAPSHOT_FRESH_THRESHOLD_SECONDS + 60
    )
    snap = {"computed_at": old.strftime("%Y-%m-%dT%H:%M:%SZ")}
    assert is_snapshot_fresh(snap) is False


def test_is_snapshot_fresh_malformed_false():
    assert is_snapshot_fresh({"computed_at": "not-a-timestamp"}) is False
    assert is_snapshot_fresh({}) is False
    assert is_snapshot_fresh({"computed_at": None}) is False


def test_is_snapshot_fresh_just_inside_window():
    """Boundary: a snapshot 9 min old is fresh; 10+ min is stale."""
    fresh_ts = datetime.now(timezone.utc) - timedelta(minutes=9)
    snap = {"computed_at": fresh_ts.strftime("%Y-%m-%dT%H:%M:%SZ")}
    assert is_snapshot_fresh(snap) is True


# ===========================================================================
# Periodic-task entry
# ===========================================================================


@pytest.mark.asyncio
async def test_run_snapshot_cycle_writes_file_end_to_end(env):
    await run_snapshot_cycle()
    snap = read_snapshot()
    assert snap is not None
    assert snap["schema_version"] == SCHEMA_VERSION


@pytest.mark.asyncio
async def test_run_snapshot_cycle_compute_failure_logged_no_raise(
    env, monkeypatch, caplog
):
    def boom():
        raise RuntimeError("compute dead")

    monkeypatch.setattr(
        "kora_cli.snapshot.state_snapshot.compute_snapshot", boom
    )
    with caplog.at_level(logging.WARNING):
        await run_snapshot_cycle()
    assert any("compute_snapshot raised" in r.message for r in caplog.records)
    # No snapshot file written.
    assert not snapshot_path().exists()


@pytest.mark.asyncio
async def test_run_snapshot_cycle_write_failure_logged_no_raise(
    env, monkeypatch, caplog
):
    def boom(snapshot):
        raise OSError("disk full")

    monkeypatch.setattr(
        "kora_cli.snapshot.state_snapshot.write_snapshot", boom
    )
    with caplog.at_level(logging.WARNING):
        await run_snapshot_cycle()
    assert any("write_snapshot raised" in r.message for r in caplog.records)


# ===========================================================================
# Public convenience surface
# ===========================================================================


def test_get_snapshot_for_routing_returns_none_when_missing(env):
    assert get_snapshot_for_routing() is None


def test_get_snapshot_for_routing_returns_fresh_snapshot(env):
    write_snapshot(compute_snapshot())
    snap = get_snapshot_for_routing()
    assert snap is not None
    assert snap["schema_version"] == SCHEMA_VERSION


def test_get_snapshot_for_routing_returns_none_when_stale(env):
    stale = compute_snapshot()
    stale["computed_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_snapshot(stale)
    assert get_snapshot_for_routing() is None
