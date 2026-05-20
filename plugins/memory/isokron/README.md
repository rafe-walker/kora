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
| ST1 | skeleton; config schema; connection plumbing (IO loop, no real handshakes); plugin discovery wiring; smoke tests |
| ST2 | reads: `read_active_role_charter` (SHA-256 integrity, asyncpg), `read_kora_capability_row` (C2 Python mirror — see "Operator pitfalls"), `read_kora_policy_registry` (RLS GUC + 31-row sanity, asyncpg); 60s TTL cache wired per-workspace; `system_prompt_block` assembles identity / CAN / CANNOT / active policies / granted caps / Rule-6 honest-label; `on_turn_start` pre-fetches all three in parallel via `asyncio.gather` |
| ST3 | scratchpad reads (`read_own_scratchpad`, `read_cross_agent_scratchpad`) against `kronicle.agent_scratchpad_entries` JOINing `public.actor_registry` for `actor_kind` / `display_name`; RLS-GUC-in-transaction; BLAKE3 integrity warn-on-mismatch; cached 60s per workspace. **Writes deferred** behind `ScratchpadWriteNotAvailableError` — see "Operator pitfalls" and `BUILD_DEVIATIONS.md` D-kr2-st3-no-scratchpad-write-mcp-tool. `sync_turn` + `on_memory_write` attempt writes through the deferred surface + catch the error gracefully. |
| ST4 | Recent `kora.*` chain events read against `hivex_foundation.event_log` (the substrate's one tenant_id-UUID-keyed table; JOIN tenant on clerk_org_id). Active Constitution revision read against `kronicle.workspace_constitution_revisions` (no `superseded_at`; ORDER BY revision_number DESC). `KoraSessionContext` shape + assembler mirroring the TS-side types.ts:130 contract. System prompt block extended with §6 "Recent kora.* activity". All six remaining ABC stubs replaced with real implementations. **Chain event emit deferred** behind `ChainEventEmitNotAvailableError` — see "Operator pitfalls" and `BUILD_DEVIATIONS.md` D-kr2-st4-no-chain-emit-mcp-tool. **KR-2 milestone closes.** |
| KR-3 ST1 | `iso_node_*` typed-graph tool family (4 tools — create / read / search / supersede) backed by `kronicle.agent_scratchpad_entries`. v0.1 packs `node_kind` into `content_inline` header (18 IsoKron entity kinds). Writes route through the deferred scratchpad path; tool handlers catch the defer + return a structured `{"ok": false, "deferred": true, "deviation_id": …}` envelope so the model gets an in-band signal. Capability checks stubbed behind `D-kr3-st1-capability-check-deferred` until KR-6 ships the Python `actorHasCapability` mirror. |
| KR-3 ST2 | `iso_link_*` typed-edge tool family (3 tools — create / traverse / list_for_node) against `public.relationlink` (ADR-0033/0034). 21 V1 link_type vocabulary. Recursive CTE for traverse (max_depth ≤ 3). Writes blocked by 3 substrate-side concurrent issues tracked as `D-kr3-st2-no-relationlink-write-mcp-tool`. |
| KR-3 ST3 (this PR) | Tool surface finalize: combined 7-tool surface via `get_tool_schemas`; `ISOKRON_TOOLSET_NAME = "isokron_memory"` constant for operator config grouping. System prompt block extended with §6a "Typed-graph tools" — model sees the 7 tool names + the Hermes-deprecation note. Hermes flat `tools/memory_tool.py:memory_tool` gets `@deprecated` markers + Rule-6 log `[kora.memory.deprecated]` on every call. Operator docs at `docs/kora-runtime/KR-3-tool-surface.md` with the 7-tool table, 18 node_kinds, 21 link_types, 4 example invocations, deferral map. Round-trip + readiness + capability-stub tests. **KR-3 milestone closes.** |

## Operator pitfalls

### Deferred-surface summary (4 open BUILD_DEVIATIONS as of KR-6)

All four follow the same shape: signature is forward-stable, body
swaps from `raise <DeferredError>` to `mcp_client.invoke(...)` when
the substrate-side dependency lands. **No caller refactor needed**.
Operators grep the deviation_id in logs to track defer rates.

| Deviation | What's deferred | Closes when |
|---|---|---|
| `D-kr2-st2-capability-matrix-mirror` | C2 Python mirror of `ACTOR_CAPABILITY_MATRIX` Kora column (parity test guards drift) | K-7 ships Sea MCP `kora__read_kora_capability_row` |
| `D-kr2-st3-no-scratchpad-write-mcp-tool` | Scratchpad writes from `sync_turn` / `on_memory_write` / `iso_node_create` / `iso_node_supersede` | K-8 ships Sea MCP `kora__write_agent_scratchpad` |
| `D-kr2-st4-no-chain-emit-mcp-tool` | Chain event emission from `on_session_end` / `on_delegation` / `iso_node_supersede` | K-9 ships Sea MCP `kora__append_event` |
| `D-kr3-st2-no-relationlink-write-mcp-tool` | `iso_link_create` writes — 3 substrate blockers in one (actor_kind CHECK + missing MCP tool + chain_event_id SECDEF) | K-10 ships the bundled substrate bucket |

**Recently closed**: `D-kr3-st1-capability-check-deferred` — KR-6
shipped the Python `actor_has_capability` helper at
`plugins/memory/isokron/capability_check.py`. Every `iso_*` tool now
gates through a real check; denied calls surface a structured
`{"ok": false, "denied": true, "capability": ..., "reason": ...}`
envelope.

### Individual pitfalls

* **Chain event emission is currently deferred — `kora.*` events are NOT being written to `event_log`.** KR-2 ST4 ships the emit API (`events.emit_kora_event`) but the substrate-side Sea MCP tool (`kora__append_event` or equivalent) doesn't exist yet (substrate main `28ff4f78`). Until it lands, every emit raises `ChainEventEmitNotAvailableError`; `sync_turn` / `on_memory_write` / `on_delegation` / `on_session_end` catch it + log a one-line WARNING tagged `D-kr2-st4-no-chain-emit-mcp-tool`. This is a chain-audit gap — operators inspecting Kora's recent activity via the `system_prompt_block` §6 section will see only events that landed in `event_log` through other paths (e.g. SECDEF-emitted events from `compact_scratchpad`). Direct INSERT into `event_log` is forbidden — it would skip the `_emit_chain_event` SECDEF and break the `prev_event_hash` / `this_event_hash` witness chain. Tracked in `BUILD_DEVIATIONS.md`.

* **`event_log` is the one genuine `tenant_id UUID`-keyed substrate table.** Every other Kora table (`kora_role_charter`, `kora_policy_registry`, `kronicle.agent_scratchpad_entries`, `kronicle.workspace_constitution_revisions`) is `workspace_id TEXT`-keyed. `read_recent_kora_events` resolves the workspace_id (Clerk `org_*`) to tenant_id via `JOIN hivex_foundation.tenant ON t.clerk_org_id = $1`. If you bypass `events.read_recent_kora_events` and write your own SQL, replicate the JOIN — a `WHERE workspace_id = $1` against `event_log` will fail (no such column on that table).

* **Constitution revision table has no `superseded_at` column.** Earlier bucket prompts referenced `WHERE superseded_at IS NULL` which would error with "column does not exist". The actual "active" semantic for `kronicle.workspace_constitution_revisions` is `ORDER BY revision_number DESC LIMIT 1` (riding the `idx_constitution_revisions_workspace_current` index). The `read_active_constitution_revision` reader gets this right; if you query the table directly, copy the SQL from `constitution.py:SELECT_ACTIVE_CONSTITUTION_REVISION_SQL`.

* **Scratchpad writes are currently deferred — sessions still run but lose their reasoning trail.** KR-2 ST3 ships the write API (`scratchpad.write_scratchpad_entry`) but the substrate-side Sea MCP tool (`kora__write_agent_scratchpad`) doesn't exist yet (substrate main `a3e77f67`). Until it lands, every write raises `ScratchpadWriteNotAvailableError`; `sync_turn` and `on_memory_write` catch it + log a one-line WARNING tagged with the BUILD_DEVIATIONS ID. Grep `D-kr2-st3-no-scratchpad-write-mcp-tool` in logs to see how often writes are being deferred. Reads (own + cross-agent) work fully. Tracked in `BUILD_DEVIATIONS.md`. The spec is explicit: do NOT bypass with direct INSERT — that would skip `cap_write_agent_scratchpad` authorization + the `approved_event_id` chain event + visibility_scope validation.

* **Scratchpad BLAKE3 integrity is warn-only, NOT fail-closed.** Unlike the Role Charter (which raises on hash mismatch), `read_own_scratchpad` and `read_cross_agent_scratchpad` log a WARNING and return the entry on mismatch. Spec § ST3: scratchpad is mutable working memory; refusing to surface a drifted entry would block sessions on transient state. Operators monitoring chain-of-custody should grep `content_hash drift` in logs.

* **Capability matrix is currently a Python mirror; parity test guards drift but a Sea MCP tool is the proper substrate path.** KR-2 ST2 ships `plugins/memory/isokron/capability_matrix_mirror.py` as a hand-translated copy of `ACTOR_CAPABILITY_MATRIX`'s Kora column (the TS const at `packages/sea-mcp-server/src/capability-matrix.ts`). A parity test (`tests/plugins/memory/test_capability_matrix_parity.py`) reads the TS source at test time and asserts every `cap_name → kora_value` matches in both directions. **CI must set `KORA_ISOKRON_REPO` to point at a cloned IsoKron substrate**, otherwise the parity test skips silently and drift won't be caught. Tracked as `D-kr2-st2-capability-matrix-mirror` in `BUILD_DEVIATIONS.md`; closes when K-7 (Sea MCP `kora__read_kora_capability_row` tool) ships.

* **Policy registry reads MUST set the RLS GUC inside the transaction.** `kora_policy_registry` has `ENABLE ROW LEVEL SECURITY` with a policy keyed off `current_setting('app.current_workspace_id', true)`. The reader calls `SELECT set_config('app.current_workspace_id', $1, true)` before the SELECT; without that, the query returns 0 rows silently. If you bypass `reads.read_kora_policy_registry` and write your own query, replicate the pattern or you'll get a confusing empty result.

* **Role Charter integrity check is fail-closed.** Recomputed SHA-256 mismatch → `RoleCharterIntegrityError`. NULL `content_md` / `content_hash` (= unpopulated K-1 ST1 shell) → same error. The provider does NOT degrade to flat MEMORY.md in either case; the session surfaces the error. Set `enable_legacy_fallback: true` to opt into the BC bridge during the cutover, but understand that means stale identity data leaks into the system prompt.

* **`memory.provider: isokron` requires a configured workspace_id.** Either set `default_workspace_id` in the config, or pass `workspace_id` via session kwargs. Without it, `system_prompt_block` returns empty string + logs a warning — Kora session runs without the Role Charter identity block, which is degraded but not broken.

* **No fast-fail connection check at `initialize()`.** Misconfigured `isokron_dsn` (wrong port, wrong credentials) surfaces as a connection error when `on_turn_start` pre-fetches, not at plugin load. By design — the pool open is lazy so lifecycle tests run without a live Postgres.

* **MCP transport choice deferred.** The `mcp_endpoint` config field accepts either `stdio://` (subprocess) or `http(s)://`. ST3 picks one and STOP-gates if neither works against the Sea MCP server's actual exposed surface.

* **Legacy fallback off by default.** If the substrate is unreachable in production, `IsoKronConnection.start()` does not raise on cold IO loop, but the first read against the pool will. Set `enable_legacy_fallback: true` to opt into the BC bridge during the KR-2 cutover; ST2 does not yet implement the fallback wiring (deferred to ST3).

## Provenance

This provider is **net-new in Kora** (no Hermes ancestor). It replaces Hermes' optional external providers (Honcho, Hindsight, Mem0, ByteRover, Supermemory, OpenViking, holographic, retaindb) for Kora's default operation. Those providers stay registered in the codebase but are not loaded by default — Joshua can re-enable any one of them manually if he wants secondary signals.
