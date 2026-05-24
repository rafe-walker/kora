"""Tests for the caching sub-plugin's marker helpers + shim.

Covers:
  - Identity-against-canonical: the ``anthropic_engine`` shim's
    ``_wrap_system_as_cacheable`` + ``_wrap_tools_as_cacheable``
    ARE the same objects as the canonical caching markers.
  - The cost-ladder plugin's hook handler imports markers from
    the canonical caching location (NOT from anthropic_engine)
    — proven by patching the canonical location and watching
    the cost-ladder hook honor it.
  - ``_wrap_system_as_cacheable`` shapes a bare-string prompt
    into a single content block with ``cache_control: ephemeral``.
  - ``_wrap_tools_as_cacheable`` marks the LAST tool only +
    leaves earlier tools untouched + never mutates the input.
  - ``_wrap_tools_as_cacheable`` returns empty list on empty
    input.
  - ``register(ctx)`` is intentionally a no-op (does not call
    ctx.register_hook — the bundled cost-ladder handler still
    owns the single pre_api_request_mutable fire).

Existing engine-level cache token accounting tests at
``tests/kora_cli/reasoning/test_anthropic_engine_caching.py``
remain canonical for the engine-resident behavior (those test
how the engine wires the wrappers into ``_make_request_kwargs``
and ResponseResult accounting).
"""

from __future__ import annotations


def test_anthropic_engine_caching_shim_is_canonical():
    """The engine's re-import shim MUST resolve to the canonical
    caching markers. Drift means the engine has a stale local
    copy of either wrapper."""
    from kora_cli.reasoning.anthropic_engine import (
        _wrap_system_as_cacheable as engine_sys,
        _wrap_tools_as_cacheable as engine_tools,
    )
    from kora_cli.reasoning.kora_hermes_plugin.caching.markers import (
        _wrap_system_as_cacheable as canonical_sys,
        _wrap_tools_as_cacheable as canonical_tools,
    )

    assert engine_sys is canonical_sys, (
        "caching system shim drift — fix anthropic_engine.py"
    )
    assert engine_tools is canonical_tools, (
        "caching tools shim drift — fix anthropic_engine.py"
    )


def test_cost_ladder_plugin_imports_markers_from_canonical_location():
    """The cost-ladder hook handler's caching wrap MUST import
    from ``kora_hermes_plugin.caching.markers`` (NOT from
    anthropic_engine). This is the explicit cross-dep cleanup
    that motivated Deliverable B.

    Proven by patching the canonical caching markers and
    confirming the cost-ladder hook honors the patched version
    (would fail if it still imported from the engine module).
    """
    from unittest.mock import patch

    from kora_cli.reasoning.kora_hermes_plugin.cost_ladder.plugin import (
        cost_ladder_and_caching_hook,
    )

    sentinel = [{"type": "text", "text": "<patched>", "cache_control": {"type": "ephemeral"}}]

    def _patched_sys(_prompt):
        return sentinel

    # If the hook still imports from anthropic_engine, patching
    # the canonical markers module is a no-op and the assertion
    # fails. The lazy-inside-function import in the hook resolves
    # at call time, so the patch on the canonical module IS what
    # the hook sees.
    with patch(
        "kora_cli.reasoning.kora_hermes_plugin.caching.markers."
        "_wrap_system_as_cacheable",
        _patched_sys,
    ):
        result = cost_ladder_and_caching_hook(
            route="slack_dm",
            api_kwargs={"system": "you are kora", "tools": []},
            api_call_count=1,
            user_message="hi",
        )

    assert result is not None
    assert result["override"]["system"] is sentinel


def test_wrap_system_shape():
    from kora_cli.reasoning.kora_hermes_plugin.caching import (
        _wrap_system_as_cacheable,
    )

    out = _wrap_system_as_cacheable("you are kora")
    assert isinstance(out, list)
    assert len(out) == 1
    assert out[0] == {
        "type": "text",
        "text": "you are kora",
        "cache_control": {"type": "ephemeral"},
    }


def test_wrap_tools_marks_last_only():
    from kora_cli.reasoning.kora_hermes_plugin.caching import (
        _wrap_tools_as_cacheable,
    )

    tools = [
        {"name": "tool_a", "input_schema": {}},
        {"name": "tool_b", "input_schema": {}},
        {"name": "tool_c", "input_schema": {}},
    ]
    out = _wrap_tools_as_cacheable(tools)

    assert len(out) == 3
    assert "cache_control" not in out[0]
    assert "cache_control" not in out[1]
    assert out[2]["cache_control"] == {"type": "ephemeral"}
    # input untouched
    for t in tools:
        assert "cache_control" not in t


def test_wrap_tools_empty_input():
    from kora_cli.reasoning.kora_hermes_plugin.caching import (
        _wrap_tools_as_cacheable,
    )

    assert _wrap_tools_as_cacheable([]) == []


def test_register_is_intentional_noop():
    """Caching's register MUST NOT call ctx.register_hook today
    — the bundled cost-ladder handler still owns the single
    pre_api_request_mutable fire. Registering caching_hook
    would double-fire the cache marker wrap."""
    from kora_cli.reasoning.kora_hermes_plugin.caching import register

    registered: list = []

    class _Ctx:
        def register_hook(self, name, cb):
            registered.append((name, cb))

    register(_Ctx())
    assert registered == [], (
        "caching/plugin.register() unexpectedly registered hooks — "
        "this would double-fire with the bundled cost-ladder hook"
    )


def test_standalone_caching_hook_returns_override_on_kora_route():
    """The standalone caching_hook (available for a future split
    but not currently registered) behaves correctly when invoked
    directly: returns an override on Kora routes."""
    from kora_cli.reasoning.kora_hermes_plugin.caching import caching_hook

    out = caching_hook(
        route="slack_dm",
        api_kwargs={
            "system": "you are kora",
            "tools": [{"name": "t1"}, {"name": "t2"}],
        },
    )
    assert out is not None
    override = out["override"]
    assert isinstance(override["system"], list)
    assert override["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert override["tools"][-1]["cache_control"] == {"type": "ephemeral"}


def test_standalone_caching_hook_noop_on_non_kora_route():
    from kora_cli.reasoning.kora_hermes_plugin.caching import caching_hook

    assert caching_hook(route="", api_kwargs={"system": "x"}) is None
    assert (
        caching_hook(route="non_kora", api_kwargs={"system": "x"}) is None
    )
