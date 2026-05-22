# retry_ceiling design — PM-followup from KR-P2-READINESS-AUDIT (#96)

## Background

R4.1 §9.8 spec text (line 106):

> A **retry-count backstop**: a `work_attempt` retrying beyond a hard ceiling without an epoch change raises a `retry_ceiling` `degradation_reason` for operator review.

R4.1 §9.1 transition table is more prescriptive:

> | any → DEGRADED-flagged | dispatch/substrate/auth check failing, or **retry-ceiling hit (§9.8)** | `primary_state` unchanged; **`claim_permission` → `critical_only` or `none`** | self when check recovers / **operator for retry_ceiling** |

So the spec mandates two things: (a) the `retry_ceiling` flag enters `degradation_reasons`, and (b) `claim_permission` narrows to `critical_only` or `none`. Operator clears via `kora_control` reset (not self-recovery).

`RETRY_CEILING` already exists in the enum (`agent/operational_state.py:93`). The audit (#96) confirmed there's no production raise-site.

### NOT to be confused with the §9.4 "failed_terminal threshold 3"

These are two distinct mechanisms:

| Mechanism | Scope | Threshold default | Trigger | Effect |
|---|---|---|---|---|
| **§9.4 `failed_terminal`** | Per-ticket | 3 logical-class failures | Sea_Ticket classifier maps result | Ticket goes to `failed_terminal` → `operator_triage`. Kora moves on. |
| **§9.8 `retry_ceiling`** | Per-attempt (likely; see Decision 3) | TBD | Re-claiming a ticket past hard ceiling WITHOUT epoch change | Kora's `degradation_reasons` += `retry_ceiling`; `claim_permission` narrows; operator review. |

`failed_terminal` is "this specific ticket is poisoned, kick it." `retry_ceiling` is "Kora is in a stuck loop on some ticket, something systemic might be broken." Different triggers, different scopes, different remediation owners (operator triage vs operator review).

### Why the "without an epoch change" qualifier matters

If a substrate PITR rewind happens, `substrate_epoch` advances. The work_attempts that lived under the prior timeline are abandoned (per §9.8). Re-claiming a ticket after a rewind is *expected* — the prior attempts didn't count toward poison-budget because they exist in a discarded timeline. The retry ceiling needs to be "retries on this ticket *since the most recent epoch reset*" — not lifetime retries.

This is what distinguishes `retry_ceiling` from a simple "max retries ever" counter.

---

## The 4 open decisions

### Decision 1 — Ceiling value

What count of work_attempts-per-ticket (without epoch change) trips `retry_ceiling`?

| Option | Value | Pros | Cons |
|---|---|---|---|
| **1A — Hard constant** | `RETRY_CEILING_HARD = 10` baked into runtime | Simplest. One number to reason about. Auditable. | No knob for operators to tune per-deployment. |
| **1B — Env-configurable constant** | `os.environ.get("KORA_RETRY_CEILING", "10")` | Operator-tunable without redeploy. Still global. | One more env var. Drift risk between staging + prod. |
| **1C — Per-criticality** | frontier=20, normal=10, low_priority=5 | Reflects business reality — frontier tickets *should* tolerate more retries. | New dimension on a fail-safe. Harder to reason about (which ticket triggered + why). Requires substrate criticality field (already exists for cost downshift). |

**Recommend**: **1B (env-configurable, default 10)**. The hard ceiling is a safety backstop, not a tuning knob — most deployments will never touch it. But operators tuning Kora for unusual workloads should have a knob without code change. The cost-ladder pattern already uses env overrides (`KORA_DEFAULT_DAILY_OVERRIDE_CAP`), so the precedent is in place.

### Decision 2 — Behavior on raise

The spec text says "degradation_reason for operator review" — but the transition table is stricter ("claim_permission → critical_only or none"). Read together: raise the flag AND narrow claim_permission.

| Option | State machine impact | Pros | Cons |
|---|---|---|---|
| **2A — degradation_reason only** | `degradation_reasons` += `retry_ceiling`; `claim_permission` unchanged | Minimal disruption. Honest signal. | Kora keeps claiming + retrying — defeats the safety. |
| **2B — degradation_reason + `claim_permission=critical_only`** | Flag raised + only criticality=critical tickets claimable | Critical work still flows; non-critical work pauses for triage. Matches transition table exactly. | Subtle for operators ("why is this critical ticket still claiming?") — needs cockpit panel surface. |
| **2C — degradation_reason + `claim_permission=none`** | Flag raised + no claims at all | Strongest stop. Forces operator triage. | Halts critical work too — may be too aggressive for a single ticket being stuck. |
| **2D — degradation_reason + transition to PAUSED** | Full PAUSED+{retry_ceiling}; operator clears via `kora_control` reset | Strongest signal; operator-only recovery (matches spec table). | Same blast radius as 2C; AND breaks the "primary_state unchanged" half of the transition table. |

**Recommend**: **2B (degradation_reason + claim_permission=critical_only)**. This is exactly what the transition table prescribes. Operator clearance via `kora_control` reset returns claim_permission to `normal` + removes the degradation reason.

### Decision 3 — Counter granularity

What counter is checked against the ceiling?

| Option | Counter | Where stored | Pros | Cons |
|---|---|---|---|---|
| **3A — Per-attempt count** | Number of retries within a SINGLE work_attempt | Process memory | Cheap to track. | A `work_attempt_id` is single-use per P4 — its "retry" is the ledger row's status field (allocated→dispatched→committed), not a re-mint. Not the right granularity. |
| **3B — Per-ticket lifetime `work_attempt_count`** | Substrate's `tickets.work_attempt_count` (already exists, surfaced in `assigned_sea_tickets.py:50`) | Substrate, durable | Survives Kora restarts. Already there. No new schema. | Doesn't account for epoch changes — needs filtering. |
| **3C — Per-ticket work_attempts since last epoch reset** | `count(work_attempts) where ticket_id=X and observed_substrate_epoch = current_epoch` | Substrate (requires new `observed_substrate_epoch` column on `work_attempts`) | Spec-correct. Handles PITR cleanly. | New substrate column — IsoKron-team work + migration. |

**Recommend**: **3B for v1, with a "no epoch change since boot" guard at runtime**. The runtime can read `substrate_epoch` once at boot and again at retry-check time — if the epoch advanced, skip the ceiling check for tickets whose `work_attempt_count` was elevated under the prior epoch. This isn't perfectly spec-correct (a tick can race a PITR mid-attempt), but it covers the 95% case using existing schema. Filed as a follow-on for **3C** when IsoKron-team is doing the next migration pass.

### Decision 4 — Raise-site location

Where does the check + raise live in the code path?

| Option | Touchpoint | Pros | Cons |
|---|---|---|---|
| **4A — Pre-claim filter** in `get_next_available_sea_ticket` G-8 | Substrate-side SQL filter excluding tickets with `work_attempt_count >= ceiling AND substrate_epoch_unchanged` | Centralized. The poller never sees stuck tickets at all. | Substrate-side change; tightly couples runtime ceiling to substrate query. Test surface duplicates runtime. |
| **4B — Post-claim check** in `sea_ticket_poller.py` after `kora__claim_sea_ticket` returns | Read `claim_count` / `work_attempt_count` from the claim response; if past ceiling, raise + release immediately | All in runtime. Substrate stays simple. The claim/release is wasteful but cheap (one round-trip). | Wastes one claim cycle per stuck ticket; could repeatedly claim+release if other tickets are unavailable. |
| **4C — Resolution-time check** in `sea_ticket_resolution.py` after each work_attempt completes | After classification, if resolution is FAILED_* AND ticket's `work_attempt_count` past ceiling → raise the degradation flag | Aligns with where threshold-3 logic would naturally live. | Reactive, not preventive — Kora does the attempt before flagging. But the spec text says "retrying beyond a hard ceiling" — past-ceiling is the trigger, so reactive is acceptable. |
| **4D — Boot-time + periodic scan** | Background task scans `tickets` table for any open ticket past ceiling | Defense-in-depth. | Async; not tied to a specific claim cycle. Hard to attribute the flag to a specific ticket from the holder's perspective. |

**Recommend**: **4C (resolution-time check)**, with a **4B-style guard at claim time as a cheap belt-and-suspenders**. Reasoning:

- 4C is where the spec's "retrying beyond a hard ceiling" most naturally lives: after each work_attempt, count the attempts; if past ceiling and the result is still FAILED_*, raise.
- 4B at claim time is a one-line check on the claim response — cheap insurance that prevents Kora from even starting attempt N+1 if the prior N already crossed the line.
- 4A is tempting but couples substrate to runtime ceiling — if the env override flips, substrate needs to re-read. Avoid.
- 4D is too async to attribute cleanly.

---

## Cross-team interactions

| Decision | Lane | Coord need |
|---|---|---|
| 1B env override (`KORA_RETRY_CEILING`) | Kora-runtime + ops | Doppler `kora-runtime-substrate` env var. No substrate change. |
| 2B `claim_permission=critical_only` transition | Kora-runtime | None — uses existing operational state transitions. Needs cockpit panel polish to surface the "why" (HEALTH-PANEL extension). |
| 3B `work_attempt_count` re-use | Kora-runtime | None — column already in `assigned_sea_tickets.py:50`. |
| 3C `observed_substrate_epoch` per-attempt column (future) | **IsoKron-team** | New migration on `work_attempts` table. Defer to v1.1. |
| 4B + 4C raise-site | Kora-runtime | None. |
| Operator clearance via `kora_control` reset clears `retry_ceiling` | Kora-runtime + substrate | Existing `kora_control` reset path (level=0) already clears all open degradation reasons on PAUSED clear — but for non-PAUSED degradation (claim_permission=critical_only without PAUSED), needs to verify the reset handler also clears flag. Likely needs ST work in `agent/operational_state_holder.py`'s reset listener. |

---

## Recommended path — summary

Combining the recommendations:

1. **Ceiling**: `KORA_RETRY_CEILING` env var, default `10`.
2. **Behavior**: raise `RETRY_CEILING` degradation_reason + transition `claim_permission` to `critical_only`. No `primary_state` change.
3. **Counter**: substrate's existing `tickets.work_attempt_count`, with a "current substrate_epoch matches" guard (runtime-side read).
4. **Raise-site**: in `sea_ticket_resolution.py` after classification, with a belt-and-suspenders pre-claim check in `sea_ticket_poller.py` consuming the claim response's `work_attempt_count`.
5. **Clearance**: operator issues `kora_control` level=0 reset; the holder's reset listener removes `retry_ceiling` from `degradation_reasons` + restores `claim_permission=normal`.

## Implementation sketch

Three small code touchpoints:

1. **`agent/retry_ceiling.py`** (new module, ~80 lines) — pure helper.
   - `RETRY_CEILING_DEFAULT = 10`
   - `get_ceiling() -> int` — env var read + default
   - `check_retry_ceiling(work_attempt_count: int, current_epoch: int, last_known_epoch: int) -> bool` — returns True if ceiling tripped AND no epoch change since last check.

2. **`plugins/memory/isokron/sea_ticket_resolution.py`** — extend `classify_failure` or wrap its return: after resolution, if FAILED_* AND `check_retry_ceiling(ticket.work_attempt_count, current_epoch, last_known_epoch)` → call holder API to raise `retry_ceiling` degradation.

3. **`plugins/memory/isokron/sea_ticket_poller.py`** — claim-cycle insert: after `kora__claim_sea_ticket` returns, if `payload.work_attempt_count >= get_ceiling() AND epoch unchanged`, immediately release the claim + raise degradation. (This is the belt-and-suspenders 4B half.)

4. **`agent/operational_state_holder.py`** — extend the kora_control reset listener to clear `retry_ceiling` from `degradation_reasons` on reset (verify existing behavior — likely already does, but worth a test).

5. **Tests** — `tests/test_retry_ceiling.py` covering: trip on count past ceiling; no trip when epoch advanced; clear on reset; env override.

Total: ~150-200 lines of new code + ~150 lines of tests. Single ST PR. Fits one bucket.

---

## Open questions for PM

1. **Ceiling default value** — I picked `10` as a round number. The R4.1 §9.4 threshold-3 (for `failed_terminal`) is a separate mechanism, so they don't need to be the same. Is `10` the right default, or do you have a stronger intuition (e.g., `5` for tighter safety, `20` for tolerant)?
2. **Per-criticality ceilings (option 1C)** — I deferred this. If you have a strong product reason (frontier tickets *must* retry more), say so and I'll re-cost the implementation.
3. **Spec ambiguity on "without an epoch change" semantics** — my read is "since last known kora_known_epoch" (runtime-tracked). An alternative read is "since this work_attempt's observed_epoch" (substrate-tracked, requires new column). Which read should anchor v1? My v1 recommendation (3B) is approximate; perfectly correct (3C) needs IsoKron-team.
4. **claim_permission narrowing — is `critical_only` right, or `none`?** The transition table allows either ("critical_only or none"). 2B keeps critical work flowing; 2C halts everything. My recommendation is 2B but you may want stricter for v1.
5. **Operator clearance scope** — does a `kora_control` level=0 reset clear `retry_ceiling` only, or all open degradation_reasons? The spec table says "operator for retry_ceiling" — implying operator-only — but level=0 reset is the existing mechanism. Confirm clearance pathway matches existing reset semantics, or do we need a more targeted reset_degradation kind?
6. **Should `retry_ceiling` emit a chain event** (e.g., `kora.retry_ceiling.observed`) when raised? The DR equivalent (`kora.dr.observed`) does, for audit trail. Recommend yes, but it's a new event vocab literal that needs the substrate-side `0159_*.sql` vocabulary migration.

---

## References

* R4.1 §9.1 + §9.4 + §9.8 — spec definitions
* KR-P2-READINESS-AUDIT PR #96 — `kora_docs/15_status_and_roadmap/2026-05-22_R4.1_section_12_readiness_audit.md` row "§9.8 retry-ceiling"
* `agent/operational_state.py:93` — `RETRY_CEILING` enum value
* `plugins/memory/isokron/sea_ticket_resolution.py` — existing classifier (no threshold-3 logic yet either)
* `plugins/memory/isokron/sea_ticket_poller.py:164` — `claim_count` exposure
* `plugins/memory/isokron/assigned_sea_tickets.py:50` — substrate `work_attempt_count` column

