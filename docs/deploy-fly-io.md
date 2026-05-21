# Kora deploy: Fly.io + Doppler (3-project credential split)

This is the operator runbook for deploying Kora to Fly.io. The shape of
the credential layout (3 separate Doppler projects rather than one) is
mandated by R2 §5 and R4.1 — credential blast-radius isolation, so one
project compromise cannot yield substrate access AND Anthropic auth AND
messaging-platform tokens at once.

## One-time setup

### 1. Doppler — 3 projects per R2 §5 / R4.1

Create three Doppler projects, each with `dev` / `stg` / `prd` configs:

| Project | Secrets | Rotation cadence |
|---|---|---|
| `kora-runtime-substrate` | `KORA_SERVICE_TOKEN`, `KORA_ISOKRON_DSN`, `KORA_DEFAULT_WORKSPACE_ID`, `KORA_SEA_MCP_ENDPOINT` | `wsk_*` rotates ~quarterly (current expires 2026-08-18) |
| `kora-runtime-anthropic` | `CLAUDE_CODE_OAUTH_TOKEN` | OAuth token rotates ~yearly |
| `kora-runtime-gateways` | `SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` (+ future email / telegram / etc.) | Per-platform |

**Critical:** `wsk_*` substrate tokens and `CLAUDE_CODE_OAUTH_TOKEN` must
NEVER live in the same Doppler project. The split is the security
boundary — compromising one project must not yield both surfaces.

### 2. Fly.io app + volume

```sh
flyctl apps create kora-runtime
flyctl volumes create kora_state --size 5 --region iad
```

The `5gb` volume size matches `fly.toml`'s `initial_size`. Adjust upward
if `~/.kora/sessions/` growth outpaces the 90-day session-store pruning.

### 3. Import secrets from all 3 Doppler projects into Fly

```sh
doppler secrets download -p kora-runtime-substrate -c prd --no-file --format docker  > .env.doppler
doppler secrets download -p kora-runtime-anthropic -c prd --no-file --format docker  >> .env.doppler
doppler secrets download -p kora-runtime-gateways  -c prd --no-file --format docker  >> .env.doppler
flyctl secrets import < .env.doppler
rm .env.doppler
```

The `rm .env.doppler` step is non-negotiable — the file contains every
plaintext token for the deploy. If a Fly secrets-import failure
interrupts the chain, manually `shred` the file before retrying.

### 4. Deploy

```sh
flyctl deploy
```

Fly's `release_command` runs `hermes doctor` before swapping in the new
machine. If `hermes doctor` exits non-zero, the deploy aborts and the
old machine keeps serving — fail-closed by design.

### 5. Verify boot

```sh
flyctl logs
```

Look for:

- `[kora] IsoKronMemoryProvider initialized for workspace_id=...`

If you see any of these, validation tripped:

- `ERROR: ANTHROPIC_API_KEY is set` — R4.1 §9.2 gate 2 blocked the boot.
  Remove the stray `ANTHROPIC_*` env var from the Fly secrets and
  redeploy. (See `docker/entrypoint.sh` — it exits 1 before the venv
  source.)
- `ERROR: Kora startup blocked — missing required env vars:` — one of
  the substrate / oauth vars is missing. Check which Doppler project it
  should have come from in the table above.

```sh
flyctl ssh console
# Inside the machine:
hermes status
# Confirm gateway running + Slack connected (if SLACK_GATEWAY_ENABLED=true).
```

## Operator-only admin web UI

The web UI on port `9119` is NOT publicly exposed (`fly.toml` declares
the internal port but no public services). To access:

```sh
flyctl proxy 9119:9119 -a kora-runtime
# Then open http://localhost:9119 in a browser.
```

The proxy tunnels through Fly's authenticated control plane — only
operators with `flyctl` access can reach the UI.

## Local dev — composing all 3 Doppler projects

Doppler runs nest cleanly. For local development against `dev` configs:

```sh
doppler run -p kora-runtime-substrate -c dev -- \
  doppler run -p kora-runtime-anthropic -c dev -- \
    doppler run -p kora-runtime-gateways -c dev -- \
      /opt/hermes/docker/entrypoint.sh hermes gateway
```

For `stg` or `prd` swap the `-c` value. Same composition works inside
CI, just substitute the entrypoint for whatever the test harness needs.

## Token rotation

| Secret | Cadence | Rotation procedure |
|---|---|---|
| `KORA_SERVICE_TOKEN` (`wsk_*`) | ~quarterly; current expires **2026-08-18** | TBD runbook (separate doc) |
| `CLAUDE_CODE_OAUTH_TOKEN` | ~yearly | Run `claude setup-token` to mint a new token, then `doppler secrets set CLAUDE_CODE_OAUTH_TOKEN=... -p kora-runtime-anthropic -c prd`, then `flyctl secrets set CLAUDE_CODE_OAUTH_TOKEN=...` and redeploy |
| Slack `*_TOKEN` / `*_SECRET` | Per Slack app policy | Rotate in the Slack app admin → `doppler secrets set ... -p kora-runtime-gateways -c prd` → redeploy |

## Why no public HTTP port?

Per R2 §5: Kora has no inbound HTTP service — the control plane is
substrate-mediated. The web UI on `9119` is an operator-only diagnostic
surface. Exposing it publicly would be:

1. Unnecessary (substrate-mediated control plane handles everything else).
2. A standing credential-exposure risk (the dashboard reveals provider
   API keys in its config view).
3. A standing attack surface (one more thing for security review to
   keep tabs on).

Operators reach the dashboard via `flyctl proxy`, which uses the same
Fly auth as `flyctl ssh` / `flyctl deploy`.
