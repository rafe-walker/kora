# marvin-runtime

Paranoid-android plugin POC for the Hermes Option C identity-as-plugin architecture.

Built as the proof-of-concept that validates the pip-installable distribution surface for external IsoKron users (CC#3 #204; companion to #199's architecture work and #203's runnable-plugin validation).

## What this is

A minimal Hermes plugin that claims the agent's identity via the `pre_agent_identity_set` hook. When Marvin is loaded alongside (or instead of) Kora's plugin, the reasoning engine wraps every inference call with Marvin's system prompt — making the agent answer as Marvin (Paranoid Android) rather than Kora.

## What this is NOT

Not a production runtime. Marvin has no substrate, no tools, no operator-facing capability. The plugin exists solely to validate that the Hermes Option C architecture supports a non-Kora identity via the documented surface.

## Install

### From source (today)

```bash
git clone https://github.com/rafe-walker/kora.git
cd kora
pip install plugins/marvin
```

### From PyPI (future — not yet published)

```bash
pip install marvin-runtime  # NOT YET ON PYPI; see CC#3 #204 PM hand-off
```

Per Joshua's `feedback-local-first-upstream-after` amendment: **PyPI publish deferred** until the architecture is battle-tested.

## Activate

Add to `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - marvin
```

Restart the Hermes daemon. Inference calls now use Marvin's identity.

## Verify

Boot logs should show:

```
[marvin] identity provider registered (soul_chars=N, system_prompt_chars=N)
```

## See also

- `HOW_TO_BUILD_YOUR_OWN_AGENT.md` (kora-docs PR #4) — full architecture walkthrough; explains the `IdentitySpec` contract, the `register_identity_provider` convenience, and how to build your OWN identity plugin (Dave / Atlas / whoever) using the same pattern.
- `MARVIN_DEMO_TRANSCRIPT.md` (kora-docs PR #5) — the 4-scenario multi-tenant validation transcript that proved Option C works.

## License

MIT.
