"""Unit tests for ``agent/constitution_pre_screen.py`` (KR-P2-A ST1).

Covers the six decision-order branches plus envelope state extraction:
  (a) substrate-tier ``kora__*`` short-circuit PASS
  (b) unmapped tool → INCONCLUSIVE
  (c) missing memory_provider → INCONCLUSIVE
  (d) cap_* not in C2 mirror (KeyError) → INCONCLUSIVE
  (e) cap denied → FAIL with envelope
  (f) cap granted → PASS with envelope
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.constitution_pre_screen import (
    KoraConstitutionEscalateError,
    KoraConstitutionRejectError,
    PreScreenEnvelope,
    PreScreenOutcome,
    PreScreenVerdict,
    SUBSTRATE_ENFORCED,
    constitution_pre_screen,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeTTLCache:
    """Minimal stand-in for plugins/memory/isokron/cache.TTLCache."""

    def __init__(self, values: dict):
        self._values = values

    def get(self, key):  # noqa: D401 — mirrors TTLCache surface
        return self._values.get(key)


@pytest.fixture
def provider_with_constitution():
    """Provider exposing ``_constitution_cache`` with one workspace populated."""

    return SimpleNamespace(
        _constitution_cache=_FakeTTLCache(
            {
                "ws-1": ("rev-uuid-abc", "deadbeef1234"),
            }
        ),
    )


@pytest.fixture
def provider_with_fresh_workspace_sentinel():
    """Workspace exists but has no Constitution revision yet — sentinel ``(None, None)``."""

    return SimpleNamespace(
        _constitution_cache=_FakeTTLCache(
            {
                "ws-fresh": (None, None),
            }
        ),
    )


# ---------------------------------------------------------------------------
# (a) kora__* short-circuit PASS
# ---------------------------------------------------------------------------



@pytest.mark.parametrize(
    "substrate_tool",
    [
        "kora__append_event",
        "kora__write_agent_scratchpad",
        "kora__create_relationlink",
        "kora__read_kora_capability_row",
    ],
)
def test_substrate_tools_pass_short_circuit(
    substrate_tool, provider_with_constitution
):
    """Substrate kora__* tools PASS without consulting the cap matrix."""
    verdict = constitution_pre_screen(
        tool_name=substrate_tool,
        tool_args={"arg1": "value"},
        actor_id="kora",
        memory_provider=provider_with_constitution,
        workspace_id="ws-1",
    )
    assert verdict.outcome is PreScreenOutcome.PASS
    assert verdict.envelope is not None
    assert verdict.envelope.tool_name == substrate_tool
    assert verdict.envelope.required_capability == SUBSTRATE_ENFORCED
    assert verdict.envelope.actor_id == "kora"
    # Substrate path intentionally omits Constitution audit — substrate
    # emits its own audit via kora__append_event.
    assert verdict.envelope.constitution_revision_id is None
    assert verdict.envelope.rules_hash is None



def test_substrate_short_circuit_works_without_memory_provider():
    """Substrate path is provider-independent — even if memory is down."""
    verdict = constitution_pre_screen(
        tool_name="kora__append_event",
        tool_args={},
        actor_id="kora",
        memory_provider=None,
    )
    assert verdict.outcome is PreScreenOutcome.PASS


# ---------------------------------------------------------------------------
# (b) Unmapped tool → INCONCLUSIVE
# ---------------------------------------------------------------------------



def test_unmapped_tool_returns_inconclusive(provider_with_constitution):
    verdict = constitution_pre_screen(
        tool_name="this_tool_does_not_exist",
        tool_args={},
        actor_id="kora",
        memory_provider=provider_with_constitution,
        workspace_id="ws-1",
    )
    assert verdict.outcome is PreScreenOutcome.INCONCLUSIVE
    assert "this_tool_does_not_exist" in verdict.reason
    assert "TOOL_CAPABILITY_MAP" in verdict.reason
    assert verdict.envelope is None


# ---------------------------------------------------------------------------
# (c) Missing memory_provider → INCONCLUSIVE (fail-CLOSED)
# ---------------------------------------------------------------------------



def test_missing_memory_provider_returns_inconclusive():
    """A mapped tool still escalates when the provider is None."""
    verdict = constitution_pre_screen(
        tool_name="read_file",
        tool_args={"path": "/etc/hosts"},
        actor_id="kora",
        memory_provider=None,
    )
    assert verdict.outcome is PreScreenOutcome.INCONCLUSIVE
    assert "IsoKronMemoryProvider" in verdict.reason
    assert "read_file" in verdict.reason
    assert verdict.envelope is None


# ---------------------------------------------------------------------------
# (d) Cap not in C2 mirror (KeyError) → INCONCLUSIVE
# ---------------------------------------------------------------------------



def test_cap_not_in_mirror_returns_inconclusive(
    provider_with_constitution,
):
    """Today's expected behavior for infra caps not yet in C2 mirror."""
    with patch(
        "agent.constitution_pre_screen.actor_has_capability",
        side_effect=KeyError("cap_local_file_io"),
    ):
        verdict = constitution_pre_screen(
            tool_name="read_file",
            tool_args={},
            actor_id="kora",
            memory_provider=provider_with_constitution,
            workspace_id="ws-1",
        )
    assert verdict.outcome is PreScreenOutcome.INCONCLUSIVE
    assert "cap_local_file_io" in verdict.reason
    assert "C2" in verdict.reason
    assert verdict.envelope is None


# ---------------------------------------------------------------------------
# (e) Cap denied → FAIL with envelope
# ---------------------------------------------------------------------------



def test_capability_denied_returns_fail_with_envelope(
    provider_with_constitution,
):
    with patch(
        "agent.constitution_pre_screen.actor_has_capability",
        return_value=False,
    ):
        verdict = constitution_pre_screen(
            tool_name="read_file",
            tool_args={"path": "/foo"},
            actor_id="kora",
            memory_provider=provider_with_constitution,
            workspace_id="ws-1",
        )
    assert verdict.outcome is PreScreenOutcome.FAIL
    assert verdict.envelope is not None
    assert verdict.envelope.tool_name == "read_file"
    assert verdict.envelope.required_capability == "cap_local_file_io"
    assert verdict.envelope.actor_id == "kora"
    assert verdict.envelope.constitution_revision_id == "rev-uuid-abc"
    assert verdict.envelope.rules_hash == "deadbeef1234"
    assert "lacks capability" in verdict.reason
    assert "cap_local_file_io" in verdict.reason


# ---------------------------------------------------------------------------
# (f) Cap granted → PASS with envelope
# ---------------------------------------------------------------------------



def test_capability_granted_returns_pass_with_envelope(
    provider_with_constitution,
):
    with patch(
        "agent.constitution_pre_screen.actor_has_capability",
        return_value=True,
    ):
        verdict = constitution_pre_screen(
            tool_name="read_file",
            tool_args={"path": "/foo"},
            actor_id="kora",
            memory_provider=provider_with_constitution,
            workspace_id="ws-1",
        )
    assert verdict.outcome is PreScreenOutcome.PASS
    assert verdict.envelope is not None
    assert verdict.envelope.tool_name == "read_file"
    assert verdict.envelope.required_capability == "cap_local_file_io"
    assert verdict.envelope.constitution_revision_id == "rev-uuid-abc"
    assert verdict.envelope.rules_hash == "deadbeef1234"


# ---------------------------------------------------------------------------
# Envelope state extraction edge cases
# ---------------------------------------------------------------------------



def test_pass_when_constitution_cache_is_sentinel(
    provider_with_fresh_workspace_sentinel,
):
    """Fresh workspace state — sentinel ``(None, None)`` in cache."""
    with patch(
        "agent.constitution_pre_screen.actor_has_capability",
        return_value=True,
    ):
        verdict = constitution_pre_screen(
            tool_name="read_file",
            tool_args={},
            actor_id="kora",
            memory_provider=provider_with_fresh_workspace_sentinel,
            workspace_id="ws-fresh",
        )
    assert verdict.outcome is PreScreenOutcome.PASS
    assert verdict.envelope is not None
    assert verdict.envelope.constitution_revision_id is None
    assert verdict.envelope.rules_hash is None



def test_pass_when_provider_has_no_constitution_cache_attr():
    """Defensive: provider exists but doesn't expose ``_constitution_cache``."""
    bare_provider = SimpleNamespace()
    with patch(
        "agent.constitution_pre_screen.actor_has_capability",
        return_value=True,
    ):
        verdict = constitution_pre_screen(
            tool_name="read_file",
            tool_args={},
            actor_id="kora",
            memory_provider=bare_provider,
            workspace_id="ws-1",
        )
    assert verdict.outcome is PreScreenOutcome.PASS
    assert verdict.envelope is not None
    assert verdict.envelope.constitution_revision_id is None
    assert verdict.envelope.rules_hash is None



def test_pass_when_workspace_id_not_provided():
    """Without workspace_id, audit state is (None, None) but verdict reaches policy."""
    provider = SimpleNamespace(
        _constitution_cache=_FakeTTLCache({"ws-1": ("rev-x", "hash-x")})
    )
    with patch(
        "agent.constitution_pre_screen.actor_has_capability",
        return_value=True,
    ):
        verdict = constitution_pre_screen(
            tool_name="read_file",
            tool_args={},
            actor_id="kora",
            memory_provider=provider,
            workspace_id=None,
        )
    assert verdict.outcome is PreScreenOutcome.PASS
    assert verdict.envelope is not None
    assert verdict.envelope.constitution_revision_id is None
    assert verdict.envelope.rules_hash is None


# ---------------------------------------------------------------------------
# Value-class shape
# ---------------------------------------------------------------------------


def test_verdict_is_frozen():
    env = PreScreenEnvelope(
        tool_name="t",
        required_capability="cap_x",
        actor_id="a",
        constitution_revision_id=None,
        rules_hash=None,
    )
    verdict = PreScreenVerdict.pass_(env)
    with pytest.raises(dataclasses.FrozenInstanceError):
        verdict.outcome = PreScreenOutcome.FAIL  # type: ignore[misc]


def test_envelope_is_frozen():
    env = PreScreenEnvelope(
        tool_name="t",
        required_capability="cap_x",
        actor_id="a",
        constitution_revision_id=None,
        rules_hash=None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        env.tool_name = "u"  # type: ignore[misc]


def test_verdict_factories_set_outcome_correctly():
    env = PreScreenEnvelope(
        tool_name="t",
        required_capability="cap_x",
        actor_id="a",
        constitution_revision_id=None,
        rules_hash=None,
    )
    p = PreScreenVerdict.pass_(env)
    f = PreScreenVerdict.fail("denied", envelope=env)
    i = PreScreenVerdict.inconclusive("unknown")
    assert p.outcome is PreScreenOutcome.PASS
    assert f.outcome is PreScreenOutcome.FAIL
    assert i.outcome is PreScreenOutcome.INCONCLUSIVE
    assert f.reason == "denied"
    assert i.reason == "unknown"
    assert i.envelope is None


def test_reject_error_carries_context():
    env = PreScreenEnvelope(
        tool_name="read_file",
        required_capability="cap_local_file_io",
        actor_id="kora",
        constitution_revision_id="rev-1",
        rules_hash="hash-1",
    )
    err = KoraConstitutionRejectError("read_file", "denied", envelope=env)
    assert err.tool_name == "read_file"
    assert err.reason == "denied"
    assert err.envelope is env
    assert "read_file" in str(err)
    assert "denied" in str(err)


def test_escalate_error_carries_context():
    esc = KoraConstitutionEscalateError(
        "read_file", "no memory provider"
    )
    assert esc.tool_name == "read_file"
    assert esc.reason == "no memory provider"
    assert "read_file" in str(esc)
    assert "no memory provider" in str(esc)
