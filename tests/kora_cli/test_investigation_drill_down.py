"""Tests for KR-FE-INVESTIGATION-DRILL-DOWN backend + FE pins.

GET /api/investigations/{caller_session_id:path} — joins every
audit row + slack_dm_log.jsonl entry sharing the given session id
into one oldest-first chronological timeline.

Behaviour:
  1. probe-shaped session id → audit rows + DM entries joined
  2. email-shaped session id → kind="email"; intent + outbound +
     slack_dm_log rows joined
  3. promotion-shaped session id → kind="promotion"; promotion +
     phrasebook.updated rows joined
  4. Unknown session id → kind="other", timeline=[] (no 404)
  5. Supported-seam allowlist obeyed — a hypothetical new seam
     not in the allowlist is filtered out
  6. Drift-guard: the JOIN endpoint exists + the FE wrapper +
     response type are declared
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
_PAGE = _REPO_ROOT / "web" / "src" / "pages" / "InvestigationDrillDownPage.tsx"
_KORA_ACTIONS = _REPO_ROOT / "web" / "src" / "pages" / "KoraActionsPage.tsx"
_PROBE_INVESTIGATIONS = (
    _REPO_ROOT / "web" / "src" / "pages" / "ProbeInvestigationsPage.tsx"
)
_WEB_SERVER = _REPO_ROOT / "kora_cli" / "web_server.py"


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


def _audit_row(
    seam: str,
    *,
    emitted_at: datetime,
    caller_session_id: str | None = None,
    source: str | None = None,
    details: dict | None = None,
) -> dict:
    return {
        "emitted_at": emitted_at.isoformat(),
        "seam": seam,
        "details": details or {},
        "source": source,
        "caller_session_id": caller_session_id,
    }


async def _call_endpoint(caller_session_id: str) -> dict:
    from kora_cli import web_server

    return await web_server.get_investigation_drill_down(caller_session_id)


@pytest.mark.asyncio
async def test_probe_session_joins_audit_and_dm_streams(env):
    sid = "probe:fly:service_unhealthy"
    now = datetime(2026, 5, 24, 14, 32, 0, tzinfo=timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _audit_row(
                "probe.wake_requested",
                emitted_at=now,
                caller_session_id=None,  # wake emitter doesn't carry sid
                details={"probe": "fly", "category": "service_unhealthy"},
            ),
            _audit_row(
                "reasoning.tool_called",
                emitted_at=now,
                caller_session_id=sid,
                source="reasoning",
                details={
                    "tool_name": "fly_machine_diff",
                    "tool_duration_ms": 200,
                    "tool_status": "ok",
                    "triggered_by": "reasoning",
                },
            ),
            _audit_row(
                "tool.probe_autofix_attempted",
                emitted_at=now,
                caller_session_id=sid,
                source="reasoning",
                details={
                    "status": "attempted",
                    "probe": "fly",
                    "action": "restart_machine",
                    "target_id": "abc123",
                    "reason_from_reasoning": "machine_stopped",
                },
            ),
            _audit_row(
                "probe.investigation_completed",
                emitted_at=now,
                caller_session_id=sid,
                source="reasoning",
                details={
                    "probe": "fly",
                    "dm_status": "sent",
                    "model_used": "claude-haiku-4-5",
                    "total_cost_usd": 0.0042,
                    "investigation_duration_ms": 3800,
                    "autofix_attempted": True,
                    "investigation_summary_text": "fixed it",
                },
            ),
        ],
    )
    _write_slack_dm_log(
        env,
        [
            {
                "sent_at": "2026-05-24T14:32:04Z",
                "channel_id": "D01J",
                "thread_ts": None,
                "text": "Hi operator",
                "slack_message_ts": "1742345059.123",
                "send_status": "ok",
                "caller_session_id": sid,
            },
        ],
    )

    body = await _call_endpoint(sid)
    assert body["session"]["caller_session_id"] == sid
    assert body["session"]["kind"] == "probe"
    # 3 audit rows (wake has caller_session_id=None so isn't joined) + 1 DM.
    assert body["total_count"] == 4
    seams = [it["seam"] for it in body["timeline"]]
    # Should include the dm log entry.
    assert "slack_dm_log.jsonl" in seams
    # Oldest-first chronological order pinned.
    timestamps = [it["emitted_at"] for it in body["timeline"]]
    assert timestamps == sorted(timestamps)


@pytest.mark.asyncio
async def test_unknown_session_returns_empty_not_404(env):
    body = await _call_endpoint("nonsense:no_such_session")
    assert body["session"]["kind"] == "other"
    assert body["timeline"] == []
    assert body["total_count"] == 0
    # Supported seams allowlist still echoed so the FE can show
    # "this is what we'd join if you had data" in the empty state.
    assert isinstance(body["supported_seams"], list)
    assert "probe.investigation_completed" in body["supported_seams"]


@pytest.mark.asyncio
async def test_email_session_kind_classified(env):
    sid = "email:abc-message-id"
    now = datetime(2026, 5, 24, 14, 32, 0, tzinfo=timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _audit_row(
                "intent.email_to_sea_ticket",
                emitted_at=now,
                caller_session_id=sid,
                details={
                    "action": "created",
                    "ticket_id": "T-12",
                    "subject": "Hi",
                    "pattern_matched": "explicit_save",
                    "confidence": "high",
                },
            ),
        ],
    )
    body = await _call_endpoint(sid)
    assert body["session"]["kind"] == "email"
    assert body["total_count"] == 1


@pytest.mark.asyncio
async def test_promotion_session_kind_classified(env):
    sid = "promotion:phrasebook:p-123"
    now = datetime(2026, 5, 24, 14, 32, 0, tzinfo=timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _audit_row(
                "promotion.proposed",
                emitted_at=now,
                caller_session_id=sid,
                source="reasoning",
                details={
                    "proposal_id": "p-123",
                    "cluster_size": 5,
                    "confidence": 0.92,
                    "proposed_pattern": "(?i)(burn)",
                    "proposed_category": "cost_query",
                    "proposed_reply_template": "Burn is {snapshot.x}.",
                    "haiku_synthesized": True,
                    "status": "pending",
                },
            ),
        ],
    )
    body = await _call_endpoint(sid)
    assert body["session"]["kind"] == "promotion"
    assert body["timeline"][0]["seam"] == "promotion.proposed"


@pytest.mark.asyncio
async def test_session_id_capped(env):
    # Defensive cap at 512 chars — anything past is silently
    # truncated so we can't be DOS'd via a 1MB path arg.
    long_sid = "x" * 600
    body = await _call_endpoint(long_sid)
    assert len(body["session"]["caller_session_id"]) == 512


@pytest.mark.asyncio
async def test_unsupported_seam_not_surfaced(env):
    # An audit row from a seam NOT in _DRILL_DOWN_SUPPORTED_SEAMS
    # (e.g. mcp.tool_called, if added later) must not surface
    # automatically. New seams require explicit opt-in.
    sid = "probe:foo:bar"
    now = datetime(2026, 5, 24, 14, 32, 0, tzinfo=timezone.utc)
    _write_audit_jsonl(
        env,
        [
            # Use a real seam that exists in SeamName Literal but is
            # NOT in the drill-down allowlist (probe.poll is a good
            # candidate — it's noisy + not investigation-relevant).
            _audit_row(
                "probe.poll",
                emitted_at=now,
                caller_session_id=sid,
                details={"probe": "foo"},
            ),
        ],
    )
    body = await _call_endpoint(sid)
    # probe.poll is not in _DRILL_DOWN_SUPPORTED_SEAMS, so even
    # though the session id matches, the row is filtered.
    assert body["total_count"] == 0


# ---------------------------------------------------------------------------
# FE source-pins
# ---------------------------------------------------------------------------


def test_fe_api_wrapper_exists():
    src = _API_TS.read_text()
    assert "getInvestigationDrillDown" in src
    assert "/api/investigations/" in src


def test_fe_response_type_declared():
    src = _API_TS.read_text()
    for f in (
        "InvestigationDrillDownResponse",
        "InvestigationDrillTimelineItem",
        "InvestigationDrillKind",
        "timeline",
        "seams_seen",
        "supported_seams",
    ):
        assert f in src, f"missing FE field: {f}"


def test_drill_down_page_exists_and_uses_panel_view():
    assert _PAGE.is_file()
    src = _PAGE.read_text()
    assert 'usePanelView("InvestigationDrillDownPage")' in src
    # Route uses :callerSessionId param — pin so a rename in the
    # router doesn't silently break drill-in links.
    assert "callerSessionId" in src


def test_route_registered():
    src = _APP_TSX.read_text()
    assert "/investigations/:callerSessionId" in src
    assert "InvestigationDrillDownPage" in src


def test_kora_actions_row_links_to_drill_down():
    src = _KORA_ACTIONS.read_text()
    # Each row must offer a drill-in link when caller_session_id
    # is present. Pin the URL shape.
    assert "/investigations/${encodeURIComponent(item.caller_session_id)}" in src


def test_probe_investigations_card_links_to_drill_down():
    src = _PROBE_INVESTIGATIONS.read_text()
    assert (
        "/investigations/${encodeURIComponent(item.caller_session_id)}"
        in src
    )


def test_supported_seams_drift_guard():
    """The drill-down allowlist (_DRILL_DOWN_SUPPORTED_SEAMS) must
    stay in lockstep with the SeamName Literal — any seam in the
    allowlist that's NOT in SeamName will silently return [] (a
    "supported" seam that yields nothing is a footgun)."""
    src = _WEB_SERVER.read_text()
    m = re.search(
        r"_DRILL_DOWN_SUPPORTED_SEAMS[^=]*=\s*\(([^)]+)\)",
        src,
        re.DOTALL,
    )
    assert m is not None
    allowlist = set(re.findall(r'"([^"]+)"', m.group(1)))

    sink_src = (
        _REPO_ROOT / "kora_cli" / "audit" / "jsonl_sink.py"
    ).read_text()
    for seam in allowlist:
        assert f'"{seam}"' in sink_src, (
            f"allowlist seam {seam!r} not in SeamName Literal — "
            f"drill-down will silently return [] for it"
        )
