# Token rotation runbook — `KORA_SERVICE_TOKEN` + `CLAUDE_CODE_OAUTH_TOKEN`

Closes R4.1 §12 readiness item: "the two tokens' rotations are documented
as one unified runbook (a wsk-token lapse while OAuth is valid burns
inference with zero durable output)."

Two credentials, two cadences, ONE runbook. Both follow the same shape:
**mint new → KR-7 smoke-test → revoke old**.

## Credentials covered

| Secret | Doppler project | Cadence | Current expiry |
|---|---|---|---|
| `KORA_SERVICE_TOKEN` (`wsk_*`) | `kora-runtime-substrate` | ~quarterly | **2026-08-18** |
| `CLAUDE_CODE_OAUTH_TOKEN` | `kora-runtime-anthropic` | ~yearly | — |

Both rotations are **independent** (separate Doppler projects, separate
expirations, separate mint procedures). **Never do them simultaneously**
— if either rotation fails, the overlap muddies the debug.

## When this runbook applies

**Proactive** — T-30, T-14, T-7, T-1 days before either token's expiry.
Set an OS-level cron / calendar reminder on your operator workstation so
the alerts fire before the lapse.

**Reactive** — either token has already lapsed and Kora is degraded /
stopped. Symptoms:

* **`wsk_*` lapsed**: next boot's gate 4 (`kora_runtime` role perms)
  fails — `/api/boot-status` shows `outcome: failed`, `gates[].outcome:
  fail` on gate 4. Chain log: `kora.boot.failed` with the gate-4 detail.
  Kora transitions to `STOPPED`.
* **`CLAUDE_CODE_OAUTH_TOKEN` lapsed**: Claude SDK calls return 401/403 at
  inference time. `/api/cost-state` may show zero burn (no inference
  happening). `/api/health` shows `worker: degraded` /
  `overall: degraded`. Tickets pile up in `failed_retryable`.

## Rotation procedure — `KORA_SERVICE_TOKEN` (wsk_*)

1. **Mint** a new `wsk_*` token via substrate-team's minting endpoint
   (`workspace_service_token.minted` chain event will fire on success;
   substrate-team owns this surface — coordinate via #claude-pms).
2. **Update Doppler** `kora-runtime-substrate` (don't delete the old
   value yet):

   ```sh
   doppler secrets set KORA_SERVICE_TOKEN=<NEW_WSK_TOKEN> \
     -p kora-runtime-substrate -c prd
   ```

3. **Push to Fly** as a single import (preserves the rest of the project's
   secrets):

   ```sh
   doppler secrets download -p kora-runtime-substrate -c prd \
     --no-file --format docker > .env.doppler
   flyctl secrets import < .env.doppler -a kora-runtime
   shred -u .env.doppler   # plaintext token; remove immediately
   ```

4. **Restart** Kora:

   ```sh
   flyctl restart -a kora-runtime
   ```

5. **Verify** via the KR-7 boot smoke (the gates that exercise the new
   token end-to-end):

   * `GET /api/boot-status` → latest boot's `outcome: ready`; gate 4
     (`kora_runtime` role perms) **pass**; gate 6 (wsk_* token valid)
     **pass**; gate 10 (KR-7 attribution smoke) **pass**.
   * `GET /api/operational-state` → `primary_state: ready`, no
     degradation reasons.
   * Chain log: `kora.boot.ready` emitted with the full
     `gate_results` payload.

6. **ONLY after boot verified READY**: revoke the OLD wsk_* via
   substrate-team's revoke endpoint (`workspace_service_token.revoked`
   fires on success).

Total elapsed (typical): ~3–5 minutes. The overlap window between
"new token live + boot verified" and "old token revoked" is the
graceful-rotation guarantee from R2 §5.

## Rotation procedure — `CLAUDE_CODE_OAUTH_TOKEN`

1. **Mint** a new OAuth token on your operator workstation. The Claude
   CLI's setup-token command walks the device-flow login:

   ```sh
   claude setup-token
   ```

   Capture the new token from the CLI output.

2. **Update Doppler** `kora-runtime-anthropic`:

   ```sh
   doppler secrets set CLAUDE_CODE_OAUTH_TOKEN=<NEW_OAUTH_TOKEN> \
     -p kora-runtime-anthropic -c prd
   ```

3. **Push to Fly + restart** (same shape as wsk_* above):

   ```sh
   doppler secrets download -p kora-runtime-anthropic -c prd \
     --no-file --format docker > .env.doppler
   flyctl secrets import < .env.doppler -a kora-runtime
   shred -u .env.doppler
   flyctl restart -a kora-runtime
   ```

4. **Verify** via an inference probe (the OAuth token isn't exercised
   by the boot gates; needs a real inference call):

   * `GET /api/boot-status` → boot still READY (boot gates don't check
     OAuth).
   * Trigger a `SUBSTRATE_HEARTBEAT`-class cron job, OR have an operator
     send a Slack DM to Kora — either path drives Anthropic SDK
     traffic. Inference success → `kora.sea_ticket.resolved` chain
     event or a Slack reply.
   * `GET /api/health` → `worker: ok`, `overall: ok`.

5. **The old OAuth token expires automatically** on its own clock — no
   explicit revoke needed unless the security posture requires it
   (compromised credential). `claude setup-token` issues fresh tokens
   without invalidating prior ones; rely on Anthropic-side expiration
   for the lapse.

## Joshua's preference (per `user_joshua` memory)

Comfortable handing over rotating credentials; **favor explicit-overlap
rotation** (mint → verify → revoke) over hot-swap. Both procedures above
follow that shape — the old credential stays valid until the new one is
boot-verified.

## T-30 / 14 / 7 / 1 day alerts (one-time operator setup)

The two tokens have asynchronous expiries; the alerts are per-token.
Suggested approach: a per-token reminder on your operator workstation's
`crontab -e`:

```cron
# wsk_* expires 2026-08-18 — alert 30 / 14 / 7 / 1 days prior.
0 9 19 7  * /usr/local/bin/slack-dm '@joshua' 'wsk_* expires in 30 days (2026-08-18)'
0 9 4  8  * /usr/local/bin/slack-dm '@joshua' 'wsk_* expires in 14 days'
0 9 11 8  * /usr/local/bin/slack-dm '@joshua' 'wsk_* expires in 7 days'
0 9 17 8  * /usr/local/bin/slack-dm '@joshua' 'wsk_* expires in 1 day — ROTATE NOW'
```

After each successful rotation, update the cron dates to the next
quarter (wsk_*) or year (OAuth) and re-arm.

## Failure modes

**`flyctl secrets import` succeeded but boot still uses old token.**
The Doppler download captured stale env; the import included a secret
that wasn't actually rotated. Re-download Doppler, confirm
`KORA_SERVICE_TOKEN` has the new value (`doppler secrets get
KORA_SERVICE_TOKEN -p kora-runtime-substrate -c prd --plain`), then
re-import + restart.

**Boot reaches gate 4 but fails with `permission denied for table ...`.**
The new wsk_* was minted with the wrong scope. Substrate-team needs to
re-mint with the correct grants. Continue using the OLD token until the
correctly-scoped replacement arrives (the OLD is still live — you didn't
revoke yet).

**Boot READY but inference returns 401.**
The OAuth token in Doppler is stale or malformed. `claude setup-token`
output sometimes has trailing whitespace — re-export the value with
explicit trimming before `doppler secrets set`.

**Both rotations attempted simultaneously and one failed.**
Stop. The diagnosis is now ambiguous — revert by re-importing the LAST
known-good Doppler snapshot (Doppler retains version history; use
`doppler activity` to find the prior version, then `doppler secrets
update --version <N>`). Restart Kora. Verify boot READY against the
old-known-good values. Then re-attempt ONE rotation at a time.

## Reference

* R2 §5 — token blast-radius isolation + unified rotation procedure.
* R4.1 §12 — readiness checklist (this runbook closes the token-rotation item).
* `docs/deploy-fly-io.md` — the 3-Doppler-project layout this rotation
  preserves (`kora-runtime-substrate`, `kora-runtime-anthropic`,
  `kora-runtime-gateways`).
* `docker/entrypoint.sh` — boot-time required-env-var validation
  (KR-P2-F-pre ST2); fails fast if either token is missing.
