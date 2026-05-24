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
    alert_id: str = "alert-abc-123",
) -> dict:
    """Mirror of the real #197 wake_consumer emission shape — drives
    the alert-investigations endpoint as the primary signal (alerts
    don't emit a separate alert.wake_requested row)."""
    return {
        "emitted_at": emitted_at.isoformat(),
        "seam": "alert.investigation_completed",
        "details": {
            "alert_id": alert_id,
            "category": category,
            "severity": severity,
            "model_used": "claude-haiku-4-5",
            "input_tokens": 1200,
            "output_tokens": 250,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 800,
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
async def test_completed_row_drives_item_listing(env):
    """KR-FE-ALERT-VIEWER-VERIFICATION: endpoint now drives off
    alert.investigation_completed (the primary signal — alerts
    don't emit a separate wake row). One completion → one item."""
    now = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _alert_completed(
                category="cost_anomaly",
                severity="critical",
                emitted_at=now,
                summary="Burn spike from a stuck cron retry loop.",
                autoaction_attempted=False,
                alert_id="alert-abc-123",
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
    assert ic["alert_id"] == "alert-abc-123"
    assert ic["dm_status"] == "sent"
    assert ic["autoaction_attempted"] is False
    assert "Burn spike" in ic["summary_text"]
    assert ic["model_used"] == "claude-haiku-4-5"
    assert ic["total_cost_usd"] == 0.0021
    assert ic["investigation_duration_ms"] == 2100
    # 24h aggregations.
    assert body["by_severity_24h"]["critical"] == 1
    assert body["by_dm_status_24h"]["sent"] == 1


@pytest.mark.asyncio
async def test_dm_entry_joined_by_session_id(env):
    now = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _alert_completed(
                category="cost_anomaly",
                severity="warning",
                emitted_at=now,
                summary="Investigated.",
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
        [_alert_completed(category="x", severity="critical", emitted_at=now)],
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


@pytest.mark.asyncio
async def test_real_197_payload_shape_renders_completely(env):
    """KR-FE-ALERT-VIEWER-VERIFICATION pin: a synthetic payload
    that matches the EXACT shape kora_cli/alerts/wake_consumer.py
    emits (post-#197) must round-trip through the endpoint with
    every operator-visible field populated. Pre-#197 the endpoint
    was driven off a forward-compat assumption (alert.wake_requested
    + title/detail fields); this test pins the new alignment so a
    regression to the speculative shape gets caught."""
    now = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
    # Mirror the exact payload kora_cli/alerts/wake_consumer.py
    # _emit_investigation_completed builds.
    real_shape = {
        "emitted_at": now.isoformat(),
        "seam": "alert.investigation_completed",
        "details": {
            "alert_id": "abc-def-uuid",
            "category": "cost_ladder",
            "severity": "warning",
            "model_used": "claude-haiku-4-5-20251001",
            "input_tokens": 1234,
            "output_tokens": 567,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 4096,
            "total_cost_usd": 0.0042,
            "investigation_duration_ms": 3800,
            "investigation_summary_text": (
                "Cost ladder reached tier=premium today — burn $42; "
                "if pace continues we hit credit-pool exhaustion in ~3 days."
            ),
            "dm_status": "sent",
            "autoaction_attempted": False,
        },
        "source": "reasoning",
        "caller_session_id": "alert:cost_ladder:warning",
    }
    _write_audit_jsonl(env, [real_shape])
    body = await _call()
    item = body["items"][0]
    ic = item["investigation_completed"]
    # Every field the FE renders.
    assert ic["alert_id"] == "abc-def-uuid"
    assert ic["model_used"] == "claude-haiku-4-5-20251001"
    assert ic["total_cost_usd"] == 0.0042
    assert ic["investigation_duration_ms"] == 3800
    assert ic["dm_status"] == "sent"
    assert ic["autoaction_attempted"] is False
    assert "Cost ladder" in ic["summary_text"]
    assert ic["reasoning_error"] is None
    # alert_category + severity come from the top-level item shape
    # (mirrors probe-investigations envelope structure).
    assert item["alert_category"] == "cost_ladder"
    assert item["severity"] == "warning"
    assert item["caller_session_id"] == "alert:cost_ladder:warning"


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
    """Both alert seams must remain in the SeamName Literal.
    ``alert.investigation_completed`` is the live emitter (#197);
    ``alert.wake_requested`` is kept as forward-compat for a future
    wake-emitter bucket (a parallel to the probe pattern where the
    cron post-hook writes wake rows separately from the consumer)."""
    sink_src = _JSONL_SINK.read_text()
    assert '"alert.wake_requested"' in sink_src
    assert '"alert.investigation_completed"' in sink_src


def test_endpoint_drives_off_completed_rows_not_wake():
    """KR-FE-ALERT-VIEWER-VERIFICATION: regression guard. The
    endpoint MUST iterate alert.investigation_completed rows as
    the primary signal. If the BE ever re-introduces a wake_rows
    iteration without first wiring an alert.wake_requested
    emitter, the panel returns [] for every install — a silent
    regression. Pin the iteration shape in source."""
    ws_src = _WEB_SERVER.read_text()
    # The endpoint's primary read must target the completed seam.
    assert (
        'seam="alert.investigation_completed"' in ws_src
    ), "endpoint must read alert.investigation_completed as primary signal"
    # And iterate over completed_rows for item construction.
    assert "for entry in completed_rows[:capped_limit]:" in ws_src, (
        "endpoint must iterate completed_rows for item construction "
        "(not wake_rows which alert path doesn't emit)"
    )


def test_drill_down_supports_alert_session_id_prefix():
    """KR-FE-INVESTIGATION-DRILL-DOWN must support alert: session
    ids. _DRILL_DOWN_SUPPORTED_SEAMS includes alert.investigation_
    completed (added in #198) so a drill-in from an alert row
    surfaces the audit trail + DM. Pin the inclusion + the
    /api/investigations/{sid} endpoint's session-id regex (or
    absence — endpoint accepts any literal match)."""
    ws_src = _WEB_SERVER.read_text()
    import re as _re

    m = _re.search(
        r"_DRILL_DOWN_SUPPORTED_SEAMS[^=]*=\s*\(([^)]+)\)",
        ws_src,
        _re.DOTALL,
    )
    assert m is not None, "_DRILL_DOWN_SUPPORTED_SEAMS tuple not found"
    seams = set(_re.findall(r'"([^"]+)"', m.group(1)))
    assert "alert.investigation_completed" in seams
    # Endpoint accepts any caller_session_id (the drill-down doesn't
    # enforce a prefix regex; it filters by literal match across
    # supported seams). Verify by checking the endpoint signature
    # accepts {caller_session_id:path}.
    assert "/api/investigations/{caller_session_id:path}" in ws_src


def test_alert_wake_consumer_emits_expected_payload_shape():
    """KR-FE-ALERT-VIEWER-VERIFICATION drift-guard: the FE
    AlertInvestigationCompleted type must mirror what
    kora_cli/alerts/wake_consumer.py actually emits. Walk the
    _emit_investigation_completed source for the details dict
    keys + assert every FE-rendered field has a corresponding
    BE-emitter source line."""
    wc_src = (
        _REPO_ROOT
        / "kora_cli"
        / "alerts"
        / "wake_consumer.py"
    ).read_text()
    api_src = _API_TS.read_text()
    # Each of these keys must (a) be in the BE emitter payload AND
    # (b) be a field in the FE type. Drift in either direction
    # surfaces here.
    for key in (
        "alert_id",
        "category",
        "severity",
        "model_used",
        "total_cost_usd",
        "investigation_duration_ms",
        "investigation_summary_text",
        "dm_status",
        "autoaction_attempted",
    ):
        assert f'"{key}"' in wc_src, (
            f"BE emitter source missing key {key!r} — verify "
            f"_emit_investigation_completed payload"
        )
    # FE type assertions (subset — the FE renames some keys for TS
    # ergonomics, e.g. ``investigation_summary_text`` → summary_text).
    for fe_field in (
        "alert_id",
        "summary_text",
        "model_used",
        "total_cost_usd",
        "investigation_duration_ms",
        "dm_status",
        "autoaction_attempted",
    ):
        assert fe_field in api_src, f"FE type missing field: {fe_field}"


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
        # KR-FE-ALERT-VIEWER-VERIFICATION — alert_id added after
        # checking the real #197 emission shape.
        "alert_id",
    ):
        assert f in src, f"missing FE field: {f}"


def test_fe_dropped_speculative_title_detail_fields():
    """KR-FE-ALERT-VIEWER-VERIFICATION: pre-#197 forward-compat
    speculated that the alert wake would carry ``title`` / ``detail``
    fields (mirror of probe.wake_requested). Real #197 emission
    has no such fields — the operator-facing context is the
    ``investigation_summary_text``. Pin removal so the speculative
    fields don't get re-introduced by a refactor."""
    src = _PAGE.read_text()
    # Substring search would match comments too — narrow to JSX
    # binding patterns (item.title / item.detail accessors).
    assert "item.title" not in src
    assert "item.detail" not in src


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
