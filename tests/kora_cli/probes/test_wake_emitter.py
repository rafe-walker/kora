"""Tests for KR-PROBE-AUDIT-AND-CONVERT — wake-event emitter.

Scenarios:
  1. emit_wake_event writes a probe.wake_requested audit row
  2. Audit row details include probe + severity + category + title
  3. envelope_enabled reflects the env state (default False; True
     when fly env truthy)
  4. envelope_fix_name reflects the per-probe envelope (none for
     non-fly probes; "restart_unhealthy_machine" for fly)
  5. Audit-write failure logs + doesn't raise
  6. emit_audit raises → wake_emitter logs + continues (fail-soft)
  7. cheap-cron contract: routine probing path doesn't invoke LLM
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kora_cli.probes import (
    Issue,
    emit_wake_event,
)
from kora_cli.probes.fix_envelopes import ENABLE_ENV_FLY


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.delenv(ENABLE_ENV_FLY, raising=False)


def _make_fly_issue(severity="critical") -> Issue:
    return Issue(
        id="probe_issue:fly:unhealthy",
        probe="fly",
        severity=severity,
        category="service_unhealthy",
        title="Fly app(s) unreachable: HTTP 401",
        detail="Fly probe reported status=unhealthy.",
        details={"apps_running": 0, "deploys_last_24h": "unknown"},
    )


def _make_supabase_issue(severity="critical") -> Issue:
    return Issue(
        id="probe_issue:supabase:unhealthy",
        probe="supabase",
        severity=severity,
        category="service_unhealthy",
        title="Supabase unreachable",
        detail="Supabase probe reported status=unhealthy.",
        details={"connections_pct": "unknown"},
    )


# ===========================================================================
# Audit emission
# ===========================================================================


def test_emit_wake_writes_audit_row():
    issue = _make_fly_issue()
    with patch("kora_cli.audit.emit_audit") as mock_emit:
        emit_wake_event(issue)
    mock_emit.assert_called_once()
    call_kwargs = mock_emit.call_args.kwargs
    assert call_kwargs["seam"] == "probe.wake_requested"
    details = call_kwargs["details"]
    assert details["probe"] == "fly"
    assert details["severity"] == "critical"
    assert details["category"] == "service_unhealthy"
    assert "Fly" in details["title"]
    assert details["snapshot_details"] == {
        "apps_running": 0,
        "deploys_last_24h": "unknown",
    }


def test_envelope_enabled_default_false_in_audit():
    """When env unset → envelope_enabled False even for fly (the only
    probe with a non-none envelope)."""
    issue = _make_fly_issue()
    with patch("kora_cli.audit.emit_audit") as mock_emit:
        emit_wake_event(issue)
    details = mock_emit.call_args.kwargs["details"]
    assert details["envelope_enabled"] is False
    assert details["envelope_fix_name"] == "restart_unhealthy_machine"


def test_envelope_enabled_true_when_env_set(monkeypatch):
    monkeypatch.setenv(ENABLE_ENV_FLY, "true")
    issue = _make_fly_issue()
    with patch("kora_cli.audit.emit_audit") as mock_emit:
        emit_wake_event(issue)
    details = mock_emit.call_args.kwargs["details"]
    assert details["envelope_enabled"] is True
    assert details["envelope_fix_name"] == "restart_unhealthy_machine"


def test_supabase_envelope_stays_disabled_even_with_env_set(monkeypatch):
    """Setting an env for a probe with a "(none)" envelope must not
    enable a non-existent fix — fail-CLOSED."""
    monkeypatch.setenv("KORA_PROBE_AUTOFIX_SUPABASE_ENABLED", "true")
    issue = _make_supabase_issue()
    with patch("kora_cli.audit.emit_audit") as mock_emit:
        emit_wake_event(issue)
    details = mock_emit.call_args.kwargs["details"]
    assert details["envelope_enabled"] is False
    assert details["envelope_fix_name"] == "(none)"


# ===========================================================================
# Fail-soft
# ===========================================================================


def test_emit_audit_raises_logs_no_raise(caplog):
    def boom(**kw):
        raise RuntimeError("audit write failed")

    issue = _make_fly_issue()
    with patch("kora_cli.audit.emit_audit", side_effect=boom):
        with caplog.at_level("WARNING"):
            emit_wake_event(issue)  # MUST not raise
    assert any("emit_audit raised" in r.message for r in caplog.records)


def test_audit_import_failure_no_raise(monkeypatch, caplog):
    """Defense in depth — if the audit module itself can't be
    imported (unusual but possible in partial-install paths), the
    emitter logs + returns."""
    import builtins

    original_import = builtins.__import__

    def importer(name, *args, **kwargs):
        if name == "kora_cli.audit":
            raise ImportError("simulated audit import fail")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importer)
    issue = _make_fly_issue()
    with caplog.at_level("WARNING"):
        emit_wake_event(issue)
    assert any("audit import failed" in r.message for r in caplog.records)


# ===========================================================================
# Cheap-cron contract — no LLM invoked
# ===========================================================================


def test_emit_wake_does_not_import_reasoning():
    """The emitter must NEVER invoke (or even import) the reasoning
    engine. Spec §2 + the cheap-cron architecture pins this — actual
    reasoning invocation belongs in the wake-listener follow-on."""
    import sys

    # Snapshot module set before; emit; verify no reasoning modules
    # got newly imported.
    before = {
        m for m in sys.modules if "reasoning" in m or "anthropic" in m
    }
    issue = _make_fly_issue()
    with patch("kora_cli.audit.emit_audit"):
        emit_wake_event(issue)
    after = {
        m for m in sys.modules if "reasoning" in m or "anthropic" in m
    }
    # Module set didn't grow during emit. (If the test process imported
    # reasoning earlier — e.g., via a fixture loading the engine — the
    # set may have entries; we only check delta.)
    new_imports = after - before
    assert new_imports == set(), (
        f"emit_wake_event triggered unexpected reasoning imports: "
        f"{new_imports}"
    )
