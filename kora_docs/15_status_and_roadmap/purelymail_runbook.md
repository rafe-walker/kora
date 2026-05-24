# Purelymail runbook — KR-FEAT-EMAIL + KR-FEAT-EMAIL-INBOUND-IMAP (Phase 2 Feature 3, both directions)

**Purpose**: end-to-end operator checklist for Kora's full
Purelymail integration — outbound SMTP **and** inbound IMAP
polling. Covers account verification, App Password mints (one or
two depending on Purelymail's scoping; see Part 2 Step 1), Doppler
secret wiring, both smoke tests, and troubleshooting mapped to the
result shapes the operator will actually see.

**Last updated**: 2026-05-22 (KR-FEAT-EMAIL-INBOUND-IMAP ST3).

**Scope**: **outbound + inbound**. Outbound shipped in KR-FEAT-EMAIL
(PRs #124/#127); inbound shipped in KR-FEAT-EMAIL-INBOUND-IMAP
(PRs #135/#138). This runbook covers both halves in two parts —
operators typically configure both at once since they share the
Purelymail account, the domain DNS records, and (likely) the App
Password.

**Companion docs**:
- `kora_runtime_doppler_env_mapping.md` — full secret mapping
  table including the Purelymail env vars consumed by this
  feature
- `kora_runtime_first_deploy_runbook.md` — daemon deploy sequence
- `slack_app_setup_runbook.md` — sibling Feature 5 runbook

This runbook assumes:
- Joshua already has a Purelymail account with 2FA enabled
- The domain (e.g. `stormhavenenterprises.com`) is already added
  to the Purelymail account
- The daemon is already deployed (per the first-deploy runbook),
  with Doppler integration live

---

# Part 1 — Outbound (SMTP)

Covers Kora sending email AS `kora@<domain>` via
`smtp.purelymail.com:465` SSL.

---

## Step 1 — Verify outbound deliverability (DNS records)

Purelymail's outbound deliverability depends on the domain having
the correct DNS records. These are one-time setup; verify they're
already in place before proceeding.

### Required records

In your DNS provider (e.g. Cloudflare, Route 53):

| Type | Host | Value | Purpose |
|---|---|---|---|
| MX | `@` | Purelymail-provided MX (typically `mx1.purelymail.com` priority 10 + `mx2.purelymail.com` priority 20) | Inbound mail routing (also required to send via Purelymail) |
| TXT | `@` | `v=spf1 include:_spf.purelymail.com -all` | SPF — authorizes Purelymail to send for the domain |
| TXT | `<selector>._domainkey.<domain>` | Purelymail-provided DKIM public key (look up in Purelymail dashboard → Domains → DKIM) | DKIM signing key |
| TXT | `_dmarc.<domain>` | `v=DMARC1; p=quarantine; rua=mailto:dmarc@<domain>` (or stricter) | DMARC policy |

### Verification command

From any shell:

```sh
DOMAIN=stormhavenenterprises.com
dig MX $DOMAIN +short
dig TXT $DOMAIN +short | grep spf1
dig TXT _dmarc.$DOMAIN +short
```

Each query should return non-empty. If anything is missing or
returns stale data, fix in your DNS provider + wait for TTL
propagation (typically 5-60 min) before the smoke test.

---

## Step 2 — Mint a Purelymail App Password

Kora authenticates to Purelymail's SMTP server using an App
Password (NOT Joshua's main account password). This requires 2FA
to be enabled on the account.

1. Sign in to [purelymail.com](https://purelymail.com).
2. Navigate to **Account** → **App Passwords** (or wherever
   Purelymail's UI exposes the App Password mint flow — name may
   differ slightly between dashboard versions).
3. Click **Generate New App Password**.
4. **Name**: `kora-runtime-outbound` (so the operator can identify
   it later for rotation).
5. **Bound email**: pick the mailbox Kora will send AS — typically
   `kora@stormhavenenterprises.com` or whatever alias Joshua wants
   as the sender identity. Note this address; it goes into
   `KORA_PUREMAIL_SMTP_USERNAME` below.
6. Copy the generated App Password. **You cannot view it again
   after closing the dialog.**
7. Paste into a secure scratch location while completing Step 3
   below (or paste directly into the Doppler CLI command — see
   next step).

**Why an App Password (not your main password)**: App Passwords
are scoped to SMTP/IMAP access. If the runtime credential is ever
compromised, you can rotate it independently of your main
account; you can also revoke without affecting Joshua's regular
email access.

---

## Step 3 — Configure Doppler secrets

The 5 outbound secrets live in the `kora-runtime-gateways` Doppler
project (same project as Slack tokens + the heartbeat probe
tokens). Set them in the deploy config Kora will use (`stg` for
staging, `prd` for production):

```sh
CONFIG=stg  # or prd
DOMAIN=stormhavenenterprises.com

# Required — fail-CLOSED on missing
doppler secrets set KORA_PUREMAIL_SMTP_USERNAME \
  "kora@$DOMAIN" \
  -p kora-runtime-gateways -c "$CONFIG"

doppler secrets set KORA_PUREMAIL_SMTP_APP_PASSWORD \
  "<paste App Password from Step 2>" \
  -p kora-runtime-gateways -c "$CONFIG"

doppler secrets set KORA_EMAIL_KORA_ALLOWED_FROM_DOMAINS \
  "$DOMAIN" \
  -p kora-runtime-gateways -c "$CONFIG"

# Optional — defaults are usually correct; only set if overriding
# (e.g. point at a staging SMTP relay for test, or switch to
# STARTTLS on 587 instead of SSL on 465).
# doppler secrets set KORA_PUREMAIL_SMTP_HOST "smtp.purelymail.com" \
#   -p kora-runtime-gateways -c "$CONFIG"
# doppler secrets set KORA_PUREMAIL_SMTP_PORT "465" \
#   -p kora-runtime-gateways -c "$CONFIG"
```

### Validation — confirm secrets land

```sh
for SECRET in KORA_PUREMAIL_SMTP_USERNAME \
              KORA_PUREMAIL_SMTP_APP_PASSWORD \
              KORA_EMAIL_KORA_ALLOWED_FROM_DOMAINS; do
  doppler secrets get "$SECRET" -p kora-runtime-gateways \
    -c "$CONFIG" --plain >/dev/null \
    && echo "OK   gateways:$SECRET" \
    || echo "MISS gateways:$SECRET"
done
```

Expected output: all three `OK`. Any `MISS` blocks the smoke
test.

### Redeploy to pick up the new secrets

The daemon reads env at process start. After setting Doppler
secrets, redeploy or restart:

```sh
flyctl deploy -a kora-runtime-staging   # staging
# or
flyctl deploy -a kora-runtime           # production
```

---

## Step 4 — Smoke test

The daemon's CLI exposes a one-shot send via the internal API.
After redeploying, exec into the running machine + trigger:

```sh
flyctl ssh console -a kora-runtime-staging
```

Once in the container:

```sh
python -c "
import asyncio
from kora_cli.clients.purelymail_client import send_email_internal

async def main():
    result = await send_email_internal(
        from_addr='kora@stormhavenenterprises.com',
        to=['joshua@stormhavenenterprises.com'],
        subject='Kora outbound smoke test',
        body_text='If you see this, Kora can send email. Reply at will.',
    )
    print(f'status={result.status}')
    print(f'message_id={result.message_id}')
    print(f'smtp_code={result.smtp_code}')
    print(f'retry_count={result.retry_count}')
    print(f'error={result.error}')

asyncio.run(main())
"
```

### Expected output

```
status=ok
message_id=<.....@stormhavenenterprises.com>
smtp_code=250
retry_count=0
error=None
```

### Verify the email arrived

Within ~30 seconds, the test email should land in Joshua's inbox at
`joshua@stormhavenenterprises.com`. Verify:

1. **From: header**: matches `kora@stormhavenenterprises.com`
2. **Subject**: `Kora outbound smoke test`
3. **Message-ID header**: matches the locally-generated id (in
   `<...@stormhavenenterprises.com>` shape). Most mail clients
   show this under message details or raw headers.
4. **DKIM**: the message passes DKIM verification (most clients
   show a small lock or "signed by" indicator).

### Verify the outbound JSONL log

```sh
tail -n 1 /home/hermes/.kora/email_outbound_log.jsonl
```

Should print one JSON line containing `"send_status": "ok"`,
`"smtp_code": 250`, and the matching `message_id`. **Body NOT in
the log** (only subject + recipients + meta — this is intentional;
operator pulls body from Purelymail's sent folder if needed).

---

## Step 5 — Troubleshooting

The client surfaces every failure through `SendResult.status`,
`SendResult.smtp_code`, and `SendResult.error`. Map operator's
observed values to the table below.

### `status=failed, smtp_code=535` (or auth error)

`535` = SMTP authentication failed. Causes:
- Wrong App Password in `KORA_PUREMAIL_SMTP_APP_PASSWORD`
- Wrong username in `KORA_PUREMAIL_SMTP_USERNAME` (must be the
  full email address bound to the App Password)
- App Password was revoked or rotated in Purelymail

**Fix**: re-mint App Password (Step 2), update Doppler secret
(Step 3), redeploy. The `error` field will contain the SMTP
server response (with the password redacted to `<REDACTED>`).

### `status=failed, smtp_code=421, retry_count=1`

`421` = service not available / channel closing. Greylisting or
upstream-side rate limit. The client already retried once (per
the SMTP retry policy); both attempts failed.

**Fix**: wait 5-15 minutes + retry. If persistent, check
Purelymail status page + verify the IP isn't blocklisted
(`postmaster.live.com`-style block-list lookup).

### `status=failed, smtp_code in {450, 451, 452}, retry_count=1`

Mailbox unavailable / local processing error / insufficient
storage. Transient upstream issue. Retry happened.

**Fix**: same as 421 — wait + retry.

### `status=failed, smtp_code in {550, 552, 553, 554}`

`5xx` permanent rejections. NOT retried (no point — deterministic).
Common causes:
- `550`: mailbox doesn't exist, recipient rejected, or sender
  domain failed SPF / DKIM / DMARC
- `552`: message too large
- `553`: from-address format invalid
- `554`: blocked (often spam-filter at recipient's end)

**Fix**: read `error` field for the specific message. For 550 SPF
failures, re-check Step 1 DNS records. For 552, reduce attachment
sizes (client also enforces 25 MiB total; recipient may have a
lower limit).

### `PurelymailRejectError: from_addr domain not in allowlist`

Caller passed a `from_addr` whose domain isn't in
`KORA_EMAIL_KORA_ALLOWED_FROM_DOMAINS`. Defense against accidental
wide-open sends.

**Fix**: either (a) use a from-domain that's in the allowlist, or
(b) add the new domain to the env var + redeploy.

### `PurelymailRejectError: too many recipients`

Caller passed >10 addresses in `to`. Defense against accidental
mass-send.

**Fix**: split the send into multiple calls. If genuinely needed,
this cap can be raised via a follow-on bucket; the 10-cap is
intentional for the AI-driven response loop where 10 is already
beyond any reasonable single-message audience.

### `PurelymailRejectError: per-attachment` or `total attachment`

Attachment exceeded the 10 MiB per-file or 25 MiB total batch cap.

**Fix**: shrink attachments or split across multiple sends.
Purelymail accepts up to its own server-side limit (typically
much larger), but Kora's per-call cap is intentionally tighter
than the server's to keep the AI response loop from accidentally
generating huge attachments.

### `status=failed, error contains "SMTPConnectError"`

TCP/SSL connection to `smtp.purelymail.com:465` failed.
- Network egress blocked (firewall, Fly machine network config)
- DNS resolution failure
- Purelymail SMTP server itself down

**Fix**: check Fly machine network egress, Purelymail status page.
For SSL handshake errors specifically (rare), try port 587 with
STARTTLS by setting `KORA_PUREMAIL_SMTP_PORT=587`.

### `status=failed, error contains "TimeoutError"`

Send took longer than the per-call 30s ceiling. Usually
upstream-slow / network-slow rather than a configuration issue.

**Fix**: retry. The client already retried once on timeout; if
it persists, check Purelymail status + Fly machine network
latency to `smtp.purelymail.com`.

---

## Step 6 — Rotation procedure

Rotate the App Password whenever:
- The Kora deployment is migrated to a new Fly app / region
- An operator suspects credential compromise
- Routine quarterly hygiene (aligned with `KORA_SERVICE_TOKEN`
  rotation in `token_rotation_runbook.md`)

Procedure:
1. Mint a new App Password (Step 2). DON'T revoke the old one yet.
2. Update Doppler with the new value (Step 3).
3. Redeploy.
4. Run the smoke test (Step 4). Confirm `status=ok`.
5. Revoke the old App Password in Purelymail dashboard.

The zero-downtime swap matters because the daemon reads env at
process start — between the Doppler update and the redeploy, the
running daemon still uses the old password. Revoking before the
redeploy would cause a window where outbound sends fail with
`535`.

---

---

# Part 2 — Inbound (IMAP polling)

Covers Kora receiving email AT `kora@<domain>` via
`imap.purelymail.com:993` SSL. The daemon polls Purelymail's INBOX
every 5 minutes (configurable), fetches UNSEEN messages, runs each
through a 5-step filter precedence (state gate → sender allowlist
→ recipient → spoofing → identity), and writes a JSONL audit
entry. No AI reply is drafted or sent from the inbound path (Lock
R3-8 (a) / KR-EMAIL-AUTOREPLY-BRANCH-REMOVAL); inbound mail is a
read-only signal that future consumers
(KR-INTENT-EMAIL-TO-SEA-TICKET) will project into Sea_Tickets.

Inbound is purely opt-in at two layers:
1. The IMAP client listener fail-softs on missing creds (daemon
   boots with inbound disabled).
2. The sender allowlist defaults to **fail-CLOSED DENY ALL** —
   nothing reaches identity check until the operator sets it.

---

## Part 2 Step 1 — Mint a Purelymail App Password for IMAP

Per the K-DG verification in ST1
(https://purelymail.com/docs/setup/technical confirmed
2026-05-22):

- Server: `imap.purelymail.com:993` SSL/TLS
- Auth: email + password (or App Password when 2FA is enabled,
  which Kora's deployments always are)

Per the 2FA docs (https://purelymail.com/docs/twoFactorAuthentication):

> *App passwords give full access to your email.*

This phrasing strongly suggests App Passwords are NOT scoped per
protocol — one App Password should work for both SMTP and IMAP.
The Purelymail docs don't confirm this explicitly, so the safe
default in this runbook is **mint a separate App Password named
`kora-runtime-inbound`** distinct from the outbound one from
Part 1 Step 2. If you'd rather verify reusability and use the
existing `kora-runtime-outbound` App Password for both, set both
Doppler secrets (`KORA_PUREMAIL_SMTP_APP_PASSWORD` and
`KORA_PUREMAIL_IMAP_APP_PASSWORD`) to the same value and confirm
with the Part 2 Step 3 smoke test before going to production.

Procedure (separate App Password — recommended default):

1. Sign in to [purelymail.com](https://purelymail.com).
2. Navigate to **Account** → **App Passwords**.
3. Click **Generate New App Password**.
4. **Name**: `kora-runtime-inbound` (for clean rotation later;
   the SMTP App Password keeps its `kora-runtime-outbound` name).
5. **Bound email**: same mailbox as outbound — typically
   `kora@stormhavenenterprises.com`. The IMAP client connects to
   THIS mailbox's INBOX.
6. Copy the App Password (cannot be re-viewed after the dialog
   closes).
7. Paste into Step 2 below.

---

## Part 2 Step 2 — Configure Doppler secrets (inbound)

7 new envs land in the same `kora-runtime-gateways` Doppler
project alongside the outbound ones. 4 are IMAP transport config;
3 are email-handler config (allowlist, recipient address, identity).

```sh
CONFIG=stg  # or prd
DOMAIN=stormhavenenterprises.com

# --- IMAP transport (4 envs) ---
# Required — fail-CLOSED on missing
doppler secrets set KORA_PUREMAIL_IMAP_USERNAME \
  "kora@$DOMAIN" \
  -p kora-runtime-gateways -c "$CONFIG"

doppler secrets set KORA_PUREMAIL_IMAP_APP_PASSWORD \
  "<paste App Password from Part 2 Step 1>" \
  -p kora-runtime-gateways -c "$CONFIG"

# Optional — defaults are usually correct
# doppler secrets set KORA_PUREMAIL_IMAP_HOST "imap.purelymail.com" \
#   -p kora-runtime-gateways -c "$CONFIG"
# doppler secrets set KORA_PUREMAIL_IMAP_PORT "993" \
#   -p kora-runtime-gateways -c "$CONFIG"

# --- Email handler config (3 envs) ---
# Sender allowlist — empty = fail-CLOSED DENY ALL
doppler secrets set KORA_EMAIL_SENDER_ALLOWLIST \
  "joshua@$DOMAIN" \
  -p kora-runtime-gateways -c "$CONFIG"

# Kora's receive address — the to[] header must include this
doppler secrets set KORA_EMAIL_KORA_ADDRESS \
  "kora@$DOMAIN" \
  -p kora-runtime-gateways -c "$CONFIG"

# Identity check — sender must match
doppler secrets set KORA_EMAIL_JOSHUA_ADDRESS \
  "joshua@$DOMAIN" \
  -p kora-runtime-gateways -c "$CONFIG"

# Optional cadence override (default 300s = 5 min)
# doppler secrets set KORA_EMAIL_IMAP_POLL_INTERVAL_SEC "300" \
#   -p kora-runtime-gateways -c "$CONFIG"
```

> **Removed**: `KORA_EMAIL_AUTO_REPLY` (Lock R3-8 (a) /
> KR-EMAIL-AUTOREPLY-BRANCH-REMOVAL). The handler no longer reads
> this env; if a legacy value remains in Doppler from before the
> removal, it's ignored cleanly. Operators may delete it at the
> next secret-rotation cadence (`doppler secrets delete
> KORA_EMAIL_AUTO_REPLY`).

### Validation

```sh
for SECRET in KORA_PUREMAIL_IMAP_USERNAME \
              KORA_PUREMAIL_IMAP_APP_PASSWORD \
              KORA_EMAIL_SENDER_ALLOWLIST \
              KORA_EMAIL_KORA_ADDRESS \
              KORA_EMAIL_JOSHUA_ADDRESS; do
  doppler secrets get "$SECRET" -p kora-runtime-gateways \
    -c "$CONFIG" --plain >/dev/null \
    && echo "OK   gateways:$SECRET" \
    || echo "MISS gateways:$SECRET"
done
```

All five should print `OK`. Any `MISS` blocks the smoke test —
the IMAP listener will boot but stay disabled OR every inbound
message will hit a fail-CLOSED filter.

### Redeploy

```sh
flyctl deploy -a kora-runtime-staging   # staging
# or
flyctl deploy -a kora-runtime           # production
```

After the redeploy completes, the daemon's IMAP listener
constructs a live `PurelymailIMAPClient` singleton; the heartbeat
scheduler starts firing `email.imap_poll` every 5 min.

---

## Part 2 Step 3 — Smoke test (Joshua → Kora)

1. From Joshua's normal mail client, send a regular email to
   `kora@stormhavenenterprises.com`.
   - **From**: `joshua@stormhavenenterprises.com` (must match
     `KORA_EMAIL_SENDER_ALLOWLIST` AND `KORA_EMAIL_JOSHUA_ADDRESS`)
   - **To**: `kora@stormhavenenterprises.com` (must match
     `KORA_EMAIL_KORA_ADDRESS`)
   - **Subject + body**: anything; the smoke test is "did it land
     in the JSONL with `handled_status: received`?"

2. Wait up to one poll cycle (5 min by default, less if you
   overrode `KORA_EMAIL_IMAP_POLL_INTERVAL_SEC`).

3. Verify the inbound JSONL log:

   ```sh
   flyctl ssh console -a kora-runtime-staging
   tail -n 1 /home/hermes/.kora/email_inbound_log.jsonl
   ```

   Expected: one JSON line containing
   `"handled_status": "received"`, the matching `subject`, and
   `"spoofing_check_skipped": true`. Body shows under
   `body_text_truncated_2k` (truncated to 2KB).

4. Verify the chain event hit the structured log:

   ```sh
   journalctl -u kora-runtime | grep "kora.email_inbound.received"
   ```

   (Or whatever log path Fly's machine uses; on Fly the daemon's
   stdout goes to `flyctl logs -a kora-runtime-staging`.)

   Expected: one line per received email with the uid, subject,
   body length, attachment count.

If both checks pass, inbound is live in receive-only mode.

---

## Part 2 Step 4 — Auto-reply (REMOVED — Lock R3-8 (a))

Previously Step 4 ("full loop: Joshua → Kora → Joshua, AUTO_REPLY
enabled") and Step 5 ("AUTO_REPLY opt-in cost trade-off") covered
enabling `KORA_EMAIL_AUTO_REPLY` so the handler would draft a
reasoning-engine reply for every identified Joshua email and send
it back via SMTP. Per Lock R3-8 (a) during the R3 walkthrough +
KR-EMAIL-AUTOREPLY-BRANCH-REMOVAL, that branch has been cut. The
handler no longer reads `KORA_EMAIL_AUTO_REPLY`, the reasoning
engine is no longer invoked from the email path, and no auto-reply
SMTP send is attempted.

What remains:

  * Inbound parsing + JSONL emission (Part 2 Step 3)
  * IMAP polling (same listener; unchanged)
  * Outbound SMTP for non-inbound-reply paths (Kora emailing
    artifacts / reports; driven from other modules using
    `PurelymailClient.send_email` directly — Part 1 still applies
    for those)

Future work: KR-INTENT-EMAIL-TO-SEA-TICKET will read the inbound
JSONL and project Joshua's emails into Sea_Tickets / scratchpad
items, replacing the auto-reply path with a structured persistence
path. The `cost_telemetry.ROUTE_EMAIL_INBOUND` literal remains
reserved for that future wiring.

---

## Part 2 Step 6 — Troubleshooting (inbound)

Map the operator's observed JSONL `handled_status` value to the
table below. The status enum is defined in
`kora_cli/handlers/email_inbound_handler.py` (`HANDLED_*`).

### `handled_status: received`

Success — email passed all 5 filters. Chain event was emitted.
No reply is sent (Lock R3-8 (a)); the inbound JSONL row is the
only side effect. Future KR-INTENT-EMAIL-TO-SEA-TICKET consumers
will read this row to project the email into a Sea_Ticket.

### `handled_status: filtered_paused` / `filtered_stopped`

The daemon's operational state was `PAUSED` or `STOPPED` when the
poll ran. The email was marked SEEN (terminal — we won't re-process
it when the state transitions back to ACTIVE).

**Fix**: bring the daemon back to `ACTIVE` via the operational-
state control surface; new emails received after the transition
will be processed normally. If Joshua needs to re-process the
dropped ones, he can re-send them.

### `handled_status: filtered_non_allowlist`

Sender not in `KORA_EMAIL_SENDER_ALLOWLIST` OR the env is unset
(empty = fail-CLOSED DENY ALL).

Check `extra.actual_sender` in the JSONL entry to see what address
was rejected.

**Fix**:
- If the env is unset: set it (see Part 2 Step 2)
- If the sender SHOULD be allowed: add to the comma-separated env
  + redeploy
- If the sender is genuinely spam/spoof: nothing — the filter
  worked

### `handled_status: filtered_wrong_recipient`

The parsed `to:` header doesn't include `KORA_EMAIL_KORA_ADDRESS`.
This usually means:
- Email was sent to a different mailbox at the same domain
  (e.g. operator's own address, BCC'd to Kora)
- Mailing-list traffic where Kora is on the list but not the
  primary recipient
- Env is unset or wrong

Check `extra.expected_recipient` + `extra.actual_recipients` in
the JSONL.

**Fix**:
- Confirm `KORA_EMAIL_KORA_ADDRESS` matches the address you want
  Kora to receive AT
- If you want Kora to process BCC mail, that's a follow-on bucket
  (current filter is strict to/cc-list inclusion)

### `handled_status: filtered_non_joshua`

Sender is on the allowlist + recipient is right, but sender's
address doesn't match `KORA_EMAIL_JOSHUA_ADDRESS`. This catches:
- Another allowlisted human who happens to be on the
  `KORA_EMAIL_SENDER_ALLOWLIST` but isn't Joshua specifically
- Env is unset (also fail-CLOSED)

**Fix**:
- Confirm `KORA_EMAIL_JOSHUA_ADDRESS` matches the From: header
  Joshua's mail client sets
- If `extra.reason == "joshua_address_env_unset"`: set the env
  + redeploy

### `handled_status: handler_error`

An unexpected exception fired inside the handler. The IMAP
message was **NOT** marked SEEN — the next poll will re-fetch
+ retry. The `error` field on the JSONL entry holds the
exception's `repr()`.

**Fix**:
- Check daemon logs for the full traceback
  (`flyctl logs -a kora-runtime-staging`)
- Common causes: malformed RFC822 (logged as parse warning),
  filesystem out-of-space on the JSONL path, unexpected
  attachment metadata shape
- If persistent for a specific email, manually mark it SEEN via
  Purelymail's webmail to stop the retry loop while you diagnose

### "Polls are running but no inbound JSONL entries ever appear"

Likely root causes:
- IMAP listener fail-soft kicked in (missing username/password
  env). Check daemon logs for
  `[kora.purelymail_imap_listener] IMAP auth env unset`.
- Poll task isn't registered. Check daemon logs for
  `[kora.heartbeat] started N periodic task(s)` — should include
  `email.imap_poll`.
- IMAP connect is failing each cycle. Check daemon logs for
  `[kora.email_inbound_imap] connect failed` lines.

### "IMAP connect failed: login refused"

App Password rejected. Causes:
- Wrong password in `KORA_PUREMAIL_IMAP_APP_PASSWORD`
- App Password revoked / rotated in Purelymail dashboard
- 2FA was disabled on the account (Purelymail rejects App
  Passwords when 2FA is off)
- Wrong username in `KORA_PUREMAIL_IMAP_USERNAME` (must be the
  full email address bound to the App Password)

**Fix**: re-mint App Password (Part 2 Step 1), update Doppler
(Part 2 Step 2), redeploy. The daemon log line will show
`<REDACTED>` where the password would have been (security
sanitizer).

### "Inbound emails are received but no reply is sent"

Expected behavior post Lock R3-8 (a) / KR-EMAIL-AUTOREPLY-BRANCH-REMOVAL.
The handler no longer drafts or sends auto-replies; inbound mail
is parsed, logged to `email_inbound_log.jsonl`, and the chain
event `[kora.email_inbound.received]` fires. That's the complete
processing path. Joshua's mail client will not see a reply from
Kora in response.

If a legacy `KORA_EMAIL_AUTO_REPLY` env value is still in Doppler
from before the removal, it's ignored — the handler doesn't read
it. The env can be deleted at the next secret-rotation cadence.

---

## Cross-references

- `kora_runtime_doppler_env_mapping.md` — canonical env mapping
  table (all Purelymail vars + the broader gateway secret family)
- `slack_app_setup_runbook.md` — sibling Feature 5 runbook;
  Slack DMs ship parallel to email both directions for Joshua's
  operator surface
- `kora_cli/clients/purelymail_client.py` — outbound SMTP client
  (Part 1)
- `kora_cli/clients/purelymail_imap_client.py` — inbound IMAP
  client (Part 2)
- `kora_cli/clients/purelymail_types.py` — `SendResult` +
  `ParsedIncomingEmail` + `AttachmentMeta` shapes
- `kora_cli/handlers/email_inbound_handler.py` — the 5-step filter
  precedence + JSONL log (no auto-reply path; Lock R3-8 (a))
- `kora_cli/listeners/email_inbound_imap_listener.py` — IMAP poll
  listener + `register_periodic_task("email.imap_poll", ...)`
