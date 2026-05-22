# Kora runtime — first deploy runbook (staging)

**Purpose**: step-by-step operator checklist for the FIRST `flyctl deploy`
of the kora-runtime daemon after the KR-D-DAEMON + KR-D-DEPLOY arc
landed on `feature/phase2-upgrades`. Target app: `kora-runtime-staging`.

**Last updated**: 2026-05-22 (KR-D-DEPLOY ST2).

This runbook assumes:

1. `feature/phase2-upgrades` has been merged to `main`, OR the deploy
   targets the branch directly (Fly's deploy honors whatever branch is
   currently checked out).
2. The `kora-runtime-staging` Fly app + `kora_state` volume exist (see
   "App setup" below if not).
3. All 3 Doppler projects have a `stg` config with the required
   secrets per `kora_runtime_doppler_env_mapping.md`.

The first PRODUCTION deploy follows the same shape with
`-a kora-runtime` and `-c prd`. Promote to prod ONLY after staging
passes the smoke checklist below.

---

## App setup (one-time, skip if already done)

```sh
flyctl apps create kora-runtime-staging
flyctl volumes create kora_state --size 5 --region iad -a kora-runtime-staging
```

Volume size matches `fly.toml`'s `initial_size = "5gb"`. Adjust upward
if `~/.kora/sessions/` growth outpaces the 90-day pruning.

---

## Step 1 — Doppler secrets confirmed

Run the validation script from
`kora_runtime_doppler_env_mapping.md`, with `CONFIG=stg`:

```sh
CONFIG=stg

# Copy + paste the validator script block from the env-mapping doc.
```

Expected: every line begins with `OK`. Stop and resolve any `MISS` or
`FAIL` before proceeding. (`KORA_PUREMAIL_HMAC_SECRET` MISS is
acceptable IF inbound-email integration is deferred; the daemon boots
without it, the email route returns 401 on every request.)

---

## Step 2 — Import Doppler secrets into Fly

The daemon's entrypoint wraps invocations with `doppler run` at
container-startup time (see `docker/dispatch.sh`), but Fly secrets ALSO
need to be present at the Fly layer for the `release_command` to see
them (`hermes doctor` runs BEFORE the entrypoint). Sync once per
secret-change:

```sh
doppler secrets download -p kora-runtime-substrate -c stg --no-file --format docker  > .env.doppler
doppler secrets download -p kora-runtime-anthropic -c stg --no-file --format docker >> .env.doppler
doppler secrets download -p kora-runtime-gateways  -c stg --no-file --format docker >> .env.doppler

flyctl secrets import < .env.doppler -a kora-runtime-staging

shred -u .env.doppler   # plaintext token file; remove immediately
```

The `shred` step is non-negotiable. If `flyctl secrets import` fails
mid-chain, `shred` the file before retrying.

---

## Step 3 — Build-only deploy first

Verify the image builds against `feature/phase2-upgrades` before
pushing a release:

```sh
flyctl deploy -a kora-runtime-staging --build-only
```

Expected: build completes; image digest printed. Failure modes:

- **Doppler CLI install layer fails** (apt repo unreachable, GPG
  verification fails): re-run; Doppler's apt repo has occasional
  blips. If consistent, fall back to one of the alternatives noted
  in PR #107's body.
- **`uv pip install` layer fails**: dependency conflict in
  `pyproject.toml`. Check the build log for the conflict line +
  resolve before retrying.

---

## Step 4 — Live deploy

```sh
flyctl deploy -a kora-runtime-staging
```

Fly's pipeline:

1. Push the built image to Fly's registry.
2. Run the `release_command` (`hermes doctor`) in a one-shot machine
    against the staged image with Fly secrets injected.
3. If release_command exits 0, swap traffic to the new machine.
4. Old machine is reaped after the swap.

If `hermes doctor` exits non-zero, the deploy ABORTS — old machine
keeps serving. Fail-CLOSED.

---

## Step 5 — Verify release_command exited 0

```sh
flyctl releases list -a kora-runtime-staging
```

The most recent release should show `status: succeeded`. If `failed`,
check:

```sh
flyctl logs -a kora-runtime-staging --no-tail | head -200
```

… for the `hermes doctor` exit reason. Common failure modes documented
in `docs/deploy-fly-io.md` step 5 ("Verify boot").

---

## Step 6 — Verify daemon boot + listener order

```sh
flyctl logs -a kora-runtime-staging
```

Expected lines in order (timestamps will vary):

```
[kora.daemon] starting in deploy_env=stg
[kora.daemon] starting listener: heartbeat
[kora.heartbeat] started 1 periodic task(s): ['kora.daemon.alive']
[kora.daemon] listener heartbeat started
[kora.daemon] starting listener: web
[kora.web] uvicorn bound on 0.0.0.0:9119
[kora.daemon] listener web started
[kora.daemon] starting listener: mcp
[kora.mcp] listener active; bearer auth from KORA_MCP_BEARER_TOKEN; 1 tool(s)
[kora.daemon] listener mcp started
[kora.daemon] starting listener: webhooks
[kora.webhook] uvicorn bound on 0.0.0.0:9118 (PUBLIC)
[kora.daemon] listener webhooks started
[kora.daemon] all 4 listener(s) started; awaiting shutdown
```

If any listener startup raises (e.g. mcp listener refuses because
`KORA_MCP_BEARER_TOKEN` is unset), the daemon aborts boot + tears down
already-started listeners in LIFO. The deploy stays on the previous
machine until the operator resolves the missing secret + redeploys.

---

## Step 7 — Operator-side admin smoke

```sh
# Terminal 1: proxy 9119 from staging machine to localhost.
flyctl proxy 9119:9119 -a kora-runtime-staging

# Terminal 2: hit the admin endpoints.
curl http://localhost:9119/api/status
```

Expected: 200 + JSON body with `daemon_state: "running"` or similar.
A 404 indicates the SPA catch-all is winning over the admin route —
investigate the daemon's web listener init.

MCP smoke (requires the bearer token locally):

```sh
TOK=$(doppler secrets get KORA_MCP_BEARER_TOKEN -p kora-runtime-gateways -c stg --plain)

curl -H "Authorization: Bearer $TOK" http://localhost:9119/mcp/tools/list
```

Expected: 200 + JSON `{"tools":[{"name":"kora__daemon_status",...}]}`.

---

## Step 8 — Public webhook smoke

Two paths — pick the one that matches what's wired:

### Path A — Slack URL-verification handshake (PREFERRED)

If a Slack app is already pointed at the staging webhook URL:

1. In the Slack app config: Settings → Event Subscriptions → Request URL
2. Set to `https://kora-runtime-staging.fly.dev/api/webhooks/slack/events`
3. Slack POSTs a `url_verification` challenge automatically.
4. Kora echoes the `challenge` payload.
5. Slack accepts the URL ("Verified ✓").

If verified: the public 9118 port + TLS cert + HMAC verify + Slack
HMAC seed all work end-to-end. Stop here for staging smoke.

### Path B — manual curl with operator-signed payload

When Slack app isn't wired yet, or for debugging:

```sh
SECRET=$(doppler secrets get KORA_SLACK_SIGNING_SECRET -p kora-runtime-gateways -c stg --plain)
TS=$(date +%s)
BODY='{"type":"url_verification","challenge":"smoke-test"}'

# Compute v0 signature.
SIG="v0=$(printf 'v0:%s:%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -hex | awk '{print $2}')"

curl -X POST https://kora-runtime-staging.fly.dev/api/webhooks/slack/events \
  -H "Content-Type: application/json" \
  -H "X-Slack-Signature: $SIG" \
  -H "X-Slack-Request-Timestamp: $TS" \
  -d "$BODY"
```

Expected: HTTP 200, body = `smoke-test` (the challenge echo).

Failure modes:

- **401** `slack_signature_mismatch` — secret in Doppler differs from
  what `openssl dgst` used; check both.
- **401** `slack_secret_unset` — Fly secrets don't have
  `KORA_SLACK_SIGNING_SECRET`; re-run Step 2.
- **404** — public route not mounted. Check `flyctl logs` for the
  webhook listener startup line; if missing, the `webhooks` listener
  is failing.
- **502 / TLS error** — Fly's edge isn't routing to the container. Check
  `fly.toml`'s second `[[services]]` block + the http_check on
  `/healthz`.

### Healthcheck smoke

```sh
curl https://kora-runtime-staging.fly.dev/healthz
```

Expected: 200 + body `ok`. This is Fly's TCP/HTTP healthcheck target;
if `/healthz` is reachable, the public port is bound correctly.

---

## Step 9 — Rollback procedure

If any step from 5-8 reveals a problem that can't be fixed forward
without operator triage:

```sh
flyctl releases list -a kora-runtime-staging
# Note the previous-known-good release version (one before the broken deploy).

flyctl releases rollback <PREV_VERSION> -a kora-runtime-staging
```

Fly swaps traffic back. The broken image stays in the registry for
post-mortem (image digest in the release list).

Doppler secrets are NOT rolled back automatically — if Step 2 imported
a bad secret, manually re-import the previous-known-good values from
Doppler (Doppler retains version history; use `doppler activity` +
`doppler secrets update --version <N>` per the token rotation runbook).

---

## Promotion to production

After staging passes all 8 smoke checks for at least one full day of
runtime (let scheduled heartbeats fire ~2880 times, watch dead-letter
rate, verify Slack/email handlers don't leak), repeat the full
sequence against `kora-runtime` (prod app) with `-c prd`:

1. Step 1 — Doppler validation with `CONFIG=prd`
2. Step 2 — `flyctl secrets import` to `-a kora-runtime`
3. Steps 3-8 — same shape, prod app, prod hostnames
4. Step 9 — rollback procedure same

**Joshua's call when to promote** — there's no automatic gate. The
staging deploy stays running as long as it's healthy.

---

## Known-good first-deploy smoke artifacts

After Step 8 succeeds, capture for the audit trail:

- `flyctl logs -a kora-runtime-staging --no-tail | head -100 > first-deploy-boot.log`
- The release version + image digest from `flyctl releases list -a kora-runtime-staging`
- The Slack URL-verification confirmation screenshot (if Path A) OR
  the curl output (if Path B)

Store under operator's secure deploy-artifact archive. Pin to the
audit trail at `kora_docs/15_status_and_roadmap/2026-05-22_R4.1_section_12_readiness_audit.md`
when the next §12 readiness pass runs.

---

## Cross-references

- `kora_runtime_doppler_env_mapping.md` — Step 1's secret list + validator script
- `docs/deploy-fly-io.md` — R2-era deploy doc; volume + apps-create still authoritative
- `kora_docs/15_status_and_roadmap/token_rotation_runbook.md` — secret rotation procedures + Doppler version history
- `kora_docs/00_canonical_current_state/r2_amendments.md` — Amendment 1 (public webhook port) — why the second `[[services]]` block exists
- `fly.toml` — `[env]` + `[mounts]` + two `[[services]]` blocks
- `docker/entrypoint.sh` + `docker/dispatch.sh` — boot-time secret injection via Doppler nested wrap
