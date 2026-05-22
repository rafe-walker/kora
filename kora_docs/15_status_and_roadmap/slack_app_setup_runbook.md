# Slack app setup runbook — KR-FEAT-SLACK-DM (Phase 2 Feature 5)

**Purpose**: end-to-end operator checklist from "no Slack app" to
"Joshua DMs Kora's bot and gets an echo reply." Covers Slack app
creation, OAuth scopes + event subscriptions, Doppler secret
wiring, smoke test, troubleshooting, and the dual-signing-secret
env transition note.

**Last updated**: 2026-05-22 (KR-FEAT-SLACK-DM ST3).

**Companion docs**:
- `kora_runtime_doppler_env_mapping.md` — full secret mapping
  table including the 3 Slack env vars consumed by this feature
- `kora_runtime_first_deploy_runbook.md` — daemon deploy sequence
  (Step 8 Path A is the Slack URL-verification smoke this runbook
  prepares for)
- `kora_docs/00_canonical_current_state/r2_amendments.md` —
  Amendment 1 (public webhook port — why \`/api/webhooks/slack/events\`
  is reachable from Slack's edge at all)

This runbook assumes the daemon is already deployed to
`kora-runtime-staging` with port 9118 public-exposed per
`fly.toml`'s second `[[services]]` block. If not, complete the
first-deploy runbook first.

---

## Step 1 — Create the Slack app

Two paths. Pick (B) if you want a paste-ready setup; pick (A) if
you prefer to walk the UI step by step.

### Path A — Manual creation via Slack UI

1. Visit [api.slack.com/apps](https://api.slack.com/apps).
2. Click **Create New App** → **From scratch**.
3. **App Name**: `Kora`.
4. **Workspace**: select Joshua's workspace.
5. Click **Create App**.

### Path B — App manifest YAML (recommended)

1. Visit [api.slack.com/apps](https://api.slack.com/apps).
2. Click **Create New App** → **From an app manifest**.
3. Select Joshua's workspace.
4. Paste the manifest below (replace `<KORA_PUBLIC_HOSTNAME>` with the
   actual Fly domain — `kora-runtime-staging.fly.dev` for staging,
   `kora-runtime.fly.dev` for prod):

```yaml
display_information:
  name: Kora
  description: Kora — Joshua's digital extension
  background_color: "#1a1a2e"
features:
  bot_user:
    display_name: Kora
    always_online: true
oauth_config:
  scopes:
    bot:
      # Post replies as the bot.
      - chat:write
      # Read DM history — needed for Slack to deliver event payloads
      # that include the text Joshua sent.
      - im:history
      # Basic DM channel read access.
      - im:read
      # Open IM channels for proactive sends (future buckets;
      # reactive replies don't strictly need this but the manifest
      # ships the full read+write scope so future features
      # don't require a re-install).
      - im:write
settings:
  event_subscriptions:
    request_url: https://<KORA_PUBLIC_HOSTNAME>/api/webhooks/slack/events
    bot_events:
      # The bot is a member of every IM channel a user opens with
      # it; this event fires for any message in those IM channels.
      - message.im
  org_deploy_enabled: false
  socket_mode_enabled: false
  token_rotation_enabled: false
```

5. Click **Next** → review → **Create**.

Slack will hit the Request URL with a `url_verification` challenge
on submission. The daemon's webhook router echoes back the
challenge (KR-D-DAEMON ST3); if Slack shows **Verified ✓**, the
public port + TLS cert + HMAC verification all worked
end-to-end. If you see **Failed**, jump to the Troubleshooting
section below.

---

## Step 2 — Install the app to the workspace

1. In your app's left sidebar → **Install App**.
2. Click **Install to Workspace**.
3. Slack prompts for OAuth consent listing the scopes from the
   manifest. Approve.
4. After install, copy the **Bot User OAuth Token** (begins with
   `xoxb-`). You'll set this as `KORA_SLACK_BOT_TOKEN` in Step 4.

---

## Step 3 — Grab the signing secret

1. App sidebar → **Basic Information** → scroll to **App
   Credentials** → **Signing Secret** → **Show** → copy the value.
2. This is what HMAC-verifies every webhook payload. You'll set it
   as `KORA_SLACK_SIGNING_SECRET` in Step 4. **DO NOT** commit it
   anywhere; it's a secret on par with the bot token.

---

## Step 4 — Set Doppler secrets

All three Slack env vars live in the `kora-runtime-gateways`
Doppler project (per `kora_runtime_doppler_env_mapping.md`).

Set them in the matching config (`stg` for staging, `prd` for
production):

```sh
CONFIG=stg   # or prd

# From Step 3.
doppler secrets set KORA_SLACK_SIGNING_SECRET="<32-hex-from-Slack>" \
  -p kora-runtime-gateways -c "$CONFIG"

# From Step 2.
doppler secrets set KORA_SLACK_BOT_TOKEN="xoxb-<your-bot-token>" \
  -p kora-runtime-gateways -c "$CONFIG"

# Joshua's Slack user ID — open Joshua's Slack profile → click the
# overflow (•••) menu → "Copy member ID" → paste here.
doppler secrets set KORA_SLACK_JOSHUA_USER_ID="U<10-char-id>" \
  -p kora-runtime-gateways -c "$CONFIG"
```

**`KORA_SLACK_JOSHUA_USER_ID` is fail-CLOSED critical**: if it's
unset, the handler drops ALL DMs (including Joshua's) with the
log line `[kora.slack_dm] KORA_SLACK_JOSHUA_USER_ID unset — all
messages dropped (fail-CLOSED)`. Verify the value before deploy.

### Push to Fly

```sh
doppler secrets download -p kora-runtime-gateways -c "$CONFIG" \
  --no-file --format docker > .env.doppler
flyctl secrets import < .env.doppler -a kora-runtime-staging
shred -u .env.doppler   # non-negotiable — see first-deploy runbook
```

The Fly app auto-restarts on `flyctl secrets import`; the new
secrets take effect on the next machine boot.

---

## Step 5 — Smoke test

After the Fly machine restarts:

1. In Slack, find the **Kora** app under **Apps** (left sidebar).
2. Click → **Messages** tab → send a DM: e.g. `ping`.
3. Within ~5 seconds, Kora replies in-thread:
   `Kora received: ping`.

If the reply doesn't arrive, jump to **Troubleshooting** below.

### Operator-side verification

In a separate terminal:

```sh
flyctl logs -a kora-runtime-staging
```

Look for:
- `[kora.webhook.slack] event accepted but not routed: type=...`
  appears for any non-message events Slack sends.
- `[kora.slack_dm.received] channel=D... user=UJOSHUA... ts=...`
  confirms the handler accepted Joshua's DM through all 5 filters.
- After the reply lands: no `[kora.slack_dm.reply_failed]` line.

JSONL record (operator can `flyctl ssh console` then `cat
/home/hermes/.kora/slack_dm_log.jsonl`):

```jsonl
{"received_at": "2026-05-22T...", "channel_id": "D...", ..., "handled_status": "received"}
{"sent_at":     "2026-05-22T...", "channel_id": "D...", ..., "send_status": "ok", "slack_message_ts": "..."}
```

Two entries per round-trip: one inbound, one outbound.

---

## Step 6 — Troubleshooting

### Slack shows "Failed" on URL verification

Symptoms: clicking **Save** on Event Subscriptions returns "Your
URL didn't respond with the value of the challenge parameter."

Diagnosis order:

1. **Daemon not running on the public port.** Verify:
   ```sh
   curl https://<KORA_PUBLIC_HOSTNAME>/healthz
   ```
   Expect `200 ok`. If not, the daemon isn't deployed or fly.toml's
   `[[services]]` block for 9118 isn't active.

2. **Signing secret mismatch.** Slack's URL-verification request
   IS signed. If `KORA_SLACK_SIGNING_SECRET` in Doppler/Fly
   doesn't match the value in Slack app's Basic Information, the
   HMAC verifier returns 401 + dead-letter logs:
   `[kora.webhook.dead_letter] source=slack reason=slack_signature_mismatch`.

   Fix: re-copy the signing secret from Slack UI → `doppler secrets
   set` → re-import to Fly → wait for restart → retry **Save** in
   Slack's Event Subscriptions panel.

3. **Timestamp skew.** Slack's 5-minute timestamp window is
   strict. If the Fly machine's clock is wildly off (rare),
   verification returns 408. Check `flyctl logs` for
   `slack_timestamp_too_old`.

4. **TLS cert not yet issued.** First-deploy: Fly may take ~30s
   to issue the Let's Encrypt cert. Wait + retry **Save**.

### No reply when Joshua DMs the bot

Diagnosis order (each writes a distinct log line in
`flyctl logs`):

1. **`[kora.slack_dm] KORA_SLACK_JOSHUA_USER_ID unset`** — Step 4
   missed setting this env. Re-set in Doppler + re-import to Fly.

2. **`handled_status: filtered_non_joshua, extra.actual_user_id:
   U...`** — `KORA_SLACK_JOSHUA_USER_ID` is set but doesn't match
   the user sending the DM. Verify the ID copied from Joshua's
   Slack profile matches the env value (case-sensitive).

3. **`handled_status: dropped_paused` or `dropped_stopped`** —
   the OperationalStateHolder is in PAUSED or STOPPED. Kora is
   intentionally not processing inbound during a pause. Resume via
   either an operator `kora_control` reset (substrate-side SECDEF
   call) or via the MCP tool `kora__request_state_transition`
   with `target_state: ready, reason: "<your reason>"`.

4. **`[kora.slack_dm.reply_failed] reason=slack_client_not_configured`** —
   `KORA_SLACK_BOT_TOKEN` is unset. The inbound is recorded but
   the daemon can't reply. Set the bot token in Doppler + re-import.

5. **`[kora.slack_dm.reply_failed] reason=transport:429`** —
   Slack rate-limited the bot. The client already retried once
   respecting `Retry-After`; if both attempts hit 429, the failure
   is logged + the JSONL entry has `send_status: failed`. Wait
   ~30s + the next DM should reply normally. If sustained, you may
   have multiple Kora deploys hitting Slack with the same token —
   verify only one Fly machine is running.

6. **`[kora.slack_dm.reply_failed] reason=slack_api:invalid_auth`** —
   bot token in Doppler is stale or revoked. Re-install the app
   to the workspace (Step 2) + grab a fresh `xoxb-` token + update
   Doppler.

7. **`handled_status: handler_error`** — unexpected exception
   inside the handler. Inspect the entry's `error` field for the
   exception repr + escalate as a Kora-runtime bug. The daemon
   returned 200 to Slack so no retry storm; safe to investigate
   without time pressure.

### Reply arrives but is wrong / truncated

- Echo format is intentionally **locked** at
  `Kora received: {text[:200]}` for ST2 — first 200 chars of the
  original message + the prefix. AI-driven replies are the
  KR-FEAT-SLACK-DM-AI follow-on bucket.
- If the echo content seems literally wrong (different text than
  what Joshua sent), compare the inbound JSONL `text` field to
  the outbound JSONL `text` field — they should differ only by
  the `Kora received: ` prefix.

### Bot replies in the wrong thread

- The handler threads under `event.thread_ts` if present, else
  `event.ts`. So a reply to an in-thread DM threads under that
  thread's root; a top-level DM gets a new thread. This is by
  design — verify by checking the outbound JSONL `thread_ts`
  field.

---

## Step 7 — Dual signing-secret env transition note

The Hermes-era Slack gateway at `gateway/platforms/slack.py` reads
`SLACK_SIGNING_SECRET` (legacy env var name). The Phase 2 daemon
webhook listener at `kora_cli/listeners/webhooks.py` reads
`KORA_SLACK_SIGNING_SECRET` (Kora-namespaced new env var name).

**Both come from the same Slack app's Basic Information page →
same secret value**, but the env var names differ because they're
consumed by different code paths.

During the transition (until the legacy Hermes gateway is fully
retired post-KR-2):

- **Operator sets BOTH env vars to the same value** in Doppler
  `kora-runtime-gateways`:
  ```sh
  SECRET="<signing-secret-from-slack-ui>"
  doppler secrets set KORA_SLACK_SIGNING_SECRET="$SECRET" \
    -p kora-runtime-gateways -c "$CONFIG"
  doppler secrets set SLACK_SIGNING_SECRET="$SECRET" \
    -p kora-runtime-gateways -c "$CONFIG"
  ```
- **Rotation**: when Slack rotates the signing secret (rare;
  operator-triggered via Slack app config), update BOTH env vars
  simultaneously. They MUST stay in sync — if one is fresh and
  the other stale, whichever code path the request hits will
  decide whether it verifies.
- **Post-KR-2 cleanup**: once `gateway/platforms/slack.py` is
  removed (or its `SLACK_SIGNING_SECRET` import is converted to
  `KORA_SLACK_SIGNING_SECRET`), the legacy env var becomes a
  no-op + can be removed from Doppler.

A consolidation refactor — fold both consumers onto a single env
var — is a tracked follow-on (noted in
`kora_runtime_doppler_env_mapping.md`).

---

## Step 8 — Promotion to production

Once staging passes the smoke test for a sustained period (Joshua
DMs Kora during normal work for a day; no `reply_failed` /
`filtered_non_joshua` entries that shouldn't be there):

1. Repeat Steps 4-5 with `-c prd` against `kora-runtime` (prod
   Fly app).
2. Update the Slack app's **Event Subscriptions** Request URL to
   point at the prod hostname.
3. Re-verify URL handshake (Slack shows **Verified ✓**).
4. Smoke-test from prod: Joshua DMs → echo reply.

**Joshua's call when to promote** — no automatic gate. Staging
runs as long as it's healthy.

---

## Step 9 — Operator obligations (ongoing)

- **Monitor `[kora.slack_dm.reply_failed]` rate**. Sustained
  non-zero rate indicates either token rotation needed, Slack
  app config drift, or sustained rate-limiting.
- **Monitor `handled_status: dropped_paused/_stopped`**. If
  Kora's PAUSED for an extended period, Joshua's DMs accumulate
  un-replied in the JSONL — operator should communicate the pause
  out-of-band (because Kora won't auto-acknowledge).
- **Rotate bot token + signing secret** per the secret-rotation
  cadence in `kora_runtime_doppler_env_mapping.md`. Slack doesn't
  enforce expiration but operator policy may; cap at yearly.
- **JSONL rotation**: append-only file at
  `<KORA_HOME>/slack_dm_log.jsonl` grows unbounded.
  Standard log-rotation tools (`logrotate` with copytruncate, or
  Fly's log-tailing to an external store) — operator-managed for
  now; deferred from this bucket per ST1's non-scope list.

---

## What's NOT in this feature (deferred)

- **AI-driven response generation** — `KR-FEAT-AI-RESPONSE-LOOP`
  bucket. The current echo is intentionally a scaffold proving
  the round-trip + audit + state-gating + rate-limit-retry shape.
- **Multi-turn conversation state** — replies thread under
  `event.thread_ts` but no concept of session state. Follow-on.
- **`kora__send_slack_dm` MCP tool** — exposing outbound DM via
  `/mcp` so other agents can ask Kora to send messages.
  `KR-MCP-SEND-TOOLS` bucket once the calling pattern is locked.
- **Multi-user support** — Joshua-only by design ("Kora is
  Joshua's digital extension"). Adding a second user requires a
  capability-gate design pass.
- **Slack interactivity** (buttons, modals, slash commands) —
  out of scope; the inbound surface is DM events only.

---

## Cross-references

- `kora_cli/listeners/webhooks.py:_handle_slack` — HMAC verifier
  + dispatch to `SlackDMHandler` (KR-D-DAEMON ST3 + KR-FEAT-SLACK-DM ST1)
- `kora_cli/handlers/slack_dm_handler.py` — 5-filter inbound +
  outbound echo reply (KR-FEAT-SLACK-DM ST1+ST2)
- `kora_cli/clients/slack_client.py` — outbound `chat.postMessage`
  with 429/5xx retry (KR-FEAT-SLACK-DM ST2)
- `kora_cli/listeners/webhook_signing.py:verify_slack_signature`
  — HMAC v0 verification (KR-D-DAEMON ST3)
- `kora_runtime_doppler_env_mapping.md` — full env table
- `kora_docs/00_canonical_current_state/r2_amendments.md` —
  why public port 9118 exists at all
