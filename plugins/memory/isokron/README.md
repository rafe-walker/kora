# IsoKron Memory Provider

Kora's substrate-backed `MemoryProvider`. After KR-2 closes, Kora's memory IS the IsoKron typed-graph substrate — flat MEMORY.md / USER.md becomes a read-only fallback only.

## Architecture (hybrid, PM-leaned)

```
┌──────────────────────────────────────────────────────────────────────┐
│                 IsoKronMemoryProvider (plugins/memory/isokron/)      │
│                                                                      │
│   ┌─────────────────┐                  ┌──────────────────────────┐  │
│   │  Read path      │                  │  Write path              │  │
│   │  (asyncpg)      │                  │  (Sea MCP client)        │  │
│   │                 │                  │                          │  │
│   │  • Role Charter │                  │  • kora__write_agent_    │  │
│   │  • Cap matrix   │                  │    scratchpad            │  │
│   │  • Policy reg.  │                  │  • append_event          │  │
│   │  • Scratchpad   │                  │    (kora.* events)       │  │
│   │  • event_log    │                  │  • kora__propose_*       │  │
│   └────────┬────────┘                  └──────────┬───────────────┘  │
│            │                                      │                  │
│            │ direct PG (read-only)                │ MCP (auth+audit) │
│            ▼                                      ▼                  │
│   ┌──────────────────────────────────────────────────────────────┐   │
│   │            IsoKron Postgres + Sea MCP server                 │   │
│   └──────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
```

**Reads** go direct to Postgres because the substrate is read-only by design at those tables (Role Charter, policy registry, capability matrix, event_log query path). asyncpg keeps a hot connection pool for prefetch latency.

**Writes** always go through the Sea MCP server's `kora__*` tool surface so authorization (capability matrix gate via `cap_*` rows), Constitution pre-screen (Plan 11 envelope), and chain-audit (event_log append) all happen substrate-side. **No direct DB writes anywhere in this provider** — the path is enforced as a structural invariant in ST3.

## Configuration

Add to `~/.kora/config.yaml`:

```yaml
memory:
  provider: isokron

plugins:
  enabled:
    - isokron
  entries:
    isokron:
      # Required — Postgres DSN for substrate reads.
      isokron_dsn: postgres://kora_runtime:${KORA_DB_PASSWORD}@db.isokron.local:5432/isokron

      # Required — Sea MCP server endpoint for writes.
      # Two transports accepted: stdio://<command> or http(s)://...
      mcp_endpoint: stdio://node ../isokron/packages/sea-mcp-server/dist/cli.js

      # Optional — Kora's primary workspace UUID.
      # Falls back to per-session resolution when unset (required for cron).
      default_workspace_id: 00000000-0000-0000-0000-000000000001

      # Optional — TTL for charter/matrix/policy in-process caches.
      # Matches the @hivex/sb1-substrate-shapes TS-side reader default.
      cache_ttl_seconds: 60

      # Optional — actor_kind enum value used for substrate writes.
      # Always 'kora' in normal operation; overridable for test fixtures.
      actor_kind: kora

      # Optional — fall back to flat MEMORY.md when substrate is down.
      # Default False (IsoKron is the source of truth); opt-in for the
      # KR-2 cutover period.
      enable_legacy_fallback: false
```

Install the dependencies:

```bash
uv sync --extra isokron
```

## What KR-2 sub-tasks fill in

This `README.md` ships with **KR-2 ST1**, which delivers the structural skeleton. The provider can be instantiated and `is_available()` returns True when config + deps are present, but every non-trivial method (`prefetch`, `sync_turn`, `system_prompt_block`, etc.) raises `NotImplementedError` with a Rule-6 honest log message tagged `[kora.isokron.todo]`.

| Sub-task | Surface |
|---|---|
| ST1 (this PR) | skeleton; config schema; connection plumbing (IO loop, no real handshakes); plugin discovery wiring; 4+ smoke tests |
| ST2 | reads: `read_active_role_charter` (SHA-256 integrity), `read_kora_capability_row` (Approach A/B/C STOP-gate decision), `read_kora_policy_registry` (31-row sanity); 60s TTL cache; `system_prompt_block` assembles identity prompt block from these |
| ST3 | scratchpad reads + writes against `kronicle.agent_scratchpad_entries`; JOIN `public.actor_registry`; visibility_scope enum; writes always via Sea MCP, never direct DB |
| ST4 | `kora.*` chain event emission via Sea MCP `append_event`; recent events read from `hivex_foundation.event_log`; E2E test removes the last `NotImplementedError` stubs |

## Operator pitfalls (KR-2 ST1)

* **`memory.provider: isokron` will half-work today.** The provider registers, `is_available()` returns True (if config + deps are present), and `initialize()` brings up the IO loop — but the first call to `system_prompt_block()` / `prefetch()` / `sync_turn()` raises `NotImplementedError`. Do NOT enable on the live Kora session until ST2 lands. Keep the default (Hermes-inherited flat MEMORY.md) for now.

* **No connection happens at construct time.** Misconfigured `isokron_dsn` (wrong port, wrong credentials) surfaces as a connection error when ST2's first real query runs, not at plugin load. ST2 will add a fast-fail health check during `initialize()`.

* **MCP transport choice deferred.** The `mcp_endpoint` config field accepts either `stdio://` (subprocess) or `http(s)://`. ST3 picks one and STOP-gates if neither works against the Sea MCP server's actual exposed surface.

* **Legacy fallback off by default.** If the substrate is unreachable in production, `IsoKronConnection.start()` raises rather than silently degrading to flat MEMORY.md. Set `enable_legacy_fallback: true` to opt into the BC bridge during the KR-2 cutover.

## Provenance

This provider is **net-new in Kora** (no Hermes ancestor). It replaces Hermes' optional external providers (Honcho, Hindsight, Mem0, ByteRover, Supermemory, OpenViking, holographic, retaindb) for Kora's default operation. Those providers stay registered in the codebase but are not loaded by default — Joshua can re-enable any one of them manually if he wants secondary signals.
