"""Unit tests for KR-P2-A ST2 wire-in helpers in ``agent/tool_executor.py``.

Covers the two new module-level helpers introduced by ST2:

- ``_run_constitution_pre_screen(agent, fn, args)`` — adapter that
  resolves the IsoKron provider + workspace_id from the agent and
  calls the ST1 pre-screen helper. Returns a verdict; never raises.
- ``_build_constitution_block_result(verdict)`` — JSON-encodes a
  FAIL/INCONCLUSIVE verdict into the model-facing tool-result string,
  including a ``block_kind`` discriminator.

The full executor flow (with real ``execute_tool_calls_*`` invocation
+ assistant-message + thread pool) is exercised by ST3's integration
test once chain-event emission is wired in.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from agent.constitution_pre_screen import (
    PreScreenEnvelope,
    PreScreenOutcome,
    PreScreenVerdict,
    SUBSTRATE_ENFORCED,
)
from agent.tool_executor import (
    _build_constitution_block_result,
    _run_constitution_pre_screen,
)


# ---------------------------------------------------------------------------
# Fake agent / provider / cache
# ---------------------------------------------------------------------------


class _FakeTTLCache:
    def __init__(self, values: dict):
        self._values = values

    def get(self, key):
        return self._values.get(key)


def _make_fake_provider(
    workspace_id_returned: str | None = "ws-1",
    constitution_cache_values: dict | None = None,
) -> SimpleNamespace:
    """Provider with the surface ``_run_constitution_pre_screen`` consults."""

    cache_values = constitution_cache_values or {
        "ws-1": ("rev-uuid-abc", "deadbeef1234"),
    }
    return SimpleNamespace(
        _resolve_workspace_id=lambda: workspace_id_returned,
        _constitution_cache=_FakeTTLCache(cache_values),
    )


def _make_fake_agent(
    *,
    provider: SimpleNamespace | None,
    no_memory_manager: bool = False,
) -> SimpleNamespace:
    if no_memory_manager:
        return SimpleNamespace(_memory_manager=None)
    if provider is None:
        # memory_manager present but no IsoKron provider registered.
        memory_manager = SimpleNamespace(
            get_provider=lambda name: None,
        )
    else:
        memory_manager = SimpleNamespace(
            get_provider=lambda name: provider if name == "isokron" else None,
        )
    return SimpleNamespace(_memory_manager=memory_manager)


# ---------------------------------------------------------------------------
# _run_constitution_pre_screen
# ---------------------------------------------------------------------------


def test_run_pre_screen_pass_path_pulls_workspace_from_provider():
    """With IsoKron provider loaded + cap granted, the verdict is PASS."""
    provider = _make_fake_provider(workspace_id_returned="ws-1")
    agent = _make_fake_agent(provider=provider)
    with patch(
        "agent.constitution_pre_screen.actor_has_capability",
        return_value=True,
    ):
        verdict = _run_constitution_pre_screen(agent, "read_file", {"path": "/x"})
    assert verdict.outcome is PreScreenOutcome.PASS
    assert verdict.envelope is not None
    # Envelope carries the cache-resolved revision + rules_hash, proving
    # the wrapper plumbed workspace_id through to the pre-screen helper.
    assert verdict.envelope.constitution_revision_id == "rev-uuid-abc"
    assert verdict.envelope.rules_hash == "deadbeef1234"
    assert verdict.envelope.required_capability == "cap_local_file_io"
    assert verdict.envelope.actor_id == "kora"


def test_run_pre_screen_no_memory_manager_returns_inconclusive():
    """Fail-CLOSED: when the agent has no _memory_manager attr, escalate."""
    agent = _make_fake_agent(provider=None, no_memory_manager=True)
    verdict = _run_constitution_pre_screen(agent, "read_file", {})
    assert verdict.outcome is PreScreenOutcome.INCONCLUSIVE
    assert "IsoKronMemoryProvider" in verdict.reason


def test_run_pre_screen_provider_not_registered_returns_inconclusive():
    """Fail-CLOSED: memory_manager loaded but isokron provider missing."""
    agent = _make_fake_agent(provider=None)
    verdict = _run_constitution_pre_screen(agent, "read_file", {})
    assert verdict.outcome is PreScreenOutcome.INCONCLUSIVE


def test_run_pre_screen_workspace_resolve_raises_treats_as_missing():
    """``_resolve_workspace_id`` is wrapped in a defensive try/except; a
    raising provider should not crash the pre-screen wire-in.

    Verdict still reaches a policy decision but with audit context absent.
    """
    def _boom():
        raise RuntimeError("provider not initialized")

    raising_provider = SimpleNamespace(
        _resolve_workspace_id=_boom,
        _constitution_cache=_FakeTTLCache({}),
    )
    agent = _make_fake_agent(provider=raising_provider)
    with patch(
        "agent.constitution_pre_screen.actor_has_capability",
        return_value=True,
    ):
        verdict = _run_constitution_pre_screen(agent, "read_file", {})
    assert verdict.outcome is PreScreenOutcome.PASS
    assert verdict.envelope is not None
    assert verdict.envelope.constitution_revision_id is None  # absent audit
    assert verdict.envelope.rules_hash is None


def test_run_pre_screen_substrate_tool_short_circuits():
    """``kora__*`` substrate tools PASS without consulting the provider."""
    agent = _make_fake_agent(provider=None, no_memory_manager=True)
    verdict = _run_constitution_pre_screen(agent, "kora__append_event", {})
    assert verdict.outcome is PreScreenOutcome.PASS
    assert verdict.envelope is not None
    assert verdict.envelope.required_capability == SUBSTRATE_ENFORCED


def test_run_pre_screen_kora_actor_id_is_propagated_to_envelope():
    """The wire-in pins actor_id='kora' (Kora is the implicit single actor)."""
    provider = _make_fake_provider()
    agent = _make_fake_agent(provider=provider)
    with patch(
        "agent.constitution_pre_screen.actor_has_capability",
        return_value=False,
    ):
        verdict = _run_constitution_pre_screen(agent, "read_file", {})
    assert verdict.outcome is PreScreenOutcome.FAIL
    assert verdict.envelope is not None
    assert verdict.envelope.actor_id == "kora"


# ---------------------------------------------------------------------------
# _build_constitution_block_result
# ---------------------------------------------------------------------------


def _make_envelope() -> PreScreenEnvelope:
    return PreScreenEnvelope(
        tool_name="read_file",
        required_capability="cap_local_file_io",
        actor_id="kora",
        constitution_revision_id="rev-uuid-abc",
        rules_hash="deadbeef1234",
    )


def test_build_block_result_fail_marks_constitution_reject():
    verdict = PreScreenVerdict.fail("denied by policy", envelope=_make_envelope())
    raw = _build_constitution_block_result(verdict)
    parsed = json.loads(raw)
    assert parsed == {
        "error": "denied by policy",
        "block_kind": "constitution_reject",
    }


def test_build_block_result_inconclusive_marks_constitution_escalate():
    verdict = PreScreenVerdict.inconclusive("operator must adjudicate")
    raw = _build_constitution_block_result(verdict)
    parsed = json.loads(raw)
    assert parsed == {
        "error": "operator must adjudicate",
        "block_kind": "constitution_escalate",
    }


def test_build_block_result_preserves_unicode():
    """ensure_ascii=False — non-ASCII reason messages survive intact."""
    verdict = PreScreenVerdict.fail("拒绝 — capability denied")
    raw = _build_constitution_block_result(verdict)
    assert "拒绝" in raw
    parsed = json.loads(raw)
    assert parsed["error"] == "拒绝 — capability denied"
    assert parsed["block_kind"] == "constitution_reject"
