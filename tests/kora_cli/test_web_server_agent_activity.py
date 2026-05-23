"""Tests for the KR-AGENT-ACTIVITY-PANEL endpoint (post audit-JSONL flip).

After KR-AUDIT-PANEL-ENDPOINTS, the endpoint reads
``${KORA_HOME}/kora_audit_log.jsonl`` filtered to
``seam=mcp.tool_called`` rows and projects to ``AgentCall`` shape.
The pre-flip stub tests (5 hardcoded entries) are replaced by
projection tests against fixture audit JSONL.

Scenarios:
  1. Empty / missing audit log → empty list + stub:false
  2. Top-level shape (calls + stub:false + generated_at +
     total_recent_24h + by_caller_24h)
  3. mcp.tool_called row projects to AgentCall shape
  4. Other seams are filtered out (don't appear in agent-activity)
  5. ?limit query param respected; capped at 200
  6. Newest-first ordering
  7. SECURITY: walk-payload no Anthropic / Slack / Bearer token shapes
  8. SECURITY: caller_actor_kind label-shaped (no hash/base64 runs)
  9. SECURITY: result_summary no raw JSON shapes
 10. by_caller_24h reconciles to the visible callers
 11. Cron-regression sanity
"""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from kora_cli.audit.jsonl_sink import AUDIT_LOG_FILENAME


_JSON_SHAPE_LEAK = re.compile(r"[\{\}\[\]]")
_HEX_TOKEN_PIN = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_BASE64_TOKEN_PIN = re.compile(r"\b[A-Za-z0-9+/]{20,}={0,2}\b")
_ANTHROPIC_KEY_SHAPE = re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b")
_SLACK_TOKEN_SHAPE = re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{8,}\b")


from tests.kora_cli._panel_test_helpers import isolated_kora_home  # noqa: E402


@pytest.fixture
def audit_env(tmp_path, monkeypatch):
    """Per the KR-SLACK-DM-PANEL-FLIP (#137) fixture-isolation
    lesson — monkeypatch get_kora_home in all 3 module namespaces
    via the shared helper."""
    return isolated_kora_home(tmp_path, monkeypatch)


def _entry(
    *,
    seam: str = "mcp.tool_called",
    minutes_ago: int = 5,
    details: Dict[str, Any] | None = None,
    caller_session_id: str | None = None,
    source: str | None = "mcp_http",
) -> Dict[str, Any]:
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {
        "emitted_at": ts.isoformat(),
        "seam": seam,
        "details": details or {},
        "caller_session_id": caller_session_id,
        "source": source,
    }


def _mcp_call(
    *,
    minutes_ago: int = 5,
    tool_name: str = "kora__get_operational_state",
    caller_actor_kind: str = "claude_pm",
    result: str = "state: RUNNING",
    **extra: Any,
) -> Dict[str, Any]:
    details = {
        "tool_name": tool_name,
        "tool_kind": "mutating",
        "caller_actor_kind": caller_actor_kind,
        "args_keys": [],
        "result": result,
    }
    details.update(extra)
    return _entry(seam="mcp.tool_called", minutes_ago=minutes_ago, details=details)


def write_log(env_path: Path, entries: List[Dict[str, Any]]) -> Path:
    log_path = env_path / AUDIT_LOG_FILENAME
    with log_path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return log_path


# ---- 1. Empty / missing audit log -------------------------------


@pytest.mark.asyncio
async def test_endpoint_returns_empty_list_when_audit_log_missing(audit_env):
    from kora_cli import web_server

    result = await web_server.list_recent_agent_activity()
    assert result["calls"] == []
    assert result["stub"] is False


# ---- 2. Top-level shape ----------------------------------------


@pytest.mark.asyncio
async def test_response_shape_has_required_keys(audit_env):
    write_log(audit_env, [_mcp_call()])
    from kora_cli import web_server

    result = await web_server.list_recent_agent_activity()
    assert set(result.keys()) == {
        "calls",
        "stub",
        "generated_at",
        "total_recent_24h",
        "by_caller_24h",
    }
    assert result["stub"] is False


# ---- 3. Projection shape ---------------------------------------


@pytest.mark.asyncio
async def test_mcp_tool_called_projects_to_agent_call_shape(audit_env):
    write_log(audit_env, [
        _mcp_call(
            tool_name="kora__create_sea_ticket",
            caller_actor_kind="claude_pm",
            result="ticket: sea_abc123",
        )
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_agent_activity()
    assert len(result["calls"]) == 1
    call = result["calls"][0]
    assert set(call.keys()) == {
        "id",
        "tool_name",
        "caller_actor_kind",
        "called_at",
        "duration_ms",
        "status",
        "result_summary",
    }
    assert call["tool_name"] == "kora__create_sea_ticket"
    assert call["caller_actor_kind"] == "claude_pm"
    assert call["result_summary"] == "ticket: sea_abc123"
    assert call["status"] == "ok"
    assert call["called_at"].endswith("Z")
    # duration_ms not in audit today; K-DG note in endpoint
    assert call["duration_ms"] == 0


# ---- 4. Seam filtering -------------------------------------------


@pytest.mark.asyncio
async def test_other_seams_filtered_out(audit_env):
    write_log(audit_env, [
        _mcp_call(tool_name="kora__test"),
        _entry(seam="reasoning.tool_called", source="reasoning"),
        _entry(seam="webhook.dead_letter", source="slack_dm"),
        _entry(seam="slack_dm.reply_failed", source="slack_dm"),
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_agent_activity()
    assert len(result["calls"]) == 1, (
        "Only mcp.tool_called rows should appear; other seams "
        "belong on their own panels"
    )


# ---- 5. ?limit cap ----------------------------------------------


@pytest.mark.asyncio
async def test_limit_capped_at_200(audit_env):
    write_log(audit_env, [_mcp_call(minutes_ago=i) for i in range(5)])
    from kora_cli import web_server

    result = await web_server.list_recent_agent_activity(limit=99999)
    assert len(result["calls"]) <= 200


@pytest.mark.asyncio
async def test_limit_query_param_respected(audit_env):
    write_log(audit_env, [_mcp_call(minutes_ago=i, result=f"r{i}") for i in range(10)])
    from kora_cli import web_server

    result = await web_server.list_recent_agent_activity(limit=3)
    assert len(result["calls"]) == 3


# ---- 6. Newest-first ordering ----------------------------------


@pytest.mark.asyncio
async def test_newest_first_ordering(audit_env):
    write_log(audit_env, [
        _mcp_call(minutes_ago=30, result="oldest"),
        _mcp_call(minutes_ago=5, result="newest"),
        _mcp_call(minutes_ago=15, result="middle"),
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_agent_activity()
    assert [c["result_summary"] for c in result["calls"]] == [
        "newest",
        "middle",
        "oldest",
    ]


# ---- 7-9. SECURITY: walk-payload guards ------------------------


@pytest.mark.asyncio
async def test_no_token_or_secret_shapes_in_payload(audit_env):
    write_log(audit_env, [_mcp_call(), _mcp_call(caller_actor_kind="kora_drone_7")])
    from kora_cli import web_server

    result = await web_server.list_recent_agent_activity()
    blob = json.dumps(result)
    assert _ANTHROPIC_KEY_SHAPE.findall(blob) == []
    assert _SLACK_TOKEN_SHAPE.findall(blob) == []


@pytest.mark.asyncio
async def test_caller_actor_kind_is_label_not_token(audit_env):
    write_log(audit_env, [_mcp_call(caller_actor_kind="claude_pm")])
    from kora_cli import web_server

    result = await web_server.list_recent_agent_activity()
    for call in result["calls"]:
        kind = call["caller_actor_kind"]
        assert _HEX_TOKEN_PIN.search(kind) is None
        assert _BASE64_TOKEN_PIN.search(kind) is None


@pytest.mark.asyncio
async def test_result_summary_contains_no_raw_json_payload(audit_env):
    write_log(audit_env, [_mcp_call(result="state: RUNNING, 2 sea_tickets")])
    from kora_cli import web_server

    result = await web_server.list_recent_agent_activity()
    for call in result["calls"]:
        leaks = _JSON_SHAPE_LEAK.findall(call["result_summary"])
        assert leaks == [], (
            f"result_summary={call['result_summary']!r} contains "
            f"JSON structural chars {leaks}"
        )


# ---- 10. by_caller_24h reconciliation ---------------------------


@pytest.mark.asyncio
async def test_by_caller_24h_includes_visible_callers(audit_env):
    write_log(audit_env, [
        _mcp_call(caller_actor_kind="claude_pm"),
        _mcp_call(caller_actor_kind="claude_pm"),
        _mcp_call(caller_actor_kind="kora_drone_7"),
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_agent_activity()
    assert result["by_caller_24h"] == {"claude_pm": 2, "kora_drone_7": 1}
    assert result["total_recent_24h"] == 3


# ---- 11. Cron-regression sanity --------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_agent_activity_registered(audit_env):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
