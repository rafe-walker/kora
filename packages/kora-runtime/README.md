# kora-runtime

Kora — Architect of Will — as a Hermes-plugin pip package.

Phase 1 of the KR-KORA-PIP-RESTRUCTURE program (CC#3, 2026-05-24). Extracted from `kora_cli/reasoning/kora_hermes_plugin/` in `rafe-walker/kora` so the Hermes-plugin half of Kora can be distributed independently of the Kora CLI / cockpit / promote-loops surface. See `kora_docs/14_research/kora_pip_packaging_2026-05-24/AUDIT.md` (kora-docs) for the 5-package split.

## What this is

The 7-sub-plugin orchestrator that drives Kora's reasoning route-through, registered as a Hermes plugin via the `hermes_agent.plugins` entry-point group. After install, Hermes's `PluginManager._scan_entry_points` finds this and wires Kora's behavior into every `agent.route` call where the route is in `KORA_ROUTES`.

The 7 sub-plugins:

| Sub-plugin | Hook(s) | Bucket |
|---|---|---|
| `cost_ladder` | `pre_api_request_mutable` (bundled w/ caching markers) | KR-PLUGIN-COST-LADDER (#185) |
| `audit` | `post_tool_call`, `post_llm_call` | KR-PLUGIN-AUDIT |
| `caching` | `cache_control: ephemeral` markers (consumed by cost-ladder) | KR-PLUGIN-CACHING |
| `short_circuit` | regex + snapshot phrasebook matcher | KR-PLUGIN-SHORT-CIRCUIT |
| `state_holders` | `on_session_start`, holder accessor registry | KR-PLUGIN-STATE-HOLDERS |
| `haiku_router` | `post_llm_call_can_reissue` | KR-HAIKU-ROUTER-PLUGIN |
| `identity` | `pre_agent_identity_set` (Option C identity-as-plugin) | KR-PLUGIN-IDENTITY (#199) |

## Installation

kora-runtime is a Hermes plugin. Hermes itself is not yet on PyPI per the 2026-05-25 operator decision (source-only until the architecture settles); operators install it from source first.

### Source-only path (today, the only path)

```bash
# 1. Clone Hermes (source-only Phase 1 dependency).
git clone https://github.com/NousResearch/hermes-agent
cd hermes-agent && pip install -e .

# 2. Install isokron-client (sister Phase 1 package).
cd /path/to/kora
pip install ./packages/isokron-client

# 3. Install kora-runtime against that.
pip install ./packages/kora-runtime
# OR for in-tree dev (recommended during Phase 1):
pip install -e ./packages/kora-runtime

# 4. Verify Hermes discovers the plugin.
hermes-agent --list-plugins  # should show 'kora'
```

### Future (post-Hermes-on-PyPI)

```bash
pip install kora-runtime  # transitively pulls hermes-agent + isokron-client
```

Tracked as KR-HERMES-PYPI-PUBLISH (deferred per `feedback-local-first-upstream-after`).

## Activate

After install, add to `~/.hermes/config.yaml` (or `~/.kora/config.yaml`):

```yaml
plugins:
  enabled:
    - kora
```

Then restart the Hermes daemon. The kora plugin's `register(ctx)` runs at boot and wires all 7 sub-plugins.

## Verify

Boot logs should show:

```
[kora_hermes] plugin registered: 7 sub-plugins + 3 orchestrator-resident hooks against KORA_ROUTES=[...]
```

## Source-only Hermes dependency

This package imports from three `agent.*` modules from Hermes:

- `agent.identity_spec` (eager — `identity/loader.py` + `identity/plugin.py`)
- `agent.cost_state_holder` (deferred — `cost_ladder/plugin.py`, `state_holders/registry.py`)
- `agent.operational_state_holder` (deferred — `state_holders/registry.py`)

The eager import means **the wheel cannot be imported without Hermes available** — but that's fine because the only consumer is the Hermes plugin loader, which by definition has Hermes loaded. CI dry-run installs verify the wheel BUILDS and pip-INSTALLS cleanly; import-time verification requires Hermes on the path.

## Deferred Kora-side dependencies

The 7 sub-plugins also use deferred (in-function) imports from a few `kora_cli.*` modules (`kora_cli.audit.emit_audit`, `kora_cli.reasoning.tool_registry`, `kora_cli.snapshot.state_snapshot`, `kora_constants.get_kora_home`, `plugins.kora_hermes.KORA_ROUTES`). These resolve at call time, not import time, so the wheel imports without Kora-CLI on the path — but the sub-plugins themselves only do useful work inside a Kora-CLI-bearing process. This is expected: kora-runtime IS the Hermes-plugin half of Kora; it's designed to live next to the rest of Kora's runtime context.

A future Phase 1c may refactor these out (introduce adapter shims so kora-runtime becomes a self-contained library a BYOA Hermes agent can use without the rest of Kora). Today, Phase 1's tight scope keeps these as deferred imports for backward-compat.

## Two-mode loading

This is the same pattern as the Marvin POC (#204):

1. **Bundled (in-tree dev)**: `plugins/kora_hermes/__init__.py` is the Hermes-discovery shim under the bundled-plugin convention. It re-exports the full public surface from `kora_runtime` via the back-compat shim at `kora_cli/reasoning/kora_hermes_plugin/__init__.py` (which does a sys.path bootstrap mirroring `plugins/marvin/__init__.py`).
2. **Pip-installed**: After `pip install kora-runtime` (with Hermes already installed), the `hermes_agent.plugins.kora` entry-point is discovered and Hermes invokes `kora_runtime.register(ctx)` directly.

Same `register` callable in both modes. Identical behavior. The bundled-plugin shim stays in the repo for in-tree dev convenience even after the operator starts using pip-install in production.

## License

MIT.
