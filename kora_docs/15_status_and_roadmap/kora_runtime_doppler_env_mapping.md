# Kora runtime — Doppler env mapping

**Purpose**: single source of truth for which Doppler project owns
which secret in the kora-runtime deploy. Operator reads this BEFORE
the first `flyctl deploy` (see companion `kora_runtime_first_deploy_runbook.md`)
and at every secret rotation.

**Last updated**: 2026-05-22 (KR-D-DEPLOY ST2).

The 3-project Doppler split is mandated by R2 §5 + R4.1 §3: credential
blast-radius isolation. One project compromise must NOT yield substrate
access AND Anthropic auth AND messaging-platform tokens at once.

---

## The three Doppler projects

| Project | Role | Rotation cadence | Owner-of-record |
|---|---|---|---|
| `kora-runtime-substrate` | Substrate auth + DB connectivity | wsk_* quarterly (current expires 2026-08-18) | substrate-team / IsoKron PM |
| `kora-runtime-anthropic` | Anthropic inference credentials | OAuth ~yearly | Joshua (operator) |
| `kora-runtime-gateways` | Inbound/outbound messaging tokens | Per-platform (typically yearly) | Joshua (operator) |

Each project has three configs: `dev` / `stg` / `prd`. The Doppler config
name matches the `KORA_DEPLOY_ENV` value Kora is booted with — so
`kora-runtime-staging` Fly app uses `-c stg` across all three projects;
production `kora-runtime` Fly app uses `-c prd`.

---

## Secret-by-project table

### `kora-runtime-substrate`

| Secret | Required by | Notes | Example shape |
|---|---|---|---|
| `KORA_SERVICE_TOKEN` | Boot gate 6 (wsk_* validity) + every substrate write | wsk_* token minted by substrate-team. Current expires 2026-08-18 per `kora_docs/15_status_and_roadmap/token_rotation_runbook.md`. | `wsk_<64-hex>` |
| `KORA_ISOKRON_DSN` | Substrate Postgres connection | Standard PG DSN; password embedded. Reachable only over flycast. | `postgresql://kora_runtime:<pw>@<host>:5432/isokron?sslmode=require` |
| `KORA_DEFAULT_WORKSPACE_ID` | Workspace pin for all SQL `SET LOCAL` calls | Set once per deploy environment; never rotated. | `<UUID>` |
| `KORA_SEA_MCP_ENDPOINT` | Substrate-side MCP endpoint (kronicle-mcp via flycast) | Internal-only Fly DNS name; `http://kronicle-mcp.internal:8080`. | `http://kronicle-mcp.internal:8080` |

### `kora-runtime-anthropic`

| Secret | Required by | Notes | Example shape |
|---|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | Boot gate 1 (Claude auth) + every inference call | Issued via `claude setup-token` on operator workstation. **Distinct from** `ANTHROPIC_API_KEY` — gate 2 fail-CLOSED rejects boot if `ANTHROPIC_API_KEY` is present (R4.1 §9.2). | `sk-ant-oat-...` |

**Anti-secret** (Kora REFUSES to boot if these are set anywhere):

- `ANTHROPIC_API_KEY`
- `ANTHROPIC_AUTH_TOKEN`

If a `kora-runtime-anthropic` Doppler config contains either, Kora's
boot gate 2 fires + the deploy aborts. Operator MUST keep these out of
the project — never even as a stale fallback.

### `kora-runtime-gateways`

| Secret | Required by | Notes | Example shape |
|---|---|---|---|
| `KORA_MCP_BEARER_TOKEN` | Daemon MCP listener (KR-D-DAEMON ST2) | Bearer token for `/mcp` HTTP transport. Listener fails-CLOSED if unset. Mint as long random hex (`openssl rand -hex 32`). Rotate quarterly alongside `wsk_*`. | 64-hex |
| `KORA_SLACK_SIGNING_SECRET` | Daemon webhook listener — Slack route (KR-D-DAEMON ST3) | From Slack app Settings → Basic Information → Signing Secret. **NEW for Phase 2** — consumed by the webhook plane on public port 9118; HMAC is the auth boundary. | 32-hex |
| `KORA_PUREMAIL_HMAC_SECRET` | Daemon webhook listener — email route (KR-D-DAEMON ST3). **Now harmless dead-code** post-KR-FEAT-EMAIL double-STOP-ASK: Purelymail has no inbound webhooks (verified 2026-05-22). Route + verifier stay in tree as reactivatable scaffolding if a webhook-relay path is later chosen. Set to any value (or leave unset) — the route 401s anyone who tries since there's no real Purelymail signing scheme to validate. | (unset OR any opaque string) |
| `SLACK_APP_TOKEN` | Legacy Slack Bolt gateway (`hermes gateway slack`) | Optional. Only required if `SLACK_GATEWAY_ENABLED=true` (the default). When `false`, leave unset. | `xapp-<token>` |
| `SLACK_BOT_TOKEN` | Legacy Slack Bolt gateway | Same gating as SLACK_APP_TOKEN. | `xoxb-<token>` |
| `SLACK_SIGNING_SECRET` | Legacy Slack Bolt gateway | Same gating as SLACK_APP_TOKEN. **Same value as `KORA_SLACK_SIGNING_SECRET`** — both come from the same Slack app's Basic Information page; the env-var split exists because the legacy gateway and the new webhook listener consume the secret via different code paths. Operator sets both to the same value until a follow-on refactor consolidates them. | Same as `KORA_SLACK_SIGNING_SECRET` |
| `SLACK_GATEWAY_ENABLED` | Toggle for the legacy Bolt gateway | Defaults to `true` in `docker/entrypoint.sh`. Set to `false` if the legacy gateway should stay dormant (e.g. running webhook-only on the daemon). | `true` / `false` |

#### Phase 2 Feature 2 — Heartbeat probes (KR-FEAT-HEARTBEAT)

The daemon's heartbeat scheduler probes 5 backend services every
`KORA_HEARTBEAT_PROBE_INTERVAL_SEC` (default 300s). Each probe needs
a Doppler-injected service token. A probe with its auth env unset
degrades gracefully (status: `unknown` in the panel + zero outbound
calls) — these secrets are NOT deploy-blocking, but the heartbeat
dashboard will show "auth env unset" until they're configured.

All 5 live in `kora-runtime-gateways` (same project as the legacy
gateway tokens — gateways = "tokens the runtime uses to reach
outbound services on Joshua's behalf").

| Secret | Probe | Mint via | Scope | Example shape |
|---|---|---|---|---|
| `KORA_VERCEL_API_TOKEN` | Vercel — recent deployments + error rate | <https://vercel.com/account/tokens> | Read-only scope sufficient (lists `/v6/deployments`). | `<32+ char opaque>` |
| `KORA_SENTRY_API_TOKEN` | Sentry — unresolved issue count | <https://sentry.io/settings/account/api/auth-tokens/> | `org:read` scope minimum (`event:read` if probe extension wants project breakdown later). | `<64-hex>` |
| `KORA_SENTRY_ORG` | Sentry — org slug for the issues query | Operator-known org slug (e.g. `stormhaven`). | — | `stormhaven` |
| `KORA_DOPPLER_API_TOKEN` | Doppler — workplace reachability | <https://dashboard.doppler.com/workplace/.../tokens> → **Service Token** (NOT a project token). Workplace read-only scope. Mint a dedicated service token for the probe — keep separate from any per-project tokens. | Workplace read-only | `dp.st.<scope>.<opaque>` |
| `KORA_SUPABASE_ANON_KEY` | Supabase — PostgREST endpoint reachability | Supabase project → Settings → API → **anon key** (NOT the service_role key). | `anon` (public) | `<JWT-shaped>` |
| `KORA_SUPABASE_URL` | Supabase — project URL | Same Project Settings page. | — | `https://<project-ref>.supabase.co` |
| `KORA_FLY_API_TOKEN` | Fly — `kora-runtime` machines state | `flyctl auth token` (operator workstation, deploy token) or Fly dashboard org tokens page. | Read access to the kora-runtime app(s). | `fly_<opaque>` |
| `KORA_FLY_STAGING_APP_NAME` | Fly — optional staging app name | Optional. Set if the operator wants the probe to ALSO check the staging app. Leave unset to probe prod only. | — | `kora-runtime-staging` |

**Validation tip**: after setting these, restart the daemon (or wait
≤5 min for the next probe cycle); `GET /api/heartbeat/services`
should flip each service from `unknown` to `healthy` / `degraded`.

#### Phase 2 Feature 3 — Purelymail (KR-FEAT-EMAIL + KR-FEAT-EMAIL-INBOUND-IMAP)

Email both directions. Outbound via SMTP (`smtp.purelymail.com:465`
SSL — per the outbound bucket's double-STOP-ASK, Purelymail has
no REST send API). Inbound via IMAP polling
(`imap.purelymail.com:993` SSL — per the K-DG verification in
KR-FEAT-EMAIL-INBOUND-IMAP ST1, no inbound webhooks either).

Full mint + smoke-test walkthrough in `purelymail_runbook.md`
(both Part 1 outbound + Part 2 inbound). All secrets live in
`kora-runtime-gateways`.

**Outbound (5 secrets):**

| Secret | Required by | Notes | Example shape |
|---|---|---|---|
| `KORA_PUREMAIL_SMTP_USERNAME` | `PurelymailClient.__init__` (fail-CLOSED on missing) | Full email address bound to the App Password. Typically `kora@stormhavenenterprises.com`. | `kora@<domain>` |
| `KORA_PUREMAIL_SMTP_APP_PASSWORD` | `PurelymailClient.__init__` (fail-CLOSED on missing) | App Password minted in Purelymail dashboard → Account → App Passwords. Assumes 2FA enabled on the account. NEVER use the main account password. | `<opaque>` |
| `KORA_PUREMAIL_SMTP_HOST` | Optional override | Default `smtp.purelymail.com`. Override only for staging / test relay. | `smtp.purelymail.com` |
| `KORA_PUREMAIL_SMTP_PORT` | Optional override | Default `465` (SSL). Use `587` for STARTTLS if the deploy environment blocks 465 outbound. | `465` (default) or `587` |
| `KORA_EMAIL_KORA_ALLOWED_FROM_DOMAINS` | `PurelymailClient.send_email` (raises if from-domain not in list) | Comma-separated allowed sender domains. Defense against accidental wide-open sends. **Unset = client rejects EVERY send** (operator-config error, not silent allow-all). | `stormhavenenterprises.com` |

**Inbound (8 secrets — 4 IMAP transport + 4 email handler):**

| Secret | Required by | Notes | Example shape |
|---|---|---|---|
| `KORA_PUREMAIL_IMAP_USERNAME` | `PurelymailIMAPClient.__init__` (fail-CLOSED on missing) | Full email address bound to the IMAP App Password. Typically same as `KORA_PUREMAIL_SMTP_USERNAME`. | `kora@<domain>` |
| `KORA_PUREMAIL_IMAP_APP_PASSWORD` | `PurelymailIMAPClient.__init__` (fail-CLOSED on missing) | App Password minted in Purelymail dashboard. Purelymail's docs don't explicitly confirm App-Password protocol scoping; safe default is a separate App Password named `kora-runtime-inbound` (operators may reuse the SMTP one if they prefer + verify). | `<opaque>` |
| `KORA_PUREMAIL_IMAP_HOST` | Optional override | Default `imap.purelymail.com`. | `imap.purelymail.com` |
| `KORA_PUREMAIL_IMAP_PORT` | Optional override | Default `993` (SSL). | `993` |
| `KORA_EMAIL_SENDER_ALLOWLIST` | `EmailInboundHandler.handle_event` (fail-CLOSED DENY ALL on unset / empty) | Comma-separated allowed inbound senders. Defense against accidental processing of non-Joshua mail. | `joshua@<domain>` |
| `KORA_EMAIL_KORA_ADDRESS` | `EmailInboundHandler.handle_event` (recipient filter) | The address Kora receives AT — parsed `to:` header must include this (case-insensitive). | `kora@<domain>` |
| `KORA_EMAIL_JOSHUA_ADDRESS` | `EmailInboundHandler.handle_event` (identity check; fail-CLOSED on unset) | The single sender address allowed past the identity filter. Inbound mail from this sender is parsed + logged; no auto-reply is sent (Lock R3-8 (a) / KR-EMAIL-AUTOREPLY-BRANCH-REMOVAL). | `joshua@<domain>` |
| `KORA_EMAIL_IMAP_POLL_INTERVAL_SEC` | Optional override | Default `300` (5 min). | `300` |

> **Removed**: `KORA_EMAIL_AUTO_REPLY` (Lock R3-8 (a) / KR-EMAIL-AUTOREPLY-BRANCH-REMOVAL). The inbound auto-reply path was cut; the env is no longer read. Legacy values in Doppler are ignored cleanly and may be removed at the next secret-rotation cadence.

**Validation tip**: after setting these, redeploy the daemon then
run the smoke tests in `purelymail_runbook.md` — Part 1 Step 4
for outbound (`SendResult(status="ok", smtp_code=250)`) and
Part 2 Step 3 for inbound (JSONL `handled_status: received`).

#### Phase 2 Feature follow-on — Alert notifier (KR-ALERT-NOTIFY ST1 + ST2)

Push-notification layer that pings Joshua via Slack DM (critical /
warning) or email (info) when alerts fire in the cockpit. All
envs in `kora-runtime-gateways`. Channels reuse the same
SlackClient + PurelymailClient set up in the rows above.

**ST1 (set during initial deploy):**

| Secret | Required by | Notes | Example shape |
|---|---|---|---|
| `KORA_SLACK_JOSHUA_USER_ID` | Slack DM channel (also used by Slack inbound handler) | The notifier passes this as `channel_id` to `chat.postMessage`; Slack auto-resolves to Joshua's DM channel | `U01ABC...` |
| `KORA_ALERT_NOTIFY_INTERVAL_SEC` | Optional override | Default `180` (3 min). Cycle cadence for the notifier periodic task. | `180` |

**ST2 throttling (optional — defaults are operator-friendly):**

| Secret | Default | Effect |
|---|---|---|
| `KORA_ALERT_NOTIFY_MODE` | `immediate` | `digest` queues warning + info for a daily email; criticals always fire immediately |
| `KORA_ALERT_NOTIFY_CATEGORY_COOLDOWN_SEC` | `1800` (30 min) | Same category can't re-dispatch within this window. Set `0` to disable. |
| `KORA_ALERT_NOTIFY_BURST_THRESHOLD` | `5` | >threshold newly-firing alerts in one cycle → ONE summary Slack DM instead of N individual |
| `KORA_ALERT_NOTIFY_DIGEST_INTERVAL_SEC` | `86400` (24h) | Digest-flush cadence when mode=digest. No-op in immediate mode. |
| `KORA_COCKPIT_URL` | unset | When set, appended to email bodies + burst summaries so operator can jump to the cockpit |

See `alert_notifier_runbook.md` for full configuration + tuning
guidance + the `kora__send_test_alert` MCP tool for channel
verification.

---

## fly.toml `[env]` values (NOT in Doppler)

These are committed in `fly.toml` and applied at every deploy. Doppler
does not own them.

| Var | Value | Why |
|---|---|---|
| `KORA_DEPLOY_ENV` | `prd` (prod), `stg` (staging) | Drives the Doppler `-c <config>` argument in `docker/dispatch.sh`. |
| `HERMES_HOME` | `/home/hermes/.kora` | Reconciles to the Fly volume mount destination (KR-D-DEPLOY ST1 Option A). |
| `KORA_WEB_HOST` | `0.0.0.0` | Daemon's web listener binds the container interface so Fly's internal proxy can reach 9119. Public exposure is governed by `[[services]]` blocks. |
| `KORA_WEB_PORT` | `9119` | Internal admin UI + MCP transport. |

---

## fly.toml `[[services]]` exposure

| Port | Exposure | Owner-listener | Notes |
|---|---|---|---|
| 9119 | INTERNAL ONLY (no `[[services.ports]]`) | `web` + `mcp` listeners on shared admin FastAPI app | Reached via `flyctl proxy 9119:9119 -a kora-runtime`. Per R2 §5 control-plane policy. |
| 9118 | PUBLIC — 443 (TLS) + 80 (force_https) | `webhooks` listener on separate FastAPI app | Per R2 §5 amendment (`kora_docs/00_canonical_current_state/r2_amendments.md`). Only `/api/webhooks/slack/events`, `/api/webhooks/email/inbound`, `/healthz` routes. HMAC is the auth boundary. |

---

## Optional / deploy-tunable env vars

| Var | Default | When to set |
|---|---|---|
| `KORA_WEBHOOK_RATE_LIMIT` | `60/minute` | Tighten via Doppler-gateways if dead-letter rate spikes suggest a flood. slowapi syntax. |
| `KORA_HEALTH_PROBE_CADENCE_SECONDS` | `300` | Lower if dashboard freshness suffers under default 5min cadence. |
| `KORA_HEARTBEAT_PROBE_INTERVAL_SEC` | `300` | KR-FEAT-HEARTBEAT — backend-service probe cadence (Vercel/Sentry/Doppler/Supabase/Fly). Distinct from `KORA_HEALTH_PROBE_CADENCE_SECONDS` (the MCP-client health-check task); both default to 5min, registered as DISTINCT scheduler tasks so one slow cycle doesn't block the other. |
| `KORA_MCP_HEALTH_CHECK_INTERVAL_SEC` | `300` | KR-MCP-CONSUMPTION ST2 — MCP-client-pool health check cadence. Same default as above; same isolation rationale. |
| `KORA_LOG_LEVEL` | `INFO` | `DEBUG` for first-deploy investigation; revert to `INFO` afterwards. |
| `KORA_DEV` | unset | Set to `1` ONLY for local-dev `kora daemon` invocation (bypasses Doppler wrap + lets `KORA_DEPLOY_ENV` default to `dev`). Never set in Fly. |

---

## Validation — operator pre-deploy checklist

Run this before every first-deploy or rotation to confirm all required
secrets are present in each Doppler project. Substitute the config
name (`prd` / `stg`) for the deploy in question:

```sh
CONFIG=stg   # or prd

for SECRET in KORA_SERVICE_TOKEN KORA_ISOKRON_DSN KORA_DEFAULT_WORKSPACE_ID KORA_SEA_MCP_ENDPOINT; do
  doppler secrets get "$SECRET" -p kora-runtime-substrate -c "$CONFIG" --plain >/dev/null \
    && echo "OK   substrate:$SECRET" \
    || echo "MISS substrate:$SECRET"
done

doppler secrets get CLAUDE_CODE_OAUTH_TOKEN -p kora-runtime-anthropic -c "$CONFIG" --plain >/dev/null \
  && echo "OK   anthropic:CLAUDE_CODE_OAUTH_TOKEN" \
  || echo "MISS anthropic:CLAUDE_CODE_OAUTH_TOKEN"

for SECRET in KORA_MCP_BEARER_TOKEN KORA_SLACK_SIGNING_SECRET SLACK_APP_TOKEN SLACK_BOT_TOKEN SLACK_SIGNING_SECRET; do
  doppler secrets get "$SECRET" -p kora-runtime-gateways -c "$CONFIG" --plain >/dev/null \
    && echo "OK   gateways:$SECRET" \
    || echo "MISS gateways:$SECRET"
done

# KR-FEAT-HEARTBEAT probe tokens — NOT deploy-blocking. The
# heartbeat panel surfaces "auth env unset" on missing probes
# rather than failing the boot. Run this section opt-in to verify
# the heartbeat-panel data path is fully configured.
for SECRET in KORA_VERCEL_API_TOKEN KORA_SENTRY_API_TOKEN KORA_SENTRY_ORG \
              KORA_DOPPLER_API_TOKEN KORA_SUPABASE_ANON_KEY KORA_SUPABASE_URL \
              KORA_FLY_API_TOKEN; do
  doppler secrets get "$SECRET" -p kora-runtime-gateways -c "$CONFIG" --plain >/dev/null \
    && echo "OK   gateways:$SECRET (heartbeat probe)" \
    || echo "MISS gateways:$SECRET (heartbeat probe — panel shows unknown)"
done

# KR-FEAT-EMAIL outbound tokens. Required for `PurelymailClient`
# to instantiate (fail-CLOSED on missing username/password); the
# allowlist env is required for any send_email call to succeed
# (operator-config error if unset). Skip this section if outbound
# email isn't yet wired (Kora boots fine without — outbound is a
# capability not a gate).
for SECRET in KORA_PUREMAIL_SMTP_USERNAME KORA_PUREMAIL_SMTP_APP_PASSWORD \
              KORA_EMAIL_KORA_ALLOWED_FROM_DOMAINS; do
  doppler secrets get "$SECRET" -p kora-runtime-gateways -c "$CONFIG" --plain >/dev/null \
    && echo "OK   gateways:$SECRET (Purelymail outbound)" \
    || echo "MISS gateways:$SECRET (Purelymail outbound — send_email will raise)"
done

# Anti-secret check — these MUST be absent.
for ANTI in ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN; do
  doppler secrets get "$ANTI" -p kora-runtime-anthropic -c "$CONFIG" --plain 2>/dev/null \
    && echo "FAIL anti-secret PRESENT: anthropic:$ANTI — remove before deploy" \
    || echo "OK   anti-secret absent: anthropic:$ANTI"
done
```

Expected output: every line begins with `OK` for the required
sections. The heartbeat-probe section + Purelymail outbound section
are opt-in capabilities — `MISS` there doesn't block deploy, just
disables the corresponding capability (panel shows `unknown` /
`send_email` raises). The `KORA_PUREMAIL_HMAC_SECRET` row was
previously deploy-relevant under the original inbound-webhook
design; post-KR-FEAT-EMAIL double-STOP-ASK it's harmless dead-code
(see the row note). Any `FAIL` (anti-secret) blocks the deploy.

---

## Rotation procedure cross-reference

Per-secret rotation procedures (operator-driven):

- `KORA_SERVICE_TOKEN` (wsk_*) + `CLAUDE_CODE_OAUTH_TOKEN` — full procedure in `kora_docs/15_status_and_roadmap/token_rotation_runbook.md` (PR #86). When that runbook is extended to cover the Phase 2 secrets below, this row links to the new sections.
- `KORA_MCP_BEARER_TOKEN` — quarterly. Mint via `openssl rand -hex 32`; `doppler secrets set` in `kora-runtime-gateways`; redeploy; old token invalidated by Doppler push.
- `KORA_SLACK_SIGNING_SECRET` + `SLACK_SIGNING_SECRET` — when Slack rotates the signing secret (rare; operator-triggered via Slack app config). Update both env vars to the new value simultaneously.
- `SLACK_APP_TOKEN` + `SLACK_BOT_TOKEN` — when re-installing the Slack app or scoping changes. Mint via Slack app config.
- `KORA_PUREMAIL_HMAC_SECRET` — DEAD CODE post KR-FEAT-EMAIL double-STOP-ASK. No rotation needed; leave unset (route 401s anyway).
- `KORA_PUREMAIL_SMTP_APP_PASSWORD` — quarterly aligned with `KORA_SERVICE_TOKEN` per `purelymail_runbook.md` Part 1 Step 6. Zero-downtime swap: mint new App Password → Doppler update → redeploy → smoke test → revoke old. Revoking before the redeploy causes a 535 window.
- `KORA_PUREMAIL_IMAP_APP_PASSWORD` — same quarterly cadence + same zero-downtime swap shape as the SMTP App Password. If the SMTP one is being reused (operator chose to use one App Password for both protocols), this env mirrors the SMTP value automatically; rotate the single password + update both Doppler secrets together.

---

## Cross-references

- `docs/deploy-fly-io.md` — R2-era deploy doc; this table SUPERSEDES the env-mapping section in that doc for Phase 2 + later. The R2 doc remains canonical for the Fly volume + apps-create steps.
- `kora_docs/15_status_and_roadmap/token_rotation_runbook.md` — wsk_* + OAuth rotation procedures.
- `kora_docs/15_status_and_roadmap/kora_runtime_first_deploy_runbook.md` — first-deploy operator checklist (this doc's companion).
- `kora_docs/00_canonical_current_state/r2_amendments.md` — Amendment 1 (public webhook port).
- `fly.toml` — `[env]` declarations + `[[services]]` blocks.
- `docker/dispatch.sh` — consumes `KORA_DEPLOY_ENV` to select the Doppler `-c` config.
