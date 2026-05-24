# KR-FORK-HOOK-VERIFY — Hermes Gateway hook coverage research

**Date**: 2026-05-23
**Author**: CC#3 (Kora Runtime)
**Status**: STOP-ASK escalation to PM. All 5 plugin-extraction buckets recommend (c) wrapper-instead. Per §4 of the bucket spec, >2 (c) outcomes trigger this signal — Council may want to revisit the Phase C plugin-extraction roadmap.

---

## Executive summary

**The GPT-5.5 finding ("`pre_llm_call` works in CLI mode but NOT in Gateway mode") is structurally orthogonal to Kora's actual reasoning path.** Both Hermes CLI mode (`cli.py`) and Hermes Gateway mode (`gateway/run.py`) drive the **same** `agent/conversation_loop.run_conversation` function, which fires every plugin hook (`pre_llm_call`, `post_llm_call`, `pre_api_request`, `post_api_request`, `on_session_start`, `transform_llm_output`, etc.) on a single chokepoint. By code inspection, the hooks DO fire in Gateway mode.

**The structural problem is different and bigger**: Kora's reasoning path (Slack DM → email → cron-wake → probe-wake) is a **deliberate bypass** of the Hermes agent system. `kora_cli/reasoning/anthropic_engine.py` constructs `anthropic.AsyncAnthropic()` and calls `client.messages.create(...)` directly. It never touches `AIAgent`, `run_conversation`, or `conversation_loop`. Therefore **no Hermes plugin hook fires for any Kora reply**, regardless of which Hermes runtime mode is active.

This means **all 5 planned plugin-extraction buckets land at (c)** — Kora-side wrapper instead of Hermes plugin. The good news: most of those wrappers are **already implemented** in Kora's engine (audit emit, cost-ladder write, telemetry record, constitution check). The plugin extraction would just be code-relocation without architectural benefit.

The fork is structurally orthogonal to Hermes plugins for the production-primary path; the plugin system continues to serve the Hermes CLI / Gateway users perfectly well. No upstream PR is recommended — there's nothing to fix in Hermes.

---

## Phase 1 — Code-inspection table

### Hook invocation sites (per VALID_HOOK)

Surface: `kora_cli/plugins.py:128-168` defines `VALID_HOOKS` (18 hooks). Dispatcher: `kora_cli/plugins.py:1404 invoke_hook(hook_name, **kwargs)` → `get_plugin_manager().invoke_hook(...)`.

| Hook | Primary invocation site | Reaches CLI mode? | Reaches Gateway mode? | Reaches Kora's reasoning path? |
|---|---|---|---|---|
| `pre_tool_call` | `model_tools.py:780` (via `get_pre_tool_call_block_message`); `tools/terminal_tool.py:2087` | Yes | Yes (via shared `model_tools`) | **NO** |
| `post_tool_call` | `model_tools.py:852` | Yes | Yes | **NO** |
| `pre_llm_call` | `agent/conversation_loop.py:506` | Yes | **Yes** (gateway → AIAgent → `run_conversation` → `conversation_loop`) | **NO** |
| `post_llm_call` | `agent/conversation_loop.py:3996` | Yes | **Yes** | **NO** |
| `pre_api_request` | `agent/conversation_loop.py:1006` | Yes | **Yes** | **NO** |
| `post_api_request` | `agent/conversation_loop.py:2988` | Yes | **Yes** | **NO** |
| `on_session_start` | `agent/conversation_loop.py:163` | Yes | **Yes** | **NO** (Kora has no per-DM session concept) |
| `on_session_end` | `agent/conversation_loop.py:4111`, `cli.py:14176`, `plugins/disk-cleanup/__init__.py:311`, etc. | Yes | Yes (via `conversation_loop`) | **NO** |
| `on_session_finalize` | `cli.py:778, 6021, 6024`; `tui_gateway/server.py:309`; `gateway/run.py:3228, 4404, 9082` | Yes | Yes | **NO** |
| `on_session_reset` | `cli.py:6109`; `tui_gateway/server.py:586, 1982`; `gateway/run.py:9152` | Yes | Yes | **NO** |
| `transform_llm_output` | `agent/conversation_loop.py:3975` | Yes | **Yes** | **NO** |
| `transform_tool_result` | `model_tools.py:873` | Yes | Yes | **NO** |
| `transform_terminal_output` | `tools/terminal_tool.py:2088` | Yes | Yes | **NO** |
| `pre_gateway_dispatch` | `gateway/run.py:6482` (gateway-only by design) | No | **Yes** | **NO** |
| `pre_approval_request` | `tools/approval.py:1209, 1347` | Yes | Yes | **NO** |
| `post_approval_response` | `tools/approval.py:1291, 1359` | Yes | Yes | **NO** |
| `subagent_stop` | `tools/delegate_tool.py:2269` | Yes | Yes (if delegate fires) | **NO** |
| `transform_input` / `transform_output` | (none found via grep — defined in `VALID_HOOKS` but no production callsite) | n/a | n/a | n/a |

**Key finding**: the "Reaches Gateway mode?" column is **Yes** for every LLM-loop hook because gateway-mode drives `AIAgent.run_conversation` (`run_agent.py:3910 — from agent.conversation_loop import run_conversation`), which is the same function `cli.py` drives. The GPT-5.5 issue may have predated this unification or referred to a different path; current Hermes code has a single chokepoint.

### Kora's reasoning path — independently verified

Grep results that establish the bypass:

```bash
# Kora's reasoning + handlers + listeners → no use of conversation_loop:
$ grep -rnE "conversation_loop|from agent.conversation_loop" \
    kora_cli/handlers/ kora_cli/reasoning/ kora_cli/listeners/
(zero matches)

# Kora's reasoning engine uses Anthropic SDK directly:
$ grep -n "messages.create\|AsyncAnthropic" kora_cli/reasoning/anthropic_engine.py
510: response = await client.messages.create(**kwargs)
587: response = await client.messages.create(  # post-call escalation
1070: from anthropic import AsyncAnthropic

# Kora has no Hermes plugin / hook system integration anywhere:
$ grep -rnE "from kora_cli.plugins|invoke_hook" \
    kora_cli/handlers/ kora_cli/reasoning/ kora_cli/listeners/ \
    kora_cli/snapshot/ kora_cli/router/
(zero matches)
```

The Hermes plugin system is import-reachable from `kora_cli/plugins.py` (Hermes code that Kora's fork inherits), but **no Kora production code imports it**. The plugin extraction would have nowhere to plug in.

### Listener-registration extensibility — Q5-3 daemon-to-gateway refactor

`PluginContext` (`kora_cli/plugins.py:287`) exposes register surfaces:
- `register_tool` (AI-callable tool)
- `register_cli_command` / `register_command` (CLI subcommand)
- `register_context_engine` (context provider)
- `register_image_gen_provider` / `register_video_gen_provider` / `register_web_search_provider` / `register_browser_provider` (provider plugins)
- `register_platform` (chat platform like Telegram / Discord — but NOT Slack-DM-as-Kora-uses-it; Hermes platform = bidirectional CLI-like agent session, not webhook → reply)
- `register_hook` (lifecycle hook callback)
- `register_skill` (skill plugin)

None of these surfaces match Kora's listener shape, which is a **background daemon** with `startup(coordinator)` / `shutdown()` callbacks driven by `DaemonCoordinator`. Hermes' plugin context has no `register_background_daemon` or `register_periodic_task` surface. The closest match is `register_platform`, which is designed for interactive agent sessions, not for webhook-driven async reply.

---

## Phase 2 — Probe script — NOT NEEDED

Code inspection is unambiguous on every hook. The dispatch path is centralized (`invoke_hook` → `PluginManager.invoke_hook`); each hook has a discoverable static call site; Kora's reasoning engine has zero references to either. No runtime probe is required to confirm the bypass.

---

## Phase 3 — Per-hook recommendations

For each of the 5 plugin-extraction buckets gated by this research:

### Bucket 1 — Constitution → `pre_tool_call`

- **Hook fires in Hermes?** Yes (both CLI + Gateway).
- **Hook fires for Kora?** **No** — Kora dispatches reasoning tools via `kora_cli/reasoning/tool_registry.execute_reasoning_tool` directly from `kora_cli/reasoning/anthropic_engine._execute_single_tool_block` (post KR-FEAT-AGENTIC-REASONING-PARALLEL). Hermes `model_tools.py:780` chokepoint is not reached.
- **Recommendation: (c) wrapper-instead.**
- **Wrapper shape**: constitution check as a synchronous function called inside `_execute_single_tool_block` BEFORE `execute_reasoning_tool(...)`. Returns `tool_result` with `is_error=true` on block. Already partially present via `ReasoningToolNotAllowed` (the reasoning allowlist) — constitution add-on is just another check in the same code site.

### Bucket 2 — Audit → `post_tool_call` + `post_llm_call`

- **Hook fires in Hermes?** Yes.
- **Hook fires for Kora?** **No** — same bypass.
- **Recommendation: (c) wrapper-instead. ALREADY IMPLEMENTED.**
- **Wrapper shape**: `_emit_audit` (KR-AUDIT-JSONL-SINK, PR #141) and `_record_call_to_telemetry` (KR-HAIKU-ROUTER, PR #165) are the wrappers. They fire per-tool-call and per-API-call respectively. No further extraction work needed; plugin form would be code-relocation without behavior change.

### Bucket 3 — Cost ladder → `pre_llm_call`

- **Hook fires in Hermes?** Yes.
- **Hook fires for Kora?** **No** — same bypass.
- **Recommendation: (c) wrapper-instead. ALREADY IMPLEMENTED.**
- **Wrapper shape**: cost-ladder accounting happens at two sites: `handler._record_inference_to_cost_ladder` (KR-CHEAP-PROMPT-CACHING, PR #158) accumulates tokens against `CostStateHolder`; `engine._record_call_to_telemetry` (#165) records per-call counters. Router-driven model selection (KR-HAIKU-ROUTER, #165) implements the Layer 1 routing the cost-ladder hook would have driven; existing `RUNG_MODEL_MAP` clamp is the Layer 2 backstop. No plugin needed.

### Bucket 4 — State holders → `on_session_start`

- **Hook fires in Hermes?** Yes.
- **Hook fires for Kora?** **No** — Kora has no per-DM "session" concept. State holders (`OperationalStateHolder`, `CostStateHolder`, `DaemonCoordinator`) are process-wide singletons initialized at daemon boot via listener `startup(coordinator)` callbacks (`kora_cli/listeners/__init__.py`).
- **Recommendation: (c) wrapper-instead. ALREADY IMPLEMENTED.**
- **Wrapper shape**: each listener's `startup` callback is the equivalent of `on_session_start` — runs once per daemon boot, initializes holders, registers periodic tasks. Plugin form would be incompatible (plugins fire per-session, not per-process; semantics differ).

### Bucket 5 — Daemon-to-gateway listener registration (Q5-3)

- **Hook fires in Hermes?** N/A — this is a registration surface question, not a runtime hook.
- **Hook fires for Kora?** N/A — Kora uses `DaemonCoordinator` for listener registration.
- **Recommendation: (c) wrapper-instead. KEEP DaemonCoordinator.**
- **Rationale**: Hermes plugin context has no `register_background_daemon` / `register_periodic_task` surface. `register_platform` is the closest match but is shaped for interactive agent sessions, not Kora's webhook → 1-shot reply pattern. Adding the missing surfaces to Hermes core would be a substantial upstream-PR (new register_* methods on PluginContext, a new lifecycle model for non-session-bound plugins), and Hermes core users haven't asked for it. `DaemonCoordinator` already implements the right shape — keep it as Kora's listener registration mechanism. The fork's `kora_cli/listeners/` tree is the right home.

---

## Phase 4 — Architecture impact summary

| Plugin extraction bucket | Status post-research | Recommendation | Wrapper status |
|---|---|---|---|
| Constitution → `pre_tool_call` | (c) | wrapper-instead | New work in `_execute_single_tool_block` |
| Audit → `post_tool_call` + `post_llm_call` | (c) | wrapper-instead | **Already implemented** (#141, #165) |
| Cost ladder → `pre_llm_call` | (c) | wrapper-instead | **Already implemented** (#158, #161, #165) |
| State holders → `on_session_start` | (c) | wrapper-instead | **Already implemented** (`DaemonCoordinator` + listener `startup` callbacks) |
| Daemon-to-gateway listener registration (Q5-3) | (c) | keep `DaemonCoordinator` | **Already implemented** |

**Aggregate**: 5/5 buckets land at (c). Per bucket spec §4: "More than 2 of the 5 plugin extraction buckets need wrapper-instead → STOP-ASK PM (this is a significant architectural revision worth signaling early; might motivate re-running council)."

**STOP-ASK signal to PM**: the Phase C plugin-extraction track as designed doesn't fit Kora's runtime architecture. The wrappers already exist or are trivial follow-ons in Kora's own code; relocating them into Hermes plugin form would be ceremony without benefit. Recommend Council revisits the Phase C goals — what was the plugin extraction trying to achieve that the wrappers don't already?

Plausible re-framing for Council: **the "plugin extraction" goal was likely conflated with "make Kora's runtime behaviors observable and testable"** — both of which the existing wrappers already provide (audit JSONL, per-route telemetry, cost-ladder counters, structured-log hook points). If the underlying goal was observability + testability, it's already met. If the goal was something else (e.g., letting third-party Hermes users adopt Kora's behaviors as plugins), that's a different conversation entirely and would require a different architecture (Kora-as-library vs Kora-as-fork).

### Upstream-PR opportunity (informational, not blocking)

The single concrete upstream-PR that would *expand the Hermes plugin surface* in a way that could in principle let Kora's behaviors become plugins: add `register_background_daemon(name, startup, shutdown, periodic=None)` to `PluginContext`. This would let listener-shaped behaviors (snapshot, telemetry, alert notifier, etc.) load as plugins in any Hermes-fork. Sketch:

```python
# kora_cli/plugins.py (PluginContext)
def register_background_daemon(
    self,
    name: str,
    startup: Callable[[Any], Awaitable[None]],
    shutdown: Callable[[], Awaitable[None]],
    *,
    periodic_task: Optional[PeriodicTaskSpec] = None,
) -> None:
    """Register a background-daemon-shape behavior. Lifecycle is
    daemon-bound, not session-bound — runs from process boot to
    process shutdown. Optional periodic_task spec for periodic
    callbacks (interval_seconds + callback)."""
    # Implementation: forward to a new DaemonRegistry that the
    # gateway's main loop drives at startup.
```

**But this would be a meaningful re-architecture of the Hermes plugin model** (a new non-session-bound lifecycle category). It's not a quick PR; landing it in Hermes upstream would require their PM buy-in and probably a design discussion. Recommend NOT pursuing this unless Council decides Kora-as-library is a strategic goal.

---

## Recommended next steps for PM

1. **Pause Phase C plugin-extraction roadmap items** that target hooks Kora bypasses. The 4 hook-bound buckets (Constitution / Audit / Cost ladder / State holders) should not be greenlit until Council reconciles the goal.
2. **Greenlight the wrapper-instead form** for any of those buckets where the goal is observable, testable behavior in Kora's own code. Audit + cost-ladder + state-holder wrappers are already shipped; Constitution wrapper is a small follow-on bucket (KR-CONSTITUTION-WRAPPER inside `_execute_single_tool_block`).
3. **Keep `DaemonCoordinator`** as Kora's listener registration mechanism. Cancel Q5-3 daemon-to-gateway refactor; it solves a non-problem given the existing architecture.
4. **Re-run Council on Phase C goals** if "plugin extraction" was meant to enable Kora-as-library or third-party adoption (different architecture, different bucket shape).

---

## Appendix A — Verification commands

```bash
# Hermes hook surface (18 hooks)
sed -n '128,168p' kora_cli/plugins.py

# Hook invocation chokepoints (excludes tests + plugin scaffolding)
grep -rnE "[\"'](pre_tool_call|post_tool_call|pre_llm_call|post_llm_call|pre_api_request|post_api_request|on_session_start|on_session_end|on_session_finalize|on_session_reset|transform_llm_output|transform_tool_result|transform_terminal_output|pre_gateway_dispatch|pre_approval_request|post_approval_response|subagent_stop)[\"']" --include="*.py" \
  | grep -v "tests/" | grep -v "plugins.py:" | grep -v "hooks.py:"

# Kora's reasoning path bypasses the loop
grep -rnE "conversation_loop|from agent.conversation_loop" \
  kora_cli/handlers/ kora_cli/reasoning/ kora_cli/listeners/
# (zero matches expected)

# Kora's engine calls SDK directly
grep -n "messages.create\|AsyncAnthropic" kora_cli/reasoning/anthropic_engine.py

# Both CLI + Gateway drive the same conversation_loop
grep -nE "from agent.conversation_loop|run_conversation\(" \
  cli.py gateway/run.py run_agent.py | head -10
```

## Appendix B — Why the GPT-5.5 "doesn't fire in Gateway" finding was (probably) outdated

The Hermes Gateway has evolved through several iterations. Earlier prototypes may have had a Gateway-specific LLM driver that bypassed `conversation_loop`; the current code at `run_agent.py:3910` makes `AIAgent.run_conversation` a thin forwarder to `agent.conversation_loop.run_conversation`. Both CLI invocation (`cli.py` → `AIAgent.chat()` → `run_conversation()`) and Gateway invocation (`gateway/run.py:_run_agent` → `AIAgent.run_conversation()`) flow through the same function — the hooks fire uniformly.

If a specific user-facing Gateway flow does bypass conversation_loop (e.g., a streaming-only endpoint with its own SDK driver), grep didn't find it in the current tree. If such a path exists and is the one GPT-5.5 was citing, it'd be worth running a probe plugin on it; but for the Phase C plugin-extraction question this doesn't change the recommendation — Kora's reasoning engine is its own thing and doesn't intersect either path.
