"""Tests for the KR-REASONING-PANEL endpoint (post audit-JSONL flip).

After KR-AUDIT-PANEL-ENDPOINTS, the endpoint reads
``${KORA_HOME}/kora_audit_log.jsonl`` filtered to
``seam=reasoning.tool_called`` rows and GROUPS by
caller_session_id so multi-tool reasoning iterations collapse
into one ReasoningCall row with ``tools_used: [...]``.

Limitations (per spec §2 Flip 2 + endpoint docstring):
  * model_used / tokens / response_text are null — audit doesn't
    capture them. KR-REASONING-PANEL-MODEL-XREF follow-on bucket
    cross-references slack_dm_log.jsonl outbound entries.
  * cost_rung_at_call is "unknown" (lowercase Enum.value — same
    rationale as the SlackDMHandledStatus contract).
"""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from kora_cli.audit.jsonl_sink import AUDIT_LOG_FILENAME


_ANTHROPIC_KEY_SHAPE = re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b")
_HEX_SECRET_SHAPE = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_VALID_COST_RUNGS = {"normal", "warn_75", "downshift_90", "hard_stop_100", "unknown"}


@pytest.fixture
def audit_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setattr("kora_constants.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr("kora_cli.config.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr("kora_cli.web_server.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr(
        "kora_cli.config.get_config_path",
        lambda: tmp_path / "config.yaml",
    )
    monkeypatch.setattr(
        "kora_cli.config.get_env_path", lambda: tmp_path / ".env"
    )
    return tmp_path


def _reasoning_tool(
    *,
    minutes_ago: int = 5,
    tool_name: str = "get_operational_state",
    tool_status: str = "ok",
    tool_duration_ms: int = 100,
    caller_session_id: str = "session-1",
    triggered_by: str = "slack_dm",
    **extra: Any,
) -> Dict[str, Any]:
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    details = {
        "tool_name": tool_name,
        "triggered_by": triggered_by,
        "tool_duration_ms": tool_duration_ms,
        "tool_status": tool_status,
    }
    details.update(extra)
    return {
        "emitted_at": ts.isoformat(),
        "seam": "reasoning.tool_called",
        "details": details,
        "caller_session_id": caller_session_id,
        "source": "reasoning",
    }


def _other_seam(seam: str, **extra) -> Dict[str, Any]:
    ts = datetime.now(timezone.utc)
    return {
        "emitted_at": ts.isoformat(),
        "seam": seam,
        "details": extra,
        "caller_session_id": None,
        "source": None,
    }


def write_log(env_path: Path, entries: List[Dict[str, Any]]) -> Path:
    log_path = env_path / AUDIT_LOG_FILENAME
    with log_path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return log_path


# ---- Empty / shape ----------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_returns_empty_list_when_audit_log_missing(audit_env):
    from kora_cli import web_server

    result = await web_server.list_recent_reasoning()
    assert result["calls"] == []
    assert result["stub"] is False


@pytest.mark.asyncio
async def test_response_shape_has_required_keys(audit_env):
    write_log(audit_env, [_reasoning_tool()])
    from kora_cli import web_server

    result = await web_server.list_recent_reasoning()
    assert set(result.keys()) == {
        "calls",
        "stub",
        "generated_at",
        "total_recent_24h",
        "by_model_24h",
        "by_status_24h",
        "tokens_total_24h",
    }
    assert result["stub"] is False


# ---- Single-tool projection -----------------------------------


@pytest.mark.asyncio
async def test_single_tool_call_projects_to_one_reasoning_call(audit_env):
    write_log(audit_env, [_reasoning_tool(tool_name="get_state", caller_session_id="s1")])
    from kora_cli import web_server

    result = await web_server.list_recent_reasoning()
    assert len(result["calls"]) == 1
    call = result["calls"][0]
    assert call["tools_used"] == ["get_state"]
    assert call["status"] == "ok"
    assert call["error_code"] is None
    # Limitations carry-forward — null until KR-REASONING-PANEL-MODEL-XREF
    assert call["model_used"] is None
    assert call["input_tokens"] == 0
    assert call["output_tokens"] == 0
    assert call["response_text_truncated_200"] is None
    assert call["cost_rung_at_call"] == "unknown"


# ---- Grouping ---------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_rows_same_session_id_collapse_to_one_call(audit_env):
    """Spec §2 Flip 2: multi-tool reasoning iteration → 1 row with
    tools_used=[name1, name2, name3]."""
    write_log(audit_env, [
        _reasoning_tool(tool_name="get_state", caller_session_id="s1", minutes_ago=10),
        _reasoning_tool(tool_name="create_ticket", caller_session_id="s1", minutes_ago=9),
        _reasoning_tool(tool_name="send_dm", caller_session_id="s1", minutes_ago=8),
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_reasoning()
    assert len(result["calls"]) == 1
    call = result["calls"][0]
    assert call["tools_used"] == ["get_state", "create_ticket", "send_dm"]


@pytest.mark.asyncio
async def test_different_session_ids_yield_separate_calls(audit_env):
    write_log(audit_env, [
        _reasoning_tool(tool_name="t1", caller_session_id="s1"),
        _reasoning_tool(tool_name="t2", caller_session_id="s2"),
        _reasoning_tool(tool_name="t3", caller_session_id="s3"),
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_reasoning()
    assert len(result["calls"]) == 3


@pytest.mark.asyncio
async def test_duration_ms_sums_across_grouped_tools(audit_env):
    write_log(audit_env, [
        _reasoning_tool(tool_duration_ms=100, caller_session_id="s1", minutes_ago=10),
        _reasoning_tool(tool_duration_ms=200, caller_session_id="s1", minutes_ago=9),
        _reasoning_tool(tool_duration_ms=300, caller_session_id="s1", minutes_ago=8),
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_reasoning()
    assert result["calls"][0]["duration_ms"] == 600


# ---- Status derivation -----------------------------------------


@pytest.mark.asyncio
async def test_all_ok_group_status_is_ok(audit_env):
    write_log(audit_env, [
        _reasoning_tool(tool_status="ok", caller_session_id="s1"),
        _reasoning_tool(tool_status="ok", caller_session_id="s1"),
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_reasoning()
    call = result["calls"][0]
    assert call["status"] == "ok"
    assert call["error_code"] is None


@pytest.mark.asyncio
async def test_not_allowed_in_group_status_is_halted(audit_env):
    write_log(audit_env, [
        _reasoning_tool(tool_status="ok", caller_session_id="s1"),
        _reasoning_tool(tool_status="not_allowed", caller_session_id="s1"),
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_reasoning()
    call = result["calls"][0]
    assert call["status"] == "halted"
    assert call["error_code"] == "capability_denied"


@pytest.mark.asyncio
async def test_execution_error_in_group_status_is_failed(audit_env):
    write_log(audit_env, [
        _reasoning_tool(tool_status="ok", caller_session_id="s1"),
        _reasoning_tool(tool_status="execution_error", caller_session_id="s1"),
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_reasoning()
    call = result["calls"][0]
    assert call["status"] == "failed"
    assert call["error_code"] == "handler_error"


# ---- Filtering --------------------------------------------------


@pytest.mark.asyncio
async def test_other_seams_filtered_out(audit_env):
    write_log(audit_env, [
        _reasoning_tool(),
        _other_seam("mcp.tool_called", tool_name="kora__test"),
        _other_seam("webhook.dead_letter", source="slack"),
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_reasoning()
    assert len(result["calls"]) == 1


# ---- SECURITY ---------------------------------------------------


@pytest.mark.asyncio
async def test_no_token_shapes_anywhere_in_payload(audit_env):
    write_log(audit_env, [_reasoning_tool()])
    from kora_cli import web_server

    result = await web_server.list_recent_reasoning()
    blob = json.dumps(result)
    assert _ANTHROPIC_KEY_SHAPE.findall(blob) == []
    assert _HEX_SECRET_SHAPE.findall(blob) == []


@pytest.mark.asyncio
async def test_cost_rung_uses_lowercase_value_string(audit_env):
    """K-DG pin preserved from PR #132: cost_rung_at_call matches
    engine.py:47-49 lowercase CostLadderRungName literal. Until the
    model-xref follow-on lands, every audit-derived call surfaces
    "unknown" — that's a valid enum member."""
    write_log(audit_env, [_reasoning_tool()])
    from kora_cli import web_server

    result = await web_server.list_recent_reasoning()
    for call in result["calls"]:
        assert call["cost_rung_at_call"] in _VALID_COST_RUNGS
        assert call["cost_rung_at_call"] == call["cost_rung_at_call"].lower()


# ---- ?limit + aggregates ---------------------------------------


@pytest.mark.asyncio
async def test_limit_applied_to_groups(audit_env):
    write_log(audit_env, [
        _reasoning_tool(caller_session_id=f"s{i}", minutes_ago=i)
        for i in range(10)
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_reasoning(limit=3)
    assert len(result["calls"]) == 3


@pytest.mark.asyncio
async def test_by_status_24h_counts_individual_rows_not_groups(audit_env):
    """Aggregate counts use INDIVIDUAL audit rows, not groups, so
    the headline number reflects total reasoning activity volume."""
    write_log(audit_env, [
        _reasoning_tool(tool_status="ok", caller_session_id="s1"),
        _reasoning_tool(tool_status="ok", caller_session_id="s1"),
        _reasoning_tool(tool_status="ok", caller_session_id="s2"),
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_reasoning()
    assert result["total_recent_24h"] == 3  # 3 raw rows
    assert len(result["calls"]) == 2  # 2 grouped sessions
    assert result["by_status_24h"]["ok"] == 3


# ---- Cron-regression sanity ----------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_reasoning_registered(audit_env):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
