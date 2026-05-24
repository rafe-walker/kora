"""Tests for KR-PROBE-AUTOFIX-EXECUTION.

Covers:
  Validation pipeline (rejections before any Fly API call):
   - unknown probe → unknown_probe
   - envelope disabled (env unset/false) → envelope_disabled
   - probe with (none) envelope → action_not_in_envelope
   - action not in envelope → action_not_in_envelope
   - target_id empty / malformed → target_id_invalid

  Fly executor:
   - FLY_API_TOKEN unset → rejected with fly_api_token_unset
   - target_id not found across configured apps → target_not_found
   - target machine already started → target_already_healthy (no restart)
   - happy path: state="stopped" → restart called → after_state captured
   - GET /machines transport raise → rejected (target_not_found)
   - POST /restart transport raise → execution_failed (before_state captured)
   - POST /restart HTTP 500 → execution_failed
   - staging app supported via env

  Audit:
   - every invocation emits one tool.probe_autofix_attempted row
   - reason field stored verbatim
   - rejection rows carry rejection_reason + detail
   - attempt rows carry before/after state + executor_duration_ms

  Reasoning allowlist integration:
   - kora__attempt_probe_autofix appears in get_reasoning_available_tools
   - execute_reasoning_tool routes through ST2_TOOL_DISPATCH with synthetic Caller
"""

from __future__ import annotations

import json
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from kora_cli.probes.fix_envelopes import ENABLE_ENV_FLY
from kora_cli.tools.probe_autofix import (
    FLY_API_TOKEN_ENV,
    FLY_STAGING_APP_NAME_ENV,
    REASON_ACTION_NOT_IN_ENVELOPE,
    REASON_ENVELOPE_DISABLED,
    REASON_FLY_API_TOKEN_UNSET,
    REASON_TARGET_ALREADY_HEALTHY,
    REASON_TARGET_ID_INVALID,
    REASON_TARGET_NOT_FOUND,
    REASON_UNKNOWN_PROBE,
    STATUS_ATTEMPTED,
    STATUS_EXECUTION_FAILED,
    STATUS_REJECTED,
    attempt_probe_autofix,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Per-test env isolation + audit redirect."""
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "kora_constants.get_kora_home", lambda: tmp_path, raising=False
    )
    monkeypatch.setenv(
        "KORA_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl")
    )
    # Most tests want the fly envelope enabled; opt out by deleting.
    monkeypatch.setenv(ENABLE_ENV_FLY, "true")
    monkeypatch.setenv(FLY_API_TOKEN_ENV, "fly-test-token")
    monkeypatch.delenv(FLY_STAGING_APP_NAME_ENV, raising=False)
    yield


def _read_audit(tmp_path) -> list:
    path = tmp_path / "audit.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text().splitlines() if line
    ]


# ---------------------------------------------------------------------------
# httpx mock plumbing
# ---------------------------------------------------------------------------


class _FakeHttpResponse:
    def __init__(self, status_code: int, json_payload: Any):
        self.status_code = status_code
        self._payload = json_payload

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Stand-in for httpx.AsyncClient — records calls + replays
    queued responses by URL pattern."""

    def __init__(self, *, get_handler=None, post_handler=None):
        self._get = get_handler
        self._post = post_handler
        self.get_calls: List[tuple] = []
        self.post_calls: List[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, headers=None):
        self.get_calls.append((url, headers))
        if self._get is None:
            raise RuntimeError("no get_handler configured")
        out = self._get(url, headers)
        if isinstance(out, Exception):
            raise out
        return out

    async def post(self, url, headers=None):
        self.post_calls.append((url, headers))
        if self._post is None:
            raise RuntimeError("no post_handler configured")
        out = self._post(url, headers)
        if isinstance(out, Exception):
            raise out
        return out


def _factory(client: _FakeAsyncClient):
    return lambda: client


def _machine(
    *,
    machine_id: str,
    state: str,
    name: str = "kora",
    region: str = "iad",
    instance_id: str = "inst-1",
) -> dict:
    return {
        "id": machine_id,
        "name": name,
        "state": state,
        "region": region,
        "instance_id": instance_id,
    }


# ===========================================================================
# Validation rejections (no Fly API call)
# ===========================================================================


@pytest.mark.asyncio
async def test_unknown_probe_rejected(tmp_path):
    result = await attempt_probe_autofix(
        probe="not-a-probe",
        action="restart_machine",
        target_id="i-abc",
        reason="testing",
    )
    assert result["status"] == STATUS_REJECTED
    assert result["rejection_reason"] == REASON_UNKNOWN_PROBE
    entries = _read_audit(tmp_path)
    assert entries[0]["details"]["rejection_reason"] == REASON_UNKNOWN_PROBE


@pytest.mark.asyncio
async def test_envelope_disabled_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv(ENABLE_ENV_FLY, raising=False)
    result = await attempt_probe_autofix(
        probe="fly",
        action="restart_machine",
        target_id="i-abc",
        reason="testing",
    )
    assert result["status"] == STATUS_REJECTED
    assert result["rejection_reason"] == REASON_ENVELOPE_DISABLED
    assert result["rejection_detail"]["enable_env"] == ENABLE_ENV_FLY


@pytest.mark.asyncio
async def test_envelope_disabled_false_value(tmp_path, monkeypatch):
    """Env set to anything non-truthy keeps envelope OFF."""
    monkeypatch.setenv(ENABLE_ENV_FLY, "false")
    result = await attempt_probe_autofix(
        probe="fly",
        action="restart_machine",
        target_id="i-abc",
        reason="testing",
    )
    assert result["status"] == STATUS_REJECTED
    assert result["rejection_reason"] == REASON_ENVELOPE_DISABLED


@pytest.mark.asyncio
@pytest.mark.parametrize("probe", ["supabase", "vercel", "sentry", "doppler"])
async def test_probes_without_envelope_always_rejected(probe, monkeypatch):
    """Envelopes declared as (none) — even with env truthy, the
    is_envelope_enabled gate returns False so we hit envelope_disabled."""
    monkeypatch.setenv(f"KORA_PROBE_AUTOFIX_{probe.upper()}_ENABLED", "true")
    result = await attempt_probe_autofix(
        probe=probe,
        action="anything",
        target_id="i-abc",
        reason="testing",
    )
    assert result["status"] == STATUS_REJECTED
    assert result["rejection_reason"] == REASON_ENVELOPE_DISABLED


@pytest.mark.asyncio
async def test_action_not_in_envelope_rejected(tmp_path):
    result = await attempt_probe_autofix(
        probe="fly",
        action="deploy_rollback",  # not in envelope
        target_id="i-abc",
        reason="testing",
    )
    assert result["status"] == STATUS_REJECTED
    assert result["rejection_reason"] == REASON_ACTION_NOT_IN_ENVELOPE
    assert result["rejection_detail"]["envelope_fix_name"] == (
        "restart_unhealthy_machine"
    )


@pytest.mark.asyncio
async def test_target_id_empty_rejected(tmp_path):
    result = await attempt_probe_autofix(
        probe="fly",
        action="restart_machine",
        target_id="",
        reason="testing",
    )
    assert result["status"] == STATUS_REJECTED
    assert result["rejection_reason"] == REASON_TARGET_ID_INVALID


@pytest.mark.asyncio
async def test_target_id_malformed_rejected(tmp_path):
    result = await attempt_probe_autofix(
        probe="fly",
        action="restart_machine",
        target_id="bad id with spaces",
        reason="testing",
    )
    assert result["status"] == STATUS_REJECTED
    assert result["rejection_reason"] == REASON_TARGET_ID_INVALID


@pytest.mark.asyncio
async def test_target_id_too_long_rejected(tmp_path):
    result = await attempt_probe_autofix(
        probe="fly",
        action="restart_machine",
        target_id="x" * 100,
        reason="testing",
    )
    assert result["status"] == STATUS_REJECTED
    assert result["rejection_reason"] == REASON_TARGET_ID_INVALID


@pytest.mark.asyncio
async def test_action_alias_restart_unhealthy_machine_accepted(tmp_path):
    """The envelope's canonical fix_name `restart_unhealthy_machine`
    is also accepted (in addition to the shorter `restart_machine`
    alias the reasoning model is more likely to use)."""
    fake = _FakeAsyncClient(
        get_handler=lambda url, h: _FakeHttpResponse(404, {}),
    )
    result = await attempt_probe_autofix(
        probe="fly",
        action="restart_unhealthy_machine",  # canonical name
        target_id="abcd1234efgh56",
        reason="canonical-name probe",
        http_client_factory=_factory(fake),
    )
    # The canonical name is accepted past the action-whitelist
    # gate — we get target_not_found (no machines returned) which
    # confirms we reached the executor.
    assert result["status"] == STATUS_REJECTED
    assert result["rejection_reason"] == REASON_TARGET_NOT_FOUND


# ===========================================================================
# Fly executor
# ===========================================================================


@pytest.mark.asyncio
async def test_fly_api_token_unset_rejected(monkeypatch, tmp_path):
    monkeypatch.delenv(FLY_API_TOKEN_ENV, raising=False)
    result = await attempt_probe_autofix(
        probe="fly",
        action="restart_machine",
        target_id="i-abc",
        reason="testing",
    )
    assert result["status"] == STATUS_REJECTED
    assert result["rejection_reason"] == REASON_FLY_API_TOKEN_UNSET


@pytest.mark.asyncio
async def test_fly_target_not_found_rejected(tmp_path):
    """Machines list returns no matching id → target_not_found."""
    fake = _FakeAsyncClient(
        get_handler=lambda url, h: _FakeHttpResponse(
            200, [_machine(machine_id="other-id", state="started")]
        ),
    )
    result = await attempt_probe_autofix(
        probe="fly",
        action="restart_machine",
        target_id="missing-id",
        reason="testing",
        http_client_factory=_factory(fake),
    )
    assert result["status"] == STATUS_REJECTED
    assert result["rejection_reason"] == REASON_TARGET_NOT_FOUND
    assert result["rejection_detail"]["searched_apps"] == ["kora-runtime"]


@pytest.mark.asyncio
async def test_fly_target_already_healthy_not_restarted(tmp_path):
    """Machine in state='started' must NOT be restarted (operator
    decision territory; envelope only covers unhealthy)."""
    fake = _FakeAsyncClient(
        get_handler=lambda url, h: _FakeHttpResponse(
            200, [_machine(machine_id="abc123", state="started")]
        ),
    )
    result = await attempt_probe_autofix(
        probe="fly",
        action="restart_machine",
        target_id="abc123",
        reason="testing",
        http_client_factory=_factory(fake),
    )
    assert result["status"] == STATUS_REJECTED
    assert result["rejection_reason"] == REASON_TARGET_ALREADY_HEALTHY
    assert result["before_state"]["state"] == "started"
    # No POST should have been issued.
    assert fake.post_calls == []


@pytest.mark.asyncio
async def test_fly_happy_path_restarts_and_captures_after_state(tmp_path):
    machine_id = "abcd1234efgh56"
    list_responses = iter(
        [
            # Before-state list
            _FakeHttpResponse(
                200, [_machine(machine_id=machine_id, state="stopped")]
            ),
            # After-state list
            _FakeHttpResponse(
                200, [_machine(machine_id=machine_id, state="started")]
            ),
        ]
    )
    fake = _FakeAsyncClient(
        get_handler=lambda url, h: next(list_responses),
        post_handler=lambda url, h: _FakeHttpResponse(200, {"ok": True}),
    )
    result = await attempt_probe_autofix(
        probe="fly",
        action="restart_machine",
        target_id=machine_id,
        reason="machine has been stopped >5 min",
        http_client_factory=_factory(fake),
    )
    assert result["status"] == STATUS_ATTEMPTED
    assert result["action_taken"] == "restart_machine"
    assert result["fly_app"] == "kora-runtime"
    assert result["before_state"]["state"] == "stopped"
    assert result["after_state"]["state"] == "started"
    assert isinstance(result["executor_duration_ms"], int)
    # POST was issued to the right URL.
    assert len(fake.post_calls) == 1
    assert fake.post_calls[0][0].endswith(f"machines/{machine_id}/restart")

    entries = _read_audit(tmp_path)
    assert len(entries) == 1
    details = entries[0]["details"]
    assert details["status"] == STATUS_ATTEMPTED
    assert details["before_state"]["state"] == "stopped"
    assert details["after_state"]["state"] == "started"
    # Reason recorded VERBATIM (unlike PR #179's email-body redaction).
    assert details["reason_from_reasoning"] == (
        "machine has been stopped >5 min"
    )
    # Canonical action name captured alongside the alias.
    assert details["action_canonical"] == "restart_unhealthy_machine"


@pytest.mark.asyncio
async def test_fly_staging_app_searched_too(monkeypatch, tmp_path):
    """When KORA_FLY_STAGING_APP_NAME is set, both apps are
    searched for the target_id."""
    monkeypatch.setenv(FLY_STAGING_APP_NAME_ENV, "kora-runtime-staging")
    machine_id = "stg-abcd123"
    # Prod returns no match; staging returns the unhealthy machine.
    list_responses = iter(
        [
            _FakeHttpResponse(200, []),  # prod /machines (empty)
            _FakeHttpResponse(
                200, [_machine(machine_id=machine_id, state="stopped")]
            ),  # staging /machines
            _FakeHttpResponse(
                200, [_machine(machine_id=machine_id, state="started")]
            ),  # staging /machines after-state
        ]
    )
    fake = _FakeAsyncClient(
        get_handler=lambda url, h: next(list_responses),
        post_handler=lambda url, h: _FakeHttpResponse(200, {"ok": True}),
    )
    result = await attempt_probe_autofix(
        probe="fly",
        action="restart_machine",
        target_id=machine_id,
        reason="staging machine flap",
        http_client_factory=_factory(fake),
    )
    assert result["status"] == STATUS_ATTEMPTED
    assert result["fly_app"] == "kora-runtime-staging"


@pytest.mark.asyncio
async def test_fly_get_machines_raises_treated_as_not_found(tmp_path):
    """If /machines transport raises on every app, target is not
    found (the executor doesn't have any list to search)."""
    fake = _FakeAsyncClient(
        get_handler=lambda url, h: httpx.ConnectError("dns fail"),
    )
    result = await attempt_probe_autofix(
        probe="fly",
        action="restart_machine",
        target_id="abc123",
        reason="testing",
        http_client_factory=_factory(fake),
    )
    assert result["status"] == STATUS_REJECTED
    assert result["rejection_reason"] == REASON_TARGET_NOT_FOUND


@pytest.mark.asyncio
async def test_fly_restart_post_raises_execution_failed(tmp_path):
    machine_id = "abc123"
    fake = _FakeAsyncClient(
        get_handler=lambda url, h: _FakeHttpResponse(
            200, [_machine(machine_id=machine_id, state="stopped")]
        ),
        post_handler=lambda url, h: httpx.ConnectError("net drop"),
    )
    result = await attempt_probe_autofix(
        probe="fly",
        action="restart_machine",
        target_id=machine_id,
        reason="testing",
        http_client_factory=_factory(fake),
    )
    assert result["status"] == STATUS_EXECUTION_FAILED
    assert result["error"] == "ConnectError"
    # before_state captured pre-failure.
    assert result["before_state"]["state"] == "stopped"
    entries = _read_audit(tmp_path)
    assert entries[0]["details"]["status"] == STATUS_EXECUTION_FAILED


@pytest.mark.asyncio
async def test_fly_restart_http_500_execution_failed(tmp_path):
    machine_id = "abc123"
    fake = _FakeAsyncClient(
        get_handler=lambda url, h: _FakeHttpResponse(
            200, [_machine(machine_id=machine_id, state="stopped")]
        ),
        post_handler=lambda url, h: _FakeHttpResponse(500, {"err": "boom"}),
    )
    result = await attempt_probe_autofix(
        probe="fly",
        action="restart_machine",
        target_id=machine_id,
        reason="testing",
        http_client_factory=_factory(fake),
    )
    assert result["status"] == STATUS_EXECUTION_FAILED
    assert result["error"] == "http_500"
    assert result["before_state"]["state"] == "stopped"


# ===========================================================================
# Reasoning allowlist integration
# ===========================================================================


def test_tool_advertised_in_reasoning_available_tools():
    from kora_cli.reasoning.tool_registry import get_reasoning_available_tools

    tools = get_reasoning_available_tools()
    names = [t["name"] for t in tools]
    assert "kora__attempt_probe_autofix" in names


def test_tool_in_mutating_subset():
    from kora_cli.reasoning.tool_registry import (
        REASONING_TOOL_ALLOWLIST,
        _REASONING_MUTATING_TOOLS,
    )

    assert "kora__attempt_probe_autofix" in REASONING_TOOL_ALLOWLIST
    assert "kora__attempt_probe_autofix" in _REASONING_MUTATING_TOOLS


@pytest.mark.asyncio
async def test_execute_reasoning_tool_dispatches_with_synthetic_caller(
    monkeypatch, tmp_path
):
    """execute_reasoning_tool routes the new mutating tool through
    ST2_TOOL_DISPATCH with the synthetic Caller."""
    from kora_cli.reasoning.tool_registry import execute_reasoning_tool

    captured = {}

    async def fake_dispatcher(params, caller):
        captured["params"] = params
        captured["caller"] = caller
        from kora_cli.listeners.mcp_tools import AttemptProbeAutofixResult

        return AttemptProbeAutofixResult(status="rejected")

    from kora_cli.listeners.mcp_tools import ST2_TOOL_DISPATCH

    monkeypatch.setitem(
        ST2_TOOL_DISPATCH,
        "kora__attempt_probe_autofix",
        fake_dispatcher,
    )

    result = await execute_reasoning_tool(
        name="kora__attempt_probe_autofix",
        tool_input={
            "probe": "fly",
            "action": "restart_machine",
            "target_id": "abc123",
            "reason": "testing",
        },
    )
    assert result.status == "rejected"
    assert captured["caller"].actor_kind == "kora_reasoning_self"
    assert captured["caller"].allowed_caps == frozenset(
        {"kora__attempt_probe_autofix"}
    )


def test_descriptor_requires_cap_gate_true_for_external_callers():
    """External MCP callers must have the cap explicitly granted
    (default-deny). Kora's reasoning loop bypasses cap_matrix via
    the REASONING_TOOL_ALLOWLIST + synthetic Caller path."""
    from kora_cli.listeners.mcp_tools import ATTEMPT_PROBE_AUTOFIX_TOOL

    assert ATTEMPT_PROBE_AUTOFIX_TOOL["requires_cap_gate"] is True
    assert ATTEMPT_PROBE_AUTOFIX_TOOL["dev_only"] is False
