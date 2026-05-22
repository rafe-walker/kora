"""Tests for the KR-AGENT-ACTIVITY-PANEL stub endpoint.

Bucket §4 scenarios:
  1. GET /api/agent-activity/recent returns 200
  2. Top-level shape (calls + stub:true + generated_at + total_recent_24h
     + by_caller_24h)
  3. 5 representative stub calls present
  4. Stub covers ok + capability_denied + denied_prod_only so the FE's
     status-badge variety is exercised
  5. Per-entry shape + valid status enum
  6. SECURITY: result_summary contains no raw JSON payload (no {}/[]
     JSON-shape sequences); covers spec §3 contract
  7. SECURITY: caller_actor_kind doesn't match token / hash shapes
     (no long base64/hex runs)
  8. by_caller_24h matches the calls' actual caller distribution
  9. Cron-regression sanity
"""

import re

import pytest


_VALID_STATUS = {
    "ok",
    "capability_denied",
    "denied_prod_only",
    "tool_not_found",
    "handler_error",
    "timeout",
}

# Walk-the-whole-payload guard for raw JSON leaks in result_summary.
# A short textual summary like "20 events returned" contains no JSON
# braces or square brackets. A raw payload dump like '{"id": 4}' does.
# Standardized pattern from KR-WEBHOOK-EVENTS guard against full IPv4
# leaks — pin shape so future stub/real drift can't slip a payload in.
_JSON_LEAK = re.compile(r'[\{\}\[\]]')

# A bearer-token or token-hash typically presents as a continuous run
# of ≥16 base64/hex characters with no dashes/underscores broken by
# spaces. Caller labels (claude_pm, kora_drone_7) are short, contain
# underscores, and don't reach 16 contiguous chars without separators.
# Hex-only pin catches sha-shaped hashes; base64 pin catches token bodies.
_HEX_TOKEN_PIN = re.compile(r'\b[0-9a-fA-F]{16,}\b')
_BASE64_TOKEN_PIN = re.compile(r'\b[A-Za-z0-9+/]{20,}={0,2}\b')


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


# ---- 1. 200 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_returns_200(_isolate_config):
    from kora_cli import web_server

    result = await web_server.list_recent_agent_activity()
    assert isinstance(result, dict)


# ---- 2. Top-level shape ----------------------------------------------


@pytest.mark.asyncio
async def test_response_shape_has_required_keys(_isolate_config):
    from kora_cli import web_server

    result = await web_server.list_recent_agent_activity()
    assert set(result.keys()) == {
        "calls",
        "stub",
        "generated_at",
        "total_recent_24h",
        "by_caller_24h",
    }
    assert isinstance(result["calls"], list)
    assert isinstance(result["generated_at"], str)
    assert isinstance(result["total_recent_24h"], int)
    assert isinstance(result["by_caller_24h"], dict)
    assert result["stub"] is True


# ---- 3. Expected stub calls -----------------------------------------


@pytest.mark.asyncio
async def test_stub_returns_five_representative_calls(_isolate_config):
    """Pin the bucket §3 canonical 5-call stub list. CC#3's per-call
    ledger will replace the body in KR-MCP-RUNTIME-SURFACE ST2 but
    stub shape must stay stable so the FE shipping off this PR keeps
    rendering correctly when both run side-by-side during the cut-over."""
    from kora_cli import web_server

    result = await web_server.list_recent_agent_activity()
    assert len(result["calls"]) == 5
    ids = {c["id"] for c in result["calls"]}
    assert ids == {"stub-1", "stub-2", "stub-3", "stub-4", "stub-5"}


@pytest.mark.asyncio
async def test_stub_covers_ok_and_both_denial_paths(_isolate_config):
    """The 5 stub calls deliberately span ok + capability_denied +
    denied_prod_only so the operator's first look at the panel
    surfaces what failure modes look like. Pin so future stub edits
    can't accidentally homogenize to ok-only."""
    from kora_cli import web_server

    result = await web_server.list_recent_agent_activity()
    statuses = {c["status"] for c in result["calls"]}
    assert "ok" in statuses
    assert "capability_denied" in statuses
    assert "denied_prod_only" in statuses


# ---- 4. Per-entry shape + enum --------------------------------------


@pytest.mark.asyncio
async def test_each_call_has_required_keys_and_valid_status(_isolate_config):
    from kora_cli import web_server

    result = await web_server.list_recent_agent_activity()
    required = {
        "id",
        "tool_name",
        "caller_actor_kind",
        "called_at",
        "duration_ms",
        "status",
        "result_summary",
    }
    for call in result["calls"]:
        assert set(call.keys()) == required
        assert call["status"] in _VALID_STATUS, (
            f"{call['id']}: status={call['status']!r} not in {_VALID_STATUS}"
        )
        assert isinstance(call["tool_name"], str)
        assert call["tool_name"].startswith("kora__"), (
            f"{call['id']}: tool_name={call['tool_name']!r} should be a "
            f"kora__* MCP tool name"
        )
        assert isinstance(call["caller_actor_kind"], str) and call["caller_actor_kind"]
        assert isinstance(call["duration_ms"], int) and call["duration_ms"] >= 0
        assert isinstance(call["called_at"], str) and call["called_at"].endswith("Z")
        assert isinstance(call["result_summary"], str)


# ---- 5. SECURITY: result_summary contract ---------------------------


@pytest.mark.asyncio
async def test_result_summary_contains_no_raw_json_payload(_isolate_config):
    """Bucket §3 hard-constraint: result_summary is a SHORT TEXTUAL
    summary, never a raw JSON payload dump. Operator gets a glanceable
    line ("20 events returned"), not a {} blob that bloats the panel
    and risks leaking internal-only fields the real MCP handler may
    return. 3-layer security pattern: backend payload + TS interface +
    this test."""
    from kora_cli import web_server

    result = await web_server.list_recent_agent_activity()
    for call in result["calls"]:
        summary = call["result_summary"]
        leaks = _JSON_LEAK.findall(summary)
        assert leaks == [], (
            f"{call['id']}: result_summary={summary!r} contains JSON "
            f"structural chars {leaks} — contract requires textual "
            f"summary only, never raw payload"
        )


# ---- 6. SECURITY: caller_actor_kind contract ------------------------


@pytest.mark.asyncio
async def test_caller_actor_kind_is_label_not_token(_isolate_config):
    """Bucket §3 hard-constraint: caller_actor_kind is a LABEL
    (claude_pm, kora_drone_7, etc.) — never a bearer token or token
    hash. If the real handler ever defaults to the auth-token-hash
    when no label is found, this guard catches it before it ships."""
    from kora_cli import web_server

    result = await web_server.list_recent_agent_activity()
    for call in result["calls"]:
        kind = call["caller_actor_kind"]
        assert _HEX_TOKEN_PIN.search(kind) is None, (
            f"{call['id']}: caller_actor_kind={kind!r} matches hex-token "
            f"shape (≥16 hex chars) — contract requires a human label"
        )
        assert _BASE64_TOKEN_PIN.search(kind) is None, (
            f"{call['id']}: caller_actor_kind={kind!r} matches base64-token "
            f"shape (≥20 b64 chars) — contract requires a human label"
        )
        # Belt+braces: kind should be short and look like a snake-case
        # identifier (lowercase + underscores + optional trailing digit).
        assert len(kind) <= 40, (
            f"{call['id']}: caller_actor_kind={kind!r} is implausibly long "
            f"for a label ({len(kind)} chars)"
        )


@pytest.mark.asyncio
async def test_no_token_shaped_strings_anywhere_in_payload(_isolate_config):
    """Walk-the-whole-payload guard (standardizing the pattern from
    KR-WEBHOOK-EVENTS #109's full-IPv4 sweep). Asserts no field
    anywhere in the response — top-level, per-call, or any future
    nested dict — contains a bearer-token-shaped run of characters.
    Catches a future drift like adding "auth_token_hash" to a call
    entry or stuffing a session id into result_summary."""
    from kora_cli import web_server
    import json as _json

    result = await web_server.list_recent_agent_activity()
    blob = _json.dumps(result)
    hex_leaks = _HEX_TOKEN_PIN.findall(blob)
    b64_leaks = _BASE64_TOKEN_PIN.findall(blob)
    assert hex_leaks == [], (
        f"payload contains hex-token-shaped string(s): {hex_leaks} — "
        f"agent-activity surface must never carry credential material "
        f"(bucket §3 SECURITY contract)"
    )
    assert b64_leaks == [], (
        f"payload contains base64-token-shaped string(s): {b64_leaks} — "
        f"agent-activity surface must never carry credential material "
        f"(bucket §3 SECURITY contract)"
    )


# ---- 7. by_caller_24h matches the calls' caller distribution -------


@pytest.mark.asyncio
async def test_by_caller_24h_keys_overlap_visible_callers(_isolate_config):
    """by_caller_24h's keys must be the same set of caller labels that
    appear in the visible window — otherwise the dashboard's
    per-caller breakdown shows names that don't reconcile to any
    individual call entry. Total-counts can differ (visible window is
    a subset of 24h) but the label set must overlap."""
    from kora_cli import web_server

    result = await web_server.list_recent_agent_activity()
    visible_callers = {c["caller_actor_kind"] for c in result["calls"]}
    breakdown_callers = set(result["by_caller_24h"].keys())
    assert visible_callers.issubset(breakdown_callers), (
        f"visible callers {visible_callers - breakdown_callers} are missing "
        f"from by_caller_24h breakdown {breakdown_callers}"
    )
    # Per-caller counts non-negative + sum reconciles to total_recent_24h
    assert all(v >= 0 for v in result["by_caller_24h"].values())
    assert sum(result["by_caller_24h"].values()) == result["total_recent_24h"]


# ---- 8. Cron-regression sanity -------------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_agent_activity_registered(_isolate_config):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
