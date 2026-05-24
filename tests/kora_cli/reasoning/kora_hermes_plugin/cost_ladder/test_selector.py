"""Unit tests for the cost-ladder selector module.

Per KR-PLUGIN-COST-LADDER (PR #182): moved from
``tests/kora_cli/router/test_cost_router.py`` to mirror the
canonical code location at ``kora_cli/reasoning/
kora_hermes_plugin/cost_ladder/selector.py``. All 50 test
cases preserved verbatim; only the import path updated to use
the canonical location.

The backward-compat shim at ``kora_cli/router/cost_router.py``
keeps the old import path working for downstream consumers;
asserted in the now-small ``tests/kora_cli/router/test_cost_
router.py`` (shim-verification suite).

Covers KR-HAIKU-ROUTER (PR #165) §2 acceptance:
  - Default → Haiku (no earning signal)
  - /opus prefix → Opus, prefix stripped from prompt
  - KORA_FORCE_OPUS env → Opus regardless of other signals
  - Iteration ≥ 2 → Opus (the iteration earning signal)
  - Decision-language regex match → Opus
  - HARD_STOP_100 → model=None, caller fail-fast
  - DOWNSHIFT_90 + Opus signal → STILL Haiku (cost backstop)
  - WARN_75 + Opus signal → STILL Haiku
  - Post-call low-confidence markers → escalate
  - Post-call short response for long input → escalate
  - Post-call confident response → no escalate
  - strip_opus_prefix is idempotent on non-prefix text
  - Operator env override of trigger patterns
  - Operator env override of low-confidence patterns
  - Operator env override of /opus prefix string
"""

from __future__ import annotations

import pytest

from kora_cli.reasoning.kora_hermes_plugin.cost_ladder import (
    DEFAULT_DECISION_PATTERNS,
    DEFAULT_HAIKU_LOW_CONFIDENCE_PATTERNS,
    DEFAULT_HAIKU_MODEL,
    DEFAULT_OPUS_MODEL,
    ENV_FORCE_OPUS,
    ENV_HAIKU_LOW_CONFIDENCE_PATTERNS,
    ENV_OPUS_PREFIX,
    ENV_OPUS_TRIGGER_PATTERNS,
    RUNG_DOWNSHIFT_90,
    RUNG_HARD_STOP_100,
    RUNG_NORMAL,
    RUNG_WARN_75,
    RoutingDecision,
    select_model_pre_call,
    should_escalate_post_call,
    strip_opus_prefix,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip every router env so each test gets the bundled
    defaults (unless the test explicitly sets one)."""
    for var in (
        ENV_FORCE_OPUS,
        ENV_OPUS_TRIGGER_PATTERNS,
        ENV_OPUS_PREFIX,
        ENV_HAIKU_LOW_CONFIDENCE_PATTERNS,
    ):
        monkeypatch.delenv(var, raising=False)
    yield


# ---------------------------------------------------------------------------
# Pre-call decision — default + earning signals
# ---------------------------------------------------------------------------


def test_default_returns_haiku():
    d = select_model_pre_call(
        message_text="anything goes",
        iteration=1,
        cost_rung=RUNG_NORMAL,
    )
    assert d.model == DEFAULT_HAIKU_MODEL
    assert d.reason == "default_haiku"
    assert d.escalated is False


def test_opus_prefix_escalates():
    d = select_model_pre_call(
        message_text="/opus tell me everything",
        iteration=1,
        cost_rung=RUNG_NORMAL,
    )
    assert d.model == DEFAULT_OPUS_MODEL
    assert d.reason == "opus_prefix"
    assert d.escalated is True


def test_opus_prefix_case_insensitive():
    d = select_model_pre_call(
        message_text="/OPUS heavy lift", iteration=1, cost_rung=RUNG_NORMAL
    )
    assert d.model == DEFAULT_OPUS_MODEL
    assert d.reason == "opus_prefix"


def test_opus_prefix_tolerates_leading_whitespace():
    d = select_model_pre_call(
        message_text="   /opus heavy", iteration=1, cost_rung=RUNG_NORMAL
    )
    assert d.reason == "opus_prefix"


def test_force_opus_env_overrides_default(monkeypatch):
    monkeypatch.setenv(ENV_FORCE_OPUS, "true")
    d = select_model_pre_call(
        message_text="anything",
        iteration=1,
        cost_rung=RUNG_NORMAL,
    )
    assert d.model == DEFAULT_OPUS_MODEL
    assert d.reason == "force_opus_env"
    assert d.escalated is True


def test_force_opus_env_explicit_arg_overrides_env_read():
    """Tests pass explicit `force_opus_env=True` even when env isn't
    set; this keeps tests env-free."""
    d = select_model_pre_call(
        message_text="anything",
        iteration=1,
        cost_rung=RUNG_NORMAL,
        force_opus_env=True,
    )
    assert d.model == DEFAULT_OPUS_MODEL


def test_iteration_two_escalates():
    d = select_model_pre_call(
        message_text="hi", iteration=2, cost_rung=RUNG_NORMAL
    )
    assert d.model == DEFAULT_OPUS_MODEL
    assert d.reason == "tool_loop_iteration"
    assert d.escalated is True


def test_iteration_three_escalates():
    """Same signal as iteration 2 — covers the >= 2 inclusive check."""
    d = select_model_pre_call(
        message_text="hi", iteration=3, cost_rung=RUNG_NORMAL
    )
    assert d.model == DEFAULT_OPUS_MODEL
    assert d.reason == "tool_loop_iteration"


@pytest.mark.parametrize(
    "text",
    [
        "should i ship this?",
        "do we approve the migration?",
        "what should i do about the alert?",
        "is it safe to merge?",
        "decide whether to scale up",
        "go/no-go on the deploy",
        "plan strategy for next quarter",
    ],
)
def test_decision_language_patterns_escalate(text):
    d = select_model_pre_call(
        message_text=text, iteration=1, cost_rung=RUNG_NORMAL
    )
    assert d.model == DEFAULT_OPUS_MODEL, (
        f"decision-language phrase {text!r} should have escalated"
    )
    assert d.reason == "decision_language"


@pytest.mark.parametrize(
    "text",
    [
        "hi",
        "thanks",
        "what's my burn",
        "how are you",
        "any alerts?",
        "ok cool",
    ],
)
def test_non_decision_text_stays_haiku(text):
    d = select_model_pre_call(
        message_text=text, iteration=1, cost_rung=RUNG_NORMAL
    )
    assert d.model == DEFAULT_HAIKU_MODEL, (
        f"non-decision text {text!r} should have stayed Haiku"
    )


# ---------------------------------------------------------------------------
# Cost-rung backstop (Layer 2)
# ---------------------------------------------------------------------------


def test_hard_stop_returns_none_model():
    d = select_model_pre_call(
        message_text="anything",
        iteration=1,
        cost_rung=RUNG_HARD_STOP_100,
    )
    assert d.model is None
    assert d.reason == "cost_ladder_halted"
    assert d.escalated is False


def test_downshift_90_clamps_to_haiku_even_with_opus_signal():
    """The cost backstop wins over an earning signal."""
    d = select_model_pre_call(
        message_text="/opus heavy lift",
        iteration=1,
        cost_rung=RUNG_DOWNSHIFT_90,
    )
    assert d.model == DEFAULT_HAIKU_MODEL
    assert d.reason == "cost_clamp:downshift_90"
    assert d.escalated is False


def test_downshift_90_clamps_even_on_iteration_two():
    d = select_model_pre_call(
        message_text="hi",
        iteration=2,
        cost_rung=RUNG_DOWNSHIFT_90,
    )
    assert d.model == DEFAULT_HAIKU_MODEL
    assert d.reason == "cost_clamp:downshift_90"


def test_warn_75_clamps_to_haiku_even_with_decision_language():
    d = select_model_pre_call(
        message_text="should i ship this?",
        iteration=1,
        cost_rung=RUNG_WARN_75,
    )
    assert d.model == DEFAULT_HAIKU_MODEL
    assert d.reason == "cost_clamp:warn_75"


def test_warn_75_clamps_even_with_force_opus_env(monkeypatch):
    """Cost backstop wins over even the operator's defensive
    KORA_FORCE_OPUS — the rung is the LAST line of defense and
    not overridable from app-layer signals."""
    monkeypatch.setenv(ENV_FORCE_OPUS, "true")
    d = select_model_pre_call(
        message_text="anything",
        iteration=1,
        cost_rung=RUNG_WARN_75,
    )
    assert d.model == DEFAULT_HAIKU_MODEL


# ---------------------------------------------------------------------------
# /opus prefix stripping
# ---------------------------------------------------------------------------


def test_strip_opus_prefix_removes_prefix():
    assert strip_opus_prefix("/opus what's up") == "what's up"


def test_strip_opus_prefix_case_insensitive():
    assert strip_opus_prefix("/OPUS heavy") == "heavy"


def test_strip_opus_prefix_idempotent_on_non_prefix_text():
    assert strip_opus_prefix("hi there") == "hi there"


def test_strip_opus_prefix_tolerates_leading_whitespace():
    assert strip_opus_prefix("  /opus heavy") == "heavy"


def test_strip_opus_prefix_non_string_passthrough():
    assert strip_opus_prefix(None) is None  # type: ignore[arg-type]
    assert strip_opus_prefix(42) == 42  # type: ignore[arg-type]


def test_strip_opus_prefix_respects_env_override(monkeypatch):
    monkeypatch.setenv(ENV_OPUS_PREFIX, "!think")
    assert strip_opus_prefix("!think hard") == "hard"
    assert strip_opus_prefix("/opus hard") == "/opus hard"


# ---------------------------------------------------------------------------
# Post-call escalation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "haiku_text",
    [
        "I'm not sure about that.",
        "im not sure honestly",
        "I don't know what to tell you.",
        "I might be wrong here.",
        "this is a guess but try X.",
        "I can't determine that.",
        "Unclear whether this is the right path.",
    ],
)
def test_post_call_low_confidence_marker_escalates(haiku_text):
    should, reason = should_escalate_post_call(
        haiku_response_text=haiku_text,
        original_message_text="anything",
    )
    assert should is True
    assert reason == "low_confidence_marker"


def test_post_call_short_response_for_long_input_escalates():
    long_input = "x" * 250
    should, reason = should_escalate_post_call(
        haiku_response_text="ok.",
        original_message_text=long_input,
    )
    assert should is True
    assert reason == "short_response_for_long_input"


def test_post_call_confident_short_response_for_short_input_does_not_escalate():
    """Short input + short reply is fine — terse Q gets terse A."""
    should, reason = should_escalate_post_call(
        haiku_response_text="ok.",
        original_message_text="ok?",
    )
    assert should is False
    assert reason == "haiku_confident"


def test_post_call_confident_long_response_does_not_escalate():
    should, reason = should_escalate_post_call(
        haiku_response_text=(
            "Here's the full plan: step A, step B, step C. "
            "Each is independently verifiable. "
            "Rollback at step B is the safest cutover point."
        ),
        original_message_text="walk me through the plan",
    )
    assert should is False
    assert reason == "haiku_confident"


def test_post_call_non_string_does_not_escalate():
    should, reason = should_escalate_post_call(
        haiku_response_text=None,  # type: ignore[arg-type]
        original_message_text="anything",
    )
    assert should is False


def test_post_call_low_confidence_pattern_env_override(monkeypatch):
    """Operator can swap in a custom low-confidence vocabulary."""
    monkeypatch.setenv(
        ENV_HAIKU_LOW_CONFIDENCE_PATTERNS,
        r"this is suss,unclear",
    )
    should, reason = should_escalate_post_call(
        haiku_response_text="this is suss",
        original_message_text="anything",
    )
    assert should is True
    # Old default markers no longer trigger.
    should2, _ = should_escalate_post_call(
        haiku_response_text="I'm not sure about that",
        original_message_text="anything",
    )
    assert should2 is False


# ---------------------------------------------------------------------------
# Decision-language env override
# ---------------------------------------------------------------------------


def test_decision_pattern_env_override(monkeypatch):
    """Operator can shrink or expand the decision-language list."""
    monkeypatch.setenv(ENV_OPUS_TRIGGER_PATTERNS, r"\bcritical\b")
    d = select_model_pre_call(
        message_text="this is critical",
        iteration=1,
        cost_rung=RUNG_NORMAL,
    )
    assert d.model == DEFAULT_OPUS_MODEL
    assert d.reason == "decision_language"

    # An old default pattern that's no longer active.
    d2 = select_model_pre_call(
        message_text="should i ship this?",
        iteration=1,
        cost_rung=RUNG_NORMAL,
    )
    assert d2.model == DEFAULT_HAIKU_MODEL


def test_decision_pattern_invalid_regex_skipped(monkeypatch, caplog):
    """A bad pattern in the env list is logged + skipped; others
    still work."""
    import logging

    caplog.set_level(logging.WARNING)
    monkeypatch.setenv(
        ENV_OPUS_TRIGGER_PATTERNS, r"[unclosed,\bcritical\b"
    )
    d = select_model_pre_call(
        message_text="this is critical",
        iteration=1,
        cost_rung=RUNG_NORMAL,
    )
    assert d.model == DEFAULT_OPUS_MODEL
    warns = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("invalid" in w for w in warns)


# ---------------------------------------------------------------------------
# RoutingDecision shape
# ---------------------------------------------------------------------------


def test_routing_decision_is_frozen():
    d = RoutingDecision(model="x", reason="r", escalated=False)
    with pytest.raises((AttributeError, TypeError)):
        d.model = "y"  # type: ignore[misc]


def test_routing_decision_default_haiku_context_is_none():
    d = RoutingDecision(model="x", reason="r", escalated=False)
    assert d.haiku_context_for_opus is None


# ---------------------------------------------------------------------------
# Earning-signal precedence — Q4 boundaries
# ---------------------------------------------------------------------------


def test_force_opus_env_beats_default_haiku_but_loses_to_cost_clamp(
    monkeypatch,
):
    """Precedence: hard_stop > cost_clamp > force_opus > iteration
    > prefix > decision_language > default. This test pins the
    boundary between force_opus and cost_clamp."""
    monkeypatch.setenv(ENV_FORCE_OPUS, "true")
    # Normal rung — force wins.
    d = select_model_pre_call(
        message_text="hi", iteration=1, cost_rung=RUNG_NORMAL
    )
    assert d.model == DEFAULT_OPUS_MODEL
    # warn_75 — cost clamp wins.
    d2 = select_model_pre_call(
        message_text="hi", iteration=1, cost_rung=RUNG_WARN_75
    )
    assert d2.model == DEFAULT_HAIKU_MODEL


def test_iteration_two_beats_opus_prefix():
    """Both signals would pick Opus; iteration check fires first
    in the decision tree (matches the spec's pseudocode order
    putting iteration check before prefix check)."""
    d = select_model_pre_call(
        message_text="/opus heavy", iteration=2, cost_rung=RUNG_NORMAL
    )
    assert d.model == DEFAULT_OPUS_MODEL
    assert d.reason == "tool_loop_iteration"
