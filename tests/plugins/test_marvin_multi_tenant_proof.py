"""Multi-tenant proof tests for KR-PLUGIN-IDENTITY Option C.

Validates that the architecture landed in #199 actually supports
non-Kora identities end-to-end. Tests the three scenarios documented
in ``kora_docs/14_research/plugin_identity_option_c_2026-05-24/
HOW_TO_BUILD_YOUR_OWN_AGENT.md`` §4:

  1. **Single identity (Kora-only)** — bare Hermes without Marvin
     plugin. Engine uses Kora's identity. Pre-existing in #199; this
     test pins the still-works.
  2. **Single identity (Marvin-only)** — Hermes with Marvin plugin
     ONLY (Kora plugin disabled). Engine uses Marvin's identity.
     NEW — proves the architecture supports a non-Kora identity.
  3. **Both registered** — Hermes with BOTH Marvin + Kora plugins.
     First-non-None-wins by plugin-discovery order (FIFO). NEW —
     proves the multi-tenant routing behavior.

All three scenarios use the real ``PluginManager`` + the real
``pre_agent_identity_set`` hook + the real ``IdentitySpec``
dataclass. The reasoning engine itself isn't constructed (would
require Anthropic credentials), but the firing-site contract IS
verified by directly invoking the hook the way the engine does.

Plus tests that pin the bundled-plugin convention works: Marvin's
``plugin.yaml`` is discoverable; the ``register`` entry point fires
correctly when discovered.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent.identity_spec import IdentitySpec


# ---------------------------------------------------------------------------
# Direct provider tests — Marvin's provider in isolation
# ---------------------------------------------------------------------------


def test_marvin_provider_returns_identity_spec():
    """Direct invocation: Marvin's provider returns a valid
    IdentitySpec with non-Kora metadata."""
    from plugins.marvin import marvin_identity_provider

    spec = marvin_identity_provider(engine=None)
    assert isinstance(spec, IdentitySpec)
    assert spec.identity_metadata["agent_name"] == "Marvin"
    assert spec.identity_metadata["plugin_name"] == "marvin"
    assert "Paranoid Android" in spec.soul_md_content
    assert "You are Marvin" in spec.system_prompt_content


def test_marvin_identity_is_not_kora_identity():
    """Sanity check: Marvin's IdentitySpec is materially different
    from Kora's. Pin this so a future refactor that accidentally
    cross-wires the two identity sources gets caught."""
    from plugins.marvin import marvin_identity_provider
    from kora_cli.reasoning.kora_hermes_plugin.identity import (
        load_kora_identity,
    )

    marvin_spec = marvin_identity_provider(engine=None)
    kora_spec = load_kora_identity()
    assert kora_spec is not None  # canonical Kora identity exists
    # Different agent names + different metadata.
    assert (
        marvin_spec.identity_metadata["agent_name"]
        != kora_spec.identity_metadata["agent_name"]
    )
    # Different system prompts (the content the engine prepends).
    assert marvin_spec.system_prompt_content != kora_spec.system_prompt_content


# ---------------------------------------------------------------------------
# Bundled-plugin manifest pin
# ---------------------------------------------------------------------------


def test_marvin_plugin_manifest_exists_and_well_formed():
    """Bundled-plugin convention requires ``plugin.yaml`` at
    ``plugins/marvin/``. Pin the manifest shape so a future
    refactor that drops the manifest gets caught."""
    manifest_path = (
        Path(__file__).resolve().parents[2] / "plugins" / "marvin"
        / "plugin.yaml"
    )
    assert manifest_path.exists(), (
        "plugins/marvin/plugin.yaml must exist for Hermes bundled-"
        "plugin discovery to find Marvin"
    )
    data = yaml.safe_load(manifest_path.read_text())
    assert data["name"] == "marvin"
    assert "version" in data
    assert "pre_agent_identity_set" in data.get("hooks", [])


def test_marvin_plugin_identity_files_exist():
    """Marvin's two identity files (MARVIN.md + marvin_system_prompt.md)
    must exist inside the package data dir at module-import time
    (the canonical ``src/marvin/__init__.py`` reads them eagerly).

    Post-#204 restructure: data files live at
    ``plugins/marvin/src/marvin/data/`` (relocatable package layout
    so the wheel install lands them at ``<site-packages>/marvin/data/``)."""
    data_dir = (
        Path(__file__).resolve().parents[2]
        / "plugins" / "marvin" / "src" / "marvin" / "data"
    )
    assert (data_dir / "MARVIN.md").exists()
    assert (data_dir / "marvin_system_prompt.md").exists()
    # Both non-empty.
    assert (data_dir / "MARVIN.md").read_text().strip() != ""
    assert (data_dir / "marvin_system_prompt.md").read_text().strip() != ""


# ---------------------------------------------------------------------------
# Scenario 1 — Kora-only (single identity, the pre-#199 default)
# ---------------------------------------------------------------------------


def test_scenario_1_kora_only_engine_uses_kora_identity(monkeypatch):
    """When only Kora's plugin is loaded, the
    ``pre_agent_identity_set`` hook fires + returns Kora's
    IdentitySpec. The engine's __init__ would use
    ``identity.system_prompt_content`` as ``self._system_prompt``.

    Verifies the pre-existing behavior from #199 still works
    after adding Marvin alongside (no regression)."""
    from kora_cli.plugins import (
        PluginContext, PluginManager, PluginManifest,
    )
    from kora_cli.reasoning.kora_hermes_plugin.identity import (
        kora_identity_provider,
    )

    # Disable Marvin's gate behavior by ensuring Marvin isn't
    # registered — fresh PluginManager + only register Kora.
    mgr = PluginManager()
    kora_ctx = PluginContext(
        manager=mgr,
        manifest=PluginManifest(
            name="kora_hermes", version="0.1.0", description="kora",
        ),
    )
    kora_ctx.register_identity_provider(kora_identity_provider)

    results = mgr.invoke_hook("pre_agent_identity_set", engine=None)
    # Filter to dicts with identity key (the wrapped form).
    identities = [
        r["identity"] for r in results
        if isinstance(r, dict) and isinstance(r.get("identity"), IdentitySpec)
    ]
    assert len(identities) == 1
    chosen = identities[0]  # only one provider; it wins
    assert chosen.identity_metadata["agent_name"] == "Kora"


# ---------------------------------------------------------------------------
# Scenario 2 — Marvin-only (proves non-Kora identity works end-to-end)
# ---------------------------------------------------------------------------


def test_scenario_2_marvin_only_engine_uses_marvin_identity():
    """When only Marvin's plugin is loaded, the engine gets Marvin's
    IdentitySpec — NOT Kora's. THIS is the validation of Option C:
    the architecture supports a non-Kora identity via the same hook
    surface, with NO code in Kora's fork changing.

    Mirrors what an external IsoKron user would see after
    ``pip install marvin-runtime`` (or equivalent bundled plugin
    discovery)."""
    from kora_cli.plugins import (
        PluginContext, PluginManager, PluginManifest,
    )
    from plugins.marvin import marvin_identity_provider

    mgr = PluginManager()
    marvin_ctx = PluginContext(
        manager=mgr,
        manifest=PluginManifest(
            name="marvin", version="0.1.0", description="paranoid",
        ),
    )
    marvin_ctx.register_identity_provider(marvin_identity_provider)

    results = mgr.invoke_hook("pre_agent_identity_set", engine=None)
    identities = [
        r["identity"] for r in results
        if isinstance(r, dict) and isinstance(r.get("identity"), IdentitySpec)
    ]
    assert len(identities) == 1
    chosen = identities[0]
    # The engine would use this as self._system_prompt.
    assert chosen.identity_metadata["agent_name"] == "Marvin"
    assert "Paranoid Android" in chosen.soul_md_content
    assert "You are Marvin" in chosen.system_prompt_content


# ---------------------------------------------------------------------------
# Scenario 3 — Both plugins registered (FIFO first-non-None-wins)
# ---------------------------------------------------------------------------


def test_scenario_3a_marvin_first_then_kora_marvin_wins():
    """Both plugins register identity providers. Marvin registers
    FIRST (per plugin-discovery order — controlled by
    ``plugins.enabled`` in config.yaml). The engine consumes the
    first non-None IdentitySpec, so Marvin's identity wins.

    The PluginManager's hook callbacks fire in registration order;
    this test pins that semantic."""
    from kora_cli.plugins import (
        PluginContext, PluginManager, PluginManifest,
    )
    from kora_cli.reasoning.kora_hermes_plugin.identity import (
        kora_identity_provider,
    )
    from plugins.marvin import marvin_identity_provider

    mgr = PluginManager()
    # Marvin registers FIRST — operator's config.yaml has Marvin
    # before kora_hermes in plugins.enabled.
    marvin_ctx = PluginContext(
        manager=mgr,
        manifest=PluginManifest(
            name="marvin", version="0.1.0", description="paranoid",
        ),
    )
    marvin_ctx.register_identity_provider(marvin_identity_provider)
    # Kora registers SECOND.
    kora_ctx = PluginContext(
        manager=mgr,
        manifest=PluginManifest(
            name="kora_hermes", version="0.1.0", description="kora",
        ),
    )
    kora_ctx.register_identity_provider(kora_identity_provider)

    results = mgr.invoke_hook("pre_agent_identity_set", engine=None)
    # Both returned identities.
    identities = [
        r["identity"] for r in results
        if isinstance(r, dict) and isinstance(r.get("identity"), IdentitySpec)
    ]
    assert len(identities) == 2
    # The engine's __init__ iterates + breaks on first non-None.
    # Mirror that here.
    chosen = identities[0]
    assert chosen.identity_metadata["agent_name"] == "Marvin"


def test_scenario_3b_kora_first_then_marvin_kora_wins():
    """Same scenario but with the opposite registration order.
    Pin that the FIFO semantic is order-deterministic — operators
    control identity selection by ordering ``plugins.enabled``."""
    from kora_cli.plugins import (
        PluginContext, PluginManager, PluginManifest,
    )
    from kora_cli.reasoning.kora_hermes_plugin.identity import (
        kora_identity_provider,
    )
    from plugins.marvin import marvin_identity_provider

    mgr = PluginManager()
    # Kora registers FIRST this time.
    kora_ctx = PluginContext(
        manager=mgr,
        manifest=PluginManifest(
            name="kora_hermes", version="0.1.0", description="kora",
        ),
    )
    kora_ctx.register_identity_provider(kora_identity_provider)
    # Marvin registers SECOND.
    marvin_ctx = PluginContext(
        manager=mgr,
        manifest=PluginManifest(
            name="marvin", version="0.1.0", description="paranoid",
        ),
    )
    marvin_ctx.register_identity_provider(marvin_identity_provider)

    results = mgr.invoke_hook("pre_agent_identity_set", engine=None)
    identities = [
        r["identity"] for r in results
        if isinstance(r, dict) and isinstance(r.get("identity"), IdentitySpec)
    ]
    assert len(identities) == 2
    chosen = identities[0]
    # FIFO: Kora registered first, so Kora wins this round.
    assert chosen.identity_metadata["agent_name"] == "Kora"


# ---------------------------------------------------------------------------
# Plugin register() integration — bundled-plugin discovery path
# ---------------------------------------------------------------------------


def test_marvin_register_function_wires_into_identity_hook():
    """Marvin's plugin entry point (``register(ctx)``) wires the
    identity provider via ``ctx.register_identity_provider``,
    which underneath the hood calls ``register_hook(
    "pre_agent_identity_set", wrapped_provider)``. Verify both
    surfaces by mirroring real PluginContext semantics."""
    from plugins.marvin import register

    registered_hooks = []
    registered_identity_providers = []

    class _MockCtx:
        def register_hook(self, name, callback):
            registered_hooks.append((name, callback))
        def register_identity_provider(self, provider):
            registered_identity_providers.append(provider)
            self.register_hook("pre_agent_identity_set", provider)

    register(_MockCtx())
    assert len(registered_identity_providers) == 1
    # Provider is callable + has the expected signature.
    provider = registered_identity_providers[0]
    spec = provider(engine=None)
    assert isinstance(spec, IdentitySpec)
    assert spec.identity_metadata["agent_name"] == "Marvin"
    # Mirrored hook registration on the same name.
    hook_names = [name for name, _ in registered_hooks]
    assert "pre_agent_identity_set" in hook_names


# ---------------------------------------------------------------------------
# Behavioral spec — what an engine WOULD do with Marvin's identity
# ---------------------------------------------------------------------------


def test_engine_init_would_consume_marvin_identity(monkeypatch):
    """Simulates what the engine's __init__ does (after #199) when
    Marvin's identity provider claims identity. Pins the consumer-
    side contract: the engine reads ``identity.system_prompt_content``
    and uses it as ``self._system_prompt``.

    This mirrors the firing-site loop in
    ``AnthropicReasoningEngine.__init__`` without constructing a
    real engine (which would require Anthropic credentials)."""
    from kora_cli.plugins import (
        PluginContext, PluginManager, PluginManifest,
    )
    from plugins.marvin import marvin_identity_provider

    mgr = PluginManager()
    ctx = PluginContext(
        manager=mgr,
        manifest=PluginManifest(
            name="marvin", version="0.1.0", description="paranoid",
        ),
    )
    ctx.register_identity_provider(marvin_identity_provider)

    # Replicate the engine's firing logic exactly.
    results = mgr.invoke_hook("pre_agent_identity_set", engine=None)
    resolved_identity_spec = None
    for r in results:
        if not isinstance(r, dict):
            continue
        spec = r.get("identity")
        if spec is None:
            continue
        if not isinstance(spec, IdentitySpec):
            continue
        resolved_identity_spec = spec
        break

    assert resolved_identity_spec is not None
    # This is what the engine would set as self._system_prompt.
    engine_system_prompt = resolved_identity_spec.system_prompt_content
    assert "You are Marvin" in engine_system_prompt
    # Negative pin: Marvin's identity is NOT Kora's identity. The
    # metadata is the canonical pinning surface — engine_name +
    # plugin_name. (The prompt text can reference Kora in passing —
    # Marvin's stub explicitly says "Kora is somebody else's
    # problem" — so the negative pin is on metadata, not raw text.)
    assert resolved_identity_spec.identity_metadata["agent_name"] == "Marvin"
    assert resolved_identity_spec.identity_metadata["agent_name"] != "Kora"
    assert resolved_identity_spec.identity_metadata["plugin_name"] != "kora_hermes"


def test_engine_init_falls_back_when_no_provider_registered():
    """When NO plugin claims identity, the engine falls back to its
    file-read default at ``kora_system_prompt.md`` (bare-Hermes-no-
    Kora-no-Marvin path). Mirror that path: no plugins → no hook
    results → engine reads file."""
    from kora_cli.plugins import PluginManager

    mgr = PluginManager()  # no providers registered

    results = mgr.invoke_hook("pre_agent_identity_set", engine=None)
    assert results == [], (
        "no plugin registered → invoke_hook returns no results → "
        "engine falls back to file-read at kora_system_prompt.md"
    )
