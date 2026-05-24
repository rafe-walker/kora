"""Shim-verification suite for ``kora_cli/router/cost_router.py``.

The 50 behavioral tests moved to
``tests/kora_cli/reasoning/kora_hermes_plugin/cost_ladder/
test_selector.py`` in KR-PLUGIN-COST-LADDER (PR #182) — mirrors
the canonical code location at
``kora_cli/reasoning/kora_hermes_plugin/cost_ladder/selector.py``.

This file remains as a small assertion suite that the
backward-compat shim at ``kora_cli/router/cost_router.py`` still
re-exports the cost-ladder public surface so downstream callers
(any external module that has historically imported from
``kora_cli.router``) keep working without modification.

If this file ever fails, the shim has drifted from the canonical
location — fix by re-exporting the missing symbol in
``kora_cli/router/cost_router.py``.
"""

from __future__ import annotations


def test_shim_reexports_full_public_surface():
    """The shim must re-export every symbol the historical
    ``kora_cli.router`` (and ``kora_cli.router.cost_router``)
    public surface advertised. Asserted by import + identity-
    against-canonical: each shim attr is the SAME object as the
    canonical module's attr (not a separately-imported copy)."""
    from kora_cli.reasoning.kora_hermes_plugin.cost_ladder import (
        constants as canonical_constants,
        selector as canonical_selector,
    )
    from kora_cli.router import cost_router as shim

    # Constants from constants.py
    for name in [
        "DEFAULT_HAIKU_MODEL",
        "DEFAULT_OPUS_MODEL",
        "ENV_FORCE_OPUS",
        "ENV_OPUS_TRIGGER_PATTERNS",
        "ENV_OPUS_PREFIX",
        "ENV_HAIKU_LOW_CONFIDENCE_PATTERNS",
        "DEFAULT_DECISION_PATTERNS",
        "DEFAULT_HAIKU_LOW_CONFIDENCE_PATTERNS",
        "RUNG_NORMAL",
        "RUNG_WARN_75",
        "RUNG_DOWNSHIFT_90",
        "RUNG_HARD_STOP_100",
        "_LOW_CONFIDENCE_SHORT_RESPONSE_THRESHOLD",
        "_LOW_CONFIDENCE_LONG_INPUT_THRESHOLD",
    ]:
        assert getattr(shim, name) is getattr(canonical_constants, name), (
            f"shim drift: {name} not re-exported (or is a copy, "
            f"not a reference) — fix kora_cli/router/cost_router.py"
        )

    # Functions + dataclass from selector.py
    for name in [
        "RoutingDecision",
        "_compiled_decision_patterns",
        "_compiled_low_confidence_patterns",
        "_opus_prefix",
        "_has_opus_prefix",
        "strip_opus_prefix",
        "select_model_pre_call",
        "should_escalate_post_call",
    ]:
        assert getattr(shim, name) is getattr(canonical_selector, name), (
            f"shim drift: {name} not re-exported — fix "
            f"kora_cli/router/cost_router.py"
        )


def test_package_level_reexport_still_works():
    """``from kora_cli.router import select_model_pre_call`` (the
    package-level import path used by ``plugins/kora_hermes/__init__.py``
    and external callers) must keep resolving."""
    from kora_cli.router import (
        DEFAULT_HAIKU_MODEL,
        RoutingDecision,
        select_model_pre_call,
        should_escalate_post_call,
        strip_opus_prefix,
    )

    decision = select_model_pre_call(
        message_text="hello",
        iteration=1,
        cost_rung="normal",
        force_opus_env=False,
    )
    assert isinstance(decision, RoutingDecision)
    assert decision.model == DEFAULT_HAIKU_MODEL
    assert decision.reason == "default_haiku"
    assert decision.escalated is False

    # should_escalate_post_call + strip_opus_prefix still resolve.
    escalate, reason = should_escalate_post_call(
        haiku_response_text="The answer is 42.",
        original_message_text="What is the answer?",
    )
    assert escalate is False
    assert reason == "haiku_confident"
    assert strip_opus_prefix("/opus do the thing") == "do the thing"


def test_canonical_path_is_the_authoritative_source():
    """Sanity: the canonical path is the place where the public
    symbols are DEFINED (``__module__`` attribute). The shim
    re-exports; the canonical module owns."""
    from kora_cli.reasoning.kora_hermes_plugin.cost_ladder import (
        RoutingDecision,
        select_model_pre_call,
    )

    assert (
        RoutingDecision.__module__
        == "kora_cli.reasoning.kora_hermes_plugin.cost_ladder.selector"
    )
    assert (
        select_model_pre_call.__module__
        == "kora_cli.reasoning.kora_hermes_plugin.cost_ladder.selector"
    )
