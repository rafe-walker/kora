# BUILD_DEVIATIONS

Each entry tags a knowing departure from the bucket spec — Approach C
fallbacks, deferred work, interim mirrors, etc. Entries move from
**Open** to **Closed** when the substrate path lands and the workaround
is removed.

Format:

```
### D-<bucket>-<slug>
- **Bucket**: link or short name
- **Why**: original constraint (blocked dependency, etc.)
- **Closes when**: the condition under which the workaround is removed
- **Guarded by**: tests / parity checks keeping the workaround correct
```

## Open

### D-kr3-st2-no-relationlink-write-mcp-tool

- **Bucket**: KR-3 ST2 (`iso_link_*` typed-edge tool family)
- **Why**: Three substrate-side blockers gate the `iso_link_create`
  write path. Verified against `packages/db/migrations/0058_relationlink.sql`
  on substrate main `41ddc208`:
  1. `created_by_actor_kind` CHECK lacks `'kora'`. The check covers
     7 actor_kinds + 3 synthetic platform kinds = 8 entries total:
     `operator, oracle, critic, claude_pm, hermes, platform_seal,
     platform_rollback, platform_session_expiry`. A Kora-side INSERT
     would fail the CHECK.
  2. No Sea MCP write tool exposes the path. The `kora__*` tool
     inventory on substrate main is: `kora__propose_convention`,
     `kora__read_escalation_queue`, `kora__propose_policy_change`.
     No `kora__create_relationlink` (or equivalent).
  3. `chain_event_id UUID NOT NULL` requires a chain event emit
     bound to the write — substrate-team owns the SECDEF wrapper
     (same pattern as `kronicle.compact_scratchpad` from Plan 02).
     Direct INSERT into `relationlink` would either fail (no
     chain_event_id) or, if filled in client-side, would break
     the chain witness invariant.
- **Closes when**: PM dispatches a substrate-side bucket that
  (a) extends the actor_kind CHECK to include `'kora'`, (b) adds
  the Sea MCP write tool, (c) ties chain-event emission into the
  same SECDEF. Then `relationlink.create_relationlink` body switches
  from `raise RelationLinkWriteNotAvailableError()` to
  `mcp_client.invoke('kora__create_relationlink', ...)`. Signature
  stays unchanged.
- **Guarded by**:
  - `plugins/memory/isokron/relationlink.py` —
    `RelationLinkWriteNotAvailableError` carries all three blockers
    verbatim in the error message; operators grep
    `D-kr3-st2-no-relationlink-write-mcp-tool` in logs.
  - `plugins/memory/isokron/tools/iso_link.py:_handle_iso_link_create`
    catches the error + returns a structured `{"ok": false,
    "deferred": true, "deviation_id": "D-kr3-st2-..."}` envelope so
    the model gets an in-band signal.
  - `tests/plugins/memory/test_iso_link_tools.py:test_create_relationlink_raises_deferred_write_error`
    asserts the message contains all three blockers.

### D-kr2-st3-no-scratchpad-write-mcp-tool

- **Bucket**: KR-2 ST3 (Scratchpad reads + writes)
- **Why**: Spec § ST3 § 3 mandates writes go through a Sea MCP tool
  (`kora__write_agent_scratchpad`) — direct INSERT is explicitly forbidden
  because the tool gates `cap_write_agent_scratchpad` authorization,
  emits the `approved_event_id NOT NULL` chain event required by
  foundation/0135, and validates `visibility_scope` semantics. The
  substrate's Sea MCP server (current main `a3e77f67`) exposes
  only `kora__propose_convention`, `kora__read_escalation_queue`, and
  `kora__propose_policy_change` — no scratchpad-write tool. Spec § ST3
  pre-authorizes this BUILD_DEVIATIONS path: "If the tool doesn't exist
  substrate-side yet, BUILD_DEVIATIONS + queue for substrate-team via
  PM coordination. Do NOT bypass with direct INSERT."
- **Closes when**: A Sea MCP write tool for `kronicle.agent_scratchpad_entries`
  ships (working name `kora__write_agent_scratchpad`; PM coordinates
  with substrate-team / files the substrate dispatch). Then
  `scratchpad.write_scratchpad_entry` swaps from raising
  `ScratchpadWriteNotAvailableError` to calling
  `mcp_client.invoke('kora__write_agent_scratchpad', ...)`. Caller
  signature stays unchanged — no provider-side refactor needed.
- **Guarded by**:
  - `plugins/memory/isokron/scratchpad.py` — top-of-module `[kora.isokron.todo]`
    tag in the docstring; `ScratchpadWriteNotAvailableError` carries the
    deviation ID in every raised message.
  - `IsoKronMemoryProvider.sync_turn` / `on_memory_write` —
    catch `ScratchpadWriteNotAvailableError` + log a one-line WARNING
    so sessions stay alive while the substrate tool ships. Operators
    grep `D-kr2-st3-no-scratchpad-write-mcp-tool` in logs to see how
    often writes are being deferred.
  - `plugins/memory/isokron/README.md` § "Operator pitfalls" —
    operator-facing notice of the deferred-write semantics.
  - `tests/plugins/memory/test_scratchpad.py` —
    `test_write_scratchpad_entry_raises_deferred_write_error` asserts
    the error message + tag stay correct.

## Closed

### D-kr2-st2-capability-matrix-mirror — closed by KR-7b (2026-05-20)

- **Bucket**: KR-2 ST2 (capability matrix Kora row)
- **Resolved by**: KR-7b — `populate_capability_matrix_from_mcp` in
  `plugins/memory/isokron/capability_matrix_mirror.py` fetches the
  authoritative Kora-row matrix from K-7's `kora__read_kora_capability_row`
  Sea MCP tool (substrate `ee730853`) at `IsoKronMemoryProvider.initialize()`
  via the KR-7a-wired `IsoKronMCPClient`, replacing the hand-mirrored
  49-entry C2 dict in place. The dict identity is preserved, so
  `capability_check.actor_has_capability` keeps consuming it by
  reference — no caller-side refactor needed. Forward-stable: K-13's
  upcoming capability additions flow through automatically at the
  next provider start.
- **Spec quote** (KR-7b § 90): *"KR-7b ships boot-time MCP fetch
  replacing hand-mirrored TS-source dict. K-7 (ee730853) shipped the
  substrate-side tool. Forward-stable; KR-7a transport wiring is
  independent and may eventually consolidate to a unified MCP client."*
  CC#3 elected Option A (use KR-7a's `IsoKronMCPClient`) over Option
  B (parallel httpx fetch path) since KR-7a is shipped — one
  canonical MCP-call pattern across all closure swaps.
- **Production-test posture** (same as KR-7): K-7's handler is a
  `notImplementedHandler` stub on substrate main; dispatch tier
  (queued substrate-team) un-stubs it. KR-7b's code shape is sound;
  mock tests verify the populate machinery; production deploys wait
  on dispatch tier. On fetch failure, hand-mirrored fallback stays in
  place + `[kora.capability_matrix.fallback]` WARNING logged so
  dev/test ergonomics survive substrate downtime.
- **Hand-mirrored fallback retained**: 49-entry C2 dict stays as the
  default at module import — same content, repurposed from "C2
  interim" to "dev/test fallback". The parity test at
  `tests/plugins/memory/test_capability_matrix_parity.py` keeps
  guarding the fallback against TS-source drift so dev parity matches
  production-substrate parity (and so when K-13 ships, the parity
  test catches the 2-line bump that the fallback needs even though
  production picks up the new caps automatically).
- **Guarded by**:
  - `tests/plugins/memory/test_capability_matrix_mcp_fetch.py` —
    11 tests covering happy populate, in-place dict mutation, defensive
    error paths (None client / missing key / non-dict / non-bool /
    non-str / propagated underlying error), and provider.initialize
    success-INFO + dual-fallback-WARNING paths.
  - `tests/plugins/memory/test_provider_end_to_end.py` —
    `_FakeMcpClient.invoke` routes by tool_name and returns a
    canonical-shape matrix for `kora__read_kora_capability_row`;
    E2E asserts the initialize-time fetch fired + replaced the dict.

### D-kr2-st4-no-chain-emit-mcp-tool — closed by KR-7 (2026-05-20)

### D-kr2-st4-no-chain-emit-mcp-tool — closed by KR-7 (2026-05-20)

- **Bucket**: KR-2 ST4 (chain event emission)
- **Resolved by**: KR-7 — `plugins/memory/isokron/events.py:emit_kora_event`
  body swaps from `raise ChainEventEmitNotAvailableError()` to
  `await mcp_client.invoke('kora__append_event', {...})`. Returns the
  K-9 substrate tool's `event_id` (UUID string). The provider's
  `_attempt_chain_event_emit` fetches the real
  :class:`IsoKronMCPClient` via `IsoKronConnection.get_mcp_client()`
  (wired in KR-7a) and surfaces substrate-side failures as
  `IsoKronMCPInvocationError` logged at ERROR (lifecycle hooks catch
  + log so the session stays alive). `iso_node_supersede`'s
  `kora.node.superseded` emit routes through the same helper instead
  of duplicating the wiring.
- **Spec quote** (KR-7 § 0): *"CC#1 just shipped K-9 (`f8487059`):
  the `kora__append_event` Sea MCP tool now exists. KR-7 swaps CC#3's
  KR-2 ST4 deferred-emit path from the placeholder error to a real
  MCP call. ~20-40 lines Python; single PR; closes one
  BUILD_DEVIATIONS."*
- **Production-test posture** (IsoKron PM #27): K-9's
  `kora__append_event` handler is currently a `notImplementedHandler`
  stub on substrate main; substrate-team's dispatch tier (queued)
  bridges Layer-A `wsk_*` auth → Layer-B `actor_kind='kora'` and
  un-stubs the handler. KR-7's code shape is sound and ships green
  with mock tests; production deploys wait on the dispatch tier
  landing. Verify-at-first-live-emit step: confirm
  `event_log.actor_id` resolves to the 0076-seeded canonical Kora
  actor (`actor_kind='kora' AND workspace_id=<Flynn workspace
  clerk_org_id>`) — if it resolves to a token-UUIDv5 instead (the
  `cowork-claude-pm` precedent), small substrate patch needed.
- **Deprecation runway**: `ChainEventEmitNotAvailableError` class
  kept exported (tagged `[kora.isokron.deprecated]`) for one release
  so any pinned downstream tests still import it. Class removal
  scheduled when KR-N audits show no remaining references.
- **Guarded by**:
  - `tests/plugins/memory/test_events.py` — replaced the
    deferred-error test with four MCP-call-path tests covering happy,
    error propagation, None-client defense, and unexpected-response
    shape; deprecation-runway test asserts the class is still
    importable.
  - `tests/plugins/memory/test_provider_end_to_end.py` —
    `_FakeProviderConnection` now exposes `get_mcp_client()` returning
    a `_FakeMcpClient`; E2E asserts both emits succeed with the
    spec-pinned tool name + arg shape.

### D-kr3-st1-capability-check-deferred — closed by KR-6 (2026-05-20)

- **Bucket**: KR-3 ST1 (`iso_node_*` tool family)
- **Resolved by**: KR-6 — Python `actor_has_capability` mirror lands at
  `plugins/memory/isokron/capability_check.py`, consuming the C2
  `ACTOR_CAPABILITY_MATRIX_KORA_COLUMN` dict. The KR-3 ST1 stub at
  `plugins/memory/isokron/tools/iso_node.py:assert_kora_can_perform`
  (which always allowed + logged `[kora.isokron.todo]
  D-kr3-st1-capability-check-deferred`) was replaced with a re-export
  from the new module; the helper is now a real check that raises
  `CapabilityDeniedError` when Kora lacks the requested capability.
- **Spec quote** (KR-6 § 0): *"KR-3 ST1 added an
  `assert_kora_can_perform(capability)` stub in the Kora runtime that
  logs every invocation with `[kora.capability.deferred]` tag but
  doesn't actually gate anything. The TypeScript-side has
  `actorHasCapability(actor_kind, capability)` at
  `packages/sea-mcp-server/src/capability-matrix.ts:657` which does
  the real check. KR-6 ships the Python equivalent on the runtime
  lane."*
- **Forward-stability invariant kept**: `capability_check.py` imports
  only the C2 mirror — no MCP / DB / network. When K-7 ships
  `kora__read_kora_capability_row` and a follow-on KR-N swap replaces
  the C2 mirror with a fresh-per-call MCP fetch, only that import line
  changes; the helper's public surface stays identical. Guarded by
  `tests/plugins/memory/test_capability_check.py::test_module_has_no_network_or_db_imports_at_load_time`
  and the companion only-imports-the-mirror test.
- **Tool-handler integration**: the iso_node + iso_link dispatchers
  catch `CapabilityDeniedError` and surface it as a structured
  `{"ok": false, "denied": true, "capability": ..., "reason": ...}`
  envelope, mirroring the deferred-write envelope pattern from KR-2
  ST3 / ST4 / KR-3 ST2 — model gets an in-band signal rather than an
  uncaught exception.
