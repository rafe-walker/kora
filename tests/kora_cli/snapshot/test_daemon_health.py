"""Tests for KR-SNAPSHOT-DAEMON-HEALTH — snapshot.daemon_health section.

Spec §2 scenarios:
  Shape:
   - daemon_health has 5 expected keys
   - per-listener entries have status / last_event_at /
     consecutive_errors

  overall_status derivation:
   - all up + 0 errors → healthy
   - 1 listener down → degraded
   - 2 listeners down → degraded
   - 3+ listeners down → unhealthy
   - 5 errors in window → degraded
   - 20+ errors in window → unhealthy
   - all listeners unknown → overall_status="unknown"
   - no coordinator → "unknown" + empty listeners + audit count
     still populates

  boot_at / uptime:
   - coordinator reports boot_at → ISO Z; uptime float
   - coordinator still booting → both "unknown"

  recent_error_count_5min:
   - 0 entries → 0
   - dead_letter + reply_failed counted
   - notification.dispatched only counted when status="failed"
   - success-path seams ignored
   - audit file missing → 0 (fail-soft)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from kora_cli.snapshot.state_snapshot import (
    SCHEMA_VERSION,
    _collect_daemon_health,
    _count_recent_audit_errors,
    _derive_overall_status,
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated KORA_HOME so the audit reader can't read the host's
    real audit log."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setattr("kora_constants.get_kora_home", lambda: tmp_path)
    monkeypatch.delenv("KORA_AUDIT_LOG_PATH", raising=False)
    return tmp_path


# ===========================================================================
# Schema
# ===========================================================================


def test_schema_version_minimum_v4_for_daemon_health():
    """Daemon health section ships at v4; subsequent bumps (v5
    KR-SNAPSHOT-TASKS) must not regress this section's presence."""
    assert SCHEMA_VERSION >= 4


# ===========================================================================
# _derive_overall_status — thresholds
# ===========================================================================


def _mk_listeners(up_count: int, down_count: int = 0) -> dict:
    out = {}
    for i in range(up_count):
        out[f"up_{i}"] = {"status": "up"}
    for i in range(down_count):
        out[f"down_{i}"] = {"status": "down"}
    return out


def test_overall_status_all_up_zero_errors_is_healthy():
    listeners = _mk_listeners(up_count=14)
    assert _derive_overall_status(listeners, 0) == "healthy"


def test_overall_status_one_down_is_degraded():
    listeners = _mk_listeners(up_count=13, down_count=1)
    assert _derive_overall_status(listeners, 0) == "degraded"


def test_overall_status_two_down_is_degraded():
    listeners = _mk_listeners(up_count=12, down_count=2)
    assert _derive_overall_status(listeners, 0) == "degraded"


def test_overall_status_three_down_is_unhealthy():
    listeners = _mk_listeners(up_count=11, down_count=3)
    assert _derive_overall_status(listeners, 0) == "unhealthy"


def test_overall_status_errors_5_is_degraded():
    listeners = _mk_listeners(up_count=14)
    assert _derive_overall_status(listeners, 5) == "degraded"


def test_overall_status_errors_19_is_degraded():
    listeners = _mk_listeners(up_count=14)
    assert _derive_overall_status(listeners, 19) == "degraded"


def test_overall_status_errors_20_is_unhealthy():
    listeners = _mk_listeners(up_count=14)
    assert _derive_overall_status(listeners, 20) == "unhealthy"


def test_overall_status_all_unknown_yields_unknown():
    listeners = {
        "a": {"status": "unknown"},
        "b": {"status": "unknown"},
    }
    assert _derive_overall_status(listeners, 0) == "unknown"


def test_overall_status_empty_listeners_yields_unknown():
    assert _derive_overall_status({}, 0) == "unknown"


def test_overall_status_unhealthy_wins_over_degraded():
    listeners = _mk_listeners(up_count=11, down_count=3)
    # 3 down → unhealthy regardless of error count being in degraded
    # band.
    assert _derive_overall_status(listeners, 5) == "unhealthy"


# ===========================================================================
# _collect_daemon_health — coordinator unavailable
# ===========================================================================


def test_daemon_health_no_coordinator_fully_degraded(env, monkeypatch):
    """When no daemon is live, the snapshot still produces the
    section — overall_status=unknown, empty listeners, but the
    audit count still resolves (file-based, not coordinator-based)."""
    monkeypatch.setattr(
        "kora_cli.daemon.current_coordinator", lambda: None
    )
    dh = _collect_daemon_health()
    assert dh["overall_status"] == "unknown"
    assert dh["boot_at"] == "unknown"
    assert dh["uptime_seconds"] == "unknown"
    assert dh["listeners"] == {}
    # recent_error_count_5min populated independent of coordinator.
    assert isinstance(dh["recent_error_count_5min"], int)


def test_daemon_health_coordinator_raises_degrades(env, monkeypatch):
    def kaboom():
        raise RuntimeError("coord kaboom")

    monkeypatch.setattr(
        "kora_cli.daemon.current_coordinator", kaboom
    )
    dh = _collect_daemon_health()
    assert dh["overall_status"] == "unknown"
    assert dh["boot_at"] == "unknown"


# ===========================================================================
# _collect_daemon_health — coordinator wired
# ===========================================================================


def _fake_coord(
    *,
    listeners,
    uptime_seconds=14400.0,
    boot_at=None,
) -> MagicMock:
    coord = MagicMock()
    coord.get_boot_at.return_value = boot_at
    coord.get_status.return_value = {
        "state": "running",
        "uptime_seconds": uptime_seconds,
        "shutdown_reason": None,
        "listeners": listeners,
        "daemon_session_id": "deadbeef",
    }
    return coord


def test_daemon_health_all_listeners_up_is_healthy(env, monkeypatch):
    listener_rows = [
        {"name": "snapshot", "started": True, "shutdown_timeout_seconds": 5.0},
        {"name": "heartbeat", "started": True, "shutdown_timeout_seconds": 5.0},
        {"name": "mcp", "started": True, "shutdown_timeout_seconds": 5.0},
    ]
    boot = datetime(2026, 5, 23, 10, 0, 0, tzinfo=timezone.utc)
    coord = _fake_coord(listeners=listener_rows, uptime_seconds=14400.0, boot_at=boot)
    monkeypatch.setattr("kora_cli.daemon.current_coordinator", lambda: coord)
    dh = _collect_daemon_health()
    assert dh["overall_status"] == "healthy"
    assert dh["boot_at"] == "2026-05-23T10:00:00Z"
    assert dh["uptime_seconds"] == 14400.0
    assert dh["listeners"]["snapshot"]["status"] == "up"
    assert dh["listeners"]["mcp"]["status"] == "up"
    # v1 — per-listener event tracking is a follow-on bucket.
    assert dh["listeners"]["snapshot"]["last_event_at"] == "unknown"
    assert dh["listeners"]["snapshot"]["consecutive_errors"] == "unknown"


def test_daemon_health_some_listeners_down_is_degraded(env, monkeypatch):
    listener_rows = [
        {"name": "a", "started": True, "shutdown_timeout_seconds": 5.0},
        {"name": "b", "started": True, "shutdown_timeout_seconds": 5.0},
        {"name": "c", "started": False, "shutdown_timeout_seconds": 5.0},
    ]
    coord = _fake_coord(
        listeners=listener_rows,
        boot_at=datetime(2026, 5, 23, 10, 0, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("kora_cli.daemon.current_coordinator", lambda: coord)
    dh = _collect_daemon_health()
    assert dh["overall_status"] == "degraded"
    assert dh["listeners"]["c"]["status"] == "down"


def test_daemon_health_three_down_is_unhealthy(env, monkeypatch):
    listener_rows = [
        {"name": "a", "started": True, "shutdown_timeout_seconds": 5.0},
        {"name": "b", "started": False, "shutdown_timeout_seconds": 5.0},
        {"name": "c", "started": False, "shutdown_timeout_seconds": 5.0},
        {"name": "d", "started": False, "shutdown_timeout_seconds": 5.0},
    ]
    coord = _fake_coord(
        listeners=listener_rows,
        boot_at=datetime(2026, 5, 23, 10, 0, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("kora_cli.daemon.current_coordinator", lambda: coord)
    dh = _collect_daemon_health()
    assert dh["overall_status"] == "unhealthy"


def test_daemon_health_booting_no_boot_at_yet(env, monkeypatch):
    """Coordinator constructed but startup not yet complete — boot_at
    is None, uptime is None."""
    coord = MagicMock()
    coord.get_boot_at.return_value = None
    coord.get_status.return_value = {
        "state": "booting",
        "uptime_seconds": None,
        "shutdown_reason": None,
        "listeners": [],
        "daemon_session_id": "deadbeef",
    }
    monkeypatch.setattr("kora_cli.daemon.current_coordinator", lambda: coord)
    dh = _collect_daemon_health()
    assert dh["boot_at"] == "unknown"
    assert dh["uptime_seconds"] == "unknown"
    # No listeners + no errors → still unknown (no signal).
    assert dh["overall_status"] == "unknown"


# ===========================================================================
# _count_recent_audit_errors — audit JSONL tail
# ===========================================================================


def _write_audit_lines(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_recent_error_count_missing_file_returns_zero(env):
    # No audit file written → fail-soft 0.
    assert _count_recent_audit_errors() == 0


def test_recent_error_count_counts_failure_seams(env):
    import json

    audit_path = env / "kora_audit_log.jsonl"
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(seconds=60)).isoformat()
    lines = [
        json.dumps(
            {
                "emitted_at": fresh,
                "seam": "webhook.dead_letter",
                "details": {"reason": "timeout"},
            }
        ),
        json.dumps(
            {
                "emitted_at": fresh,
                "seam": "slack_dm.reply_failed",
                "details": {"reason": "channel gone"},
            }
        ),
        # Success-path seam — should not count.
        json.dumps(
            {
                "emitted_at": fresh,
                "seam": "mcp.tool_called",
                "details": {"tool_kind": "read"},
            }
        ),
    ]
    _write_audit_lines(audit_path, lines)
    assert _count_recent_audit_errors() == 2


def test_recent_error_count_notification_only_when_failed(env):
    import json

    audit_path = env / "kora_audit_log.jsonl"
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(seconds=30)).isoformat()
    lines = [
        json.dumps(
            {
                "emitted_at": fresh,
                "seam": "notification.dispatched",
                "details": {"status": "delivered"},
            }
        ),
        json.dumps(
            {
                "emitted_at": fresh,
                "seam": "notification.dispatched",
                "details": {"status": "failed"},
            }
        ),
        json.dumps(
            {
                "emitted_at": fresh,
                "seam": "notification.dispatched",
                "details": {"status": "FAILED"},
            }
        ),
    ]
    _write_audit_lines(audit_path, lines)
    # Only the two failed entries (case-insensitive) count.
    assert _count_recent_audit_errors() == 2


def test_recent_error_count_excludes_old_entries(env):
    import json

    audit_path = env / "kora_audit_log.jsonl"
    now = datetime.now(timezone.utc)
    old = (now - timedelta(seconds=600)).isoformat()  # 10 min ago
    fresh = (now - timedelta(seconds=30)).isoformat()
    lines = [
        json.dumps(
            {
                "emitted_at": old,
                "seam": "webhook.dead_letter",
                "details": {},
            }
        ),
        json.dumps(
            {
                "emitted_at": fresh,
                "seam": "webhook.dead_letter",
                "details": {},
            }
        ),
    ]
    _write_audit_lines(audit_path, lines)
    # Only the fresh entry; the 10-min-old is outside the 5-min window.
    assert _count_recent_audit_errors() == 1


def test_recent_error_count_7_errors_returns_7(env):
    import json

    audit_path = env / "kora_audit_log.jsonl"
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(seconds=60)).isoformat()
    lines = [
        json.dumps(
            {
                "emitted_at": fresh,
                "seam": "webhook.dead_letter",
                "details": {},
            }
        )
        for _ in range(7)
    ]
    _write_audit_lines(audit_path, lines)
    assert _count_recent_audit_errors() == 7
