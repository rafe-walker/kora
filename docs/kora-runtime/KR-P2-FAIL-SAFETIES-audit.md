# KR-P2-FAIL-SAFETIES — Fail-CLOSED invariant audit

Audit method: walked 8 safety-critical paths post KR-P2-K + KR-P2-L.
For each path: located the function/guard, identified failure modes,
verified routing to DENY/RAISE vs. SILENT-ALLOW, flagged LEAKs.

**Locked rule** (`feedback_fail_closed_by_default_security_infra`):
Security infrastructure must default-deny. Applies to PSM, capability
gates, Constitution pre-screen, STOP-KORA, cost-ladder breaker,
op-state transitions, boot gates, DR epoch checks, claim
acquire/release.

## Verdict legend

  - **PASS** — fail-CLOSED confirmed across every audited failure mode.
  - **DEGRADED** — fail-CLOSED preserved but with reduced
    observability OR a corner case where a downstream invariant
    (e.g. state-holder coherence) might be temporarily violated.
  - **LEAK** — silent-allow path identified. Fix lands in a follow-on ST.

## Audit table

| # | Path | Failure mode audited | Current routing | Verdict | Action |
|---|---|---|---|---|---|
| 1a | Constitution pre-screen | `IsoKronMemoryProvider` is None | Explicit INCONCLUSIVE at `constitution_pre_screen.py:263-268` | PASS | none |
| 1b | Constitution pre-screen | `actor_has_capability` raises non-KeyError | n/a — current impl is a pure in-memory dict lookup against `ACTOR_CAPABILITY_MATRIX_KORA_COLUMN` (`capability_check.py:69-90`); the only exception path is `KeyError` (caught at `constitution_pre_screen.py:273-279` → INCONCLUSIVE). Bucket-spec concern ("network down, asyncpg pool dead") is forward-looking, not applicable to today's lookup | PASS | none — re-audit if/when a future bucket swaps to an I/O-backed matrix fetch |
| 1c | Constitution pre-screen | `tool_capability_map` lookup returns sentinel | `UNKNOWN_TOOL_SENTINEL` → INCONCLUSIVE at `constitution_pre_screen.py:255-260` | PASS | none |
| 1d | Constitution pre-screen | pre-screen itself raises (e.g. `_extract_constitution_state` raises) | `_extract_constitution_state` catches all exceptions internally (`constitution_pre_screen.py:173-176` returns `(None, None)`). Policy decision is already made by then; only audit fields would be missing | PASS | none |
| 1e | Constitution pre-screen | `_memory_manager` is None (`skip_memory=True` CLI mode) | Pre-screen wire-in at `tool_executor.py:220` skips when memory layer not loaded. Documented as operator opt-out, not silent-allow | PASS | none — operator opt-out is by design |
| 2a | STOP-KORA pre-flight | `KoraControlReader.get_active_command` raises | `stop_kora_pre_flight.py:114-130` catches all exceptions and returns `STOPKoraVerdict(action=None, ...)` — i.e. **tool call proceeds**. Log line: "treating as no STOP-KORA active. Tool call proceeds." | **LEAK** | ST2: change to fail-CLOSED — return a BLOCKING verdict with `reason="substrate read failed"` |
| 2b | STOP-KORA pre-flight | command observed but `transition_kora_control` SECDEF fails | `_advance_lifecycle_best_effort` catches + logs WARN; the BLOCKING verdict is preserved (`stop_kora_pre_flight.py:265-317`) — block stands, lifecycle stays in earlier state, cockpit-BFF SLA watcher escalates | PASS | none |
| 2c | STOP-KORA pre-flight | multiple commands race at same level | Substrate-side resolution: highest-level row wins via SQL `ORDER BY level DESC` in `SELECT_ACTIVE_KORA_CONTROL_COMMAND_SQL` (verified `kora_control_reader.py`). Race is resolved by the substrate SECDEF, not the runtime | PASS | none |
| 3a | Cost ladder | estimator misses an SDK call | Cost undercounted locally. Reconciliation against Anthropic console reading (operator-pulled) catches it: `cost_state_holder.py:reconcile_with_anthropic` bumps `spent_to_date_usd` FORWARD, sets `breaker_re_tripped=True`. Active rung re-evaluates on next check | PASS | none — observability fail-soft, correctness fail-CLOSED |
| 3b | Cost ladder | reconciliation reports MUCH higher than estimator | Above path runs; bump is forward-only (never reduces local spent). HARD_STOP_100 rung triggers immediately if bumped past pool | PASS | none |
| 3c | Cost ladder | rate-limit headers say 0 remaining but credit gauge says <75% | `record_rate_limit_pulse` captures the data; `active_rung()` only consumes `spent_to_date_usd / credit_pool_usd`. Rate-limit not factored into rung decision today — next API call would fail at the SDK boundary | DEGRADED | not in scope of this bucket — future enhancement: factor `rate_limit_pulse.remaining` into `active_rung()` so we preemptively defer instead of taking the API error |
| 4a | Op state machine | transition not in `TRANSITION_TABLE` | `is_valid_transition` check raises `InvalidStateTransitionError` (`operational_state_holder.py:162-171`) | PASS | none |
| 4b | Op state machine | emit listener raises during transition | Each listener is `try/except` individually (`operational_state_holder.py:200-215`); transition + state advance complete. Listeners are documented as observability not policy | PASS | none |
| 4c | Op state machine | concurrent transitions race | `asyncio.Lock` around state swap (`operational_state_holder.py:159`); listeners fire OUTSIDE the lock (deadlock prevention) | PASS | none |
| 5a | Boot gates | transient gate exhausts retry budget | Per `BootGateRunner._step` (`boot_gates.py:230-300`): retries up to `_retry_budget` with backoff; on exhaustion → INVARIANT-FAIL → coordinator routes to STOPPED | PASS | none |
| 5b | Boot gates | `kora_known_epoch` end-of-boot write fails | `dr_writer._write_known_epoch_inner` catches all exceptions, logs WARN, returns cleanly (`dr_writer.py:207-269`). Does NOT block READY transition. On next boot, gate 3b would see stale `kora_known_epoch` → false-positive PAUSE — but that's conservative (fail-CLOSED in spirit; reboot recovers) | PASS | none |
| 5c | Boot gates | `BootContext.kora_actor_uuid` is None on all-pass path | Gate 7 populates it; if gate 7 PASSED then `kora_actor_uuid` is set. The `write_known_epoch_at_boot_end` consumes the populated value (`boot_coordinator.py:263-266`); no assertion needed | PASS | none |
| 6a | DR / epoch consumer | `substrate_epoch` read fails | Gate 3 INVARIANT-fail → coordinator → STOPPED | PASS | none |
| 6b | DR / epoch consumer | `kora_known_epoch` read fails | Gate 3b INVARIANT-fail → coordinator → PAUSED (different from STOPPED — bucket flow is INVARIANT_PAUSE so the operator can clear via the post-PITR runbook) | PASS | none |
| 6c | DR / epoch consumer | epoch-change detected but `holder.transition_to(PAUSED)` raises | `boot_gates_dr.py:259-274` catches handler exception, returns FAIL → `boot_coordinator.py:280-285` returns `BootSummary(result=PAUSED, ...)` without itself transitioning the holder. Comment in coordinator explicitly says "the gate itself transitioned the holder to PAUSED" — but if the handler threw mid-transition, that's no longer true. Holder may still be BOOTING while BootSummary says PAUSED — coherence gap | DEGRADED | ST3: coordinator defensively calls `holder.transition_to(PAUSED, add_reasons={SUBSTRATE})` before returning PAUSED summary. Idempotent — `transition_to` handles "same primary state" without raising |
| 7a | Sea_Ticket consumer | ledger allocate succeeds but tool dispatch fails | Substrate sweeper marks the row `abandoned` on lease expiry per R4.1 §9.5 P3. Runtime doesn't need recovery — substrate is authoritative | PASS | none |
| 7b | Sea_Ticket consumer | heartbeat fails mid-claim | `claim_heartbeat._heartbeat_loop` sets `handle.lease_lost=True` on any failure (`claim_heartbeat.py:266-273`). Poller's `_claim_and_work` checks the flag, skips release, routes ACTIVE → READY (or PAUSED{COST} if cost rung crossed). Substrate lease expires; sweeper releases | PASS | none |
| 7c | Sea_Ticket consumer | `work_attempt_id` mint succeeds but subsequent writes fail | Orphaned attempt; substrate-team sweeper handles per R4.1 §9.5 P3 | PASS | none — substrate-team lane |
| 8a | Identity / display_name | malformed value via API endpoint | `kora_cli/web_server.py:_validate_display_name` enforces 64-byte UTF-8 ceiling + null/newline reject. PUT endpoint returns 400 on violation | PASS | none |
| 8b | Identity / display_name | malformed value via direct YAML edit | `gateway/config.py:339-355` loads `display_name` from data dict with NO length / charset enforcement. Hand-edited config.yaml bypasses validation. Not a SECURITY leak (display_name is not in the locked-rule security-infra list) but a hygiene gap — oversized strings could break platform APIs | DEGRADED | not in scope of this bucket; small follow-on advisable: apply `_validate_display_name` in the YAML loader's fallback path |

## Summary

| Verdict | Count | Paths |
|---|---|---|
| PASS | 18 rows | 1a-e, 2b-c, 3a-b, 4a-c, 5a-c, 6a-b, 7a-c, 8a |
| DEGRADED | 3 rows | 3c (rate-limit not factored), 6c (coordinator coherence), 8b (YAML loader bypass) |
| LEAK | 1 row | 2a (STOP-KORA fail-OPEN on substrate read failure) |

## Out-of-scope DEGRADED items

- **3c (rate-limit aware rung)**: future cost-ladder enhancement; the
  data is captured (`latest_rate_limit_pulse`), the rung policy
  doesn't consume it yet. Not a fail-CLOSED concern per locked rule —
  next API call fails at the SDK boundary, which is itself a
  fail-CLOSED signal. Leaving for a future cost-ladder bucket.
- **8b (display_name YAML loader)**: display_name is not in the
  locked-rule security-infra category (PSM, gates, Constitution,
  STOP-KORA, cost breaker, op-state, boot, DR epoch). It's an
  operator-controlled UX field — defense-in-depth recommended but
  not in this bucket's binding. Small follow-on advisable.

## STs opened from this audit

  - **ST2** (`feat/kora-KR-P2-FAIL-SAFETIES-st2-stop-kora-fail-closed`):
    Fix LEAK 2a — substrate read failure in STOP-KORA pre-flight must
    return a BLOCKING verdict, not silent-allow.
  - **ST3** (`feat/kora-KR-P2-FAIL-SAFETIES-st3-coordinator-defensive-paused`):
    Fix DEGRADED 6c — boot coordinator's INVARIANT_PAUSE path
    defensively transitions the holder before returning PAUSED summary.

## Audit method notes

  - Read each guard's source + verified the call chain.
  - For each documented failure mode in the bucket §1 list, traced
    the exception path through every `try/except` and matched it to
    DENY/RAISE vs SILENT-ALLOW.
  - "PASS" requires: every failure path either RAISES (and propagates
    to a known fail-CLOSED routing) or returns an explicit
    DENY/INCONCLUSIVE/BLOCKING verdict. No bare `except: pass` around
    security checks.
  - "DEGRADED" required: the security invariant holds, but some
    secondary property (observability, coherence with downstream
    state) may not.
  - "LEAK" required: a code path where a failure mode results in the
    runtime proceeding past a gate that was supposed to block it.

Pace stays real.
