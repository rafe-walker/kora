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
| KR-3 ST3 | Tool surface finalize: combined 7-tool surface via `get_tool_schemas`; `ISOKRON_TOOLSET_NAME = "isokron_memory"` constant for operator config grouping. System prompt block extended with §6a "Typed-graph tools" — model sees the 7 tool names + the Hermes-deprecation note. Hermes flat `tools/memory_tool.py:memory_tool` gets `@deprecated` markers + Rule-6 log `[kora.memory.deprecated]` on every call. Operator docs at `docs/kora-runtime/KR-3-tool-surface.md` with the 7-tool table, 18 node_kinds, 21 link_types, 4 example invocations, deferral map. Round-trip + readiness + capability-stub tests. **KR-3 milestone closes.** |
| KR-6 | Python `actor_has_capability` helper at `capability_check.py` replaces the KR-3 ST1 stub. Real check against the C2 mirror; `CapabilityDeniedError` carries `.capability` + `.reason`; tool dispatchers catch + surface a structured `{"ok": false, "denied": true, …}` envelope. Forward-stability invariant tested: module imports only the C2 mirror (no MCP/DB/network at load). **Closes `D-kr3-st1`**. |
| KR-7a (this PR) | **MCP client transport wiring** — closes the silent KR-2 ST3 stub at `connection.py:244`. New `mcp_client.py` (`IsoKronMCPClient`) wraps stdio + HTTP transports composing the canonical helpers from `tools/mcp_tool.py` (validate_remote_mcp_url, resolve_stdio_command, build_safe_env, sanitize_error, exc_str). Service-token auth: HTTP injects `Authorization: Bearer`, stdio injects `KORA_SERVICE_TOKEN` env var. New config fields: `mcp_service_token: SecretStr | None` (reads `KORA_SERVICE_TOKEN` env fallback); `mcp_transport` derived from `mcp_endpoint` URL prefix. Connection lifecycle: `get_mcp_client()` lazy-opens on first access; `close()` tears down in reverse order. **Unblocks KR-7 (chain-emit swap), KR-8 (scratchpad-write swap), KR-10-swap (relationlink-write swap)** — each now ships as the originally-spec'd ~20-40 LOC mechanical patch. |

## Operator pitfalls

### Deferred-surface summary (2 open BUILD_DEVIATIONS as of KR-7b)

Both follow the same shape: signature is forward-stable, body swaps
from `raise <DeferredError>` to `mcp_client.invoke(...)` when the
substrate-side dependency lands. **No caller refactor needed**.
Operators grep the deviation_id in logs to track defer rates.

| Deviation | What's deferred | Closes when |
|---|---|---|
| `D-kr2-st3-no-scratchpad-write-mcp-tool` | Scratchpad writes from `sync_turn` / `on_memory_write` / `iso_node_create` / `iso_node_supersede` | K-8 ships Sea MCP `kora__write_agent_scratchpad` (now merged → KR-8 dispatchable) |
| `D-kr3-st2-no-relationlink-write-mcp-tool` | `iso_link_create` writes — 3 substrate blockers in one (actor_kind CHECK + missing MCP tool + chain_event_id SECDEF) | K-10 ships the bundled substrate bucket (now merged → KR-9 dispatchable) |

**Recently closed**:
- `D-kr2-st2-capability-matrix-mirror` — KR-7b replaced the hand-
  mirrored C2 dict with an authoritative MCP fetch via
  `kora__read_kora_capability_row` at provider initialize. The
  hand-mirrored 49 entries stay as a dev/test fallback (parity test
  still guards drift) so substrate downtime falls back to the same
  data dev sees. Production posture per IsoKron PM #27: same as
  KR-7 — substrate dispatch tier un-stubs the K-7 handler;
  operators grep `[kora.capability_matrix.fallback]` to confirm
  fetch health.
- `D-kr2-st4-no-chain-emit-mcp-tool` — KR-7 swapped `emit_kora_event`
  to route through `kora__append_event` via the KR-7a-wired
  `IsoKronMCPClient`. Substrate-side failures surface as
  `IsoKronMCPInvocationError` logged at ERROR; lifecycle hooks catch
  + log so the session stays alive.
- `D-kr3-st1-capability-check-deferred` — KR-6 shipped the Python
  `actor_has_capability` helper at
  `plugins/memory/isokron/capability_check.py`. Every `iso_*` tool
  now gates through a real check; denied calls surface a structured
  `{"ok": false, "denied": true, "capability": ..., "reason": ...}`
  envelope.

### MCP client (KR-7a)

The Sea MCP server is reached via the runtime's `IsoKronMCPClient`
(`plugins/memory/isokron/mcp_client.py`). Two transports, config-driven:

- **`stdio://<command>`** — spawns the Sea MCP server as a subprocess
  (development + tests). The runtime injects `KORA_SERVICE_TOKEN` into
  the subprocess env so the server's auth layer reads it via the
  standard `service-token-auth.ts` middleware shape.
- **`http(s)://host:port/...`** — talks to a long-running HTTP MCP
  server (production). The runtime injects `Authorization: Bearer
  <token>` on every request.

The service token is read from `mcp_service_token` config field OR
the `KORA_SERVICE_TOKEN` env var (config wins on conflict). When
unset, the client still opens (tests work end-to-end without a real
token); production deploys wait on substrate-team provisioning per
`coordination/from_kora_pm/24_kora_runtime_service_token_provisioning_request.md`.

The client is lazy-opened by `IsoKronConnection.get_mcp_client()` on
first write-path call — reads + lifecycle smokes don't pay the
transport-open cost. Lifecycle (start / close / double-call
idempotency) is fully tested with mocked transports.

### Individual pitfalls

* **Chain event emission routes through `kora__append_event` via the
  KR-7a-wired `IsoKronMCPClient`.** KR-7 closed the deferred-emit surface.
  `events.emit_kora_event` now calls `mcp_client.invoke('kora__append_event', …)`
  and returns the substrate-assigned `event_id`. Substrate-side
  failures surface as `IsoKronMCPInvocationError`; the provider's
  `_attempt_chain_event_emit` catches at the lifecycle boundary
  (`on_session_end` / `on_delegation` / `iso_node_supersede`) and logs
  at ERROR (`[kora.chain.emit.failed]`) so operators see drops without
  the session crashing. Successful emits log INFO `[kora.chain.emit]`
  with the event_id. **Production-test posture** per IsoKron PM #27:
  the substrate-side K-9 handler is currently a `notImplementedHandler`
  stub; the dispatch tier (queued substrate-team) un-stubs + resolves
  Layer-A→Layer-B `actor_kind='kora'`. Until then live emits return
  substrate errors; mock-tested code shape stays correct.

* **`event_log` is the one genuine `tenant_id UUID`-keyed substrate table.** Every other Kora table (`kora_role_charter`, `kora_policy_registry`, `kronicle.agent_scratchpad_entries`, `kronicle.workspace_constitution_revisions`) is `workspace_id TEXT`-keyed. `read_recent_kora_events` resolves the workspace_id (Clerk `org_*`) to tenant_id via `JOIN hivex_foundation.tenant ON t.clerk_org_id = $1`. If you bypass `events.read_recent_kora_events` and write your own SQL, replicate the JOIN — a `WHERE workspace_id = $1` against `event_log` will fail (no such column on that table).

* **Constitution revision table has no `superseded_at` column.** Earlier bucket prompts referenced `WHERE superseded_at IS NULL` which would error with "column does not exist". The actual "active" semantic for `kronicle.workspace_constitution_revisions` is `ORDER BY revision_number DESC LIMIT 1` (riding the `idx_constitution_revisions_workspace_current` index). The `read_active_constitution_revision` reader gets this right; if you query the table directly, copy the SQL from `constitution.py:SELECT_ACTIVE_CONSTITUTION_REVISION_SQL`.

* **Scratchpad writes are currently deferred — sessions still run but lose their reasoning trail.** KR-2 ST3 ships the write API (`scratchpad.write_scratchpad_entry`) but the substrate-side Sea MCP tool (`kora__write_agent_scratchpad`) doesn't exist yet (substrate main `a3e77f67`). Until it lands, every write raises `ScratchpadWriteNotAvailableError`; `sync_turn` and `on_memory_write` catch it + log a one-line WARNING tagged with the BUILD_DEVIATIONS ID. Grep `D-kr2-st3-no-scratchpad-write-mcp-tool` in logs to see how often writes are being deferred. Reads (own + cross-agent) work fully. Tracked in `BUILD_DEVIATIONS.md`. The spec is explicit: do NOT bypass with direct INSERT — that would skip `cap_write_agent_scratchpad` authorization + the `approved_event_id` chain event + visibility_scope validation.

* **Scratchpad BLAKE3 integrity is warn-only, NOT fail-closed.** Unlike the Role Charter (which raises on hash mismatch), `read_own_scratchpad` and `read_cross_agent_scratchpad` log a WARNING and return the entry on mismatch. Spec § ST3: scratchpad is mutable working memory; refusing to surface a drifted entry would block sessions on transient state. Operators monitoring chain-of-custody should grep `content_hash drift` in logs.

* **Capability matrix is now MCP-fetched at provider initialize (KR-7b).** `IsoKronMemoryProvider.initialize()` calls `populate_capability_matrix_from_mcp` via the KR-7a-wired `IsoKronMCPClient`, replacing the hand-mirrored dict contents in place. The hand-mirrored 49 entries remain as a dev/test fallback so substrate downtime is non-fatal; the parity test (`tests/plugins/memory/test_capability_matrix_parity.py`) still guards the fallback against TS-source drift. **CI must set `KORA_ISOKRON_REPO`** for the parity test, otherwise it skips silently. Operators grep `[kora.capability_matrix.fallback]` in production logs to confirm the substrate fetch is succeeding (per IsoKron PM #27 production-test posture, the K-7 handler is currently a `notImplementedHandler` stub awaiting dispatch tier; until then the fallback is the operating state). Closed as `D-kr2-st2-capability-matrix-mirror` in `BUILD_DEVIATIONS.md`.

* **Policy registry reads MUST set the RLS GUC inside the transaction.** `kora_policy_registry` has `ENABLE ROW LEVEL SECURITY` with a policy keyed off `current_setting('app.current_workspace_id', true)`. The reader calls `SELECT set_config('app.current_workspace_id', $1, true)` before the SELECT; without that, the query returns 0 rows silently. If you bypass `reads.read_kora_policy_registry` and write your own query, replicate the pattern or you'll get a confusing empty result.

* **Role Charter integrity check is fail-closed.** Recomputed SHA-256 mismatch → `RoleCharterIntegrityError`. NULL `content_md` / `content_hash` (= unpopulated K-1 ST1 shell) → same error. The provider does NOT degrade to flat MEMORY.md in either case; the session surfaces the error. Set `enable_legacy_fallback: true` to opt into the BC bridge during the cutover, but understand that means stale identity data leaks into the system prompt.

* **`memory.provider: isokron` requires a configured workspace_id.** Either set `default_workspace_id` in the config, or pass `workspace_id` via session kwargs. Without it, `system_prompt_block` returns empty string + logs a warning — Kora session runs without the Role Charter identity block, which is degraded but not broken.

* **No fast-fail connection check at `initialize()`.** Misconfigured `isokron_dsn` (wrong port, wrong credentials) surfaces as a connection error when `on_turn_start` pre-fetches, not at plugin load. By design — the pool open is lazy so lifecycle tests run without a live Postgres.

* **MCP transport choice deferred.** The `mcp_endpoint` config field accepts either `stdio://` (subprocess) or `http(s)://`. ST3 picks one and STOP-gates if neither works against the Sea MCP server's actual exposed surface.

* **Legacy fallback off by default.** If the substrate is unreachable in production, `IsoKronConnection.start()` does not raise on cold IO loop, but the first read against the pool will. Set `enable_legacy_fallback: true` to opt into the BC bridge during the KR-2 cutover; ST2 does not yet implement the fallback wiring (deferred to ST3).

## Provenance

This provider is **net-new in Kora** (no Hermes ancestor). It replaces Hermes' optional external providers (Honcho, Hindsight, Mem0, ByteRover, Supermemory, OpenViking, holographic, retaindb) for Kora's default operation. Those providers stay registered in the codebase but are not loaded by default — Joshua can re-enable any one of them manually if he wants secondary signals.
