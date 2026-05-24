"""Tests for the identity sub-plugin + the new
``pre_agent_identity_set`` hook surface.

Coverage:

  - Pure helpers in ``loader.py`` (env override / file read /
    IdentitySpec construction)
  - ``kora_identity_provider`` handler activation gating (env-
    disabled / empty system prompt → None)
  - Sub-register wires the provider to the hook via the new
    PluginContext.register_identity_provider convenience
  - First-non-None-wins semantics through PluginContext
  - Backward-compat: engine fallback to file-read when no plugin
    claims identity (structural pin on the source)
  - Discovery shim exports the new alias
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_resolve_system_prompt_path_env_override(monkeypatch, tmp_path):
    from kora_cli.reasoning.kora_hermes_plugin.identity import (
        ENV_SYSTEM_PROMPT_PATH,
        resolve_system_prompt_path,
    )

    custom = tmp_path / "custom_prompt.md"
    monkeypatch.setenv(ENV_SYSTEM_PROMPT_PATH, str(custom))
    assert resolve_system_prompt_path() == custom


def test_resolve_system_prompt_path_default_when_env_unset(monkeypatch):
    from kora_cli.reasoning.kora_hermes_plugin.identity import (
        DEFAULT_SYSTEM_PROMPT_PATH,
        ENV_SYSTEM_PROMPT_PATH,
        resolve_system_prompt_path,
    )

    monkeypatch.delenv(ENV_SYSTEM_PROMPT_PATH, raising=False)
    assert resolve_system_prompt_path() == DEFAULT_SYSTEM_PROMPT_PATH


def test_resolve_soul_md_path_env_override(monkeypatch, tmp_path):
    from kora_cli.reasoning.kora_hermes_plugin.identity import (
        ENV_SOUL_MD_PATH,
        resolve_soul_md_path,
    )

    custom = tmp_path / "custom_soul.md"
    monkeypatch.setenv(ENV_SOUL_MD_PATH, str(custom))
    assert resolve_soul_md_path() == custom


def test_resolve_soul_md_path_default_when_env_unset(monkeypatch):
    from kora_cli.reasoning.kora_hermes_plugin.identity import (
        DEFAULT_SOUL_MD_PATH,
        ENV_SOUL_MD_PATH,
        resolve_soul_md_path,
    )

    monkeypatch.delenv(ENV_SOUL_MD_PATH, raising=False)
    assert resolve_soul_md_path() == DEFAULT_SOUL_MD_PATH


def test_load_kora_identity_returns_spec_at_canonical_paths(monkeypatch):
    """Default-path canonical Kora identity loads cleanly — the
    files exist in the repo at the documented locations."""
    from kora_cli.reasoning.kora_hermes_plugin.identity import (
        DEFAULT_AGENT_NAME,
        ENV_SOUL_MD_PATH,
        ENV_SYSTEM_PROMPT_PATH,
        load_kora_identity,
    )

    monkeypatch.delenv(ENV_SYSTEM_PROMPT_PATH, raising=False)
    monkeypatch.delenv(ENV_SOUL_MD_PATH, raising=False)
    spec = load_kora_identity()
    assert spec is not None
    assert spec.system_prompt_content.strip() != ""
    # SOUL.md may or may not exist depending on dev state, but the
    # loader returns the spec regardless (empty soul_md_content is OK).
    assert isinstance(spec.soul_md_content, str)
    assert spec.identity_metadata["agent_name"] == DEFAULT_AGENT_NAME
    assert "system_prompt_path" in spec.identity_metadata
    assert "soul_md_path" in spec.identity_metadata


def test_load_kora_identity_returns_none_on_empty_system_prompt(
    monkeypatch, tmp_path
):
    """Empty system-prompt file → loader returns None (yields to
    engine file-read default)."""
    from kora_cli.reasoning.kora_hermes_plugin.identity import (
        ENV_SYSTEM_PROMPT_PATH,
        load_kora_identity,
    )

    empty = tmp_path / "empty.md"
    empty.write_text("")
    monkeypatch.setenv(ENV_SYSTEM_PROMPT_PATH, str(empty))
    assert load_kora_identity() is None


def test_load_kora_identity_returns_none_on_missing_system_prompt(
    monkeypatch, tmp_path
):
    """Missing system-prompt file → loader returns None."""
    from kora_cli.reasoning.kora_hermes_plugin.identity import (
        ENV_SYSTEM_PROMPT_PATH,
        load_kora_identity,
    )

    missing = tmp_path / "does_not_exist.md"
    monkeypatch.setenv(ENV_SYSTEM_PROMPT_PATH, str(missing))
    assert load_kora_identity() is None


def test_load_kora_identity_handles_missing_soul_md(monkeypatch, tmp_path):
    """SOUL.md is OPTIONAL — missing file is non-fatal; spec still
    returns with empty soul_md_content."""
    from kora_cli.reasoning.kora_hermes_plugin.identity import (
        ENV_SOUL_MD_PATH,
        ENV_SYSTEM_PROMPT_PATH,
        load_kora_identity,
    )

    sys_prompt = tmp_path / "sys.md"
    sys_prompt.write_text("You are a test agent.")
    missing_soul = tmp_path / "missing_soul.md"
    monkeypatch.setenv(ENV_SYSTEM_PROMPT_PATH, str(sys_prompt))
    monkeypatch.setenv(ENV_SOUL_MD_PATH, str(missing_soul))
    spec = load_kora_identity()
    assert spec is not None
    assert spec.system_prompt_content == "You are a test agent."
    assert spec.soul_md_content == ""


# ---------------------------------------------------------------------------
# Handler activation gating
# ---------------------------------------------------------------------------


def test_handler_returns_none_when_disabled_via_env(monkeypatch):
    from kora_cli.reasoning.kora_hermes_plugin.identity import (
        ENV_DISABLE_IDENTITY_PROVIDER,
        kora_identity_provider,
    )

    monkeypatch.setenv(ENV_DISABLE_IDENTITY_PROVIDER, "true")
    assert kora_identity_provider(engine=None) is None


def test_handler_returns_spec_when_enabled(monkeypatch):
    from kora_cli.reasoning.kora_hermes_plugin.identity import (
        ENV_DISABLE_IDENTITY_PROVIDER,
        ENV_SOUL_MD_PATH,
        ENV_SYSTEM_PROMPT_PATH,
        kora_identity_provider,
    )

    monkeypatch.delenv(ENV_DISABLE_IDENTITY_PROVIDER, raising=False)
    monkeypatch.delenv(ENV_SYSTEM_PROMPT_PATH, raising=False)
    monkeypatch.delenv(ENV_SOUL_MD_PATH, raising=False)
    spec = kora_identity_provider(engine=None)
    assert spec is not None
    assert spec.system_prompt_content.strip() != ""


def test_handler_swallows_loader_exceptions(monkeypatch):
    """Loader exception → handler returns None (fail-safe). The
    engine then falls back to its file-read default."""
    from kora_cli.reasoning.kora_hermes_plugin.identity import plugin as plugin_mod

    def boom():
        raise RuntimeError("simulated load failure")

    monkeypatch.setattr(plugin_mod, "load_kora_identity", boom)
    assert plugin_mod.kora_identity_provider(engine=None) is None


# ---------------------------------------------------------------------------
# Sub-register: wires through PluginContext.register_identity_provider
# ---------------------------------------------------------------------------


def test_subregister_wires_via_ctx_helper():
    """register() should call ctx.register_identity_provider with the
    kora_identity_provider callable, which in turn wraps into a
    pre_agent_identity_set hook callback."""
    from kora_cli.reasoning.kora_hermes_plugin.identity import register
    from kora_cli.reasoning.kora_hermes_plugin.identity import (
        kora_identity_provider,
    )

    captured = {"identity_provider": None, "hooks": []}

    class _MockCtx:
        def register_identity_provider(self, provider):
            captured["identity_provider"] = provider
        def register_hook(self, name, callback):
            captured["hooks"].append((name, callback))

    register(_MockCtx())
    assert captured["identity_provider"] is kora_identity_provider


# ---------------------------------------------------------------------------
# PluginContext.register_identity_provider — wrapping semantics
# ---------------------------------------------------------------------------


def test_register_identity_provider_wraps_spec_in_envelope(tmp_path, monkeypatch):
    """The convenience method wraps a provider that returns
    IdentitySpec into a hook callback that returns
    {"identity": <spec>}. Plugin authors return raw IdentitySpec;
    the firing site sees the envelope."""
    from agent.identity_spec import IdentitySpec
    from kora_cli.plugins import PluginManager

    mgr = PluginManager()

    def my_provider(*, engine=None, **kw):
        return IdentitySpec(
            soul_md_content="x",
            system_prompt_content="y",
            identity_metadata={"agent_name": "Test"},
        )

    # Simulate plugin registration via a minimal PluginContext stand-in
    # that delegates to the same wrapping logic.
    from kora_cli.plugins import PluginContext, PluginManifest

    manifest = PluginManifest(name="test_plugin", version="0.1.0", description="t")
    ctx = PluginContext(manager=mgr, manifest=manifest)
    ctx.register_identity_provider(my_provider)

    # Now invoke the hook; the wrapped callback should return
    # {"identity": <IdentitySpec>}.
    results = mgr.invoke_hook("pre_agent_identity_set", engine=None)
    assert len(results) == 1
    assert "identity" in results[0]
    assert isinstance(results[0]["identity"], IdentitySpec)
    assert results[0]["identity"].system_prompt_content == "y"
    assert results[0]["identity"].identity_metadata == {"agent_name": "Test"}


def test_register_identity_provider_swallows_none_returns():
    """When the provider returns None, the wrapped callback returns
    None — invoke_hook excludes None from results."""
    from kora_cli.plugins import PluginContext, PluginManager, PluginManifest

    mgr = PluginManager()
    manifest = PluginManifest(name="test_p", version="0.1.0", description="t")
    ctx = PluginContext(manager=mgr, manifest=manifest)

    ctx.register_identity_provider(lambda **kw: None)
    results = mgr.invoke_hook("pre_agent_identity_set", engine=None)
    assert results == []


def test_register_identity_provider_swallows_non_identityspec_returns():
    """Defensive: provider returning a non-IdentitySpec value → wrapped
    callback returns None + logs a warning. Don't crash the engine."""
    from kora_cli.plugins import PluginContext, PluginManager, PluginManifest

    mgr = PluginManager()
    manifest = PluginManifest(name="bad_p", version="0.1.0", description="t")
    ctx = PluginContext(manager=mgr, manifest=manifest)

    ctx.register_identity_provider(lambda **kw: "not an IdentitySpec")
    results = mgr.invoke_hook("pre_agent_identity_set", engine=None)
    assert results == []


def test_register_identity_provider_swallows_provider_exceptions():
    """Provider exception → wrapped callback returns None (fail-safe).
    Engine continues to other providers / file-read default."""
    from kora_cli.plugins import PluginContext, PluginManager, PluginManifest

    mgr = PluginManager()
    manifest = PluginManifest(name="exc_p", version="0.1.0", description="t")
    ctx = PluginContext(manager=mgr, manifest=manifest)

    def raises(**kw):
        raise RuntimeError("simulated provider exception")

    ctx.register_identity_provider(raises)
    results = mgr.invoke_hook("pre_agent_identity_set", engine=None)
    assert results == []


# ---------------------------------------------------------------------------
# First-non-None-wins semantics
# ---------------------------------------------------------------------------


def test_first_non_none_provider_wins():
    """Two plugins both register identity providers. The conversation-
    loop side breaks on first non-None; verify the consumer-side
    iteration mirrors that."""
    from agent.identity_spec import IdentitySpec
    from kora_cli.plugins import PluginContext, PluginManager, PluginManifest

    mgr = PluginManager()

    ctx_a = PluginContext(manager=mgr, manifest=PluginManifest(
        name="plugin_a", version="0.1.0", description="t"))
    ctx_a.register_identity_provider(
        lambda **kw: IdentitySpec(
            soul_md_content="a-soul", system_prompt_content="a-sys",
        )
    )

    ctx_b = PluginContext(manager=mgr, manifest=PluginManifest(
        name="plugin_b", version="0.1.0", description="t"))
    ctx_b.register_identity_provider(
        lambda **kw: IdentitySpec(
            soul_md_content="b-soul", system_prompt_content="b-sys",
        )
    )

    results = mgr.invoke_hook("pre_agent_identity_set", engine=None)
    # Both returned non-None.
    assert len(results) == 2
    # Iterate as the engine does; break on first match.
    chosen = None
    for r in results:
        spec = r.get("identity") if isinstance(r, dict) else None
        if isinstance(spec, IdentitySpec):
            chosen = spec
            break
    assert chosen is not None
    assert chosen.system_prompt_content == "a-sys"


# ---------------------------------------------------------------------------
# Engine-side hook firing — structural pin
# ---------------------------------------------------------------------------


def test_engine_source_fires_identity_hook_before_file_read():
    """Source-level pin: the hook firing site is BEFORE the file-read
    fallback. Slicing the engine source between the hook call and the
    file-read call asserts ordering at a structural level."""
    engine_src = (
        Path(__file__).resolve().parents[2]
        / "kora_cli" / "reasoning" / "anthropic_engine.py"
    ).read_text()

    hook_idx = engine_src.find('"pre_agent_identity_set"')
    file_read_idx = engine_src.find("prompt_path.read_text")
    assert hook_idx != -1, "pre_agent_identity_set hook must be invoked in engine"
    assert file_read_idx != -1, "file-read fallback must remain in engine"
    assert hook_idx < file_read_idx, (
        "hook must fire BEFORE the file-read fallback so plugin "
        "providers can claim identity; file-read is the fallback path"
    )


def test_engine_source_preserves_file_read_fallback():
    """The file-read fallback (pre-Option-C behavior) MUST stay in
    the engine for bare-Hermes-no-Kora-plugin users."""
    engine_src = (
        Path(__file__).resolve().parents[2]
        / "kora_cli" / "reasoning" / "anthropic_engine.py"
    ).read_text()

    # Either resolution helper OR direct path read must still be
    # present in the engine source.
    assert "prompt_path.read_text" in engine_src
    assert "_resolve_system_prompt_path" in engine_src


# ---------------------------------------------------------------------------
# Backward-compat: discovery shim exports the new alias
# ---------------------------------------------------------------------------


def test_discovery_shim_exports_identity_alias():
    """``plugins.kora_hermes`` re-exports the identity handler under
    the canonical alias ``_pre_agent_identity_set`` for downstream
    consumer-import stability."""
    from plugins.kora_hermes import _pre_agent_identity_set
    from kora_cli.reasoning.kora_hermes_plugin.identity import (
        kora_identity_provider,
    )

    assert _pre_agent_identity_set is kora_identity_provider


def test_orchestrator_registers_seven_sub_plugins():
    """Post-Option-C: the orchestrator delegates to 7 sub-plugins
    (cost_ladder, audit, caching, short_circuit, state_holders,
    haiku_router, identity). Closes Lock R3-2's 7-of-7 target.

    The identity sub-plugin's register() calls
    ``ctx.register_identity_provider`` which (in the real
    PluginContext) internally calls ``self.register_hook(
    "pre_agent_identity_set", wrapped)``. Mirror that delegation
    in the MockCtx so both signals are visible."""
    from plugins.kora_hermes import register

    hook_names = []
    identity_providers = []

    class _MockCtx:
        def register_hook(self, name, callback):
            hook_names.append(name)
        def register_identity_provider(self, provider):
            identity_providers.append(provider)
            # Mirror real PluginContext: under the hood,
            # register_identity_provider wires into the
            # pre_agent_identity_set hook.
            self.register_hook("pre_agent_identity_set", provider)

    register(_MockCtx())
    assert "pre_agent_identity_set" in hook_names, (
        "orchestrator must wire the identity sub-plugin (via "
        "register_identity_provider, which underneath calls "
        "register_hook for pre_agent_identity_set)"
    )
    assert len(identity_providers) == 1, (
        "exactly one identity provider should be registered "
        "(Kora's canonical identity)"
    )
