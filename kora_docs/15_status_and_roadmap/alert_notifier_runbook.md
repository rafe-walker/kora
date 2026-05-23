# Alert notifier runbook — KR-ALERT-NOTIFY ST1 + ST2

**Purpose**: operator checklist for tuning Kora's push-notification
behavior. Covers the 6 envs that gate cadence + throttling +
channel routing + mode, plus the dev-only test tool for
verifying Slack DM and email channels are wired correctly.

**Last updated**: 2026-05-23 (KR-ALERT-NOTIFY ST2).

**Scope**: the **push-notification layer** sitting between the
alerts panel (#145) and Joshua's inboxes (Slack DM + email).
Alerts themselves are surfaced regardless of notifier config —
this runbook is about how the operator gets pinged when they're
not staring at the cockpit.

**Companion docs**:
- `purelymail_runbook.md` — outbound SMTP + inbound IMAP config
  (the notifier uses the same Purelymail account for the email
  channel)
- `slack_app_setup_runbook.md` — Slack DM bot setup
- `kora_runtime_doppler_env_mapping.md` — full secret table

Default behavior:
- Cycle every 3 min
- Per-category cooldown 30 min
- Burst threshold 5 alerts/cycle
- Immediate mode (no digest queue)

---

## Step 1 — Pick a notification mode

`KORA_ALERT_NOTIFY_MODE` is the top-level switch. Two values:

| Value | Critical | Warning | Info |
|---|---|---|---|
| `immediate` (default) | Slack DM | Slack DM | Email |
| `digest` | Slack DM (immediate) | Queued for digest | Queued for digest |

**Pick `immediate`** when Joshua wants every actionable signal as
fast as Kora can compute it. Default.

**Pick `digest`** when Joshua wants a less-interrupted day and
doesn't need warning/info alerts as they fire — Kora batches
them into one email per `KORA_ALERT_NOTIFY_DIGEST_INTERVAL_SEC`
(default 24h). Criticals always fire immediately regardless of
mode — there's no "queue a critical" because the whole point is
operator action NOW.

```sh
# digest mode (more focus; warnings + info batched daily)
doppler secrets set KORA_ALERT_NOTIFY_MODE digest \
  -p kora-runtime-gateways -c stg

# immediate mode (every alert pings live)
doppler secrets set KORA_ALERT_NOTIFY_MODE immediate \
  -p kora-runtime-gateways -c stg
```

---

## Step 2 — Tune throttling (ST2)

The notifier ships with three throttling mechanisms layered on
top of the set-diff dedup from ST1. All three are envvars; all
three default to operator-friendly values. **Tune only if
defaults misbehave for your traffic pattern.**

### Per-category cooldown

`KORA_ALERT_NOTIFY_CATEGORY_COOLDOWN_SEC` (default `1800` = 30 min)

Same `category` can't dispatch within this window. Prevents
storms — e.g. 5 services flipping in/out over 30 min produce
ONE notification (the first), not 5. Cooldown is per-category,
not per-alert-id, so distinct alerts within the same category
share the window.

Disable: set to `0`. Every newly-firing alert dispatches
regardless of category history. Use only for debugging.

```sh
# Disable cooldown for verification testing
doppler secrets set KORA_ALERT_NOTIFY_CATEGORY_COOLDOWN_SEC 0 \
  -p kora-runtime-gateways -c stg
```

### Burst dampening

`KORA_ALERT_NOTIFY_BURST_THRESHOLD` (default `5`)

When more than `threshold` newly-firing alerts pass the cooldown
filter in a single cycle, the notifier sends ONE summary Slack
DM instead of N individual ones. The summary lists severity
counts on the first line and each alert title below.

**Trade-off**: criticals are bundled into the summary too —
spec §2 ST2 is firm on no carve-out. If 6 alerts fire including
2 criticals, the operator gets one summary Slack DM listing all
6 (criticals named first). Rationale: a 6-alert cycle is itself
a critical operator signal. One scannable summary beats six
buzzes. If the operator needs per-alert detail they open the
cockpit (link included in the summary when
`KORA_COCKPIT_URL` is set).

### Daily digest

When `KORA_ALERT_NOTIFY_MODE=digest`, warning + info alerts queue
in memory; the daemon flushes the queue once per
`KORA_ALERT_NOTIFY_DIGEST_INTERVAL_SEC` (default 86400 = 24h)
as a single email grouped by severity + category.

The queue is **in-memory only**. Daemon restart loses queued
warnings/info. They'll re-emerge on the first post-restart
cycle if still active.

```sh
# Hourly digest instead of daily
doppler secrets set KORA_ALERT_NOTIFY_DIGEST_INTERVAL_SEC 3600 \
  -p kora-runtime-gateways -c stg
```

---

## Step 3 — Cycle cadence

`KORA_ALERT_NOTIFY_INTERVAL_SEC` (default `180` = 3 min)

How often the notifier diffs the active alert set against the
previous cycle. **Lower** = faster reactions, more aggregator
work + API churn. **Higher** = laggy notifications but cheaper.

3 min balances responsiveness against burn. Don't drop below 60s
unless you're debugging — the aggregator itself does in-memory
work, but each cycle that fires a new alert does a Slack /
SMTP roundtrip.

```sh
doppler secrets set KORA_ALERT_NOTIFY_INTERVAL_SEC 60 \
  -p kora-runtime-gateways -c stg
```

---

## Step 4 — Required envs (channel wiring)

These were set up during KR-MCP-SEND-TOOLS and the Purelymail
runbook. The notifier reuses them:

| Env | Required for | Notes |
|---|---|---|
| `KORA_SLACK_JOSHUA_USER_ID` | Slack DM channel | The notifier passes this as `channel_id` to `chat.postMessage`; Slack auto-resolves to Joshua's DM channel |
| `KORA_EMAIL_JOSHUA_ADDRESS` | Email channel | The `to:` for both individual info-level sends and the digest |
| `KORA_EMAIL_KORA_ADDRESS` | Email channel | The `from:` Kora sends as |
| `KORA_COCKPIT_URL` | Optional — adds a cockpit link to emails + burst summaries | e.g. `https://kora.example/cockpit` |

If any required env is unset, the notifier's dispatch will fail.
The alert ID still enters the dedup set so the next cycle
doesn't re-spam; the audit JSONL records the failure. The alert
remains visible in the cockpit either way.

---

## Step 5 — Operator verification (dev-only)

The `kora__send_test_alert` MCP tool fires a synthetic alert
through the live notifier. Bypasses dedup + cooldown + burst +
digest throttling — every call dispatches.

**Refuses on `KORA_DEPLOY_ENV=prd`** to prevent synthetic-alert
pollution of production audit trails. Use only in staging / dev.

```sh
# Fire a critical (Slack DM) test alert
curl -X POST https://kora.example/mcp \
  -H "Authorization: Bearer $KORA_MCP_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
      "name": "kora__send_test_alert",
      "arguments": {"severity": "critical"}
    }
  }'
```

Returns a `SendTestAlertResult` with `success`, `severity`,
`channel`, `alert_id`, optional `error`, `caller_actor_kind`,
`deploy_env`.

**Verify all 3 channels before declaring the deployment ready:**

```sh
# 1. critical → Slack DM
... arguments: {"severity": "critical"}
# Joshua should see a red Slack DM within seconds.

# 2. warning → Slack DM (same channel; different emoji)
... arguments: {"severity": "warning"}

# 3. info → email
... arguments: {"severity": "info"}
# Joshua should see a `[Kora alert] info: ...` email within seconds.
```

Each test alert appears in the audit panel under
`notification.dispatched` with severity + status. Operator can
confirm dispatch happened even if Slack / SMTP delivery dropped
the message.

---

## Step 6 — Audit trail

Every dispatch attempt (success OR failure) emits a JSONL entry
on the `notification.dispatched` audit seam:

```json
{
  "emitted_at": "...",
  "seam": "notification.dispatched",
  "details": {
    "channel": "slack_dm" | "email",
    "alert_id": "<stable id>",
    "severity": "critical" | "warning" | "info" | "burst" | "digest",
    "category": "<aggregator category>" | "burst_summary" | "digest_email",
    "status": "ok" | "failed",
    "error": "<exception type>" (only on failed),
    // Burst summary entries additionally include:
    "burst_count": 7,
    "burst_alert_ids": [...]
    // Digest entries additionally include:
    "digest_count": 12,
    "digest_alert_ids": [...]
  }
}
```

The audit panel surfaces these alongside the other 4 audit seams
(mcp.tool_called / webhook.dead_letter / slack_dm.reply_failed /
reasoning.tool_called).

**Diagnostic patterns**:

- `status: failed, error: RuntimeError` after Slack/email envs
  changed → re-check `KORA_SLACK_JOSHUA_USER_ID` /
  `KORA_EMAIL_JOSHUA_ADDRESS` / `KORA_EMAIL_KORA_ADDRESS`.
- Many `cooldown_suppressed > 0` entries in the cycle log → a
  category is flapping faster than the 30-min cooldown. Either
  tune cooldown OR find the underlying instability.
- `severity: burst` repeatedly → bursts are common; consider
  raising `KORA_ALERT_NOTIFY_BURST_THRESHOLD` or moving to
  digest mode.

---

## Troubleshooting

### "Alerts fire in the cockpit but I never get pinged"

Check, in order:
1. `KORA_SLACK_JOSHUA_USER_ID` set + matches Joshua's real Slack ID
2. The Slack client listener booted (daemon logs show
   `[kora.slack_client_listener] SlackClient constructed`)
3. Fire `kora__send_test_alert` with severity=critical — does the
   test alert arrive?
4. Inspect the audit panel for `notification.dispatched` rows;
   `status: failed` rows include the error type.

### "I keep getting the same alert"

Probably means the alert is resolving + re-firing across cycles.
Check the cockpit — does the alert toggle between active and
not-active? If yes, the underlying source signal is flapping.
The cooldown is per-category so distinct ids in the same category
will still flap-suppress; but if the category itself appears and
disappears outside the cooldown window, the de-dup set considers
each emergence "new."

Fix: tune the underlying source. Cost ladder flap = adjust
thresholds in `agent/cost_state_holder.py`. Probe flap = tune
the probe's pass/fail boundary.

### "Bursts arrive but criticals get bundled into the summary"

That's the documented spec §2 ST2 behavior. If operator wants
criticals always individual: file a follow-on bucket to add a
carve-out. Default is intentional (a 6-alert cycle is a meta-
signal; the summary names the criticals at the top).

### "Daemon restarted and I got re-pinged for already-active alerts"

PM Q3 default — `last_alert_ids` is in-memory only. Fresh boot
treats all currently-active alerts as "newly firing." If this
becomes annoying, file a bucket to add JSONL persistence for
the dedup set.

---

## Cross-references

- `kora_cli/alerts/notifier.py` — AlertNotifier source
- `kora_cli/listeners/alert_notifier_listener.py` — listener +
  periodic tasks
- `kora_cli/listeners/mcp_tools.py` — `kora__send_test_alert`
  source
- `purelymail_runbook.md` — email channel config
- `slack_app_setup_runbook.md` — Slack DM channel config
- `kora_runtime_doppler_env_mapping.md` — all envs in one table
