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

### D-kr2-st3-no-scratchpad-write-mcp-tool — closed by KR-8 (2026-05-21)

- **Bucket**: KR-2 ST3 (Scratchpad reads + writes)
- **Resolved by**: KR-8 —
  `plugins/memory/isokron/scratchpad.py:write_scratchpad_entry` body
  swaps from `raise ScratchpadWriteNotAvailableError()` to
  `await mcp_client.invoke('kora__write_agent_scratchpad', {...})`.
  Returns the substrate-assigned `scratchpad_entry_id` (UUID string).
  Provider's `_attempt_scratchpad_write` fetches the
  :class:`IsoKronMCPClient` via
  `IsoKronConnection.get_mcp_client()` (KR-7a-wired) and surfaces
  substrate-side failures as `IsoKronMCPInvocationError` logged at
  ERROR (lifecycle hooks catch + log so the session stays alive).
  `iso_node_create` and `iso_node_supersede` tool handlers route
  through the same path; their response envelopes flip from
  `{ok: False, deferred: True, …}` to `{ok: True, entry_id: …}` on
  success, or `{ok: False, substrate_error: True, tool_name,
  message}` on substrate failure.
- **Spec quote** (KR-8 § 0): *"CC#1 just shipped K-8 (`bd165eb2`):
  `kora__write_agent_scratchpad` Sea MCP tool. KR-8 swaps CC#3's KR-2
  ST3 deferred-write path from `raise ScratchpadWriteNotAvailableError`
  to a real `mcp_client.invoke('kora__write_agent_scratchpad', ...)`
  call. ~30-60 min ship, ~20-40 LOC."*
- **Production-test posture** (IsoKron PM #27): K-8 handler is
  currently a `notImplementedHandler` stub on substrate main;
  substrate-team's dispatch tier (queued, task #395) un-stubs +
  bridges Layer-A `wsk_*` auth → Layer-B `actor_kind='kora'`. KR-8's
  code shape is sound and ships green with mock tests; production
  deploys wait on the dispatch tier landing.
- **Substrate-canonical chain literal** (K-DG note from spec § 1):
  K-8's internal flow emits `kronicle.agent_scratchpad.created` (NOT
  `kora.scratchpad.entry.created`). The runtime doesn't pass an
  event_type — the substrate emits internally as part of the SECDEF
  flow. Verify-at-first-live-emit step: confirm `event_log.actor_id`
  resolves to the 0076-seeded canonical Kora actor (same posture as
  KR-7's chain-emit verification).
- **Deprecation runway**: `ScratchpadWriteNotAvailableError` class
  kept exported tagged `[kora.isokron.deprecated]` for one release so
  any pinned downstream tests still resolve. Removal when KR-N audits
  show no remaining references.
- **Guarded by**:
  - `tests/plugins/memory/test_scratchpad.py` — replaced the
    deferred-error test with five MCP-call-path tests (happy +
    error propagation + None-client defense + bad-response shape +
    deprecation-runway).
  - `tests/plugins/memory/test_iso_node_tools.py` — flipped
    `test_iso_node_create_returns_deferred_payload` to
    `test_iso_node_create_returns_ok_envelope_with_substrate_entry_id`
    + added a `_substrate_error_surfaces_structured_envelope` test;
    flipped supersede test to assert success + inherited node_kind.
  - `tests/plugins/memory/test_tool_finalize.py` — round-trip tests
    updated to assert success envelopes.
  - `tests/plugins/memory/test_provider_end_to_end.py` —
    `_FakeMcpClient` extended with `kora__write_agent_scratchpad`
    routing; E2E asserts 3 scratchpad writes + 2 chain emits fire
    via the spec-pinned tool names + arg shapes.

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
