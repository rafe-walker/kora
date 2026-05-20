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

### D-kr2-st4-no-chain-emit-mcp-tool

- **Bucket**: KR-2 ST4 (chain event emission + recent events read + finalize)
- **Why**: Spec § ST4 § 1 mandates chain events go through a Sea MCP
  tool (working name `kora__append_event`) — direct INSERT into
  `hivex_foundation.event_log` is forbidden because it would skip the
  substrate's `_emit_chain_event` SECDEF (which sets `prev_event_hash` /
  `this_event_hash` to maintain chain witness integrity). The Sea MCP
  server on substrate main `28ff4f78` exposes only
  `kora__propose_convention`, `kora__read_escalation_queue`,
  `kora__propose_policy_change` — no append-event tool. Same pattern
  as the ST3 scratchpad-write deferral.
- **Closes when**: A Sea MCP append-event tool ships (working name
  `kora__append_event`; PM coordinates with substrate-team / files
  the substrate dispatch — likely K-9 on CC#1's lane, queued behind
  K-7 + K-8). When it lands, `events.emit_kora_event` body switches
  from `raise ChainEventEmitNotAvailableError()` to
  `mcp_client.invoke('kora__append_event', ...)`. Caller signature
  stays unchanged — `provider._attempt_chain_event_emit` and every
  lifecycle hook that uses it (`sync_turn`, `on_memory_write`,
  `on_session_end`, `on_delegation`) keep working without refactor.
- **Guarded by**:
  - `plugins/memory/isokron/events.py` — top-of-module `[kora.isokron.todo]`
    tag; `ChainEventEmitNotAvailableError` carries the deviation ID in
    every raised message.
  - `IsoKronMemoryProvider._attempt_chain_event_emit` — catches
    `ChainEventEmitNotAvailableError` + logs a one-line WARNING
    tagged with the deviation ID and the event_type that was skipped.
    Operators grep `D-kr2-st4-no-chain-emit-mcp-tool` in logs.
  - `plugins/memory/isokron/README.md` § "Operator pitfalls" —
    chain event deferral notice.
  - `tests/plugins/memory/test_events.py` —
    `test_emit_kora_event_raises_deferred_write_error` asserts the
    error message + tag stay correct.

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

### D-kr2-st2-capability-matrix-mirror

- **Bucket**: KR-2 ST2 (IsoKron memory provider — read paths)
- **Why**: Sea MCP server does not expose a `kora__read_kora_capability_row`
  tool on main (`b0804640`, 2026-05-20). The `ACTOR_CAPABILITY_MATRIX`
  source of truth is a TS const at
  `packages/sea-mcp-server/src/capability-matrix.ts`, not a Postgres
  table — so Approach B (asyncpg SELECT) is not viable. PM-decided
  STOP-gate resolution on 2026-05-20: ship C2 (Python mirror) now
  rather than block CC#3 on a CC#1 dependency.
- **Closes when**: K-7 (Sea MCP capability-row tool) ships — at that
  point the read path swaps to call the MCP tool, the Python mirror
  becomes a test fixture only, and the parity test stays in place
  as a smoke check across CI configurations that still hit the
  mirror as a fallback. PM is drafting the K-7 bucket.
- **Guarded by**:
  - `plugins/memory/isokron/capability_matrix_mirror.py` — top-of-file
    `[kora.isokron.todo]` tag.
  - `tests/plugins/memory/test_capability_matrix_parity.py` —
    parses the TS source and asserts every cap_name → kora_value
    matches the Python mirror in both directions.
  - `plugins/memory/isokron/README.md` § "Operator pitfalls" —
    operator-facing drift notice.

## Closed

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
