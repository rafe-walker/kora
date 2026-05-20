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

### D-kr3-st2-no-relationlink-write-mcp-tool — closed by KR-9 (2026-05-21)

- **Bucket**: KR-3 ST2 (`iso_link_*` typed-edge tool family)
- **Resolved by**: KR-9 —
  `plugins/memory/isokron/relationlink.py:create_relationlink` body
  swaps from `raise RelationLinkWriteNotAvailableError()` to
  `await mcp_client.invoke('kora__create_relationlink', {...})`.
  Returns the substrate-assigned `link_id` (UUID string). The Sea
  MCP tool wraps `public.kora_create_relationlink` SECDEF which does
  actor_registry JOIN validation + active-edge uniqueness check +
  emits `kora.relationlink.created` chain event FIRST + INSERTs the
  row with the returned event_id as `chain_event_id` (single atomic
  transaction). `iso_link_create` tool handler envelope flips from
  `{ok: False, deferred: True, …}` to `{ok: True, link_id: …}` on
  success, or `{ok: False, substrate_error: True, tool_name,
  message}` on substrate failure (e.g. uniqueness violation,
  actor-kind mismatch).
- **Spec quote** (KR-9 § 0): *"CC#1 shipped K-10 (`35e67f18`) +
  IsoKron PM applied `0083` to prod. The full RelationLink-write
  stack is now live substrate-side: actor_kind CHECK has 'kora',
  `public.kora_create_relationlink` SECDEF function exists,
  `kora.relationlink.created` event literal in
  event_log_event_type_check (300-literal set),
  `kora__create_relationlink` Sea MCP tool registered."*
- **All three pre-KR-9 blockers resolved substrate-side by K-10**:
  (a) actor_kind CHECK extended to include `'kora'` via 0083;
  (b) Sea MCP tool registered; (c) chain-event emission tied into
  the SECDEF (`kora.relationlink.created` is the 300th literal in
  `event_log_event_type_check`).
- **Production-test posture** (IsoKron PM #27): K-10 handler is
  currently a `notImplementedHandler` stub awaiting dispatch tier
  (substrate task #395). KR-9's code shape is sound and ships green
  with mock tests; production deploys wait on dispatch tier landing.
  Verify-at-first-live-emit: confirm `event_log.actor_id` resolves
  to the 0076-seeded canonical Kora actor + `relationlink` row has
  `created_by_actor_kind = 'kora'` + `chain_event_id` matches the
  emitted event.
- **Deprecation runway**: `RelationLinkWriteNotAvailableError` class
  kept exported tagged `[kora.isokron.deprecated]` for one release.
  Removal when KR-N audits show no remaining references.
- **Forward-stable signature note**: KR-9 added `rationale_block_id`
  and `evidence_block_ids` parameters to match the K-10 tool input
  schema. The legacy `rationale` parameter is preserved for one
  release back-compat (silently dropped — superseded by
  `rationale_block_id`).
- **Guarded by**:
  - `tests/plugins/memory/test_iso_link_tools.py` — replaced the
    deferred-error test with six MCP-call-path tests (happy +
    error propagation + None-client defense + bad-response shape +
    optional-args pass-through + deprecation-runway).
  - `test_iso_link_create_handler_returns_ok_envelope_with_substrate_link_id`
    + `test_iso_link_create_handler_surfaces_substrate_error_envelope`
    cover the handler's envelope flips.

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
