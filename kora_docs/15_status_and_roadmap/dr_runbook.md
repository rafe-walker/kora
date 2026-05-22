# DR runbook — post-PITR `substrate_epoch` bump (R4.1 §9.8)

Closes R4.1 §12 readiness item: "DR runbook mandates the post-restore
`substrate_epoch` bump as a non-negotiable step, with explicit post-restore
verification that gate 3b fired on Kora's next boot."

## When this runbook applies

Substrate has been rewound (PITR / restore from snapshot / explicit DB
reset). Kora's persisted `kora_known_epoch` is now ahead of (or out of
sync with) `substrate_epoch` and her gate 3b boot check will trip until
the bump procedure below runs.

## Symptoms

* `/api/operational-state` returns `primary_state: paused` with
  `degradation_reasons: ["substrate", ...]` and `claim_permission: none`.
* `/api/dr-state` returns `match_status: mismatch_detected` (or
  `pending_runbook`) and `runbook_pending: true`.
* `/api/boot-status` shows the most-recent boot's gate 3b
  (`SubstrateContractVersionGate`-paired epoch check) as `fail` with a
  `kora.dr.observed` event in the chain log (`payload.observed_substrate_epoch`
  + `payload.kora_known_epoch` show the mismatched pair).
* If wired, cockpit alerts fire on `kora.dr.observed`.

## Pre-procedure checks (~30s)

1. Confirm the PITR rewind actually happened (substrate-team / cockpit
   operator).
2. Confirm you (running this runbook) are an operator-class actor —
   `bump_substrate_epoch` rejects any caller without
   `actor_kind='operator'` in `actor_registry`.
3. Confirm no other Kora instances are running against the same
   workspace (Fly: `flyctl status -a kora-runtime`; expect one machine).

## The bump procedure

`bump_substrate_epoch` is a SECDEF in `public` schema, granted to
`kronicle_app_role` + `app_admin`. Each call advances `substrate_epoch`
by exactly **+1**. If the rewind moved `substrate_epoch` from `N` back to
`M` and Kora's `kora_known_epoch` is at `N`, you must call the bump
`(N - M + 1)` times to land at `N+1` (strictly greater than the highest
value Kora has ever observed).

Read the current values first:

```sql
SELECT
  public.substrate_epoch()  AS substrate_epoch,
  public.kora_known_epoch() AS kora_known_epoch;
```

Then bump in a loop until `substrate_epoch > kora_known_epoch`:

```sql
-- Replace <OPERATOR_ACTOR_UUID> with your actor_registry.actor_id.
SELECT public.bump_substrate_epoch('<OPERATOR_ACTOR_UUID>'::uuid);
-- Repeat as many times as needed.
```

Verify the new value:

```sql
SELECT public.substrate_epoch();  -- expect > previous kora_known_epoch
```

## Clear Kora's PAUSED state

Issue a `kora_control` reset (level=0) to supersede the open
PAUSED{substrate} command. Reset is operator-only — `issue_kora_control`
rejects `actor_kind='kora'` callers and rejects `level=0` from any
non-operator actor (`0090_kora_control_secdefs.sql`).

```sql
SELECT *
  FROM public.issue_kora_control(
    p_workspace_id      => '<WORKSPACE_ID>',
    p_issuer_session_id => '<COCKPIT_SESSION_ID>',
    p_issuer_actor_id   => '<OPERATOR_ACTOR_UUID>'::uuid,
    p_level             => 0,
    p_kind              => 'reset',
    p_reason            => 'post-PITR substrate_epoch bumped; clearing PAUSED{substrate}'
  );
```

The reset is observed + acknowledged + enforced by Kora's
`KoraControlReader` on her next pre-claim or per-tool check (KR-P2-J).
Verify via:

```
GET /api/kora-control/observed-state
```

Expected: the new reset command appears under `recently_enforced` (or
`active` if Kora hasn't yet picked it up — typically <60s).

## Verify gate 3b fired on next boot

If Kora doesn't auto-restart on PAUSED-clearance, restart her:

```sh
flyctl restart -a kora-runtime
```

On the next boot, `make_paused_substrate_cleared_listener` fires when
the holder transitions PAUSED+{substrate} → READY: it reads the now-
advanced `substrate_epoch` and writes it to `kora_known_epoch` via the
Kora-only `kora_write_known_epoch` SECDEF (`dr_writer.py`).

Verify in this order:

1. `GET /api/dr-state` → `match_status: clean`, `runbook_pending: false`.
2. `GET /api/boot-status` → most-recent boot's `outcome: ready`, every
   gate including gate 3b shows `outcome: pass`.
3. `GET /api/operational-state` → `primary_state: ready`,
   `claim_permission: normal`, `degradation_reasons: []`.
4. Chain log: `kora.dr.observed` fired on the failed boot (audit trail);
   subsequent boot fires no further `kora.dr.observed`.

## Failure modes

**The bump didn't advance `substrate_epoch` past `kora_known_epoch`.**
You under-counted the bump calls. Re-read both values via the read
SECDEFs and bump the remaining delta.

**Kora boots but immediately re-pauses with `degradation_reason=substrate`.**
The PAUSED-clearance listener's write failed — check Kora's logs for
`[kora.dr_epoch] PAUSED-clear write skipped` (kora actor_id unresolved,
or `_connection` missing). The end-of-boot writer
(`write_known_epoch_at_boot_end`) also writes on successful boot; if
that's failing too, the cause is shared — typically substrate auth or
network. Resolve substrate connectivity, restart, re-verify.

**`issue_kora_control` raised `level=0 reset is operator-only`.**
The `issuer_actor_id` you passed is not `actor_kind='operator'`. Use a
verified operator UUID from `actor_registry` (NOT a Kora actor).

**Cockpit reset didn't take.**
A higher-level kora_control command is still open. `/api/kora-control/observed-state`
lists open commands under `active`. Per R4.1 §9.3 "highest open level
wins" — only a level=0 reset supersedes. If a higher-level command is
present from a different session, supersede it with another level=0
reset.

**`KoraKnownEpochMonotonicViolation` raised at boot.**
`kora_known_epoch` is forward-only. If Kora attempts to write an
`observed_epoch` strictly less than the current `kora_known_epoch`,
the SECDEF raises `23514` and the runtime surfaces it as
`KoraKnownEpochMonotonicViolation` (`dr_epoch.py`). This indicates a
bigger problem than a routine PITR — the substrate timeline moved
backwards beyond the kora_known_epoch checkpoint without a successful
bump. Stop the runbook and escalate to substrate-team.

## Reference

* R4.1 §9.8 (DR / epoch handshake) — pins P1 (substrate_epoch sourced
  outside rewound timeline) + P5 (kora_known_epoch forward-only).
* `packages/db/migrations/0100_kora_dr_epoch_substrate.sql` — substrate
  side (table + read SECDEFs + `bump_substrate_epoch`).
* `agent/dr_handler.py` — gate 3b runtime handler.
* `agent/dr_writer.py` — end-of-boot + PAUSED-clearance writers.
* `plugins/memory/isokron/dr_epoch.py` — runtime read + write helpers.
