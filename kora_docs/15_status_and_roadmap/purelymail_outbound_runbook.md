# Purelymail outbound runbook — KR-FEAT-EMAIL (Phase 2 Feature 3, outbound-only)

**Purpose**: end-to-end operator checklist from "Purelymail account
configured" to "Kora sends an email and it arrives in Joshua's
inbox." Covers Purelymail account verification, App Password mint,
Doppler secret wiring, smoke test, and troubleshooting mapped to
the `SendResult` fields the operator will actually see.

**Last updated**: 2026-05-22 (KR-FEAT-EMAIL ST2).

**Scope**: **outbound-only**. Per the bucket's double-STOP-ASK
descope (Purelymail has no inbound webhooks AND no REST send API),
inbound email handling is deferred to the
`KR-FEAT-EMAIL-INBOUND-IMAP` bucket whenever Joshua has a concrete
product use case for Kora receiving email. Outbound ships now via
SMTP.

**Companion docs**:
- `kora_runtime_doppler_env_mapping.md` — full secret mapping
  table including the 5 Purelymail env vars consumed by this
  feature
- `kora_runtime_first_deploy_runbook.md` — daemon deploy sequence
- `KR-FEAT-EMAIL-INBOUND-IMAP_deferred.md` (kora-docs) — design
  for the future inbound side

This runbook assumes:
- Joshua already has a Purelymail account with 2FA enabled
- The domain (e.g. `stormhavenenterprises.com`) is already added
  to the Purelymail account
- The daemon is already deployed (per the first-deploy runbook),
  with Doppler integration live

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

## Inbound — deferred

This runbook covers OUTBOUND only. Kora's inbound email handling
was descoped during the bucket's STOP-ASK cycle (Purelymail has
no inbound webhooks). When Joshua has a concrete use case for
Kora receiving email, the deferred bucket
`KR-FEAT-EMAIL-INBOUND-IMAP_deferred.md` (in `kora-docs`) captures
the IMAP-polling design that will replace the webhook architecture.

Until then, anyone who emails `kora@stormhavenenterprises.com`
will see their message land in the Purelymail mailbox where it
can be read manually; the daemon does not yet process inbound
messages.

---

## Cross-references

- `kora_runtime_doppler_env_mapping.md` — canonical env mapping
  table (5 Purelymail vars + the broader gateway secret family)
- `slack_app_setup_runbook.md` — sibling Feature 5 runbook;
  Slack DMs ship parallel to email outbound for Joshua's
  operator surface
- `KR-FEAT-EMAIL-INBOUND-IMAP_deferred.md` — future inbound side
- `kora_cli/clients/purelymail_client.py` — the client this
  runbook configures
- `kora_cli/clients/purelymail_types.py` — `SendResult` shape
  referenced in the troubleshooting table
