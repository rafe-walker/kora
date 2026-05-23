"""Tests for KR-ALERT-NOTIFY ST2 — kora__send_test_alert MCP tool.

Bucket §4 Q4 — dev-only test tool. Bypasses dedup + cooldown +
burst + digest throttling so each operator invocation fires a
synthetic alert through the live AlertNotifier.

Scenarios:
  1. Tool descriptor present in ST2_TOOL_DESCRIPTORS
  2. Dispatch entry present in ST2_TOOL_DISPATCH
  3. requires_cap_gate=True (matches all mutating tools)
  4. dev_only=True flag in descriptor
  5. Input schema enforces severity enum + additionalProperties=False
  6. Refuses on KORA_DEPLOY_ENV=prd via _ST2_DevOnlyError
  7. Accepts on KORA_DEPLOY_ENV=stg + KORA_DEPLOY_ENV unset
  8. Invalid severity raises _ST2_ToolInputError
  9. AlertNotifier unavailable → _ST2_ToolInputError
 10. Critical/warning routes through synthetic dispatch → Slack
 11. Info routes through synthetic dispatch → email
 12. Synthetic alert id is unique per call (timestamp-based)
 13. Each call generates fresh timestamp-based id (so two calls fire)
 14. Audit emit records "dispatched:<channel>" on success
 15. Audit emit records "failed:<channel>:<error>" on failure
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kora_cli.listeners import mcp_tools
from kora_cli.listeners.mcp_caller_auth import Caller
from kora_cli.listeners.mcp_tools import (
    SEND_TEST_ALERT_TOOL,
    ST2_TOOL_DESCRIPTORS,
    ST2_TOOL_DISPATCH,
    SendTestAlertResult,
    _ST2_DevOnlyError,
    _ST2_ToolInputError,
    _dispatch_send_test_alert,
    _execute_send_test_alert,
)


def _make_caller(*, allowed=("kora__send_test_alert",)) -> Caller:
    return Caller(actor_kind="test_operator", allowed_caps=list(allowed))


def _make_dispatch_outcome(
    *,
    success: bool = True,
    channel: str = "slack_dm",
    error: str | None = None,
):
    from kora_cli.alerts.notifier import DispatchOutcome

    return DispatchOutcome(
        alert_id="test_alert:test",
        severity="warning",
        channel=channel,
        success=success,
        error=error,
    )


@pytest.fixture
def fake_notifier():
    notifier = MagicMock()
    notifier.dispatch_synthetic_alert = AsyncMock(
        return_value=_make_dispatch_outcome()
    )
    return notifier


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("KORA_DEPLOY_ENV", raising=False)


# ===========================================================================
# Descriptor / registration
# ===========================================================================


def test_tool_descriptor_in_st2_descriptors():
    assert SEND_TEST_ALERT_TOOL in ST2_TOOL_DESCRIPTORS
    assert SEND_TEST_ALERT_TOOL["name"] == "kora__send_test_alert"


def test_dispatch_entry_present():
    assert "kora__send_test_alert" in ST2_TOOL_DISPATCH
    assert ST2_TOOL_DISPATCH["kora__send_test_alert"] is _dispatch_send_test_alert


def test_descriptor_requires_cap_gate():
    assert SEND_TEST_ALERT_TOOL["requires_cap_gate"] is True


def test_descriptor_marked_dev_only():
    assert SEND_TEST_ALERT_TOOL["dev_only"] is True


def test_input_schema_enforces_severity_enum():
    schema = SEND_TEST_ALERT_TOOL["inputSchema"]
    assert schema["additionalProperties"] is False
    assert "severity" in schema["required"]
    assert set(schema["properties"]["severity"]["enum"]) == {
        "critical",
        "warning",
        "info",
    }


# ===========================================================================
# Prod refusal
# ===========================================================================


@pytest.mark.asyncio
async def test_refuses_on_prd(monkeypatch, fake_notifier):
    monkeypatch.setenv("KORA_DEPLOY_ENV", "prd")
    with patch(
        "kora_cli.listeners.alert_notifier_listener.current_alert_notifier",
        return_value=fake_notifier,
    ):
        with pytest.raises(_ST2_DevOnlyError, match="refuses on KORA_DEPLOY_ENV=prd"):
            await _execute_send_test_alert(
                severity="warning", caller=_make_caller()
            )
    # Notifier should NOT have been called.
    fake_notifier.dispatch_synthetic_alert.assert_not_called()


@pytest.mark.asyncio
async def test_accepts_on_stg(monkeypatch, fake_notifier):
    monkeypatch.setenv("KORA_DEPLOY_ENV", "stg")
    with patch(
        "kora_cli.listeners.alert_notifier_listener.current_alert_notifier",
        return_value=fake_notifier,
    ):
        result = await _execute_send_test_alert(
            severity="warning", caller=_make_caller()
        )
    assert result.success is True
    assert result.deploy_env == "stg"


@pytest.mark.asyncio
async def test_accepts_when_deploy_env_unset(monkeypatch, fake_notifier):
    monkeypatch.delenv("KORA_DEPLOY_ENV", raising=False)
    with patch(
        "kora_cli.listeners.alert_notifier_listener.current_alert_notifier",
        return_value=fake_notifier,
    ):
        result = await _execute_send_test_alert(
            severity="critical", caller=_make_caller()
        )
    assert result.success is True
    assert result.deploy_env == "unknown"


# ===========================================================================
# Validation
# ===========================================================================


@pytest.mark.asyncio
async def test_invalid_severity_raises(monkeypatch, fake_notifier):
    with patch(
        "kora_cli.listeners.alert_notifier_listener.current_alert_notifier",
        return_value=fake_notifier,
    ):
        with pytest.raises(_ST2_ToolInputError, match="severity must be"):
            await _execute_send_test_alert(
                severity="urgent", caller=_make_caller()
            )


@pytest.mark.asyncio
async def test_notifier_unavailable_raises():
    with patch(
        "kora_cli.listeners.alert_notifier_listener.current_alert_notifier",
        return_value=None,
    ):
        with pytest.raises(
            _ST2_ToolInputError, match="alert_notifier_unavailable"
        ):
            await _execute_send_test_alert(
                severity="info", caller=_make_caller()
            )


# ===========================================================================
# Dispatch routing
# ===========================================================================


@pytest.mark.asyncio
async def test_critical_dispatches_through_synthetic(monkeypatch, fake_notifier):
    fake_notifier.dispatch_synthetic_alert = AsyncMock(
        return_value=_make_dispatch_outcome(channel="slack_dm")
    )
    with patch(
        "kora_cli.listeners.alert_notifier_listener.current_alert_notifier",
        return_value=fake_notifier,
    ):
        result = await _execute_send_test_alert(
            severity="critical", caller=_make_caller()
        )
    assert result.success is True
    assert result.channel == "slack_dm"
    assert result.severity == "critical"
    fake_notifier.dispatch_synthetic_alert.assert_awaited_once()
    synthetic_alert = fake_notifier.dispatch_synthetic_alert.await_args.args[0]
    assert synthetic_alert.severity == "critical"
    assert synthetic_alert.category == "test_alert"
    assert synthetic_alert.id.startswith("test_alert:")


@pytest.mark.asyncio
async def test_info_dispatches_to_email(monkeypatch, fake_notifier):
    fake_notifier.dispatch_synthetic_alert = AsyncMock(
        return_value=_make_dispatch_outcome(channel="email")
    )
    with patch(
        "kora_cli.listeners.alert_notifier_listener.current_alert_notifier",
        return_value=fake_notifier,
    ):
        result = await _execute_send_test_alert(
            severity="info", caller=_make_caller()
        )
    assert result.channel == "email"


# ===========================================================================
# Unique ids per call
# ===========================================================================


@pytest.mark.asyncio
async def test_each_call_generates_unique_id(monkeypatch, fake_notifier):
    """Each invocation produces a unique alert_id so repeated tests
    don't collide with dedup (even though synthetic path bypasses
    dedup, unique ids are still good practice for audit trail)."""
    with patch(
        "kora_cli.listeners.alert_notifier_listener.current_alert_notifier",
        return_value=fake_notifier,
    ):
        result1 = await _execute_send_test_alert(
            severity="warning", caller=_make_caller()
        )
        await asyncio.sleep(0.001)  # ensure timestamp resolution distinguishes
        result2 = await _execute_send_test_alert(
            severity="warning", caller=_make_caller()
        )
    assert result1.alert_id != result2.alert_id
    assert result1.alert_id.startswith("test_alert:")
    assert result2.alert_id.startswith("test_alert:")


# ===========================================================================
# Failure path
# ===========================================================================


@pytest.mark.asyncio
async def test_dispatch_failure_returns_failed_result(
    monkeypatch, fake_notifier
):
    fake_notifier.dispatch_synthetic_alert = AsyncMock(
        return_value=_make_dispatch_outcome(
            success=False, channel="slack_dm", error="RuntimeError"
        )
    )
    with patch(
        "kora_cli.listeners.alert_notifier_listener.current_alert_notifier",
        return_value=fake_notifier,
    ):
        result = await _execute_send_test_alert(
            severity="critical", caller=_make_caller()
        )
    assert result.success is False
    assert result.error == "RuntimeError"


# ===========================================================================
# Audit emit
# ===========================================================================


@pytest.mark.asyncio
async def test_audit_records_dispatched_result(monkeypatch, fake_notifier):
    with patch(
        "kora_cli.listeners.alert_notifier_listener.current_alert_notifier",
        return_value=fake_notifier,
    ), patch.object(mcp_tools, "_emit_audit") as mock_emit:
        await _execute_send_test_alert(
            severity="warning", caller=_make_caller()
        )
    mock_emit.assert_called_once()
    kw = mock_emit.call_args.kwargs
    assert kw["tool"] == "kora__send_test_alert"
    assert kw["args"] == {"severity": "warning"}
    assert kw["result"].startswith("dispatched:")


@pytest.mark.asyncio
async def test_audit_records_failed_result(monkeypatch, fake_notifier):
    fake_notifier.dispatch_synthetic_alert = AsyncMock(
        return_value=_make_dispatch_outcome(
            success=False, error="RuntimeError"
        )
    )
    with patch(
        "kora_cli.listeners.alert_notifier_listener.current_alert_notifier",
        return_value=fake_notifier,
    ), patch.object(mcp_tools, "_emit_audit") as mock_emit:
        await _execute_send_test_alert(
            severity="info", caller=_make_caller()
        )
    kw = mock_emit.call_args.kwargs
    assert kw["result"].startswith("failed:")
