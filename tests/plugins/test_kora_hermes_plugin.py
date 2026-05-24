"""Tests for the kora_hermes bundled plugin (ST1 scaffold).

Covers:
  - Plugin is auto-discovered + loaded by ``PluginManager``
  - 6 hooks registered against the plugin manager
  - KORA_ROUTES is in sync with kora_cli.telemetry KNOWN_ROUTES
    (minus the "unknown" sentinel)
  - Hook handlers no-op gracefully on non-Kora routes (empty route
    or unknown route literal)
  - Hook handlers fire for Kora routes (return None / no exception
    — ST2 plumbs actual behavior, ST1 just proves the gate)
  - The ``_source_to_kora_route`` helper maps the 3 known sources
    correctly + returns "" on unknown
  - Engine toggle: KORA_REASONING_USE_GATEWAY off → bypass path
    runs (no NotImplementedError); on → _respond_via_gateway
    raises NotImplementedError (ST1 by design)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# KORA_ROUTES in-sync check
# ---------------------------------------------------------------------------


def test_kora_routes_in_sync_with_known_routes():
    """KORA_ROUTES MUST equal KNOWN_ROUTES minus the "unknown"
    sentinel. Drift means either a new route was added to
    telemetry without updating the plugin's gate OR vice versa —
    both are operator-confusing bugs."""
    from kora_cli.telemetry import KNOWN_ROUTES
    from plugins.kora_hermes import KORA_ROUTES

    expected = set(KNOWN_ROUTES) - {"unknown"}
    assert set(KORA_ROUTES) == expected, (
        f"drift between plugin KORA_ROUTES + telemetry KNOWN_ROUTES: "
        f"plugin has {sorted(KORA_ROUTES)}, expected {sorted(expected)}"
    )


# ---------------------------------------------------------------------------
# Plugin discovery
# ---------------------------------------------------------------------------


def test_plugin_is_discovered_but_opt_in():
    """Bundled plugin at plugins/kora_hermes/ is DISCOVERED by
    ``PluginManager.discover_and_load`` (parsed manifest enters
    the plugins dict) but loads only when the operator opts in
    via ``plugins.enabled`` (standalone-plugin contract in
    Hermes). The kora-runtime deploy config enables it; tests
    use a different fixture to exercise the plugin in isolation."""
    from kora_cli.plugins import PluginManager

    mgr = PluginManager()
    mgr.discover_and_load(force=True)
    # Manifest discovered + entered the registry even though
    # disabled — operator-opt-in policy.
    assert "kora_hermes" in mgr._plugins, (
        f"kora_hermes manifest not discovered; got: "
        f"{sorted(mgr._plugins.keys())}"
    )
    # By default (no plugins.enabled tweak), the load is skipped
    # with an explanatory error.
    loaded = mgr._plugins["kora_hermes"]
    assert loaded.enabled is False
    assert "not enabled in config" in (loaded.error or "")


def test_register_function_wires_six_hooks():
    """The plugin's register(ctx) function registers exactly 6
    hooks. Test directly with a mock context — bypasses Hermes's
    opt-in plugins.enabled gate (which is operator-policy
    territory, not the plugin's responsibility)."""
    from plugins.kora_hermes import register

    registered = []

    class _MockCtx:
        def register_hook(self, name, callback):
            registered.append((name, callback))

    register(_MockCtx())
    hook_names = [name for name, _ in registered]
    assert sorted(hook_names) == sorted([
        "on_session_start",
        "pre_api_request_mutable",
        "pre_tool_list_finalized",
        "pre_tool_call",
        "post_tool_call",
        "post_llm_call",
    ])
    # Each registered callback is callable.
    for name, callback in registered:
        assert callable(callback), f"{name} callback isn't callable"


# ---------------------------------------------------------------------------
# _is_kora_call gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "route,expected",
    [
        ("slack_dm", True),
        ("email_inbound", True),
        ("email_outbound_compose", True),
        ("mcp_tool", True),
        ("alert_investigation", True),
        ("probe_investigation", True),
        ("tool_loop_iteration", True),
        ("scheduled_task", True),
        ("", False),  # sentinel for "not a Kora call"
        ("unknown", False),  # telemetry's bucket-other literal
        ("non_kora_random", False),
        (None, False),
        (42, False),
    ],
)
def test_is_kora_call(route, expected):
    from plugins.kora_hermes import _is_kora_call

    assert _is_kora_call(route) is expected


# ---------------------------------------------------------------------------
# Hook handlers — no-op on non-Kora calls; fire on Kora calls
# ---------------------------------------------------------------------------


def test_pre_api_request_mutable_returns_none_on_non_kora(caplog):
    """Plugin must NOT mutate api_kwargs when the call isn't a
    Kora-tagged call. Returning None lets Hermes proceed with the
    api_kwargs unmodified — critical for hermes-fork users who
    aren't running Kora."""
    import logging

    from plugins.kora_hermes import _pre_api_request_mutable

    caplog.set_level(logging.DEBUG)
    result = _pre_api_request_mutable(
        route="",
        api_kwargs={"model": "claude-opus-4-7", "max_tokens": 100},
    )
    assert result is None


# ``test_pre_api_request_mutable_returns_none_on_kora_st1`` — removed
# in ST2 wire-up. ST2 replaced the stub with the real cost-router +
# caching logic; the new contract (override dict returned for Kora-
# tagged calls) is covered in test_kora_hermes_plugin_st2.py.


def test_pre_tool_list_finalized_returns_none_st1():
    from plugins.kora_hermes import _pre_tool_list_finalized

    # Non-Kora route: no-op.
    assert (
        _pre_tool_list_finalized(route="", tools=[{"name": "x"}]) is None
    )
    # Kora route: also no-op in ST1 (debug-only).
    assert (
        _pre_tool_list_finalized(route="slack_dm", tools=[{"name": "x"}])
        is None
    )


def test_pre_tool_call_returns_none_st1():
    from plugins.kora_hermes import _pre_tool_call

    assert _pre_tool_call(tool_name="any", args={}, route="") is None
    assert _pre_tool_call(tool_name="any", args={}, route="slack_dm") is None


def test_post_tool_call_no_exception():
    """post_tool_call is observer-only; just ensure no exception."""
    from plugins.kora_hermes import _post_tool_call

    _post_tool_call(tool_name="t", result={"ok": True}, route="")
    _post_tool_call(tool_name="t", result={"ok": True}, route="slack_dm")


def test_post_llm_call_no_exception():
    from plugins.kora_hermes import _post_llm_call

    _post_llm_call(route="")
    _post_llm_call(route="slack_dm")


def test_on_session_start_no_exception():
    from plugins.kora_hermes import _on_session_start

    _on_session_start(route="")
    _on_session_start(route="slack_dm")


# ---------------------------------------------------------------------------
# _source_to_kora_route mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source,expected_route",
    [
        ("slack_dm", "slack_dm"),
        ("email", "email_inbound"),
        ("mcp", "mcp_tool"),
        ("unknown", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_source_to_kora_route(source, expected_route):
    from kora_cli.reasoning.anthropic_engine import _source_to_kora_route

    assert _source_to_kora_route(source) == expected_route


# ---------------------------------------------------------------------------
# Engine toggle — default off + opt-in raises NotImplementedError
# ---------------------------------------------------------------------------


def _make_incoming(text: str = "hi", source: str = "slack_dm"):
    from kora_cli.reasoning.engine import IncomingMessage

    return IncomingMessage(
        text=text,
        source=source,
        received_at=datetime.now(timezone.utc),
        metadata={},
    )


def _make_context(rung: str = "normal", state: str = "ready"):
    from kora_cli.reasoning.engine import ConversationContext

    return ConversationContext(
        recent_messages=[],
        current_operational_state=state,
        current_cost_ladder_rung=rung,
    )


def _make_engine(system_prompt_path, response):
    from kora_cli.reasoning.anthropic_engine import AnthropicReasoningEngine

    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=response)
    client.close = AsyncMock()
    return AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )


@pytest.fixture
def system_prompt_path(tmp_path):
    p = tmp_path / "kora_system_prompt.md"
    p.write_text("You are Kora.\n", encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _oauth(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-test")


def _fake_response(text: str = "hi", model: str = "claude-haiku-4-5-20251001"):
    usage = MagicMock()
    usage.input_tokens = 10
    usage.output_tokens = 5
    usage.cache_creation_input_tokens = 0
    usage.cache_read_input_tokens = 0
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = text
    r = MagicMock()
    r.content = [text_block]
    r.stop_reason = "end_turn"
    r.model = model
    r.usage = usage
    return r


@pytest.mark.asyncio
async def test_toggle_off_uses_bypass_path(
    monkeypatch, system_prompt_path
):
    """Default behavior: KORA_REASONING_USE_GATEWAY unset →
    existing bypass path runs. No NotImplementedError."""
    monkeypatch.delenv("KORA_REASONING_USE_GATEWAY", raising=False)
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)

    engine = _make_engine(system_prompt_path, _fake_response("hi"))
    result = await engine.respond(_make_incoming(), _make_context())
    assert result.error is None
    assert result.text == "hi"


@pytest.mark.asyncio
async def test_toggle_explicit_false_uses_bypass(
    monkeypatch, system_prompt_path
):
    monkeypatch.setenv("KORA_REASONING_USE_GATEWAY", "false")
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)

    engine = _make_engine(system_prompt_path, _fake_response("hi"))
    result = await engine.respond(_make_incoming(), _make_context())
    assert result.error is None


# ``test_toggle_on_routes_to_gateway_st1_not_implemented`` +
# ``test_toggle_on_resolves_route_before_raising`` +
# ``test_toggle_on_unknown_source_resolves_to_empty_route`` —
# removed in ST2 wire-up. ST2 replaced the NotImplementedError
# stub with the actual AIAgent route-through path; toggle-on
# behavior is now covered by tests in
# test_kora_hermes_plugin_st2.py (end_to_end + paused +
# hard_stop + interrupted + exception + route-threading
# variants).
