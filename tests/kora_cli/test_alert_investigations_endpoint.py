"""KR-FE-ALERT-INVESTIGATIONS-VIEWER (forward-compat #420) tests.

Endpoint /api/alert-investigations mirrors /api/probe-investigations:

  * Reads alert.wake_requested + alert.investigation_completed audit
    rows (both added to SeamName Literal in this bucket as
    forward-compat)
  * Joins slack_dm_log.jsonl outbound entries by caller_session_id
    ``alert:{category}:{severity}``
  * Returns empty cleanly when none of the seams have any rows
    (the CC#1 #420 forward-compat case)

Plus FE source-pins: page exists + uses panel-view + route registered
+ nav entry + deep-links to InvestigationDrillDown.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_API_TS = _REPO_ROOT / "web" / "src" / "lib" / "api.ts"
_APP_TSX = _REPO_ROOT / "web" / "src" / "App.tsx"
_PAGE = _REPO_ROOT / "web" / "src" / "pages" / "AlertInvestigationsPage.tsx"
_WEB_SERVER = _REPO_ROOT / "kora_cli" / "web_server.py"
_JSONL_SINK = _REPO_ROOT / "kora_cli" / "audit" / "jsonl_sink.py"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setattr("kora_constants.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr("kora_cli.config.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr("kora_cli.web_server.get_kora_home", lambda: tmp_path)
    return tmp_path


def _write_audit_jsonl(env_dir: Path, entries: list[dict]) -> None:
    log_path = env_dir / "kora_audit_log.jsonl"
    with log_path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, default=str) + "\n")


def _write_slack_dm_log(env_dir: Path, entries: list[dict]) -> None:
    log_path = env_dir / "slack_dm_log.jsonl"
    with log_path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, default=str) + "\n")


def _alert_wake(
    *,
    category: str,
    severity: str,
    emitted_at: datetime,
    title: str = "",
    detail: str = "",
) -> dict:
    return {
        "emitted_at": emitted_at.isoformat(),
        "seam": "alert.wake_requested",
        "details": {
            "category": category,
            "severity": severity,
            "title": title,
            "detail": detail,
        },
        "source": "cron",
        "caller_session_id": None,
    }


def _alert_completed(
    *,
    category: str,
    severity: str,
    emitted_at: datetime,
    dm_status: str = "sent",
    autoaction_attempted: bool = False,
    summary: str = "",
) -> dict:
    return {
        "emitted_at": emitted_at.isoformat(),
        "seam": "alert.investigation_completed",
        "details": {
            "category": category,
            "severity": severity,
            "model_used": "claude-haiku-4-5",
            "total_cost_usd": 0.0021,
            "investigation_duration_ms": 2100,
            "dm_status": dm_status,
            "autoaction_attempted": autoaction_attempted,
            "investigation_summary_text": summary,
        },
        "source": "reasoning",
        "caller_session_id": f"alert:{category}:{severity}",
    }


async def _call(window: str = "24h", limit: int = 50) -> dict:
    from kora_cli import web_server

    return await web_server.get_alert_investigations(
        window=window, limit=limit
    )


@pytest.mark.asyncio
async def test_empty_when_no_alert_seams(env):
    """The forward-compat case: until #420 ships, the alert seams
    have zero rows. Endpoint must return ``items=[]`` cleanly so
    the FE renders an empty state rather than 404."""
    body = await _call()
    assert body["items"] == []
    assert body["total_count"] == 0
    assert "dm_status_values" in body
    # Per-severity + per-dm-status counts all zero, but the keys
    # exist so the FE filter chips render with 0 counts.
    assert set(body["by_severity_24h"].keys()) == {
        "critical",
        "warning",
        "info",
    }
    assert all(v == 0 for v in body["by_severity_24h"].values())


@pytest.mark.asyncio
async def test_join_wake_and_completed_by_session(env):
    now = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _alert_wake(
                category="cost_anomaly",
                severity="critical",
                emitted_at=now,
                title="Daily burn 3x baseline",
                detail="Today: $42; baseline: $14",
            ),
            _alert_completed(
                category="cost_anomaly",
                severity="critical",
                emitted_at=now,
                summary="Burn spike from a stuck cron retry loop.",
                autoaction_attempted=False,
            ),
        ],
    )
    body = await _call()
    assert body["total_count"] == 1
    item = body["items"][0]
    assert item["alert_category"] == "cost_anomaly"
    assert item["severity"] == "critical"
    assert item["caller_session_id"] == "alert:cost_anomaly:critical"
    assert item["investigation_completed"] is not None
    ic = item["investigation_completed"]
    assert ic["dm_status"] == "sent"
    assert ic["autoaction_attempted"] is False
    assert "Burn spike" in ic["summary_text"]
    # 24h aggregations.
    assert body["by_severity_24h"]["critical"] == 1
    assert body["by_dm_status_24h"]["sent"] == 1


@pytest.mark.asyncio
async def test_dm_entry_joined_by_session_id(env):
    now = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _alert_wake(
                category="cost_anomaly",
                severity="warning",
                emitted_at=now,
            ),
        ],
    )
    _write_slack_dm_log(
        env,
        [
            {
                "sent_at": "2026-05-24T12:00:05Z",
                "channel_id": "D01J",
                "thread_ts": None,
                "text": "alert dm body (not echoed)",
                "slack_message_ts": "1742345200.000",
                "send_status": "ok",
                "caller_session_id": "alert:cost_anomaly:warning",
            },
        ],
    )
    body = await _call()
    item = body["items"][0]
    assert item["dm_entry"] is not None
    assert item["dm_entry"]["channel_id"] == "D01J"
    assert item["dm_entry"]["send_status"] == "ok"
    # Privacy contract: no message text echoed.
    assert "alert dm body" not in json.dumps(body)


@pytest.mark.asyncio
async def test_non_alert_session_ignored(env):
    """A slack_dm_log row whose caller_session_id is shaped like a
    probe (probe:foo:bar) must NOT join into an alert investigation
    even if the timestamps line up."""
    now = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
    _write_audit_jsonl(
        env,
        [_alert_wake(category="x", severity="critical", emitted_at=now)],
    )
    _write_slack_dm_log(
        env,
        [
            {
                "sent_at": "2026-05-24T12:00:05Z",
                "channel_id": "D01J",
                "thread_ts": None,
                "text": "probe-shaped dm",
                "slack_message_ts": "1742345200.000",
                "send_status": "ok",
                "caller_session_id": "probe:fly:service_unhealthy",
            },
        ],
    )
    body = await _call()
    assert body["items"][0]["dm_entry"] is None


# ---------------------------------------------------------------------------
# Drift guard: alert dm_status reuses probe values
# ---------------------------------------------------------------------------


def test_alert_dm_status_drift_guard():
    """_ALERT_DM_STATUS_VALUES is an alias for _DM_STATUS_VALUES —
    pin that they remain identical until alert dm_status diverges
    from probe dm_status (no concrete reason to today; alert wake
    consumer reuses the probe DM dispatch path)."""
    ws_src = _WEB_SERVER.read_text()
    assert "_ALERT_DM_STATUS_VALUES: Tuple[str, ...] = _DM_STATUS_VALUES" in ws_src


def test_alert_seams_in_seamname_literal():
    """Both alert seams must be in the SeamName Literal so
    read_audit_entries doesn't ValidationError on them once #420
    starts emitting. Forward-compat addition lives in this
    bucket; the emitter lands with #420."""
    sink_src = _JSONL_SINK.read_text()
    assert '"alert.wake_requested"' in sink_src
    assert '"alert.investigation_completed"' in sink_src


# ---------------------------------------------------------------------------
# FE source-pins
# ---------------------------------------------------------------------------


def test_fe_api_wrapper_exists():
    src = _API_TS.read_text()
    assert "getAlertInvestigations" in src
    assert "/api/alert-investigations" in src


def test_fe_response_type_declared():
    src = _API_TS.read_text()
    for f in (
        "AlertInvestigationsResponse",
        "AlertInvestigationItem",
        "AlertInvestigationCompleted",
        "alert_category",
        "autoaction_attempted",
    ):
        assert f in src, f"missing FE field: {f}"


def test_alert_investigations_page_exists():
    assert _PAGE.is_file()
    src = _PAGE.read_text()
    assert 'usePanelView("AlertInvestigationsPage")' in src


def test_route_registered():
    src = _APP_TSX.read_text()
    assert "/alert-investigations" in src
    assert "AlertInvestigationsPage" in src


def test_nav_entry_present():
    src = _APP_TSX.read_text()
    nav_block = re.search(
        r'path:\s*"/alert-investigations"[^}]+labelKey:\s*"alertInvestigations"',
        src,
        re.DOTALL,
    )
    assert nav_block, "nav entry for /alert-investigations missing"


def test_alert_card_links_to_drill_down():
    """Each alert investigation card must offer a drill-in link to
    /investigations/<caller_session_id> (mirror of probe variant)."""
    src = _PAGE.read_text()
    assert (
        "/investigations/${encodeURIComponent(item.caller_session_id)}"
        in src
    )


def test_drill_down_allowlist_includes_alert_seams():
    """KR-FE-INVESTIGATION-DRILL-DOWN's _DRILL_DOWN_SUPPORTED_SEAMS
    must include both alert seams so an alert.investigation drill
    surfaces the wake + completed rows once #420 starts emitting."""
    ws_src = _WEB_SERVER.read_text()
    m = re.search(
        r"_DRILL_DOWN_SUPPORTED_SEAMS[^=]*=\s*\(([^)]+)\)",
        ws_src,
        re.DOTALL,
    )
    assert m is not None
    seams = set(re.findall(r'"([^"]+)"', m.group(1)))
    assert "alert.wake_requested" in seams
    assert "alert.investigation_completed" in seams
