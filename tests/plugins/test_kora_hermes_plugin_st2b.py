"""ST2B tool-bridge tests for KR-REASONING-ROUTE-THROUGH-GATEWAY.

Covers the new ``pre_tool_call_can_provide_result`` Hermes hook
+ the Kora plugin's bridge handler + ``get_kora_tools_for_agent``
shape conversion + the ``_respond_via_gateway`` tool-population
integration.

ST2B scope:
  - Hook surface (added to VALID_HOOKS; fires inside
    ``handle_function_call``; first-non-None-result wins;
    fail-safe on plugin exceptions)
  - Bridge handler short-circuits Hermes dispatch for Kora's
    reasoning tools; returns None for non-Kora tools
    (Hermes-fork safety — plugin loaded on non-Kora deploys
    doesn't break their tool dispatch)
  - get_kora_tools_for_agent converts Anthropic → Hermes shape
  - _respond_via_gateway populates agent.tools from the bridge
    + agent.valid_tool_names tracks the names
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Hook surface — pre_tool_call_can_provide_result
# ---------------------------------------------------------------------------


def test_new_hook_in_valid_hooks():
    from kora_cli.plugins import VALID_HOOKS

    assert "pre_tool_call_can_provide_result" in VALID_HOOKS


def test_register_hook_accepts_new_hook_without_warning(caplog):
    """Registering the new hook MUST NOT trigger the unknown-hook
    warning — confirms it's properly in VALID_HOOKS."""
    import logging

    from kora_cli.plugins import PluginContext, PluginManifest

    caplog.set_level(logging.WARNING)
    manifest = MagicMock(spec=PluginManifest)
    manifest.name = "test_plugin"
    manager = MagicMock()
    ctx = PluginContext.__new__(PluginContext)
    ctx.manifest = manifest
    ctx._manager = manager

    ctx.register_hook(
        "pre_tool_call_can_provide_result", lambda **kw: None
    )
    warns = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert not any("pre_tool_call_can_provide_result" in w for w in warns)


# ---------------------------------------------------------------------------
# get_kora_tools_for_agent — Anthropic → Hermes shape
# ---------------------------------------------------------------------------


def test_get_kora_tools_for_agent_returns_hermes_shape():
    """Each entry is ``{"type": "function", "function": {...}}``."""
    from plugins.kora_hermes import get_kora_tools_for_agent

    tools = get_kora_tools_for_agent()
    assert len(tools) >= 1  # registry ships >= 1 tool
    for t in tools:
        assert t["type"] == "function"
        assert set(t["function"].keys()) >= {
            "name",
            "description",
            "parameters",
        }


def test_get_kora_tools_for_agent_all_names_in_allowlist():
    """Every tool returned matches the reasoning-tool allowlist —
    drift-guarded so a registry-side rename can't leak a tool
    that the bridge handler won't recognize."""
    from kora_cli.reasoning.tool_registry import REASONING_TOOL_ALLOWLIST
    from plugins.kora_hermes import get_kora_tools_for_agent

    names = {t["function"]["name"] for t in get_kora_tools_for_agent()}
    assert names <= set(REASONING_TOOL_ALLOWLIST), (
        f"agent tools contain names outside the reasoning allowlist: "
        f"{names - set(REASONING_TOOL_ALLOWLIST)}"
    )


def test_get_kora_tools_for_agent_empty_on_registry_failure(monkeypatch):
    """If the registry import / call fails, function returns [] —
    engine falls back to toolless route-through (ST2 posture)."""
    from plugins import kora_hermes as kh

    def _explode():
        raise RuntimeError("registry down")

    monkeypatch.setattr(
        "kora_cli.reasoning.tool_registry.get_reasoning_available_tools",
        _explode,
    )
    assert kh.get_kora_tools_for_agent() == []


# ---------------------------------------------------------------------------
# _tool_bridge_provide_result — bridge handler
# ---------------------------------------------------------------------------


def test_bridge_returns_none_for_non_kora_tool():
    """Hermes-fork safety: non-Kora tools → None → Hermes default
    dispatch runs unchanged."""
    from plugins.kora_hermes import _tool_bridge_provide_result

    assert _tool_bridge_provide_result(tool_name="write_file", args={}) is None
    assert _tool_bridge_provide_result(tool_name="bash", args={"cmd": "ls"}) is None
    assert _tool_bridge_provide_result(tool_name="", args={}) is None


def test_bridge_dispatches_kora_tool_to_reasoning_registry(monkeypatch):
    """Kora-allowlisted tool → bridge calls execute_reasoning_tool
    + returns ``{"result": <json>}``."""
    from plugins.kora_hermes import _tool_bridge_provide_result

    # Stub the registry's executor to return a Pydantic-like model.
    class _StubResult:
        def model_dump_json(self):
            return '{"value": "stubbed"}'

    async def _fake_execute(name, tool_input):
        assert name == "kora__get_operational_state"
        return _StubResult()

    monkeypatch.setattr(
        "kora_cli.reasoning.tool_registry.execute_reasoning_tool",
        _fake_execute,
    )

    out = _tool_bridge_provide_result(
        tool_name="kora__get_operational_state", args={}
    )
    assert isinstance(out, dict)
    assert "result" in out
    payload = json.loads(out["result"])
    assert payload == {"value": "stubbed"}


def test_bridge_handles_dispatch_exception_with_is_error_result(monkeypatch):
    """Tool dispatch raises → bridge returns
    ``{"result": '{"error": ...}'}`` so the reasoning loop sees
    a tool_result with an error (vs Hermes crashing on
    unregistered tool)."""
    from plugins.kora_hermes import _tool_bridge_provide_result

    async def _exploding_execute(name, tool_input):
        raise RuntimeError("substrate down")

    monkeypatch.setattr(
        "kora_cli.reasoning.tool_registry.execute_reasoning_tool",
        _exploding_execute,
    )

    out = _tool_bridge_provide_result(
        tool_name="kora__get_operational_state", args={}
    )
    assert isinstance(out, dict)
    assert "result" in out
    payload = json.loads(out["result"])
    assert "error" in payload
    assert "kora_tool_dispatch_error" in payload["error"]
    assert "RuntimeError" in payload["error"]


def test_bridge_handles_non_pydantic_result(monkeypatch):
    """If execute_reasoning_tool returns something without
    ``model_dump_json``, fall back to ``json.dumps(default=str)``."""
    from plugins.kora_hermes import _tool_bridge_provide_result

    async def _fake_execute(name, tool_input):
        return {"raw": "dict"}

    monkeypatch.setattr(
        "kora_cli.reasoning.tool_registry.execute_reasoning_tool",
        _fake_execute,
    )

    out = _tool_bridge_provide_result(
        tool_name="kora__get_operational_state", args={}
    )
    assert json.loads(out["result"]) == {"raw": "dict"}


# ---------------------------------------------------------------------------
# Hook fires in model_tools.handle_function_call
# ---------------------------------------------------------------------------


def test_handle_function_call_fires_provide_result_hook(monkeypatch):
    """When the hook returns ``{"result": ...}``, Hermes's
    registry.dispatch MUST be short-circuited."""
    from kora_cli.plugins import PluginManager

    captured: list = []

    class _FakeManager:
        def invoke_hook(self, name, **kw):
            captured.append((name, kw))
            if name == "pre_tool_call_can_provide_result":
                return [{"result": "stubbed-bridge-result"}]
            return []

    fake = _FakeManager()
    monkeypatch.setattr(
        "kora_cli.plugins.get_plugin_manager", lambda: fake
    )

    # Spy on registry.dispatch — it must NOT be called when the
    # hook short-circuited.
    import model_tools

    dispatch_calls: list = []

    def _spy_dispatch(name, args, **kw):
        dispatch_calls.append(name)
        return "should-not-reach"

    monkeypatch.setattr(model_tools.registry, "dispatch", _spy_dispatch)

    result = model_tools.handle_function_call("any_tool", {})
    assert result == "stubbed-bridge-result"
    assert dispatch_calls == [], (
        "registry.dispatch must NOT be called when the provide_result "
        "hook short-circuits"
    )
    # The hook fired before dispatch.
    hook_calls = [n for n, _ in captured if n == "pre_tool_call_can_provide_result"]
    assert len(hook_calls) == 1


def test_handle_function_call_falls_through_on_no_provide(monkeypatch):
    """When no plugin returns ``{"result": ...}``, Hermes's default
    dispatch must run."""

    class _FakeManager:
        def invoke_hook(self, name, **kw):
            if name == "pre_tool_call_can_provide_result":
                return [None, {"other": "ignored"}, {}]  # all no-op
            return []

    monkeypatch.setattr(
        "kora_cli.plugins.get_plugin_manager", lambda: _FakeManager()
    )

    import model_tools

    dispatch_calls: list = []

    def _spy_dispatch(name, args, **kw):
        dispatch_calls.append(name)
        return "hermes-default-result"

    monkeypatch.setattr(model_tools.registry, "dispatch", _spy_dispatch)

    result = model_tools.handle_function_call("any_tool", {})
    assert result == "hermes-default-result"
    assert dispatch_calls == ["any_tool"]


def test_handle_function_call_hook_exception_falls_through(
    monkeypatch, caplog
):
    """Plugin raises in the hook → caught + debug-logged → Hermes
    default dispatch runs (fail-safe)."""
    import logging

    class _FakeManager:
        def invoke_hook(self, name, **kw):
            if name == "pre_tool_call_can_provide_result":
                raise RuntimeError("plugin imploded")
            return []

    monkeypatch.setattr(
        "kora_cli.plugins.get_plugin_manager", lambda: _FakeManager()
    )

    import model_tools

    def _spy_dispatch(name, args, **kw):
        return "hermes-default-after-hook-failure"

    monkeypatch.setattr(model_tools.registry, "dispatch", _spy_dispatch)
    caplog.set_level(logging.DEBUG)
    result = model_tools.handle_function_call("any_tool", {})
    assert result == "hermes-default-after-hook-failure"


# ---------------------------------------------------------------------------
# End-to-end: _respond_via_gateway populates tools + dispatch fires
# ---------------------------------------------------------------------------


def _make_incoming(text: str = "what's my burn?", source: str = "slack_dm"):
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


@pytest.fixture
def system_prompt_path(tmp_path):
    p = tmp_path / "kora_system_prompt.md"
    p.write_text("You are Kora.\n", encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _oauth_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-test")


def _make_engine(system_prompt_path):
    from kora_cli.reasoning.anthropic_engine import AnthropicReasoningEngine

    return AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=MagicMock()
    )


@pytest.mark.asyncio
async def test_respond_via_gateway_populates_agent_tools(
    monkeypatch, system_prompt_path
):
    """When the toggle is ON, _respond_via_gateway populates
    agent.tools with Kora's 5 reasoning tools (replaces ST2's
    toolless ``= []``)."""
    fake_agent = MagicMock()
    fake_agent.model = "claude-haiku-4-5-20251001"
    fake_agent.run_conversation = lambda u, s=None: {
        "final_response": "ok",
        "model": "claude-haiku-4-5-20251001",
        "input_tokens": 1,
        "output_tokens": 1,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "completed": True,
        "interrupted": False,
    }
    fake_class = MagicMock(return_value=fake_agent)
    monkeypatch.setattr("run_agent.AIAgent", fake_class)
    monkeypatch.setenv("KORA_REASONING_USE_GATEWAY", "true")

    engine = _make_engine(system_prompt_path)
    await engine.respond(_make_incoming(), _make_context())

    # agent.tools populated from Kora's registry — Hermes-shape.
    assert isinstance(fake_agent.tools, list)
    assert len(fake_agent.tools) >= 1
    names = {
        t["function"]["name"]
        for t in fake_agent.tools
        if isinstance(t, dict) and "function" in t
    }
    assert "kora__get_operational_state" in names
    assert fake_agent.valid_tool_names == names


@pytest.mark.asyncio
async def test_respond_via_gateway_falls_back_when_tools_unavailable(
    monkeypatch, system_prompt_path
):
    """get_kora_tools_for_agent raises → engine falls back to
    agent.tools = [] (toolless route-through; ST2 posture)."""
    fake_agent = MagicMock()
    fake_agent.run_conversation = lambda u, s=None: {
        "final_response": "ok",
        "model": "h",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "completed": True,
        "interrupted": False,
    }
    fake_class = MagicMock(return_value=fake_agent)
    monkeypatch.setattr("run_agent.AIAgent", fake_class)
    monkeypatch.setenv("KORA_REASONING_USE_GATEWAY", "true")

    monkeypatch.setattr(
        "plugins.kora_hermes.get_kora_tools_for_agent",
        lambda: (_ for _ in ()).throw(RuntimeError("registry down")),
    )

    engine = _make_engine(system_prompt_path)
    await engine.respond(_make_incoming(), _make_context())
    assert fake_agent.tools == []
    assert fake_agent.valid_tool_names == set()


# ---------------------------------------------------------------------------
# Sample tool-use trace: end-to-end through handle_function_call
# ---------------------------------------------------------------------------


@pytest.fixture
def _register_bridge_hook_in_global_manager():
    """Append the bridge handler to the process-global
    PluginManager's hook list for a sample-trace test, then
    remove it on teardown. Uses try/finally semantic via yield
    so a test failure inside the body still cleans up —
    without this, a partial test run pollutes downstream tests
    that rely on un-registered hook state (the global manager
    is a singleton across tests)."""
    from kora_cli.plugins import get_plugin_manager
    from plugins.kora_hermes import _tool_bridge_provide_result

    mgr = get_plugin_manager()
    mgr._hooks.setdefault(
        "pre_tool_call_can_provide_result", []
    ).append(_tool_bridge_provide_result)
    try:
        yield _tool_bridge_provide_result
    finally:
        callbacks = mgr._hooks.get(
            "pre_tool_call_can_provide_result", []
        )
        if _tool_bridge_provide_result in callbacks:
            callbacks.remove(_tool_bridge_provide_result)


def test_sample_tool_use_trace_kora_tool_via_bridge(
    monkeypatch, _register_bridge_hook_in_global_manager
):
    """Sample trace: Hermes asks to dispatch ``kora__get_
    operational_state`` → bridge intercepts → Kora's
    execute_reasoning_tool runs → result string is returned to
    Hermes loop. (No Hermes registry.dispatch call.)"""
    import model_tools

    # 1. Stub execute_reasoning_tool to return a controlled
    # result (avoids substrate-dep flakiness).
    class _Stub:
        def model_dump_json(self):
            return '{"primary_state": "ready", "claim_permission": "normal"}'

    async def _fake_execute(name, tool_input):
        return _Stub()

    monkeypatch.setattr(
        "kora_cli.reasoning.tool_registry.execute_reasoning_tool",
        _fake_execute,
    )

    # 2. Spy on Hermes default dispatch — MUST NOT fire.
    dispatch_calls: list = []
    monkeypatch.setattr(
        model_tools.registry,
        "dispatch",
        lambda name, args, **kw: dispatch_calls.append(name) or "WRONG",
    )

    # 3. Trace: invoke handle_function_call as Hermes would.
    result = model_tools.handle_function_call(
        function_name="kora__get_operational_state",
        function_args={},
        task_id="trace_test",
    )

    # 4. Verify the trace:
    # - Hermes dispatch NEVER fired
    assert dispatch_calls == []
    # - Bridge result is the Kora result JSON
    parsed = json.loads(result)
    assert parsed["primary_state"] == "ready"
    assert parsed["claim_permission"] == "normal"


def test_sample_tool_use_trace_non_kora_tool_falls_through(
    monkeypatch, _register_bridge_hook_in_global_manager
):
    """Sample trace: Hermes asks to dispatch ``write_file`` (a
    Hermes-native tool, NOT in Kora's allowlist) → bridge returns
    None → Hermes default dispatch runs unchanged. Confirms
    Hermes-fork users with the plugin loaded see no regression
    on their own tools."""
    import model_tools

    dispatch_calls: list = []
    monkeypatch.setattr(
        model_tools.registry,
        "dispatch",
        lambda name, args, **kw: (
            dispatch_calls.append(name) or "hermes-default"
        ),
    )

    result = model_tools.handle_function_call(
        function_name="write_file",
        function_args={"path": "/tmp/x", "content": "hi"},
    )

    # Hermes dispatch DID fire for non-Kora tool — fork users safe.
    assert dispatch_calls == ["write_file"]
    assert result == "hermes-default"


# ---------------------------------------------------------------------------
# Bypass-path regression — toggle OFF unchanged
# ---------------------------------------------------------------------------


def _fake_anthropic_response(text: str = "bypass works"):
    usage = MagicMock()
    usage.input_tokens = 10
    usage.output_tokens = 5
    usage.cache_creation_input_tokens = 0
    usage.cache_read_input_tokens = 0
    block = MagicMock()
    block.type = "text"
    block.text = text
    r = MagicMock()
    r.content = [block]
    r.stop_reason = "end_turn"
    r.model = "claude-haiku-4-5-20251001"
    r.usage = usage
    return r


@pytest.mark.asyncio
async def test_toggle_off_bypass_unchanged_post_st2b(
    monkeypatch, system_prompt_path
):
    """ST2B's tool-bridge changes don't leak into the toggle-OFF
    bypass path. Default behavior: existing bypass runs cleanly."""
    monkeypatch.delenv("KORA_REASONING_USE_GATEWAY", raising=False)
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)

    fake_aiagent_class = MagicMock()
    monkeypatch.setattr("run_agent.AIAgent", fake_aiagent_class)

    from kora_cli.reasoning.anthropic_engine import (
        AnthropicReasoningEngine,
    )

    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(
        return_value=_fake_anthropic_response("bypass works")
    )
    client.close = AsyncMock()
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )

    result = await engine.respond(_make_incoming(), _make_context())
    assert result.text == "bypass works"
    assert result.error is None
    fake_aiagent_class.assert_not_called()
