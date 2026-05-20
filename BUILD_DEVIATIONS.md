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
