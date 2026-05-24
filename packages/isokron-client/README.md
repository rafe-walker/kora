# isokron-client

IsoKron substrate client library — the BYOA foundation for any Hermes-based agent that wants to plug into the IsoKron substrate.

Phase 1 of the KR-KORA-PIP-RESTRUCTURE program (CC#3, 2026-05-24). Extracted from `plugins/memory/isokron/` in `rafe-walker/kora` so the substrate primitives can be consumed independently of the Kora Hermes-plugin shape that wraps them. See `kora_docs/14_research/kora_pip_packaging_2026-05-24/AUDIT.md` (kora-docs) for the 5-package split this package is the foundation of.

## What this is

A pure Python library that exposes the IsoKron substrate surface — events / reads / control / scratchpad / policy / constitution / capability / sea-tickets — to any consumer that can `pip install isokron-client`. Zero coupling to Hermes plugin lifecycle, no `register(ctx)` entry point, no MemoryProvider subclass. Construct an `IsoKronConnection`, hand it to the concept-module functions, get substrate data back.

## What this is NOT

This is NOT a Hermes plugin. The `IsoKronMemoryProvider` Hermes plugin (the MemoryProvider subclass that lets Hermes wire IsoKron in as the agent's memory backend) still lives in the Kora tree at `plugins/memory/isokron/` and imports FROM this library. If your goal is "make Hermes use IsoKron as its memory provider", you want the Kora tree's plugin half, not just this library.

If your goal is "I'm writing a custom Hermes-based agent and I want it to read/write the IsoKron substrate directly (chain events, sea tickets, policy, scratchpad, etc.) without taking the full Kora memory-provider lifecycle on board" — this library is exactly the right thing.

## Install

### From source (today — the only path)

```bash
git clone https://github.com/rafe-walker/kora.git
cd kora
pip install ./packages/isokron-client
```

### From PyPI (future)

Not yet on PyPI. Per `feedback-local-first-upstream-after` (Joshua, 2026-04): publish only after the library has shipped through one or two real BYOA consumers and the API surface settles. Tracking via Phase 1b in `AUDIT.md`.

## Two-consumers contract

This library is designed for two real consumers as of Phase 1:

  1. **Kora itself** (via `plugins/memory/isokron/` — the Hermes plugin shim that subclasses `MemoryProvider`). All existing Kora-internal callers continue working via the backward-compat alias the shim installs in `sys.modules`.

  2. **BYOA Hermes agents** — your custom agent + a `register()` function that wires IsoKron primitives in however you want. See the example below.

A future consumer is the Marvin POC (`plugins/marvin/`); today Marvin is identity-only (does not touch IsoKron), so the BYOA-with-isokron-client demonstration here is illustrative.

## BYOA usage example

A minimal custom Hermes-based agent that uses `isokron-client` to emit a chain event after every model action and read the active role charter at session boot:

```python
# my_agent/__init__.py — a custom Hermes plugin

from isokron_client.connection import IsoKronConnection
from isokron_client.config import IsoKronProviderConfig
from isokron_client.events import emit_kora_event
from isokron_client.reads import read_active_role_charter


_connection: IsoKronConnection | None = None


async def _on_session_start(*, session_id: str, **kw):
    """Read the active role charter at session start."""
    global _connection
    if _connection is None:
        config = IsoKronProviderConfig(
            isokron_dsn="postgres://...",
            mcp_endpoint="stdio://node /opt/sea-mcp/dist/cli.js",
            default_workspace_id="00000000-0000-0000-0000-000000000001",
            cache_ttl_seconds=60,
        )
        _connection = IsoKronConnection(config)
        await _connection.initialize()

    pool = await _connection.get_pg_pool()
    charter = await read_active_role_charter(pool)
    print(f"[my_agent] loaded charter sha256={charter.rules_hash}")


async def _post_tool_call(*, tool_name: str, args: dict, result, **kw):
    """Emit a chain event after every tool call."""
    if _connection is None:
        return
    await emit_kora_event(
        mcp_client=_connection.get_mcp_client(),
        event_name="my_agent.tool_called",
        payload={"tool": tool_name, "args_keys": list(args.keys())},
        workspace_id="00000000-0000-0000-0000-000000000001",
    )


def register(ctx):
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("post_tool_call", _post_tool_call)
```

That's the whole BYOA contract: `pip install isokron-client`, import the concept modules you need, compose them with your own plugin's `register(ctx)`. No `MemoryProvider` subclassing, no `register_memory_provider`, no Hermes-discovery shim — your plugin handles its own lifecycle and just calls the library's functions.

## Public surface — 8 conceptual sub-modules

| Concept | Module(s) | Key exports |
|---|---|---|
| `events` | `isokron_client.events` | `emit_kora_event`, `read_recent_kora_events`, `RecentChainEvent`, `ChainEventRow` |
| `reads` | `isokron_client.reads` | `read_active_role_charter`, `read_kora_policy_registry`, `read_kora_capability_row`, `policies_as_mapping` |
| `control` | `isokron_client.kora_control_reader`, `isokron_client.observed_kora_control` | `KoraControlReader`, `KoraControlCommand`, `get_observed_state_via_provider` |
| `scratchpad` | `isokron_client.scratchpad` | `read_own_scratchpad`, `read_cross_agent_scratchpad`, `write_scratchpad_entry`, `ScratchpadEntry`, `ScratchpadKind`, `VisibilityScope` |
| `policy` | subset of `isokron_client.reads` | `read_kora_policy_registry`, `policies_as_mapping` |
| `constitution` | `isokron_client.constitution` | `read_active_constitution_revision` |
| `capability` | `isokron_client.capability_check`, `isokron_client.capability_matrix_mirror` | `actor_has_capability`, `assert_kora_can_perform`, `CapabilityDeniedError`, matrix parity helpers |
| `sea_tickets` | `isokron_client.assigned_sea_tickets`, `isokron_client.cost_deferred_tickets` | `read_assigned_sea_tickets`, `read_deferred_cost_limit_tickets` |

Plus infrastructure: `connection`, `mcp_client`, `config`, `models`, `cache`. Plus standalone concept modules: `claim_heartbeat`, `dr_epoch`, `kora_operation_ledger`, `session_context`, `relationlink`.

## Out of scope for Phase 1 (deferred to Phase 1b)

The following pieces are substrate-adjacent but were left in the Kora tree on operator decision to keep Phase 1 tight:

- `kora_cli/clients/kora_control_writer.py` — SECDEF write for `issue_kora_control` (asyncpg, no MCP wrapper).
- `kora_cli/audit/jsonl_sink.py` — local JSONL audit sink (currently file-only; future substrate bridge is one function).
- `kora_cli/heartbeat_probes/supabase.py` — substrate health probe.

These move in Phase 1b once the Phase 1 API has settled. Tracked as task #450 in the operator's queue.

## License

MIT.
