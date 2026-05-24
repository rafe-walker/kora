"""Source-pin + integration tests for KR-FE-PROBE-INVESTIGATION-VIEWER.

Backend endpoint /api/probe-investigations joins three sources:

  1. probe.wake_requested audit rows (PR #163 wake_emitter)
  2. reasoning.tool_called audit rows where caller_session_id ==
     "probe:{probe}:{category}" (PR #166 wired this caller_session
     shape via _derive_caller_session_id in anthropic_engine.py)
  3. snapshot.service_health[probe] (current health → resolution)

Tests:

  Backend
    1. caller_session_id pattern drift guard — endpoint's join key
       must match anthropic_engine._derive_caller_session_id exactly
    2. empty audit log → calm empty response (zero counts)
    3. window=24h filters out older wakes
    4. window=all returns everything
    5. window=7d works
    6. caller_session_id non-probe pattern ignored in join
    7. resolution_status maps health correctly
    8. wake without follow-on reasoning → investigation=null
    9. wake WITH follow-on reasoning → investigation populated
   10. limit cap obeyed (and clamped to 1-200)
   11. tool_calls chronologised (older first within investigation)
   12. errored tool calls flagged via any_errored
   13. exc_type carried through when present
   14. SECURITY: response does NOT echo raw tool input/output bodies

  FE source-pins
   15. api.getProbeInvestigations wrapper exists
   16. ProbeInvestigationsResponse type declared
   17. ProbeInvestigationItem fields declared (caller_session_id, etc)
   18. Page file exists + uses usePanelView
   19. Route registered in App.tsx
   20. Nav entry present
   21. Empty state rendered (calm message)
   22. v1_notes banner rendered (so deferred scope is visible)
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_API_TS = _REPO_ROOT / "web" / "src" / "lib" / "api.ts"
_APP_TSX = _REPO_ROOT / "web" / "src" / "App.tsx"
_PAGE = _REPO_ROOT / "web" / "src" / "pages" / "ProbeInvestigationsPage.tsx"
_WEB_SERVER = _REPO_ROOT / "kora_cli" / "web_server.py"
_ANTHROPIC_ENGINE = (
    _REPO_ROOT / "kora_cli" / "reasoning" / "anthropic_engine.py"
)


# ---------------------------------------------------------------------------
# Fixture: isolated KORA_HOME + fresh audit JSONL
# ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated KORA_HOME with the same 3-namespace monkeypatch the
    other panel tests use (see tests/kora_cli/_panel_test_helpers.py
    for the lesson-learned context; we duplicate inline here so the
    test stays readable on its own)."""
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


def _write_snapshot(env_dir: Path, payload: dict) -> None:
    """Write a snapshot file. Adds a fresh ``computed_at`` if
    missing — without it, ``is_snapshot_fresh`` returns False and
    ``read_snapshot()`` returns None, which silently breaks the
    service_health pickup."""
    if "computed_at" not in payload:
        payload = {
            **payload,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }
    snap_dir = env_dir / "cache"
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "daemon_snapshot.json").write_text(
        json.dumps(payload, default=str)
    )


def _wake_entry(
    probe: str,
    category: str,
    severity: str,
    title: str,
    detail: str,
    emitted_at: datetime,
    envelope_enabled: bool = False,
    envelope_fix_name: str = "(none)",
) -> dict:
    return {
        "emitted_at": emitted_at.isoformat(),
        "seam": "probe.wake_requested",
        "details": {
            "probe": probe,
            "severity": severity,
            "category": category,
            "title": title,
            "detail": detail,
            "snapshot_details": {},
            "envelope_enabled": envelope_enabled,
            "envelope_fix_name": envelope_fix_name,
        },
        "source": None,
        "caller_session_id": None,
    }


def _reasoning_entry(
    *,
    tool_name: str,
    caller_session_id: str,
    emitted_at: datetime,
    tool_duration_ms: int = 50,
    tool_status: str = "ok",
    exc_type: str | None = None,
) -> dict:
    details: dict = {
        "tool_name": tool_name,
        "triggered_by": "reasoning",
        "tool_duration_ms": tool_duration_ms,
        "tool_status": tool_status,
    }
    if exc_type is not None:
        details["exc_type"] = exc_type
    return {
        "emitted_at": emitted_at.isoformat(),
        "seam": "reasoning.tool_called",
        "details": details,
        "source": "reasoning",
        "caller_session_id": caller_session_id,
    }


async def _call_endpoint(
    env_dir: Path, window: str = "24h", limit: int = 50
) -> dict:
    """Call the endpoint as a coroutine, bypassing the auth
    middleware (which would 401 in a TestClient). Mirrors the
    pattern in tests/kora_cli/test_phrasebook_endpoints.py."""
    from kora_cli import web_server

    return await web_server.get_probe_investigations(
        window=window, limit=limit
    )


# ---------------------------------------------------------------------------
# 1. caller_session_id pattern drift guard
# ---------------------------------------------------------------------------


def test_caller_session_id_matches_reasoning_engine():
    """The endpoint joins reasoning.tool_called rows on
    caller_session_id == "probe:{probe}:{category}". That literal
    MUST match what anthropic_engine._derive_caller_session_id
    builds when message.source == "probe_investigation". Drift here
    silently breaks the panel (no rows joined; investigation
    always null).

    Source-of-truth: anthropic_engine.py:_derive_caller_session_id,
    probe_investigation branch (lines 1283-1289 at last K-DG)."""
    engine_src = _ANTHROPIC_ENGINE.read_text()
    # The engine builds f"probe:{probe}:{category}" — pin both the
    # f-string shape AND the metadata keys it pulls.
    assert re.search(
        r'return f"probe:\{probe\}:\{category\}"', engine_src
    ), "anthropic_engine._derive_caller_session_id probe shape changed"
    assert "probe_name" in engine_src and "issue_category" in engine_src
    # Endpoint side uses the same shape.
    ep_src = _WEB_SERVER.read_text()
    assert 'f"probe:{probe}:{category}"' in ep_src, (
        "endpoint join key drifted from engine"
    )
    # And the regex pre-filter in the endpoint matches probe: prefix.
    assert "_PROBE_CALLER_SESSION_RE" in ep_src
    assert r'r"^probe:([a-zA-Z0-9_-]+):([a-zA-Z0-9_-]+)$"' in ep_src


# ---------------------------------------------------------------------------
# 2-5. Empty / window-filter behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_audit_log_returns_calm_zero_response(env):
    """Empty state matters — PR #166 just landed, most envs see
    zero wakes for a while. Response must succeed + return zero
    counts + empty items (not error / 404 / null)."""
    _write_snapshot(env, {"service_health": {}})
    body = await _call_endpoint(env)
    assert body["total_count"] == 0
    assert body["items"] == []
    assert body["active_count"] == 0
    assert body["resolved_count"] == 0
    assert body["window"] == "24h"


@pytest.mark.asyncio
async def test_window_24h_filters_older_wakes(env):
    now = datetime.now(timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _wake_entry(
                "fly", "unhealthy", "critical", "in", "in window",
                now - timedelta(hours=2),
            ),
            _wake_entry(
                "fly", "unhealthy", "critical", "out", "out of window",
                now - timedelta(days=3),
            ),
        ],
    )
    _write_snapshot(env, {"service_health": {"fly": "unhealthy"}})
    body = await _call_endpoint(env, window="24h")
    assert body["total_count"] == 1
    assert body["items"][0]["title"] == "in"


@pytest.mark.asyncio
async def test_window_all_returns_everything(env):
    now = datetime.now(timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _wake_entry(
                "fly", "unhealthy", "critical", "recent", "x",
                now - timedelta(hours=1),
            ),
            _wake_entry(
                "fly", "unhealthy", "critical", "ancient", "x",
                now - timedelta(days=30),
            ),
        ],
    )
    _write_snapshot(env, {"service_health": {"fly": "unhealthy"}})
    body = await _call_endpoint(env, window="all")
    assert body["total_count"] == 2
    assert body["since"] is None


@pytest.mark.asyncio
async def test_window_7d_works(env):
    now = datetime.now(timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _wake_entry(
                "fly", "unhealthy", "critical", "5d", "x",
                now - timedelta(days=5),
            ),
            _wake_entry(
                "fly", "unhealthy", "critical", "8d", "x",
                now - timedelta(days=8),
            ),
        ],
    )
    _write_snapshot(env, {"service_health": {}})
    body = await _call_endpoint(env, window="7d")
    assert body["total_count"] == 1


# ---------------------------------------------------------------------------
# 6. Non-probe caller_session_id is ignored in the join
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_probe_caller_session_ids_ignored(env):
    """Other reasoning calls (slack_dm, email, mcp) write
    reasoning.tool_called rows with different caller_session_id
    shapes ("C123:1700.0", "email:msg-id", etc). The endpoint's
    pre-filter must drop those even though they're the same seam.
    """
    now = datetime.now(timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _wake_entry(
                "fly", "unhealthy", "critical", "t", "d",
                now - timedelta(minutes=10),
            ),
            # Slack DM reasoning call — must NOT be joined.
            _reasoning_entry(
                tool_name="snapshot_read",
                caller_session_id="C12345:1716489000.0",
                emitted_at=now - timedelta(minutes=8),
            ),
            # Email reasoning call.
            _reasoning_entry(
                tool_name="snapshot_read",
                caller_session_id="email:abc-123@purelymail",
                emitted_at=now - timedelta(minutes=7),
            ),
        ],
    )
    _write_snapshot(env, {"service_health": {"fly": "unhealthy"}})
    body = await _call_endpoint(env)
    assert body["total_count"] == 1
    # Investigation should be null because no probe:* keyed
    # reasoning rows were present.
    assert body["items"][0]["investigation"] is None


# ---------------------------------------------------------------------------
# 7. resolution_status maps health correctly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "current_health,expected_status",
    [
        ("healthy", "resolved"),
        ("unhealthy", "active"),
        ("degraded", "active"),
        ("unknown", "unknown"),
        ("", "unknown"),
    ],
)
@pytest.mark.asyncio
async def test_resolution_status_maps_from_current_health(
    env, current_health, expected_status
):
    now = datetime.now(timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _wake_entry(
                "fly", "unhealthy", "critical", "t", "d",
                now - timedelta(minutes=10),
            ),
        ],
    )
    _write_snapshot(env, {"service_health": {"fly": current_health}})
    body = await _call_endpoint(env)
    assert body["items"][0]["resolution_status"] == expected_status
    assert body["items"][0]["current_probe_health"] == current_health


# ---------------------------------------------------------------------------
# 8. Wake without follow-on reasoning → investigation=null
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wake_without_reasoning_keeps_investigation_null(env):
    """An engine_unavailable / cost_ladder_halted fallback can
    leave a wake with zero reasoning.tool_called rows — the panel
    must still surface the wake (the operator needs to know it
    fired) but show investigation=null so the FE can render
    "no calls recorded" without crashing on missing fields."""
    now = datetime.now(timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _wake_entry(
                "doppler", "rotation_overdue", "warning", "t", "d",
                now - timedelta(minutes=5),
            ),
        ],
    )
    _write_snapshot(env, {"service_health": {"doppler": "degraded"}})
    body = await _call_endpoint(env)
    assert body["items"][0]["investigation"] is None


# ---------------------------------------------------------------------------
# 9. Wake with follow-on reasoning → investigation populated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wake_with_reasoning_joins_correctly(env):
    now = datetime.now(timezone.utc)
    wake_ts = now - timedelta(minutes=15)
    _write_audit_jsonl(
        env,
        [
            _wake_entry(
                "fly", "unhealthy", "critical", "t", "d", wake_ts,
                envelope_enabled=True,
                envelope_fix_name="rotate_fly_token",
            ),
            _reasoning_entry(
                tool_name="snapshot_read",
                caller_session_id="probe:fly:unhealthy",
                emitted_at=wake_ts + timedelta(seconds=2),
                tool_duration_ms=80,
            ),
            _reasoning_entry(
                tool_name="run_runbook",
                caller_session_id="probe:fly:unhealthy",
                emitted_at=wake_ts + timedelta(seconds=5),
                tool_duration_ms=210,
            ),
        ],
    )
    _write_snapshot(env, {"service_health": {"fly": "healthy"}})
    body = await _call_endpoint(env)
    item = body["items"][0]
    assert item["caller_session_id"] == "probe:fly:unhealthy"
    inv = item["investigation"]
    assert inv is not None
    assert inv["call_count"] == 2
    assert inv["total_duration_ms"] == 290
    assert inv["any_errored"] is False
    # Resolution status: probe is healthy now → resolved.
    assert item["resolution_status"] == "resolved"
    # Envelope info echoed.
    assert item["envelope_enabled"] is True
    assert item["envelope_fix_name"] == "rotate_fly_token"


# ---------------------------------------------------------------------------
# 10. limit cap obeyed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_limit_caps_items(env):
    now = datetime.now(timezone.utc)
    entries = [
        _wake_entry(
            "fly", "unhealthy", "critical", f"t{i}", "d",
            now - timedelta(minutes=i),
        )
        for i in range(60)
    ]
    _write_audit_jsonl(env, entries)
    _write_snapshot(env, {"service_health": {"fly": "unhealthy"}})
    body = await _call_endpoint(env, limit=10)
    assert body["total_count"] == 10
    # Newest-first ordering pinned by the reader; the first wake
    # was 0 minutes ago, the last 59 minutes ago. limit=10 → 10
    # most recent.
    assert body["items"][0]["title"] == "t0"


@pytest.mark.asyncio
async def test_limit_clamped_to_max(env):
    """limit > 200 clamps to 200 (defensive against unbounded
    response sizes)."""
    _write_snapshot(env, {"service_health": {}})
    body = await _call_endpoint(env, limit=5000)
    assert body["total_count"] == 0  # no entries; just verifies no crash.


# ---------------------------------------------------------------------------
# 11. tool_calls chronologised within an investigation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_calls_chronologised(env):
    """The reader returns audit rows newest-first across all rows;
    within a single investigation the FE wants oldest-first so the
    operator can read "snapshot → diagnose → fix" left-to-right."""
    now = datetime.now(timezone.utc)
    wake_ts = now - timedelta(minutes=10)
    _write_audit_jsonl(
        env,
        [
            _wake_entry(
                "fly", "unhealthy", "critical", "t", "d", wake_ts,
            ),
            _reasoning_entry(
                tool_name="third",
                caller_session_id="probe:fly:unhealthy",
                emitted_at=wake_ts + timedelta(seconds=30),
            ),
            _reasoning_entry(
                tool_name="first",
                caller_session_id="probe:fly:unhealthy",
                emitted_at=wake_ts + timedelta(seconds=10),
            ),
            _reasoning_entry(
                tool_name="second",
                caller_session_id="probe:fly:unhealthy",
                emitted_at=wake_ts + timedelta(seconds=20),
            ),
        ],
    )
    _write_snapshot(env, {"service_health": {"fly": "unhealthy"}})
    body = await _call_endpoint(env)
    tool_names = [
        c["tool_name"] for c in body["items"][0]["investigation"]["tool_calls"]
    ]
    assert tool_names == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# 12-13. Errored tool calls flagged + exc_type carried
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_any_errored_flag_set_when_tool_failed(env):
    now = datetime.now(timezone.utc)
    wake_ts = now - timedelta(minutes=5)
    _write_audit_jsonl(
        env,
        [
            _wake_entry(
                "fly", "unhealthy", "critical", "t", "d", wake_ts,
            ),
            _reasoning_entry(
                tool_name="snapshot_read",
                caller_session_id="probe:fly:unhealthy",
                emitted_at=wake_ts + timedelta(seconds=1),
                tool_status="ok",
            ),
            _reasoning_entry(
                tool_name="run_runbook",
                caller_session_id="probe:fly:unhealthy",
                emitted_at=wake_ts + timedelta(seconds=3),
                tool_status="error",
                exc_type="HTTPError",
            ),
        ],
    )
    _write_snapshot(env, {"service_health": {"fly": "unhealthy"}})
    body = await _call_endpoint(env)
    inv = body["items"][0]["investigation"]
    assert inv["any_errored"] is True
    second = inv["tool_calls"][1]
    assert second["exc_type"] == "HTTPError"
    assert second["tool_status"] == "error"


# ---------------------------------------------------------------------------
# 14. SECURITY — response shape doesn't include tool input/output bodies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_response_does_not_leak_raw_tool_bodies(env):
    """Defense-in-depth: even though the writer (anthropic_engine.
    _emit_tool_called_audit) deliberately omits input/output bodies
    from the audit details dict, a future writer might add a
    'result' or 'arguments' field carelessly. This endpoint
    projects only the field names it expects so any future leak
    is contained.

    Test: inject a tool_called row with a hostile 'result' field;
    verify it doesn't appear in the response."""
    now = datetime.now(timezone.utc)
    wake_ts = now - timedelta(minutes=2)
    leak_payload = "SECRET_DOPPLER_TOKEN_xoxb-leak-12345"
    _write_audit_jsonl(
        env,
        [
            _wake_entry(
                "fly", "unhealthy", "critical", "t", "d", wake_ts,
            ),
            {
                "emitted_at": (
                    wake_ts + timedelta(seconds=1)
                ).isoformat(),
                "seam": "reasoning.tool_called",
                "details": {
                    "tool_name": "snapshot_read",
                    "triggered_by": "reasoning",
                    "tool_duration_ms": 10,
                    "tool_status": "ok",
                    # Future-writer leak vector — must NOT appear
                    # in the projected response.
                    "result": leak_payload,
                    "arguments": {"hostile": leak_payload},
                },
                "source": "reasoning",
                "caller_session_id": "probe:fly:unhealthy",
            },
        ],
    )
    _write_snapshot(env, {"service_health": {"fly": "unhealthy"}})
    body = await _call_endpoint(env)
    serialized = json.dumps(body)
    assert leak_payload not in serialized, (
        "endpoint projection regression — raw audit details leaked"
    )


# ---------------------------------------------------------------------------
# 15-17. FE source-pins: api wrapper + types
# ---------------------------------------------------------------------------


def test_api_wrapper_exists():
    src = _API_TS.read_text()
    assert "getProbeInvestigations" in src
    assert "/api/probe-investigations" in src


def test_response_type_declared():
    src = _API_TS.read_text()
    assert "ProbeInvestigationsResponse" in src
    assert "ProbeInvestigationItem" in src
    assert "ProbeReasoningToolCall" in src
    # ProbeResolutionStatus union — pinned values match backend.
    assert (
        'export type ProbeResolutionStatus = "resolved" | "active" | "unknown"'
        in src
    )


def test_item_fields_declared():
    src = _API_TS.read_text()
    # The fields the FE renders. If any are renamed in the backend
    # without updating the type, the page render breaks silently.
    for field in (
        "wake_event_id",
        "wake_timestamp",
        "probe_name",
        "issue_category",
        "severity",
        "caller_session_id",
        "investigation",
        "current_probe_health",
        "resolution_status",
        "envelope_enabled",
        "envelope_fix_name",
    ):
        assert field in src, f"missing field in TS type: {field}"


# ---------------------------------------------------------------------------
# 18-20. Page + route + nav pins
# ---------------------------------------------------------------------------


def test_page_exists_and_uses_panel_view():
    assert _PAGE.is_file()
    src = _PAGE.read_text()
    assert 'usePanelView("ProbeInvestigationsPage")' in src


def test_route_registered():
    src = _APP_TSX.read_text()
    assert "/probe-investigations" in src
    assert "ProbeInvestigationsPage" in src


def test_nav_entry_present():
    src = _APP_TSX.read_text()
    # Nav entry uses the i18n labelKey + a display label. The
    # `/probe-investigations` literal must appear in a NavItem
    # entry, not just the routes dict.
    nav_block = re.search(
        r'path:\s*"/probe-investigations"[^}]+labelKey:\s*"probeInvestigations"',
        src,
        re.DOTALL,
    )
    assert nav_block, "nav entry for /probe-investigations missing"


# ---------------------------------------------------------------------------
# 21. Empty state rendered (V2: V1NotesBanner removed — see test below)
# ---------------------------------------------------------------------------


def test_empty_state_message_in_page():
    """The spec calls out empty-state as critical — "calm
    everything's-healthy message, not an empty card list." The
    page must declare a dedicated empty-state component with
    reassuring copy, not just an empty <div>."""
    src = _PAGE.read_text()
    assert "EmptyState" in src
    # Both "everything's healthy" and "No probe wakes" must appear
    # so the reassurance copy is committed (regression guard
    # against someone later replacing it with "No data").
    assert "Everything's healthy" in src
    assert "No probe wakes" in src


# ---------------------------------------------------------------------------
# KR-FE-PROBE-INVESTIGATION-VIEWER-V2 — V1 banner removed + V2 surfaces
# ---------------------------------------------------------------------------


def test_v1_notes_banner_removed_in_v2():
    """PR #184 closed the 3 V1NotesBanner gaps BE-side (cost +
    model + dm_sent + autofix_attempted). KR-FE-PROBE-INVESTIGATION-
    VIEWER-V2 deletes the apology banner — the data is now live.
    Regression guard: a re-rendered V1NotesBanner JSX element would
    silently indicate someone reverted the V2 wiring. Comments
    mentioning V1NotesBanner by name (e.g. the V2 page docstring
    explaining what changed) are fine — only the JSX usage is
    the regression target."""
    src = _PAGE.read_text()
    assert "<V1NotesBanner" not in src, (
        "V1NotesBanner JSX reintroduced — V2 closed the 3 gaps; "
        "remove the banner. If you need to apologize for a NEW "
        "deferred field, write a V2NotesBanner instead."
    )
    assert "function V1NotesBanner" not in src, (
        "V1NotesBanner component reintroduced — see above"
    )


def test_dm_status_chip_filter_present():
    """V2 adds a dm_status chip-filter so the operator can triage
    failed_send first. Pin: the FilterChips reference + the
    DM_STATUS_CATEGORIES literal must exist in the page source."""
    src = _PAGE.read_text()
    assert "DM_STATUS_CATEGORIES" in src
    assert "PROBE_DM_STATUS_VALUES" in src
    # The 4 enum values must all appear as chip categories.
    for v in (
        "failed_send",
        "sent",
        "engine_unavailable_fallback",
        "engine_unavailable_failed_send",
    ):
        assert v in src, f"missing dm_status chip category: {v}"


def test_autofix_attempted_badge_in_page():
    """When investigation_completed.autofix_attempted=true the row
    surfaces a "🔧 fix attempted" badge so the operator can tell
    that a probe_autofix invocation rode along with the
    investigation."""
    src = _PAGE.read_text()
    assert "fix attempted" in src
    assert "autofix_attempted" in src


def test_dm_status_drift_guard():
    """dm_status values must match between BE projection allowlist
    (_DM_STATUS_VALUES in web_server.py) and FE constant
    (PROBE_DM_STATUS_VALUES in api.ts). Drift here silently
    breaks the dm_status chip-filter (chip clicks become no-ops
    against an unknown enum value)."""
    expected = {
        "sent",
        "failed_send",
        "engine_unavailable_fallback",
        "engine_unavailable_failed_send",
    }

    ws_src = _WEB_SERVER.read_text()
    m = re.search(
        r"_DM_STATUS_VALUES[^=]*=\s*\(([^)]+)\)",
        ws_src,
        re.DOTALL,
    )
    assert m is not None, "BE _DM_STATUS_VALUES tuple not found"
    be_values = set(re.findall(r'"(\w+)"', m.group(1)))
    assert be_values == expected, f"BE drift: {be_values}"

    fe_src = _API_TS.read_text()
    m = re.search(
        r"PROBE_DM_STATUS_VALUES[^=]*=\s*\[([^\]]+)\]",
        fe_src,
    )
    assert m is not None, "FE PROBE_DM_STATUS_VALUES not found"
    fe_values = set(re.findall(r'"(\w+)"', m.group(1)))
    assert fe_values == expected, f"FE drift: {fe_values}"


def test_v2_response_fields_declared():
    """The V2 response carries the per-investigation projection +
    DM entry projection — pin the FE TS types so a rename on either
    side surfaces at type-check time, not runtime."""
    src = _API_TS.read_text()
    for field in (
        "investigation_completed",
        "ProbeInvestigationCompleted",
        "ProbeInvestigationDmEntry",
        "dm_entry",
        "dm_status_values",
        "by_dm_status_24h",
    ):
        assert field in src, f"V2 missing FE field: {field}"
