"""Tests for the KR-CHEAP-PRE-WARMED-SNAPSHOT /api/snapshot endpoint.

Scenarios:
  1. Endpoint returns 200 + stale-flag JSON when no snapshot exists
  2. Endpoint returns the snapshot dict when fresh on disk
  3. Endpoint returns stale-flag when on-disk snapshot is older
     than the freshness window
  4. CC#2 #137 fixture-isolation discipline applied (3-namespace
     get_kora_home monkeypatch)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """3-namespace get_kora_home discipline per CC#2 #137."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setattr("kora_constants.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr("kora_cli.config.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr(
        "kora_cli.web_server.get_kora_home", lambda: tmp_path, raising=False
    )
    monkeypatch.delenv("KORA_SNAPSHOT_PATH", raising=False)
    return tmp_path


@pytest.mark.asyncio
async def test_endpoint_returns_stale_flag_when_no_snapshot(_isolate):
    from kora_cli import web_server

    result = await web_server.get_daemon_snapshot()
    assert result == {"error": "no_snapshot", "stale": True}


@pytest.mark.asyncio
async def test_endpoint_returns_snapshot_when_fresh(_isolate):
    from kora_cli import web_server
    from kora_cli.snapshot import compute_snapshot, write_snapshot

    write_snapshot(compute_snapshot())
    result = await web_server.get_daemon_snapshot()
    assert "error" not in result
    # KR-CHEAP-COST-TELEMETRY bumped schema v1 → v2 (added cost_telemetry);
    # later schema bumps (v3 cost-fields / v4 daemon_health /
    # v5 tasks-populated) keep cost_telemetry present.
    assert result["schema_version"] >= 2
    assert "computed_at" in result
    assert "operational_state" in result
    assert "alerts" in result
    assert "cost_ladder" in result
    assert "service_health" in result
    assert "cost_telemetry" in result


@pytest.mark.asyncio
async def test_endpoint_returns_stale_flag_when_snapshot_old(_isolate):
    from kora_cli import web_server
    from kora_cli.snapshot import compute_snapshot, write_snapshot

    stale = compute_snapshot()
    stale_ts = datetime.now(timezone.utc) - timedelta(hours=1)
    stale["computed_at"] = stale_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    write_snapshot(stale)

    result = await web_server.get_daemon_snapshot()
    assert result == {"error": "no_snapshot", "stale": True}
