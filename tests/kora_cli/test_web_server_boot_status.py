"""Tests for ``GET /api/boot-status`` (KR-P2-CLEANUP ST4).

Covers both branches after the stub → live flip:

  * **Empty-history branch** — no boot has completed this process
    lifetime. ``BootGateRunner._recent_history`` is empty;
    ``last_result()`` returns ``None``. The endpoint returns the
    stub-shape with ``stub: True`` + an ``error`` field.
  * **Live branch** — at least one boot has been recorded. Endpoint
    projects ``last_result()`` into ``current`` and the rest of
    ``recent_history(limit=20)`` into ``history`` (newest first).
    No ``stub`` flag.
"""

from datetime import datetime, timezone

import pytest

from agent.boot_coordinator import BootResult, BootSummary
from agent.boot_gates import (
    BootGateRunner,
    BootHistoryEntry,
    GateClass,
    GateOutcome,
    GateResult,
)


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


@pytest.fixture(autouse=True)
def _reset_boot_history():
    BootGateRunner._reset_history_for_tests()
    yield
    BootGateRunner._reset_history_for_tests()


# ---- Empty-history branch — stub + error ---------------------------------


@pytest.mark.asyncio
async def test_empty_history_returns_stub_with_error(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_boot_status()

    assert result["stub"] is True
    assert "error" in result
    assert "no boot has completed" in result["error"]
    assert set(result.keys()) >= {"current", "history", "stub", "error"}


@pytest.mark.asyncio
async def test_empty_history_stub_shape_matches_documented(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_boot_status()
    current = result["current"]
    assert set(current.keys()) >= {
        "boot_id", "primary_state", "started_at", "completed_at",
        "elapsed_ms", "outcome", "gates",
    }
    assert current["outcome"] in _VALID_BOOT_OUTCOME
    for gate in current["gates"]:
        assert set(gate.keys()) >= {
            "gate_id", "title", "gate_class", "outcome", "elapsed_ms", "detail",
        }
        assert gate["outcome"] in _VALID_GATE_OUTCOME
        assert gate["gate_class"] in _VALID_GATE_CLASS


# ---- Live branch — boot history populated --------------------------------


def _make_gate_result(
    *,
    gate_id: str = "1",
    gate_class: GateClass = GateClass.TRANSIENT,
    outcome: GateOutcome = GateOutcome.PASS,
    elapsed_ms: int = 412,
    detail: str = "claude auth status: ok",
    started_at: datetime = datetime(2026, 5, 21, 19, 30, 0, tzinfo=timezone.utc),
    completed_at: datetime = datetime(
        2026, 5, 21, 19, 30, 0, 412_000, tzinfo=timezone.utc
    ),
) -> GateResult:
    """Build a GateResult with the fields the panel projection cares
    about. ``title`` is intentionally NOT on GateResult (it's a
    ClassVar on the Gate class itself); the panel projection falls
    back to ``gate_id`` when no title can be resolved.

    ``started_at`` / ``completed_at`` are required by GateResult — the
    panel projection doesn't use them but the dataclass demands them.
    """
    return GateResult(
        gate_id=gate_id,
        gate_class=gate_class,
        outcome=outcome,
        elapsed_ms=elapsed_ms,
        detail=detail,
        started_at=started_at,
        completed_at=completed_at,
    )


def _make_entry(
    *,
    boot_id: str,
    started: datetime,
    completed: datetime,
    result: BootResult = BootResult.READY,
    gates: list[GateResult] | None = None,
    failed: GateResult | None = None,
) -> BootHistoryEntry:
    if gates is None:
        gates = [_make_gate_result()]
    return BootHistoryEntry(
        boot_id=boot_id,
        started_at=started,
        completed_at=completed,
        summary=BootSummary(
            result=result,
            gate_results=gates,
            failed_gate=failed,
        ),
    )


@pytest.mark.asyncio
async def test_live_branch_with_single_boot_renders_current_without_history(
    _isolate_config,
):
    from kora_cli import web_server

    BootGateRunner.record(
        _make_entry(
            boot_id="boot_a1b2c3d4",
            started=datetime(2026, 5, 21, 19, 30, 0, tzinfo=timezone.utc),
            completed=datetime(2026, 5, 21, 19, 30, 8, tzinfo=timezone.utc),
        )
    )

    result = await web_server.get_boot_status()

    # Live path — no stub flag.
    assert "stub" not in result
    assert "error" not in result
    # Current renders the recorded boot.
    assert result["current"]["boot_id"] == "boot_a1b2c3d4"
    assert result["current"]["outcome"] == "ready"
    assert result["current"]["elapsed_ms"] == 8000
    assert len(result["current"]["gates"]) == 1
    # No prior history yet — only the current.
    assert result["history"] == []


@pytest.mark.asyncio
async def test_live_branch_renders_history_newest_first(_isolate_config):
    from kora_cli import web_server

    # Record three boots — last is "current"; first two go to history.
    BootGateRunner.record(
        _make_entry(
            boot_id="boot_old",
            started=datetime(2026, 5, 21, 18, 0, 0, tzinfo=timezone.utc),
            completed=datetime(2026, 5, 21, 18, 0, 5, tzinfo=timezone.utc),
            result=BootResult.STOPPED,
            failed=_make_gate_result(
                outcome=GateOutcome.FAIL,
                detail="auth_validation_failed",
                gate_id="1",
            ),
        )
    )
    BootGateRunner.record(
        _make_entry(
            boot_id="boot_mid",
            started=datetime(2026, 5, 21, 18, 30, 0, tzinfo=timezone.utc),
            completed=datetime(2026, 5, 21, 18, 30, 4, tzinfo=timezone.utc),
        )
    )
    BootGateRunner.record(
        _make_entry(
            boot_id="boot_new",
            started=datetime(2026, 5, 21, 19, 0, 0, tzinfo=timezone.utc),
            completed=datetime(2026, 5, 21, 19, 0, 7, tzinfo=timezone.utc),
        )
    )

    result = await web_server.get_boot_status()

    # Current is the newest.
    assert result["current"]["boot_id"] == "boot_new"
    # History is newest-first (most-recent of the past at index 0).
    history = result["history"]
    assert [h["boot_id"] for h in history] == ["boot_mid", "boot_old"]
    # Failed entry surfaces the failure-detail fields.
    failed_entry = next(h for h in history if h["boot_id"] == "boot_old")
    assert failed_entry["outcome"] == "failed"
    assert failed_entry["failed_gate_id"] == "1"
    # GateResult has no title field; panel falls back to gate_id.
    assert failed_entry["failed_gate_title"] == "1"
    assert "auth_validation_failed" in failed_entry["detail"]


@pytest.mark.asyncio
async def test_recent_history_limit_caps_at_20(_isolate_config):
    """Sanity: the ring max enforced by collections.deque — record
    25 entries, verify only the last 20 are visible."""
    for i in range(25):
        BootGateRunner.record(
            _make_entry(
                boot_id=f"boot_{i:02d}",
                started=datetime(2026, 5, 21, 12, i, 0, tzinfo=timezone.utc),
                completed=datetime(2026, 5, 21, 12, i, 1, tzinfo=timezone.utc),
            )
        )
    all_history = BootGateRunner.recent_history()
    assert len(all_history) == 20
    # Oldest in the ring is boot_05 (boot_00..boot_04 dropped).
    assert all_history[0].boot_id == "boot_05"
    assert all_history[-1].boot_id == "boot_24"


# ---- BootGateRunner accessor unit tests ----------------------------------


def test_last_result_returns_none_when_history_empty():
    assert BootGateRunner.last_result() is None


def test_recent_history_returns_empty_list_when_history_empty():
    assert BootGateRunner.recent_history() == []


def test_recent_history_limit_zero_returns_empty():
    BootGateRunner.record(
        _make_entry(
            boot_id="x",
            started=datetime(2026, 5, 21, tzinfo=timezone.utc),
            completed=datetime(2026, 5, 21, tzinfo=timezone.utc),
        )
    )
    assert BootGateRunner.recent_history(limit=0) == []


# ---- Cron-regression sanity ----------------------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_boot_status_registered(_isolate_config):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
