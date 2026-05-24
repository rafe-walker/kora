"""Tests for the audit sub-plugin.

Covers:
  - Identity-against-canonical: the ``anthropic_engine`` shim's
    ``_emit_tool_called_audit`` IS the same object as the
    canonical writer (catches shim drift loudly).
  - Identity-against-canonical: the orchestrator's
    ``_post_tool_call`` + ``_post_llm_call`` re-exports are the
    same objects as the audit sub-plugin's handlers.
  - The writer emits a JSONL row through the canonical sink with
    the expected ``reasoning.tool_called`` seam.
  - The writer's structured-log line stays VERBATIM (dual-write
    contract preserved).
  - The audit sub-plugin's ``register(ctx)`` wires both expected
    hooks (post_tool_call + post_llm_call).
  - Handlers no-op on non-Kora routes; debug-log on Kora routes.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch


def test_anthropic_engine_audit_shim_is_canonical():
    """The engine's re-import shim MUST resolve to the canonical
    writer (same object). Drift means the engine has a stale
    local copy."""
    from kora_cli.reasoning.anthropic_engine import (
        _emit_tool_called_audit as engine_shim,
    )
    from kora_cli.reasoning.kora_hermes_plugin.audit.writer import (
        _emit_tool_called_audit as canonical,
    )

    assert engine_shim is canonical, (
        "audit shim drift: engine's _emit_tool_called_audit is "
        "not the canonical sub-plugin writer — fix the shim "
        "in kora_cli/reasoning/anthropic_engine.py"
    )


def test_orchestrator_reexports_audit_handlers_via_identity():
    """The orchestrator re-exports ``_post_tool_call`` +
    ``_post_llm_call`` from the audit sub-plugin so the
    discovery shim's existing import line keeps resolving."""
    from kora_cli.reasoning.kora_hermes_plugin.audit.plugin import (
        _post_llm_call as canonical_pllm,
        _post_tool_call as canonical_ptc,
    )
    from kora_cli.reasoning.kora_hermes_plugin.plugin import (
        _post_llm_call as orchestrator_pllm,
        _post_tool_call as orchestrator_ptc,
    )

    assert orchestrator_ptc is canonical_ptc
    assert orchestrator_pllm is canonical_pllm


def test_discovery_shim_reexports_resolve_to_audit_subplugin():
    """``from plugins.kora_hermes import _post_tool_call``
    (the import path tests at ``tests/plugins/test_kora_hermes
    _plugin.py`` use) must keep resolving to the audit canonical
    handler."""
    from kora_cli.reasoning.kora_hermes_plugin.audit.plugin import (
        _post_llm_call as canonical_pllm,
        _post_tool_call as canonical_ptc,
    )
    from plugins.kora_hermes import (
        _post_llm_call as discovery_pllm,
        _post_tool_call as discovery_ptc,
    )

    assert discovery_ptc is canonical_ptc
    assert discovery_pllm is canonical_pllm


def test_writer_emits_through_canonical_sink(tmp_path, caplog):
    """End-to-end: calling _emit_tool_called_audit writes a row
    through ``kora_cli.audit.emit_audit`` with the expected
    seam + details shape + preserves the structured-log line."""
    import logging

    from kora_cli.reasoning.kora_hermes_plugin.audit.writer import (
        _emit_tool_called_audit,
    )

    log_path = tmp_path / "audit.jsonl"

    with patch(
        "kora_cli.audit.jsonl_sink._resolve_log_path",
        return_value=log_path,
    ):
        caplog.set_level(logging.INFO, logger="kora_cli")
        _emit_tool_called_audit(
            tool_name="kora__alerts_search",
            triggered_by="slack_dm",
            caller_session_id="C123:1700000000.001",
            tool_duration_ms=42,
            tool_status="ok",
        )

    # Structured-log dual-write preserved
    assert any(
        "[kora.reasoning.tool_called]" in rec.getMessage()
        and "tool=kora__alerts_search" in rec.getMessage()
        and "tool_status=ok" in rec.getMessage()
        for rec in caplog.records
    )

    # JSONL row written through canonical sink
    assert log_path.exists(), "writer did not emit a JSONL row"
    rows = [json.loads(line) for line in log_path.read_text().splitlines() if line]
    assert len(rows) == 1
    row = rows[0]
    assert row["seam"] == "reasoning.tool_called"
    assert row["source"] == "reasoning"
    assert row["caller_session_id"] == "C123:1700000000.001"
    assert row["details"]["tool_name"] == "kora__alerts_search"
    assert row["details"]["tool_status"] == "ok"
    assert row["details"]["tool_duration_ms"] == 42
    assert "exc_type" not in row["details"]


def test_writer_includes_exc_type_when_provided(tmp_path):
    """When ``exc_type`` is passed, both the log line and the
    JSONL row include it."""
    from kora_cli.reasoning.kora_hermes_plugin.audit.writer import (
        _emit_tool_called_audit,
    )

    log_path = tmp_path / "audit.jsonl"
    with patch(
        "kora_cli.audit.jsonl_sink._resolve_log_path",
        return_value=log_path,
    ):
        _emit_tool_called_audit(
            tool_name="kora__test",
            triggered_by="slack_dm",
            caller_session_id="S",
            tool_duration_ms=1,
            tool_status="error",
            exc_type="ValueError",
        )

    rows = [json.loads(line) for line in log_path.read_text().splitlines() if line]
    assert rows[0]["details"]["exc_type"] == "ValueError"


def test_register_wires_post_tool_call_and_post_llm_call():
    """The audit sub-register MUST attach both expected hooks."""
    from kora_cli.reasoning.kora_hermes_plugin.audit import register
    from kora_cli.reasoning.kora_hermes_plugin.audit.plugin import (
        _post_llm_call,
        _post_tool_call,
    )

    registered: list = []

    class _Ctx:
        def register_hook(self, name, cb):
            registered.append((name, cb))

    register(_Ctx())

    names = [n for n, _ in registered]
    assert sorted(names) == ["post_llm_call", "post_tool_call"]
    cbs = dict(registered)
    assert cbs["post_tool_call"] is _post_tool_call
    assert cbs["post_llm_call"] is _post_llm_call


def test_handlers_noop_on_non_kora_routes():
    from kora_cli.reasoning.kora_hermes_plugin.audit.plugin import (
        _post_llm_call,
        _post_tool_call,
    )

    # Should not raise — empty route is the "not a Kora call" sentinel.
    _post_tool_call(tool_name="x", result=None, route="")
    _post_llm_call(route="", model="")
    _post_tool_call(tool_name="x", result=None, route="non_kora_random")
    _post_llm_call(route="non_kora_random", model="")


def test_handlers_fire_on_kora_routes_without_exception():
    from kora_cli.reasoning.kora_hermes_plugin.audit.plugin import (
        _post_llm_call,
        _post_tool_call,
    )

    _post_tool_call(tool_name="t", result={"ok": True}, route="slack_dm")
    _post_llm_call(route="slack_dm", model="claude-haiku-4-5-20251001")
