# KR-3 — Typed-Graph Tool Surface

KR-3 ships Kora's beads-pattern memory surface: 7 model-facing MCP
tools that replace Hermes' flat `memory.*` family with typed-node
and typed-edge operations against the IsoKron substrate.

The tools register through `MemoryProvider.get_tool_schemas`
(consumed by `MemoryManager`) under the logical toolset name
`isokron_memory`. They surface automatically when
`memory.provider: isokron` is configured in `~/.kora/config.yaml`.

## The 7 tools

| Tool | Purpose | Path | Status |
|---|---|---|---|
| `iso_node_create` | Append a new typed node (18 canonical IsoKron entity kinds) | write | **deferred** (D-kr2-st3) |
| `iso_node_read` | Point read by `entry_id` | read | live |
| `iso_node_search` | Filter by `node_kind` + free-text substring + `cross_agent_only` | read | live |
| `iso_node_supersede` | Append-only revision (inherits original `node_kind`); emits `kora.node.superseded` | write | **deferred** (D-kr2-st3 + D-kr2-st4) |
| `iso_link_create` | Declare a typed edge (21 V1 link_type vocabulary per ADR-0033) | write | **deferred** (D-kr3-st2 — 3 substrate blockers) |
| `iso_link_traverse` | Walk edges from a start node; recursive CTE; `max_depth ≤ 3` | read | live |
| `iso_link_list_for_node` | All active edges where the node is source or target | read | live |

**Reads work today** against the existing substrate; **writes are deferred**
behind structured `{"ok": false, "deferred": true, "deviation_id": "D-…"}`
envelopes until the substrate-side Sea MCP tools ship. The model gets
an in-band signal rather than an exception escape, and the
deviation_id maps directly to a BUILD_DEVIATIONS entry with a closure
condition.

## 18 canonical `node_kind` values

```
Decision  Gotcha  Pattern  Convention  Concept  Ticket  FailedAttempt
AcceptanceTest  GamingPattern  Project  Component  Milestone  CrossCut
ExternalDependency  Resource  Tool  Schema  KronicleBlock
```

Source of truth: `plugins/memory/isokron/tools/iso_node.py:NODE_KINDS`,
mirrored from `packages/sea-mcp-server/src/capability-matrix.ts` and
the IsoKron canonical-entity-kinds doc.

**Prefer the most specific kind over `Concept`.** `Concept` is the
"don't know what to call this" fallback; using it for everything
defeats the value of the typed graph.

## 21 V1 `link_type` values (ADR-0033)

**Sea-idea** (11): `parent_of`, `relates_to`, `inspired_by`,
`responds_to`, `conflicts_with`, `supersedes`, `blocks`, `depends_on`,
`condenses_into`, `branches_from`, `references`.

**Platform-wide** (10): `derived_from`, `applies_to`, `validates`,
`grounds_in`, `documented_in`, `implements`, `duplicates`,
`caused_by`, `part_of`, `covers`.

Reserved for v1.5+: `same_as`.

The `relationlink` SQL column is un-CHECK'd `TEXT` (extensible vocab);
the JSON Schema enum on `iso_link_create` enforces it client-side, and
the application-layer per-pair gate config is the normative source of
truth.

## `cross_agent_dereferenceable` — handoffs to Critic / Oracle

Set `cross_agent_dereferenceable=true` on `iso_node_create` when the
node should be visible to Critic and Oracle (and any future
cross-agent reader). The default is `false` (agent-private).

Examples of when to use `true`:
- Override rationale that Critic needs to review.
- Handoff context for a delegation to `claude_pm`.
- Self-critique that Oracle should consider during her advisory pass.

Cross-agent entries are also surfaced via `iso_node_search` when
`cross_agent_only=true` and via `iso_link_*` traversal across actor_kinds.

## Example invocations

### Record a Decision

```json
{
  "name": "iso_node_create",
  "arguments": {
    "node_kind": "Decision",
    "title": "Use IsoKron substrate for Kora's memory",
    "content_summary": "Picked IsoKron over flat MEMORY.md because…",
    "cross_agent_dereferenceable": false,
    "scratchpad_kind": "route_decision"
  }
}
```

### Search for past Gotchas mentioning a topic

```json
{
  "name": "iso_node_search",
  "arguments": {
    "node_kind": "Gotcha",
    "text_query": "asyncpg pool",
    "limit": 5
  }
}
```

### Walk supersession chains from a Pattern

```json
{
  "name": "iso_link_traverse",
  "arguments": {
    "from_entity_id": "<pattern-uuid>",
    "link_types": ["supersedes"],
    "direction": "outgoing",
    "max_depth": 3
  }
}
```

### Declare a contradiction (deferred — surfaces structured defer envelope)

```json
{
  "name": "iso_link_create",
  "arguments": {
    "from_entity_id": "<decision-A-uuid>",
    "from_entity_kind": "Decision",
    "to_entity_id": "<decision-B-uuid>",
    "to_entity_kind": "Decision",
    "link_type": "conflicts_with",
    "rationale": "Newer evidence makes B unworkable"
  }
}
```

Today this returns:
```json
{
  "ok": false,
  "deferred": true,
  "deviation_id": "D-kr3-st2-no-relationlink-write-mcp-tool",
  "message": "[kora.isokron.todo] relationlink writes deferred — three blockers…"
}
```

When K-10 (RelationLink Kora-write enablement substrate bucket) ships,
the same call returns `{"ok": true, "link_id": "<uuid>"}` with no
caller-side refactor needed.

## Open deferrals (closure paths)

| Deviation | Affects | Closes when |
|---|---|---|
| `D-kr2-st2-capability-matrix-mirror` | `cap_*` checks | K-7 (Sea MCP `kora__read_kora_capability_row`) |
| `D-kr2-st3-no-scratchpad-write-mcp-tool` | `iso_node_create`, `iso_node_supersede` writes | K-8 (Sea MCP `kora__write_agent_scratchpad`) |
| `D-kr2-st4-no-chain-emit-mcp-tool` | `iso_node_supersede` chain event | K-9 (Sea MCP `kora__append_event`) |
| `D-kr3-st1-capability-check-deferred` | `assert_kora_can_perform` stub | KR-6 (Python `actorHasCapability` mirror) |
| `D-kr3-st2-no-relationlink-write-mcp-tool` | `iso_link_create` write (3 substrate blockers in one) | K-10 (substrate bucket — CHECK + MCP tool + chain SECDEF) |

All five follow the same shape: signature is forward-stable, body
swaps from `raise <DeferredError>` to `mcp_client.invoke(...)`. No
caller refactor needed when substrate ships.

## Hermes flat `memory` tool — deprecated

The flat `memory` tool (file-backed MEMORY.md / USER.md) stays
loadable for one-release runway so existing test fixtures + cron jobs
don't break. Every call logs a deprecation WARNING tagged
`[kora.memory.deprecated]`. Removal targeted KR-7 or later.

Operators migrating: grep `[kora.memory.deprecated]` in logs to find
remaining usage sites, then replace with the equivalent typed-graph
tool call:

| Flat | Typed-graph equivalent |
|---|---|
| `memory.add` | `iso_node_create` with `node_kind="Concept"` (or more specific) |
| `memory.replace` | `iso_node_supersede` with `supersession_reason` |
| `memory.remove` | No direct equivalent — typed-graph is append-only; the original entry stays as `status='superseded'` rather than being deleted |

## Operator config

Default `~/.kora/config.yaml` enables the toolset when the IsoKron
provider is selected:

```yaml
memory:
  provider: isokron
  toolsets:
    isokron_memory: true   # the 7-tool typed-graph surface (KR-3)
```

The `toolsets.isokron_memory` switch is informational today —
`MemoryProvider.get_tool_schemas` is the source of truth for which
tools surface, and that hook returns the full 7-tool set whenever the
isokron provider is active. The switch lets operators turn the surface
off without disabling the rest of the provider (reads + caching stay
live).
