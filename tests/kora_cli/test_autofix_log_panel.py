"""KR-FE-AUTOFIX-LOG-PANEL — backend + FE source-pin tests.

3rd consumer of AuditPanelKit. Symmetric pattern to PR #180/#183.

Tests:

  Backend (12):
    1. Endpoint registered
    2. Empty audit → calm zero response
    3. by_status_24h initialized with all 3 statuses + unknown
    4. attempted: projected with executor_duration_ms +
       before_state_label + after_state_label + action_canonical
    5. rejected: projected with rejection_reason + truncated
       rejection_detail
    6. execution_failed: projected with error + before_state_label
       (executor sometimes records before-state even on failure)
    7. Unknown status coerced to "unknown"
    8. Daily-attempted sparkline math (14 buckets [today-13, today])
    9. 24h cutoff drops older counts but keeps in items list
   10. SECURITY: before/after state dicts NOT propagated whole —
       only `state` field exposed as before_state_label /
       after_state_label
   11. SECURITY: arbitrary details fields don't leak through
       projection
   12. reason_from_reasoning truncated to 300 chars

  Drift guard (1):
   13. STATUS values match across THREE sources:
       (a) BE projection _PROBE_AUTOFIX_STATUS_VALUES
       (b) BE emitter STATUS_* constants in
           kora_cli/tools/probe_autofix.py
       (c) FE constant PROBE_AUTOFIX_STATUS_VALUES in
           web/src/lib/api.ts

  FE source-pins (8):
   14. api.getProbeAutofixRecent wrapper exists
   15. ProbeAutofixEvent + Response + Status types declared
   16. PROBE_AUTOFIX_STATUS_VALUES FE constant exported
   17. AutofixLogPage exists + uses usePanelView + uses AuditPanelKit
   18. Route + nav entry registered
   19. PROBE_AUTOFIX_CATEGORIES used by FilterChips
   20. Empty state copy committed
   21. SECURITY: page does NOT reference event.before_state or
       event.after_state as nested-dict reads (only the
       _label fields the projection exposes)
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import pytest

from tests.kora_cli._panel_test_helpers import isolated_kora_home


_REPO_ROOT = Path(__file__).resolve().parents[2]
_API_TS = _REPO_ROOT / "web" / "src" / "lib" / "api.ts"
_APP_TSX = _REPO_ROOT / "web" / "src" / "App.tsx"
_PAGE = _REPO_ROOT / "web" / "src" / "pages" / "AutofixLogPage.tsx"
_WEB_SERVER = _REPO_ROOT / "kora_cli" / "web_server.py"
_TOOL_PY = _REPO_ROOT / "kora_cli" / "tools" / "probe_autofix.py"


@pytest.fixture
def env(tmp_path, monkeypatch):
    return isolated_kora_home(tmp_path, monkeypatch)


def _write_audit_jsonl(env_dir: Path, entries: list) -> None:
    log_path = env_dir / "kora_audit_log.jsonl"
    with log_path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, default=str) + "\n")


def _autofix_entry(
    status: str,
    *,
    emitted_at: datetime,
    probe: str = "fly",
    action: str = "restart_machine",
    action_canonical: str | None = None,
    target_id: str = "1781e9f6c12d83",
    reason: str = "Machine unhealthy for 4 ticks; envelope allows restart.",
    executor_duration_ms: int | None = None,
    before_state: dict | None = None,
    after_state: dict | None = None,
    rejection_reason: str | None = None,
    rejection_detail: dict | None = None,
    error: str | None = None,
    extra: dict | None = None,
    caller_session_id: str = "engine-session-x",
) -> dict:
    details: Dict[str, Any] = {
        "status": status,
        "probe": probe,
        "action": action,
        "target_id": target_id,
        "reason_from_reasoning": reason,
    }
    if action_canonical is not None:
        details["action_canonical"] = action_canonical
    if executor_duration_ms is not None:
        details["executor_duration_ms"] = executor_duration_ms
    if before_state is not None:
        details["before_state"] = before_state
    if after_state is not None:
        details["after_state"] = after_state
    if rejection_reason is not None:
        details["rejection_reason"] = rejection_reason
    if rejection_detail is not None:
        details["rejection_detail"] = rejection_detail
    if error is not None:
        details["error"] = error
    if extra is not None:
        details.update(extra)
    return {
        "emitted_at": emitted_at.isoformat(),
        "seam": "tool.probe_autofix_attempted",
        "details": details,
        "source": "reasoning",
        "caller_session_id": caller_session_id,
    }


async def _call_endpoint(env_dir: Path, limit: int = 100) -> dict:
    from kora_cli import web_server

    return await web_server.list_recent_probe_autofix(limit=limit)


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


def test_endpoint_registered():
    from kora_cli import web_server

    paths = {getattr(r, "path", None) for r in web_server.app.routes}
    assert "/api/probe-autofix/recent" in paths


@pytest.mark.asyncio
async def test_empty_audit_returns_calm_zero_response(env):
    body = await _call_endpoint(env)
    assert body["events"] == []
    assert body["total_recent_24h"] == 0
    for status in ("attempted", "rejected", "execution_failed"):
        assert body["by_status_24h"][status] == 0


@pytest.mark.asyncio
async def test_by_status_24h_initialized_for_all_known_statuses(env):
    body = await _call_endpoint(env)
    for status in ("attempted", "rejected", "execution_failed"):
        assert status in body["by_status_24h"]


@pytest.mark.asyncio
async def test_attempted_projection(env):
    now = datetime.now(timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _autofix_entry(
                "attempted",
                emitted_at=now - timedelta(minutes=10),
                action_canonical="restart_unhealthy_machine",
                executor_duration_ms=3214,
                before_state={
                    "id": "1781e9f6c12d83",
                    "state": "stopped",
                    "region": "iad",
                    "instance_id": "secret-instance-id-leak",
                },
                after_state={
                    "id": "1781e9f6c12d83",
                    "state": "started",
                },
            )
        ],
    )
    e = (await _call_endpoint(env))["events"][0]
    assert e["status"] == "attempted"
    assert e["action_canonical"] == "restart_unhealthy_machine"
    assert e["executor_duration_ms"] == 3214
    assert e["before_state_label"] == "stopped"
    assert e["after_state_label"] == "started"
    # Whole-dict leak guard
    assert "before_state" not in e or not isinstance(e.get("before_state"), dict)
    assert "after_state" not in e or not isinstance(e.get("after_state"), dict)
    serialized = json.dumps(e)
    assert "secret-instance-id-leak" not in serialized, (
        "SECURITY: before_state's instance_id must not leak"
    )
    assert '"region"' not in serialized, (
        "SECURITY: before_state's region must not leak"
    )


@pytest.mark.asyncio
async def test_rejected_projection(env):
    now = datetime.now(timezone.utc)
    big_detail = {"received_action": "weird", "envelope_fix_name": "x"}
    _write_audit_jsonl(
        env,
        [
            _autofix_entry(
                "rejected",
                emitted_at=now - timedelta(minutes=5),
                rejection_reason="action_not_in_envelope",
                rejection_detail=big_detail,
            )
        ],
    )
    e = (await _call_endpoint(env))["events"][0]
    assert e["status"] == "rejected"
    assert e["rejection_reason"] == "action_not_in_envelope"
    assert len(e["rejection_detail"]) <= 200


@pytest.mark.asyncio
async def test_execution_failed_projection(env):
    now = datetime.now(timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _autofix_entry(
                "execution_failed",
                emitted_at=now - timedelta(minutes=2),
                error="ConnectionRefusedError",
                executor_duration_ms=812,
                before_state={"state": "stopped", "region": "iad"},
            )
        ],
    )
    e = (await _call_endpoint(env))["events"][0]
    assert e["status"] == "execution_failed"
    assert e["error"] == "ConnectionRefusedError"
    assert e["executor_duration_ms"] == 812
    assert e["before_state_label"] == "stopped"


@pytest.mark.asyncio
async def test_unknown_status_coerced(env):
    now = datetime.now(timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _autofix_entry(
                "future_status",
                emitted_at=now - timedelta(minutes=1),
            )
        ],
    )
    e = (await _call_endpoint(env))["events"][0]
    assert e["status"] == "unknown"


@pytest.mark.asyncio
async def test_daily_attempted_sparkline_math(env):
    now = datetime.now(timezone.utc)
    entries = [
        _autofix_entry(
            "attempted",
            emitted_at=now - timedelta(hours=i),
            target_id=f"machine-{i}",
        )
        for i in range(3)
    ]
    # 1 rejected today (must NOT count toward sparkline)
    entries.append(
        _autofix_entry(
            "rejected",
            emitted_at=now - timedelta(hours=2),
            rejection_reason="envelope_disabled",
        )
    )
    # 1 attempted 5 days ago
    entries.append(
        _autofix_entry(
            "attempted",
            emitted_at=now - timedelta(days=5),
            target_id="old",
        )
    )
    # 1 attempted 30 days ago (outside window)
    entries.append(
        _autofix_entry(
            "attempted",
            emitted_at=now - timedelta(days=30),
            target_id="ancient",
        )
    )
    _write_audit_jsonl(env, entries)
    sl = (await _call_endpoint(env))["daily_attempted_14d"]
    assert len(sl) == 14
    assert [b["date"] for b in sl] == sorted(b["date"] for b in sl)
    assert sum(b["count"] for b in sl) == 4


@pytest.mark.asyncio
async def test_24h_cutoff(env):
    now = datetime.now(timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _autofix_entry(
                "attempted",
                emitted_at=now - timedelta(hours=1),
                target_id="recent",
            ),
            _autofix_entry(
                "attempted",
                emitted_at=now - timedelta(days=2),
                target_id="old",
            ),
        ],
    )
    body = await _call_endpoint(env)
    assert len(body["events"]) == 2
    assert body["total_recent_24h"] == 1
    assert body["by_status_24h"]["attempted"] == 1


@pytest.mark.asyncio
async def test_arbitrary_details_dont_leak(env):
    now = datetime.now(timezone.utc)
    leak_str = "PRIVATE_FLY_TOKEN_leak-abc123"
    _write_audit_jsonl(
        env,
        [
            _autofix_entry(
                "attempted",
                emitted_at=now - timedelta(minutes=1),
                extra={
                    "operator_token": leak_str,
                    "fly_api_key": leak_str,
                    "raw_response": {"deep": leak_str},
                },
            )
        ],
    )
    body = await _call_endpoint(env)
    serialized = json.dumps(body)
    assert leak_str not in serialized
    assert "operator_token" not in serialized
    assert "fly_api_key" not in serialized


@pytest.mark.asyncio
async def test_reason_truncated(env):
    now = datetime.now(timezone.utc)
    long_reason = "X" * 1000
    _write_audit_jsonl(
        env,
        [
            _autofix_entry(
                "attempted",
                emitted_at=now - timedelta(minutes=1),
                reason=long_reason,
            )
        ],
    )
    e = (await _call_endpoint(env))["events"][0]
    assert len(e["reason_from_reasoning"]) == 300


# ---------------------------------------------------------------------------
# Drift guard
# ---------------------------------------------------------------------------


def test_status_values_drift_guard():
    """The 3 status values must match across 3 sources."""
    expected = {"attempted", "rejected", "execution_failed"}

    ws_src = _WEB_SERVER.read_text()
    m = re.search(
        r"_PROBE_AUTOFIX_STATUS_VALUES\s*=\s*\(([^)]+)\)",
        ws_src,
    )
    assert m is not None
    be_values = set(re.findall(r'"(\w+)"', m.group(1)))
    assert be_values == expected, f"BE projection drift: {be_values}"

    tool_src = _TOOL_PY.read_text()
    for const_name, expected_val in [
        ("STATUS_ATTEMPTED", "attempted"),
        ("STATUS_REJECTED", "rejected"),
        ("STATUS_EXECUTION_FAILED", "execution_failed"),
    ]:
        assert re.search(
            rf'{const_name}\s*=\s*"({expected_val})"',
            tool_src,
        ), f"emitter constant {const_name} drift in {_TOOL_PY}"

    fe_src = _API_TS.read_text()
    m = re.search(
        r"PROBE_AUTOFIX_STATUS_VALUES[^=]*=\s*\[([^\]]+)\]",
        fe_src,
    )
    assert m is not None
    fe_values = set(re.findall(r'"(\w+)"', m.group(1)))
    assert fe_values == expected, f"FE constant drift: {fe_values}"


def test_seam_literal_includes_probe_autofix_attempted():
    sink_src = (_REPO_ROOT / "kora_cli" / "audit" / "jsonl_sink.py").read_text()
    assert '"tool.probe_autofix_attempted"' in sink_src


# ---------------------------------------------------------------------------
# FE source-pins
# ---------------------------------------------------------------------------


def test_api_wrapper_exists():
    src = _API_TS.read_text()
    assert "getProbeAutofixRecent" in src
    assert "/api/probe-autofix/recent" in src


def test_response_types_declared():
    src = _API_TS.read_text()
    for ts_type in (
        "ProbeAutofixEvent",
        "ProbeAutofixEventsResponse",
        "ProbeAutofixStatus",
        "ProbeAutofixDailyCount",
    ):
        assert ts_type in src


def test_fe_status_values_constant_exported():
    src = _API_TS.read_text()
    assert "export const PROBE_AUTOFIX_STATUS_VALUES" in src


def test_page_exists_uses_panel_view_and_kit():
    assert _PAGE.is_file()
    src = _PAGE.read_text()
    assert 'usePanelView("AutofixLogPage")' in src
    # 3rd consumer of the kit — must import from it.
    assert "@/components/AuditPanelKit" in src


def test_route_and_nav_registered():
    src = _APP_TSX.read_text()
    assert "/probe-autofix-log" in src
    assert "AutofixLogPage" in src
    assert re.search(
        r'path:\s*"/probe-autofix-log"[^}]+labelKey:\s*"probeAutofixLog"',
        src,
        re.DOTALL,
    )


def test_filter_chips_iterate_canonical_status_values():
    src = _PAGE.read_text()
    assert "PROBE_AUTOFIX_CATEGORIES" in src
    for status in ("attempted", "rejected", "execution_failed"):
        assert f'key: "{status}"' in src
    assert "PROBE_AUTOFIX_STATUS_VALUES" in src


def test_empty_state_copy_committed():
    src = _PAGE.read_text()
    assert "No probe-autofix attempts recorded yet" in src
    assert "events match this filter" in src


def test_page_does_not_render_full_state_dicts():
    """SECURITY: page must read event.before_state_label /
    event.after_state_label (the projection-exposed scalars),
    NOT event.before_state / event.after_state (which would
    require the full dict to be exposed)."""
    src = _PAGE.read_text()
    # Allowed: event.before_state_label / event.after_state_label
    # Forbidden: event.before_state / event.after_state as dict reads
    forbidden = [
        r"event\.before_state\b(?!_label)",
        r"event\.after_state\b(?!_label)",
    ]
    for pat in forbidden:
        matches = re.findall(pat, src)
        assert not matches, (
            f"PRIVACY/SECURITY REGRESSION: page references {pat!r} — "
            f"only the projection-exposed _label fields are allowed; "
            f"full state dicts must NEVER leak through this panel"
        )
