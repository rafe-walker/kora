# R2 amendments — running record

This document records authoritative amendments to the R2 spec
(`kora_docs/21_program_council/Kora_v1_Program_Spec_R2*.md`) made
after R2 sealed. Each amendment carries a date, the bucket / PR that
introduced it, the threat-model rationale, and the exact spec text
being narrowed or extended.

The R2 spec's adversarial round closed before Phase 2's webhook
ingress was scoped; some §5 language was authored against a
control-plane-only deployment model and needs scoping clarifications
as Kora's surface grows. This file is where those clarifications
live so future readers don't see "R2 says no public ports" and
treat that as contradicting a public webhook port that ships under
this amendment.

---

## Amendment 1 — Public webhook port (KR-D-DAEMON ST3)

**Date**: 2026-05-22
**Bucket**: `kora_docs/17_cc_bucket_prompts/KR-D-DAEMON_always_alive_runtime.md` (commit 54032c6) — §5 Q1 decision; PR `feat(kora): KR-D-DAEMON ST3 — Slack + email webhook routers + R2 §5 amendment`.
**PM ruling**: Q1 "(D) with two-uvicorn refinement".

### Original R2 §5 text being amended

> Kora has no inbound HTTP service — the control plane is substrate-mediated (pull-based via `kora_control`), and the admin web UI is operator-only via `flyctl proxy`. No public ports on the `kora-runtime` Fly app.

(Paraphrased from `docs/deploy-fly-io.md:123` and `fly.toml:21-24`; the R2 spec text is the design ancestor of both.)

### Amendment text

> **Scope of "no public ports"**: applies to **control-plane traffic** (admin UI, MCP, `kora_control` commands, ledger writes, chain emits). The control plane stays internal-only — reached via `flyctl proxy` on port 9119.
>
> **Webhook ingress is a separate traffic class** with HMAC as the auth boundary (Slack signing secret per Slack's v0 scheme, Purelymail HMAC-SHA256 over body) and per-IP rate limiting (slowapi default 60 req/min/IP) as the flood backstop. The webhook plane MAY be exposed publicly on a **dedicated port** (currently 9118), served by a **separate FastAPI() instance + separate uvicorn process** inside the daemon, mounting ONLY:
>
> - `POST /api/webhooks/slack/events`
> - `POST /api/webhooks/email/inbound`
>
> Plus `GET /healthz` (no auth) for the Fly HTTP healthcheck.
>
> **The two FastAPI apps are NEVER merged.** Admin routes are structurally impossible to surface on the public port: they live on a different app object, mounted by a different listener, served by a different uvicorn process. Shared daemon-coordinator dependencies (chain emitter, future ledger writer) are injected by DI, not by shared app state.

### Threat model rationale

The R2 §5 "no public ports" rule was authored against a single-class threat model: **arbitrary inbound traffic against the control plane**. The control plane has no application-layer auth (it presumes Fly's internal flycast routing as the trust boundary); exposing it publicly would defeat that.

Webhook ingress is a **different threat model**:

1. **Application-layer auth is mandatory** for webhook traffic and is enforced at the request boundary (HMAC verification on every request; bypass impossible without the signing secret).
2. **The signing secret is the security control**, not network-layer isolation. Compromise of the signing secret is the threat to defend against — and the rotation procedure for that secret lives in `kora_docs/15_status_and_roadmap/token_rotation_runbook.md` (extends naturally to Slack + Purelymail secrets).
3. **The data flow is one-way (ingress only)** — the webhook handlers don't return user data; the response is at most `{"ok": true}` or a Slack URL-verification challenge echo. The route surface gives an attacker no leverage to extract state.
4. **Blast-radius isolation is structural**: the public uvicorn knows only how to dispatch to the two webhook handlers. Admin routes are not in its route table at all.

Per-IP rate limiting (60 req/min default; tunable via `KORA_WEBHOOK_RATE_LIMIT`) caps flood blast radius without restricting legitimate webhook volume (Slack + Purelymail event rates are normally <1 req/min/source).

### Operator obligations

Operators MUST:

- Set `KORA_SLACK_SIGNING_SECRET` in Doppler project `kora-runtime-gateways` BEFORE deploy (else Slack webhooks reject 401, dead-letter logged).
- Set `KORA_PUREMAIL_HMAC_SECRET` in Doppler project `kora-runtime-gateways` BEFORE deploy.
- Monitor `[kora.webhook.dead_letter]` log lines via Fly logs or OPS panel — sustained dead-letter rate indicates either a misconfigured Slack/Purelymail app pointing at a wrong endpoint, or active probing.
- Verify Purelymail's actual webhook signing scheme at integration time — `kora_cli/listeners/webhook_signing.py:verify_purelymail_signature` ships the conservative default (HMAC-SHA256 over body, header `X-Purelymail-Signature` with optional `sha256=` prefix); flag any divergence in a follow-on PR.

### Cross-references

- `fly.toml` — second `[[services]]` block exposing port 9118 with public ports 443 (TLS) + 80 (force_https) + http_check on `/healthz`.
- `kora_cli/listeners/webhooks.py` — the public-port FastAPI factory + WebhookListener uvicorn lifecycle.
- `kora_cli/listeners/webhook_signing.py` — HMAC verifiers (pure functions; vector-tested).
- `kora_cli/listeners/webhook_dead_letter.py` — structured-log dead-letter recorder.
- `kora_docs/15_status_and_roadmap/token_rotation_runbook.md` — extends to cover Slack + Purelymail secret rotation.

### Future work flagged

- **Persistent dead-letter records** — the bucket spec called for `kora_operation_ledger` rows on verification failure. The ledger's schema (substrate migration 0093) requires `work_attempt_id`/`workspace_id`/`ticket_id` — all tied to Sea_Ticket dispatch. A webhook dead-letter has none of those. Today's implementation uses structured logging (`logger.warning("[kora.webhook.dead_letter] ...")`). When substrate-team adds either (a) a `webhook_dead_letters` table, (b) a `kora.webhook.dead_letter` chain-event vocab literal, or (c) a permissive ledger shape, the runtime extension is a small change in `webhook_dead_letter.py` (the log-line emit is the stable seam).
- **Purelymail scheme verification** — see operator-obligations bullet above.
