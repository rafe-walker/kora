"""Tests for agent.identity_spec — IdentitySpec dataclass."""

from __future__ import annotations

import pytest

from agent.identity_spec import IdentitySpec, IdentityProvider


def test_identity_spec_basic_construction():
    spec = IdentitySpec(
        soul_md_content="I am Marvin.",
        system_prompt_content="You are paranoid.",
    )
    assert spec.soul_md_content == "I am Marvin."
    assert spec.system_prompt_content == "You are paranoid."
    assert spec.identity_metadata == {}


def test_identity_spec_with_metadata():
    spec = IdentitySpec(
        soul_md_content="x",
        system_prompt_content="y",
        identity_metadata={"agent_name": "Marvin", "version": "0.1"},
    )
    assert spec.identity_metadata == {"agent_name": "Marvin", "version": "0.1"}


def test_identity_spec_is_frozen():
    spec = IdentitySpec(soul_md_content="x", system_prompt_content="y")
    with pytest.raises(Exception):  # FrozenInstanceError on dataclasses
        spec.soul_md_content = "new"  # type: ignore[misc]


def test_identity_spec_metadata_defaults_to_empty_dict():
    spec = IdentitySpec(soul_md_content="x", system_prompt_content="y")
    assert spec.identity_metadata == {}
    # Each instance gets its own dict — no shared-mutable-default trap.
    spec_b = IdentitySpec(soul_md_content="a", system_prompt_content="b")
    assert spec.identity_metadata is not spec_b.identity_metadata


def test_identity_provider_type_alias_exists():
    """IdentityProvider is a Callable type alias — exists for
    plugin authors to type-annotate their provider functions."""
    assert IdentityProvider is not None
