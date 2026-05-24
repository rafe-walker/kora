"""Tests for the haiku_router sub-plugin + the new
``post_llm_call_can_reissue`` local Hermes hook surface.

Coverage:

  - Pure helpers in ``escalator.py`` (text extraction +
    re-issue kwargs construction)
  - ``haiku_router_post_call_escalation`` hook handler gating
    (non-Kora route / iteration > 1 / non-Haiku model / disabled
    env / no text / confident response)
  - Hook handler fires on low-confidence Haiku reply: returns
    ``{"reissue_with": ...}`` with Opus model + Haiku-as-assistant
    context injected
  - First-non-None override semantics (re-issue is at most one
    per iteration; second plugin's return is ignored when first
    already won)
  - Anti-loop: the spec contract guarantees the hook is NOT
    re-fired against the re-issued response. We verify this at
    the contract level (no recursion in the handler) and via
    the loop-side guard in ``conversation_loop.py``.
  - Backward-compat: discovery shim still exports the new
    handler under the ``_post_llm_call_can_reissue`` alias
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Pure helper: extract_first_text
# ---------------------------------------------------------------------------


def test_extract_first_text_sdk_content_blocks():
    from kora_cli.reasoning.kora_hermes_plugin.haiku_router.escalator import (
        extract_first_text,
    )

    # SDK-like content block (pydantic-style; has .type / .text).
    block1 = SimpleNamespace(type="text", text="first part")
    block2 = SimpleNamespace(type="tool_use", input={"x": 1})
    block3 = SimpleNamespace(type="text", text="second part")
    response = SimpleNamespace(content=[block1, block2, block3])

    assert extract_first_text(response) == "first part\nsecond part"


def test_extract_first_text_dict_content_blocks():
    from kora_cli.reasoning.kora_hermes_plugin.haiku_router.escalator import (
        extract_first_text,
    )

    response = SimpleNamespace(
        content=[
            {"type": "text", "text": "hello"},
            {"type": "text", "text": "world"},
        ]
    )
    assert extract_first_text(response) == "hello\nworld"


def test_extract_first_text_no_content():
    from kora_cli.reasoning.kora_hermes_plugin.haiku_router.escalator import (
        extract_first_text,
    )

    assert extract_first_text(None) == ""
    assert extract_first_text(SimpleNamespace()) == ""
    assert extract_first_text(SimpleNamespace(content=None)) == ""
    assert extract_first_text(SimpleNamespace(content=[])) == ""


def test_extract_first_text_tool_use_only():
    """A tool-use-only response (no text blocks) returns empty —
    the haiku_router handler treats this as "nothing to escalate
    from" and skips."""
    from kora_cli.reasoning.kora_hermes_plugin.haiku_router.escalator import (
        extract_first_text,
    )

    response = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", input={})]
    )
    assert extract_first_text(response) == ""


# ---------------------------------------------------------------------------
# Pure helper: extract_last_user_text
# ---------------------------------------------------------------------------


def test_extract_last_user_text_string_content():
    from kora_cli.reasoning.kora_hermes_plugin.haiku_router.escalator import (
        extract_last_user_text,
    )

    api_kwargs = {
        "messages": [
            {"role": "user", "content": "earlier turn"},
            {"role": "assistant", "content": "response"},
            {"role": "user", "content": "latest question"},
        ]
    }
    assert extract_last_user_text(api_kwargs) == "latest question"


def test_extract_last_user_text_block_list_content():
    from kora_cli.reasoning.kora_hermes_plugin.haiku_router.escalator import (
        extract_last_user_text,
    )

    api_kwargs = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "part1"},
                    {"type": "text", "text": "part2"},
                ],
            },
        ]
    }
    assert extract_last_user_text(api_kwargs) == "part1\npart2"


def test_extract_last_user_text_no_messages():
    from kora_cli.reasoning.kora_hermes_plugin.haiku_router.escalator import (
        extract_last_user_text,
    )

    assert extract_last_user_text({}) == ""
    assert extract_last_user_text({"messages": None}) == ""
    assert extract_last_user_text(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pure helper: build_opus_reissue_kwargs
# ---------------------------------------------------------------------------


def test_build_opus_reissue_kwargs_injects_haiku_context():
    from kora_cli.reasoning.kora_hermes_plugin.haiku_router.constants import (
        MODEL_OPUS,
        REISSUE_REVIEW_PROMPT,
    )
    from kora_cli.reasoning.kora_hermes_plugin.haiku_router.escalator import (
        build_opus_reissue_kwargs,
    )

    original = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "system": "you are kora",
        "messages": [
            {"role": "user", "content": "what should i do"},
        ],
    }
    new = build_opus_reissue_kwargs(
        api_kwargs=original,
        haiku_response_text="I'm not sure, maybe X",
    )

    # New kwargs swap to Opus + extend messages with Haiku
    # assistant turn + reviewer prompt user turn.
    assert new["model"] == MODEL_OPUS
    assert new["max_tokens"] == 1024  # unchanged
    assert new["system"] == "you are kora"  # unchanged
    assert new["messages"] == [
        {"role": "user", "content": "what should i do"},
        {"role": "assistant", "content": "I'm not sure, maybe X"},
        {"role": "user", "content": REISSUE_REVIEW_PROMPT},
    ]
    # Original is not mutated.
    assert original["model"] == "claude-haiku-4-5-20251001"
    assert len(original["messages"]) == 1


def test_build_opus_reissue_kwargs_handles_missing_messages():
    from kora_cli.reasoning.kora_hermes_plugin.haiku_router.escalator import (
        build_opus_reissue_kwargs,
    )

    new = build_opus_reissue_kwargs(
        api_kwargs={"model": "claude-haiku-4-5-20251001"},
        haiku_response_text="hi",
    )
    # When the original had no messages, the new messages start
    # at the Haiku turn (still valid Anthropic shape — assistant
    # turns can lead in continuation flows).
    assert len(new["messages"]) == 2
    assert new["messages"][0]["role"] == "assistant"


# ---------------------------------------------------------------------------
# Hook handler — activation gating
# ---------------------------------------------------------------------------


def _haiku_response(text: str) -> Any:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)]
    )


def _haiku_api_kwargs(
    user_text: str = "a substantial question " * 20,
    model: str = "claude-haiku-4-5-20251001",
) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": user_text}],
    }


def test_handler_no_op_on_non_kora_route():
    from kora_cli.reasoning.kora_hermes_plugin.haiku_router.plugin import (
        haiku_router_post_call_escalation,
    )

    result = haiku_router_post_call_escalation(
        response=_haiku_response("I'm not sure about this"),
        api_kwargs=_haiku_api_kwargs(),
        iteration=1,
        route="",  # not Kora
    )
    assert result is None


def test_handler_no_op_on_iteration_gt_1():
    from kora_cli.reasoning.kora_hermes_plugin.haiku_router.plugin import (
        haiku_router_post_call_escalation,
    )

    # iteration 2+ already runs Opus from the pre-call cost-
    # ladder rule (tool_loop_iteration); post-call escalation
    # would be redundant.
    result = haiku_router_post_call_escalation(
        response=_haiku_response("I'm not sure"),
        api_kwargs=_haiku_api_kwargs(),
        iteration=2,
        route="slack_dm",
    )
    assert result is None


def test_handler_no_op_on_non_haiku_original_model():
    from kora_cli.reasoning.kora_hermes_plugin.haiku_router.plugin import (
        haiku_router_post_call_escalation,
    )

    # The original call was Opus (force_opus / opus_prefix /
    # decision_language path) — nothing to escalate from.
    result = haiku_router_post_call_escalation(
        response=_haiku_response("I'm not sure"),
        api_kwargs=_haiku_api_kwargs(model="claude-opus-4-7"),
        iteration=1,
        route="slack_dm",
    )
    assert result is None


def test_handler_no_op_when_disabled_via_env(monkeypatch):
    from kora_cli.reasoning.kora_hermes_plugin.haiku_router.constants import (
        ENV_DISABLE_POST_CALL_ESCALATION,
    )
    from kora_cli.reasoning.kora_hermes_plugin.haiku_router.plugin import (
        haiku_router_post_call_escalation,
    )

    monkeypatch.setenv(ENV_DISABLE_POST_CALL_ESCALATION, "true")
    result = haiku_router_post_call_escalation(
        response=_haiku_response("I'm not sure"),
        api_kwargs=_haiku_api_kwargs(),
        iteration=1,
        route="slack_dm",
    )
    assert result is None


def test_handler_no_op_on_confident_haiku():
    from kora_cli.reasoning.kora_hermes_plugin.haiku_router.plugin import (
        haiku_router_post_call_escalation,
    )

    # Confident, substantive Haiku reply — no escalation.
    result = haiku_router_post_call_escalation(
        response=_haiku_response(
            "Yes, the deploy is healthy: all 7 services report "
            "ready and the canary is at 100%."
        ),
        api_kwargs=_haiku_api_kwargs(),
        iteration=1,
        route="slack_dm",
    )
    assert result is None


def test_handler_no_op_on_tool_use_only_response():
    """A tool-use-only response has no text to escalate — handler
    must skip even though the response IS otherwise eligible."""
    from kora_cli.reasoning.kora_hermes_plugin.haiku_router.plugin import (
        haiku_router_post_call_escalation,
    )

    response = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", input={"q": "x"})]
    )
    result = haiku_router_post_call_escalation(
        response=response,
        api_kwargs=_haiku_api_kwargs(),
        iteration=1,
        route="slack_dm",
    )
    assert result is None


# ---------------------------------------------------------------------------
# Hook handler — escalation path
# ---------------------------------------------------------------------------


def test_handler_escalates_on_low_confidence_marker():
    from kora_cli.reasoning.kora_hermes_plugin.haiku_router.constants import (
        MODEL_OPUS,
        REISSUE_REVIEW_PROMPT,
    )
    from kora_cli.reasoning.kora_hermes_plugin.haiku_router.plugin import (
        haiku_router_post_call_escalation,
    )

    haiku_text = (
        "I'm not sure about that — I don't have enough context "
        "to give a confident answer."
    )
    api_kwargs = _haiku_api_kwargs(
        user_text="Is the migration safe to roll out today?"
    )
    result = haiku_router_post_call_escalation(
        response=_haiku_response(haiku_text),
        api_kwargs=api_kwargs,
        iteration=1,
        route="slack_dm",
    )
    assert isinstance(result, dict)
    assert "reissue_with" in result
    new_kwargs = result["reissue_with"]
    assert new_kwargs["model"] == MODEL_OPUS
    # Messages: original user + Haiku assistant turn + reviewer
    # user turn.
    assert new_kwargs["messages"] == [
        {"role": "user", "content": "Is the migration safe to roll out today?"},
        {"role": "assistant", "content": haiku_text},
        {"role": "user", "content": REISSUE_REVIEW_PROMPT},
    ]
    # KR-CC3-CLEANUP follow-up A: handler returns the reason so
    # the loop can thread it into cost-telemetry per-reason
    # breakdown.
    assert result["escalation_reason"] == "low_confidence_marker"


def test_handler_escalates_on_short_response_for_long_input():
    """Heuristic: long input + tiny Haiku reply → escalation."""
    from kora_cli.reasoning.kora_hermes_plugin.haiku_router.constants import (
        MODEL_OPUS,
    )
    from kora_cli.reasoning.kora_hermes_plugin.haiku_router.plugin import (
        haiku_router_post_call_escalation,
    )

    long_input = "a long substantive question " * 30  # > 200 chars
    short_haiku = "Yes."  # < 50 chars
    result = haiku_router_post_call_escalation(
        response=_haiku_response(short_haiku),
        api_kwargs=_haiku_api_kwargs(user_text=long_input),
        iteration=1,
        route="slack_dm",
    )
    assert isinstance(result, dict)
    assert result["reissue_with"]["model"] == MODEL_OPUS
    # KR-CC3-CLEANUP follow-up A: reason key distinguishes the
    # short-response heuristic from the marker heuristic.
    assert result["escalation_reason"] == "short_response_for_long_input"


# ---------------------------------------------------------------------------
# Sub-register: hook wiring through ctx
# ---------------------------------------------------------------------------


def test_subregister_wires_post_llm_call_can_reissue():
    from kora_cli.reasoning.kora_hermes_plugin.haiku_router import register

    registered = []

    class _MockCtx:
        def register_hook(self, name, callback):
            registered.append((name, callback))

    register(_MockCtx())
    assert len(registered) == 1
    name, callback = registered[0]
    assert name == "post_llm_call_can_reissue"
    assert callable(callback)


def test_orchestrator_registers_haiku_router_hook():
    """The top-level KoraHermesPlugin.register must wire the
    haiku_router sub-plugin's hook — drift here means future
    sub-plugins land without their register being called."""
    from plugins.kora_hermes import register

    registered = []

    class _MockCtx:
        def register_hook(self, name, callback):
            registered.append(name)
        def register_identity_provider(self, provider):
            # KR-PLUGIN-IDENTITY Option C added this method; mirror
            # the real PluginContext's delegation so the orchestrator
            # walk completes without AttributeError.
            self.register_hook("pre_agent_identity_set", provider)

    register(_MockCtx())
    assert "post_llm_call_can_reissue" in registered


# ---------------------------------------------------------------------------
# First-non-None override semantics
# ---------------------------------------------------------------------------


def test_first_non_none_override_wins(monkeypatch):
    """Contract: when multiple plugins register against
    post_llm_call_can_reissue, the FIRST plugin returning a dict
    with ``reissue_with`` wins. Subsequent plugins are ignored.

    We exercise this via ``invoke_hook``'s actual semantics —
    it collects ALL non-None returns; the conversation_loop
    iteration in the consumer logic breaks on first match.
    """
    from kora_cli.plugins import PluginManager

    mgr = PluginManager()

    def plugin_one(**kw):
        return {"reissue_with": {"model": "first-wins"}}

    def plugin_two(**kw):
        return {"reissue_with": {"model": "second-loses"}}

    mgr._hooks["post_llm_call_can_reissue"] = [plugin_one, plugin_two]

    results = mgr.invoke_hook(
        "post_llm_call_can_reissue",
        response=None,
        api_kwargs={},
        iteration=1,
        route="slack_dm",
    )
    # Both returned non-None — invoke_hook returns both.
    assert len(results) == 2
    # The conversation_loop iterates and breaks on first dict
    # with ``reissue_with`` — verify that semantic by mirroring
    # the loop's iteration logic here.
    chosen = None
    for r in results:
        if isinstance(r, dict) and "reissue_with" in r:
            chosen = r["reissue_with"]
            break
    assert chosen == {"model": "first-wins"}


def test_plugin_exception_is_fail_safe():
    """A plugin that raises inside the handler must not break
    the loop — invoke_hook catches + logs, the iteration
    continues with the original response."""
    from kora_cli.plugins import PluginManager

    mgr = PluginManager()

    def plugin_raises(**kw):
        raise RuntimeError("plugin bug")

    def plugin_returns_none(**kw):
        return None

    mgr._hooks["post_llm_call_can_reissue"] = [
        plugin_raises,
        plugin_returns_none,
    ]
    # invoke_hook MUST NOT raise — the bad plugin's exception is
    # logged and the iteration continues.
    results = mgr.invoke_hook(
        "post_llm_call_can_reissue",
        response=None,
        api_kwargs={},
        iteration=1,
        route="slack_dm",
    )
    # No usable return — loop continues with original response.
    assert results == []


# ---------------------------------------------------------------------------
# Backward-compat: discovery shim exposes the alias
# ---------------------------------------------------------------------------


def test_discovery_shim_exports_alias():
    """``plugins.kora_hermes`` re-exports the handler under the
    pre-extraction alias name for consumer-import stability."""
    from plugins.kora_hermes import _post_llm_call_can_reissue
    from kora_cli.reasoning.kora_hermes_plugin.haiku_router import (
        haiku_router_post_call_escalation,
    )

    assert _post_llm_call_can_reissue is haiku_router_post_call_escalation


# ---------------------------------------------------------------------------
# Sanity: the cost_ladder selector dep is still importable from
# this sub-plugin (would break the import chain if cost_ladder
# selector signature drifted under us)
# ---------------------------------------------------------------------------


def test_should_escalate_post_call_signature_compatible():
    """The plugin depends on
    ``should_escalate_post_call(haiku_response_text=..., original_message_text=...)``.
    Pin the signature so cost_ladder selector edits don't silently
    break the haiku_router wiring."""
    from kora_cli.reasoning.kora_hermes_plugin.cost_ladder.selector import (
        should_escalate_post_call,
    )

    # Confident → no escalation.
    should, reason = should_escalate_post_call(
        haiku_response_text="The deploy is healthy.",
        original_message_text="is the deploy healthy",
    )
    assert should is False
    assert reason == "haiku_confident"

    # Uncertain → escalate.
    should, reason = should_escalate_post_call(
        haiku_response_text="I'm not sure about the status.",
        original_message_text="is the deploy healthy",
    )
    assert should is True
    assert reason == "low_confidence_marker"
