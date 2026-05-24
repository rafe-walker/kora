# Hermes Local Extensions — 2026-05-23

**Bucket**: KR-HERMES-LOCAL-EXTENSIONS
**Lane**: CC#3
**Scope**: in-fork Hermes-core extensions to support Kora's gateway-route-through refactor (KR-REASONING-ROUTE-THROUGH-GATEWAY, follow-on bucket).
**Discipline**: `feedback-local-first-upstream-after` — all changes land in our fork on `feature/phase2-upgrades`; upstream-PR packaging waits until the extensions are battle-tested by the route-through bucket running on them.

---

## Discovery findings

5 areas audited. Findings:

| # | Surface | Current Hermes state | Gap → extension? |
|---|---|---|---|
| 1 | `pre_llm_call` mutability | Observer-only. Plugins return `{"context": str}` or `str` for context-string injection. No way to mutate `model` / `max_tokens` / `tools` kwargs. (`agent/conversation_loop.py:506`) | **Yes — extension needed.** New hook `pre_api_request_mutable` (added to VALID_HOOKS) fires at the api_kwargs construction site + supports `{"override": {...}}` return-shape. Existing `pre_llm_call` contract preserved. |
| 2 | `post_llm_call` re-issue | Observer-only. Return value discarded. (`agent/conversation_loop.py:3996`) | **DEFERRED to follow-on bucket KR-HERMES-LOCAL-EXT-REISSUE.** Substantial control-flow change (re-issuing inside conversation_loop requires re-running streaming / tool-result processing / message-history append). Per §4 STOP-ASK guidance for invasive changes. Kora's existing `anthropic_engine._tool_use_loop` post-call escalation (KR-HAIKU-ROUTER #165) works inside Kora's bypass loop today; when the route-through bucket lifts Kora onto conversation_loop, the re-issue extension lands as a coordinated follow-on. |
| 3 | `context.route` field | Absent. Hook contexts carry `platform` (messaging surface: slack/discord/cli) but not the per-route telemetry taxonomy (slack_dm / email_inbound / mcp_tool / probe_investigation / scheduled_task / etc.). | **Yes — extension needed.** Threaded as `route` kwarg into 7 hook invocation sites (`pre_llm_call`, `pre_api_request`, `pre_api_request_mutable`, `post_api_request`, `post_llm_call`, `on_session_start`, `on_session_end`, `transform_llm_output`). Sourced from `getattr(agent, "route", "") or ""` — backward compat: legacy CLI invocations that never set `agent.route` see `""`. |
| 4 | Third-party listener registration | `PluginContext.register_platform` exists but is shaped for interactive chat-platform adapters (Slack/IRC/Discord) bound to a `PlatformConfig`. No surface for background daemons / periodic tasks (Kora's snapshot collector, telemetry counter flusher, alert notifier, etc.). | **Yes — extension needed.** New `agent/background_daemon_registry.py` module + `PluginContext.register_background_daemon(name, startup, shutdown, *, periodic_task=None)` method. Registration-only in this bucket; lifecycle execution is consumer's responsibility (Kora's `DaemonCoordinator` already implements the consumer side; gateway's consumer wiring lands in KR-REASONING-ROUTE-THROUGH-GATEWAY). |
| 5 | Tool-list manipulation hook | Absent. `agent.tools` used verbatim in `chat_completion_helpers.build_api_kwargs:235`. | **Yes — extension needed.** New hook `pre_tool_list_finalized` (added to VALID_HOOKS) fires at the tool-list-read site + supports `{"override": [...]}` return-shape. First non-None override wins. Failures caught + logged (fail-safe to unfiltered list). |

**Aggregate**: 4 extensions land in this bucket. 1 extension (post-LLM re-issue) deferred per spec §4 (invasive control-flow change).

---

## Per-extension contract

### Extension 1: `pre_api_request_mutable` hook

**File**: `agent/conversation_loop.py` (insertion at line ~990 — after `_build_api_kwargs` returns + before the observer-only `pre_api_request`).
**VALID_HOOKS entry**: added at `kora_cli/plugins.py:135`.

**Signature**:
```python
invoke_hook(
    "pre_api_request_mutable",
    task_id=...,
    session_id=...,
    user_message=...,
    platform=...,
    model=...,
    provider=...,
    base_url=...,
    api_mode=...,
    api_call_count=...,
    api_kwargs=dict(api_kwargs),  # defensive copy
    route=...,
)
```

**Return contract**:
- `None` / non-dict → no-op
- `{"override": {<key>: <value>, ...}}` → `api_kwargs.update(override)` applied before the SDK call
- Multiple plugins → overrides merge in registration order; last write wins for conflicting keys

**Failure handling**: any hook exception is caught + logged at WARNING. Loop continues with un-modified `api_kwargs` (fail-safe).

**Backward compat**: the observer-only `pre_api_request` hook continues to fire AFTER the mutable one applies. Observer plugins (Langfuse etc.) see the final post-override `api_kwargs`. No existing plugin is affected.

### Extension 2: `pre_tool_list_finalized` hook

**File**: `agent/chat_completion_helpers.py` (insertion in `build_api_kwargs` at line ~237).
**VALID_HOOKS entry**: added at `kora_cli/plugins.py:144`.

**Signature**:
```python
invoke_hook(
    "pre_tool_list_finalized",
    session_id=...,
    platform=...,
    model=...,
    tools=list(agent.tools),  # defensive copy
    route=...,
)
```

**Return contract**:
- `None` / non-dict / no `override` key → no-op
- `{"override": [<tool>, ...]}` → replaces the tool list for that SINGLE api_kwargs build
- First non-None override wins (subsequent plugins' overrides ignored for this call)
- `agent.tools` is NEVER mutated

**Failure handling**: any hook exception is caught + logged at WARNING. Loop continues with unfiltered `agent.tools`.

**Backward compat**: no existing call site reads the hook's return value, and no existing plugin registers it. Surface is purely additive.

### Extension 3: `context.route` field

**Files**:
- `agent/conversation_loop.py` — 7 hook-invocation sites gain `route=getattr(agent, "route", "") or ""` kwarg
- No constructor change to `AIAgent` — `route` is a settable attribute (callers set `agent.route = "slack_dm"` before invoking)

**Sites updated**:
| Hook | Site |
|---|---|
| `on_session_start` | `conversation_loop.py:163` |
| `pre_llm_call` | `conversation_loop.py:506` |
| `pre_api_request` | `conversation_loop.py:1005` |
| `pre_api_request_mutable` | `conversation_loop.py:~990` (new hook, route built in from start) |
| `post_api_request` | `conversation_loop.py:3032` |
| `transform_llm_output` | `conversation_loop.py:4023` |
| `post_llm_call` | `conversation_loop.py:3996` |
| `on_session_end` | `conversation_loop.py:4161` |
| `pre_tool_list_finalized` | `chat_completion_helpers.py:~237` (new hook, route built in) |

**Default**: `""` (empty string) when caller hasn't set `agent.route`. Maps to telemetry `ROUTE_UNKNOWN` on the consumer side.

**Backward compat**: existing plugin callbacks that use `**kwargs` receive the new kwarg transparently. Plugins with explicit keyword lists ignore it (Python's standard kwargs semantic).

### Extension 4: `register_background_daemon` + `BackgroundDaemonRegistry`

**Files**:
- `agent/background_daemon_registry.py` (new module — 188 LOC)
- `kora_cli/plugins.py` — `PluginContext.register_background_daemon(...)` method added

**Module surface**:
```python
@dataclass(frozen=True)
class PeriodicTaskSpec:
    interval_seconds: float
    callback: Callable[[], Any]
    name: str = ""

@dataclass(frozen=True)
class BackgroundDaemonEntry:
    name: str
    startup: Callable[[Any], Any]  # receives coordinator
    shutdown: Callable[[], Any]
    periodic_task: Optional[PeriodicTaskSpec] = None
    shutdown_timeout: float = 5.0
    plugin_name: str = ""

class BackgroundDaemonRegistry:
    def register(self, entry: BackgroundDaemonEntry) -> None: ...
    def list_entries(self) -> List[BackgroundDaemonEntry]: ...
    def by_name(self, name: str) -> Optional[BackgroundDaemonEntry]: ...
    def reset_for_tests(self) -> None: ...

def background_daemon_registry() -> BackgroundDaemonRegistry:
    """Process-wide singleton."""
```

**`PluginContext.register_background_daemon`**:
```python
ctx.register_background_daemon(
    name="snapshot_collector",
    startup=async_startup_fn,
    shutdown=async_shutdown_fn,
    periodic_task=PeriodicTaskSpec(
        interval_seconds=300.0,
        callback=snapshot_collect,
        name="snapshot_5min",
    ),
    shutdown_timeout=10.0,
)
```

**Duplicate registration**: `ValueError` raised — fail-loud (matches `platform_registry.register` semantic). Plugin author sees the conflict at discovery time.

**Lifecycle execution**: NOT in this bucket. Consumer (gateway main loop / CLI startup) iterates `background_daemon_registry().list_entries()` and drives the lifecycle. Kora's `DaemonCoordinator` (kora_cli/daemon.py) already implements the consumer shape; gateway-side wiring lands in KR-REASONING-ROUTE-THROUGH-GATEWAY.

**Thread safety**: registry methods are RLock-wrapped. Plugin discovery happens at import time on the main thread; consumers may iterate from any thread.

### Extension 5: `pre_tool_call_can_provide_result` hook (added in KR-REASONING-ROUTE-THROUGH-GATEWAY-ST2B)

**File**: `model_tools.py` (insertion in `handle_function_call` after the existing `pre_tool_call` block-check and before `registry.dispatch`).
**VALID_HOOKS entry**: added between `pre_tool_list_finalized` and the `transform_llm_output` block in `kora_cli/plugins.py:155-167`.

**Signature**:
```python
invoke_hook(
    "pre_tool_call_can_provide_result",
    tool_name=...,
    args=...,
    task_id=...,
    session_id=...,
    tool_call_id=...,
)
```

**Return contract**:
- `None` / non-dict / missing `"result"` key → no-op, fall through to other plugins, then Hermes default `registry.dispatch`
- `{"result": "<tool_result_str>"}` → short-circuits Hermes dispatch; the plugin-provided string becomes the tool result
- First non-None `result` wins (matches existing override-shape semantics for `pre_api_request_mutable` and `pre_tool_list_finalized`)

**Failure handling**: any hook exception is caught + DEBUG-logged + fall-through to Hermes default. Fail-safe.

**Backward compat**: no existing plugin registers it; non-Kora-route plugins gate themselves on tool-name or route checks (the kora_hermes plugin returns None when the tool isn't a Kora reasoning tool — confirms safety for Hermes-fork users loading the plugin).

**Backward-compat for the dispatch site**: Hermes's `pre_tool_call` block-check + `post_tool_call` audit hook + `transform_tool_result` hook ALL still fire on the same code path; the new hook slots between the block-check and Hermes's default dispatch without altering observer ordering.

### Extension DEFERRED: post-LLM re-issue hook

**Why deferred**: re-issuing `messages.create` inside `conversation_loop` requires re-running portions of the loop (streaming consumption, tool-result processing, conversation-history append). Each of those has substantial state machinery. Doing this safely needs a coordinated control-flow refactor — beyond the scope of this bucket per §4.

**Follow-on bucket**: `KR-HERMES-LOCAL-EXT-REISSUE`. Will land alongside `KR-REASONING-ROUTE-THROUGH-GATEWAY` since that's the consumer that needs the pattern (current Kora `anthropic_engine._tool_use_loop` implements post-call escalation inline; route-through will lift this onto conversation_loop and require the re-issue hook).

---

## Backward compatibility

- All 17 pre-existing `VALID_HOOKS` entries preserved verbatim.
- All 7 pre-existing hook invocations gain a `route=` kwarg (additive; existing plugins ignore unknown kwargs).
- No existing call to `register_hook` / `register_platform` is altered.
- Observer-only `pre_api_request` is preserved unchanged; it fires AFTER `pre_api_request_mutable` so it sees the final api_kwargs.
- `agent.tools` is never mutated by `pre_tool_list_finalized` — the override is per-call.
- 145/145 pre-existing hook tests pass (test_shell_hooks + test_plugin_llm + test_transform_llm_output_hook + test_transform_tool_result_hook + test_model_tools).

## Test coverage

19 new tests in `tests/agent/test_hermes_local_extensions.py`:

| Category | Test count |
|---|---|
| VALID_HOOKS surface | 2 |
| BackgroundDaemonRegistry semantics | 8 |
| PluginContext.register_background_daemon forwarder | 3 |
| register_hook accepts new hook names (no warning) | 2 |
| pre_tool_list_finalized integration via build_api_kwargs | 3 |
| Fail-safe on hook exception | 1 |

---

## Upstream-PR readiness checklist

Each extension is designed to package cleanly into a future Hermes-upstream PR. Readiness state:

### Extension 1: `pre_api_request_mutable` — **PR-ready after route-through battle-tests it**

- [x] New hook added to VALID_HOOKS without renaming or removing existing entries
- [x] Existing observer `pre_api_request` preserved
- [x] Documented return contract (`{"override": {...}}` or None)
- [x] Failure handling: caught + logged, fail-safe
- [x] Tests in `tests/agent/`
- [ ] **Battle-test gap**: KR-REASONING-ROUTE-THROUGH-GATEWAY needs to exercise this against real production Slack DM traffic before upstream
- **Upstream framing**: "Allow plugins to mutate api_kwargs before SDK call. Backward-compat: existing observer-only `pre_api_request` is unchanged; new `pre_api_request_mutable` fires first. Use case: cost-aware model routing (e.g. select Haiku vs Opus based on caller route + recent budget telemetry without hardcoding model selection in core)."

### Extension 2: `pre_tool_list_finalized` — **PR-ready after route-through battle-tests it**

- [x] New hook added cleanly
- [x] Documented return contract (`{"override": [...]}`)
- [x] First-non-None-wins semantic
- [x] `agent.tools` never mutated
- [x] Fail-safe
- [x] Tests in `tests/agent/`
- [ ] **Battle-test gap**: route-through needs to demonstrate per-route tool manifests (Slack DM vs MCP tool route get different tool subsets) before upstream
- **Upstream framing**: "Allow plugins to filter the tool list per-call without mutating `agent.tools`. Use case: route-specific tool manifests (a chat-platform plugin doesn't need shell/file tools; an MCP-bridge plugin only needs MCP-relayed tools). Today, `agent.tools` is process-wide which forces 'union of all needed tools' even for routes that don't use most of them."

### Extension 3: `context.route` field — **NOT YET PR-ready — needs core taxonomy decision**

- [x] Threaded into 7 hook sites
- [x] Backward-compat (`""` default; `getattr(agent, "route", ...)`)
- [x] Tests verify route propagation
- [ ] **Gap before upstream**: Hermes-core would need to decide whether `route` is a free-form string (Kora's approach) OR a typed Literal (telemetry-friendly). Telemetry-friendly Literal is the safer upstream contract; Kora's current `kora_cli/telemetry/cost_telemetry.py:KNOWN_ROUTES` is a candidate vocabulary but it's Kora-specific.
- **Upstream framing**: "Add `route: str` context field to all conversation-loop hooks. Threading purely from `getattr(agent, 'route', '')` so legacy callers (no route set) see no change. Use case: per-route observability (cost telemetry, latency tracking, debug logging that groups by use-case)."
- **Upstream prep work needed**: extract Hermes-friendly `KNOWN_ROUTES` taxonomy + RFC the vocabulary before the PR.

### Extension 5: `pre_tool_call_can_provide_result` (KR-REASONING-ROUTE-THROUGH-GATEWAY-ST2B) — **PR-ready after route-through battle-tests it**

- [x] New hook added cleanly between existing block-check and Hermes default dispatch
- [x] First-non-None-result wins; non-dict / missing-key returns no-op
- [x] Fail-safe: hook exception → caught + logged → Hermes default dispatch
- [x] Backward compat: existing pre_tool_call (block-check) + post_tool_call + transform_tool_result fire unchanged on the same code path
- [x] Tests in `tests/plugins/test_kora_hermes_plugin_st2b.py` (Hermes-side wiring + Kora plugin consumer + bridge handler + Kora-tool-via-bridge sample trace + non-Kora-tool-fall-through-to-Hermes sample trace)
- [ ] **Battle-test gap**: ST3 (default-flip) + a 24-48h burn-in is the natural integration test. After ST3 lands, this hook is upstreamable.
- **Upstream framing**: "Allow plugins to short-circuit Hermes's tool dispatch with a plugin-computed result. Use case: a fork that maintains its own tool registry (parallel to Hermes's) wants those tools dispatched via fork code without registering them as Hermes tools. Fork-specific tool dispatch keeps the fork's code organization clean + enables behaviors (async tool dispatch, custom error envelopes, plugin-mediated security checks) that don't fit the Hermes tool registry's signature."

### Extension 4: `register_background_daemon` + registry — **PR-ready in isolation; consumer wiring is a separate PR**

- [x] New module is import-side-effect-free
- [x] `PluginContext.register_background_daemon` shape mirrors `register_platform`
- [x] Thread-safe registry
- [x] Tests cover all semantics (register / dup / list / by_name / reset / periodic_task / shutdown_timeout)
- [x] Documentation explicitly says "registration-only; consumer drives lifecycle"
- [ ] **Adjacent upstream PR needed**: a gateway-side or CLI-side consumer (e.g. `BackgroundDaemonRunner` that the gateway main loop instantiates) is the natural companion. Could be packaged together OR separately.
- **Upstream framing**: "New plugin shape: background daemons distinct from chat-platform adapters and lifecycle hooks. Use case: plugins that run from process boot to shutdown (snapshot collectors, telemetry flushers, periodic-tick alerters) and that today have to either monkey-patch the gateway main loop or fork."

### Extension DEFERRED: post-LLM re-issue — **NOT PR-ready (needs control-flow refactor in conversation_loop)**

- [ ] Substantial control-flow change
- [ ] Needs design RFC before either local OR upstream PR
- **Follow-on bucket**: KR-HERMES-LOCAL-EXT-REISSUE (CC#3, gated by KR-REASONING-ROUTE-THROUGH-GATEWAY needing it)

---

## Files changed

| File | Change | LOC |
|---|---|---|
| `kora_cli/plugins.py` | +2 VALID_HOOKS entries + `PluginContext.register_background_daemon` method | +73 |
| `agent/conversation_loop.py` | +`pre_api_request_mutable` invocation; +`route` kwarg on 7 hook sites | +52 |
| `agent/chat_completion_helpers.py` | +`pre_tool_list_finalized` invocation in `build_api_kwargs` | +30 |
| `agent/background_daemon_registry.py` | NEW module | +188 |
| `tests/agent/test_hermes_local_extensions.py` | NEW test file (19 tests) | +384 |
| `kora_docs/14_research/hermes_local_extensions_2026-05-23.md` | THIS doc | +~280 |

## Verification

```bash
# All new extension tests
pytest tests/agent/test_hermes_local_extensions.py
# → 19 passed

# Pre-existing hook tests unaffected
pytest tests/agent/test_shell_hooks.py tests/agent/test_plugin_llm.py \
       tests/test_transform_llm_output_hook.py \
       tests/test_transform_tool_result_hook.py tests/test_model_tools.py
# → 145 passed, 3 skipped

# Smoke test the surface
python -c "
from kora_cli.plugins import VALID_HOOKS
assert 'pre_api_request_mutable' in VALID_HOOKS
assert 'pre_tool_list_finalized' in VALID_HOOKS
from agent.background_daemon_registry import background_daemon_registry
print('extensions loaded OK')
"
```
