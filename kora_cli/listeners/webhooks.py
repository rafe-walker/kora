"""Webhook listener — PUBLIC port 9118 (KR-D-DAEMON ST3).

Per PM Q1 ruling (R2 §5 amendment, kora_docs/00_canonical_current_state/r2_amendments.md):
the webhook plane is a SEPARATE FastAPI() instance bound to a
SEPARATE uvicorn process inside the daemon. Mounts ONLY:

  POST /api/webhooks/slack/events
  POST /api/webhooks/email/inbound

… so admin-UI / MCP / control-plane routes are structurally
impossible to surface on the public port: they live on a different
app object. HMAC verification is the auth boundary.

# Rate limiting

slowapi (chosen over fastapi-limiter — no Redis dependency, single-
machine daemon, in-memory token bucket sufficient). 60 req/min per
remote IP across all webhook routes; well above legitimate volume,
catches floods/probes.

# Listener lifecycle

Reuses ST2's WebListener pattern — programmatic uvicorn via
``Server.serve()`` + ``should_exit`` for graceful shutdown.

# Handler bodies = no-op in this ST

ST3 ships the routing + verification + dead-lettering + rate
limiting. Real handlers land in:
  - Feature 5 (Slack DM) — ``KR-FEAT-SLACK-DM`` bucket.
  - Feature 3 (Email reply) — ``KR-FEAT-EMAIL`` bucket.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from kora_cli.daemon import DEFAULT_SHUTDOWN_TIMEOUT, register_daemon_listener
from kora_cli.listeners.webhook_dead_letter import emit_webhook_dead_letter
from kora_cli.listeners.webhook_signing import (
    verify_purelymail_signature,
    verify_slack_signature,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults + env knobs
# ---------------------------------------------------------------------------

DEFAULT_WEBHOOK_HOST = "0.0.0.0"  # public bind — see R2 amendment
DEFAULT_WEBHOOK_PORT = 9118

# Rate limit. Bucket spec: "60 req/min/IP default; webhooks legit
# traffic is well under that." Operator can tighten via env.
DEFAULT_RATE_LIMIT = "60/minute"

SLACK_SECRET_ENV = "KORA_SLACK_SIGNING_SECRET"
EMAIL_SECRET_ENV = "KORA_PUREMAIL_HMAC_SECRET"
RATE_LIMIT_ENV = "KORA_WEBHOOK_RATE_LIMIT"


# ---------------------------------------------------------------------------
# Public FastAPI app — DELIBERATELY separate instance from admin
# ---------------------------------------------------------------------------


def _build_webhook_app() -> FastAPI:
    """Construct the public-port FastAPI app.

    Factory pattern (not module-level) so tests can spin up a fresh
    app per test without slowapi's process-wide rate-limit state
    bleeding across runs.
    """
    app = FastAPI(
        title="Kora Webhook Ingress",
        description=(
            "PUBLIC port. Only HMAC-verified webhook endpoints live "
            "here. Admin UI + MCP + control-plane routes are on the "
            "INTERNAL port 9119."
        ),
    )

    limiter = Limiter(key_func=get_remote_address, default_limits=[])
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    rate_limit = (
        os.environ.get(RATE_LIMIT_ENV, "").strip() or DEFAULT_RATE_LIMIT
    )

    # -----------------------------------------------------------------
    # Slack — POST /api/webhooks/slack/events
    # -----------------------------------------------------------------

    @app.post("/api/webhooks/slack/events")
    @limiter.limit(rate_limit)
    async def slack_events(request: Request):
        return await _handle_slack(request)

    # -----------------------------------------------------------------
    # Email inbound — POST /api/webhooks/email/inbound
    # -----------------------------------------------------------------

    @app.post("/api/webhooks/email/inbound")
    @limiter.limit(rate_limit)
    async def email_inbound(request: Request):
        return await _handle_email(request)

    # -----------------------------------------------------------------
    # Healthcheck — GET /healthz (no rate limit; used by Fly TCP check)
    # -----------------------------------------------------------------

    @app.get("/healthz")
    async def healthz():
        return PlainTextResponse("ok")

    return app


# ---------------------------------------------------------------------------
# Slack handler
# ---------------------------------------------------------------------------


async def _handle_slack(request: Request) -> Response:
    raw_body = await request.body()
    sig = request.headers.get("x-slack-signature")
    ts = request.headers.get("x-slack-request-timestamp")

    secret = os.environ.get(SLACK_SECRET_ENV, "").strip()
    outcome = verify_slack_signature(
        signing_secret=secret,
        raw_body=raw_body,
        signature_header=sig,
        timestamp_header=ts,
    )
    if not outcome.ok:
        emit_webhook_dead_letter(
            source="slack",
            reason=outcome.reason,
            headers=dict(request.headers),
            peer_ip=_peer_ip(request),
            request_id=request.headers.get("x-request-id"),
            body_bytes=len(raw_body),
        )
        # 408 specifically for timestamp-too-old per bucket spec; 401
        # for everything else.
        if outcome.reason == "slack_timestamp_too_old":
            return JSONResponse(
                {"error": outcome.reason},
                status_code=status.HTTP_408_REQUEST_TIMEOUT,
            )
        return JSONResponse(
            {"error": outcome.reason},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # URL-verification handshake — Slack's initial app-install ping.
    # Echo the challenge inline; this happens once per workspace.
    try:
        import json

        payload = json.loads(raw_body)
    except Exception:
        # Verified-but-not-JSON shouldn't happen for Slack events;
        # log as a curiosity + 200 so Slack doesn't retry forever.
        logger.info("[kora.webhook.slack] verified non-JSON body; ignoring")
        return JSONResponse({"ok": True})

    if isinstance(payload, dict) and payload.get("type") == "url_verification":
        challenge = payload.get("challenge", "")
        return PlainTextResponse(challenge)

    # KR-FEAT-SLACK-DM ST1 — route verified `event_callback` payloads
    # to the Slack-DM handler. The handler owns Kora-specific filtering
    # (identity / channel-type / bot / subtype / state-gate) + JSONL
    # persistence + chain-event emit. Belt-and-suspenders exception
    # guard at the listener boundary: the handler wraps its own body
    # too, but if SlackDMHandler construction itself fails, we still
    # need to 200 Slack to prevent its aggressive retries.
    if isinstance(payload, dict) and payload.get("type") == "event_callback":
        try:
            from kora_cli.handlers.slack_dm_handler import SlackDMHandler

            handler = SlackDMHandler()
            await handler.handle_event(payload)
        except Exception as exc:
            logger.warning(
                "[kora.webhook.slack] handler raised %r — dead-lettering",
                exc,
            )
            emit_webhook_dead_letter(
                source="slack",
                reason=f"handler_error: {type(exc).__name__}",
                headers=dict(request.headers),
                peer_ip=_peer_ip(request),
                request_id=request.headers.get("x-request-id"),
                body_bytes=len(raw_body),
            )
        return JSONResponse({"ok": True})

    # Other Slack event-wrapper types we don't route (e.g.
    # rate_limit, app_rate_limited). Log + 200 OK.
    logger.info(
        "[kora.webhook.slack] event accepted but not routed: type=%s",
        payload.get("type") if isinstance(payload, dict) else "(unknown)",
    )
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Email handler
# ---------------------------------------------------------------------------


async def _handle_email(request: Request) -> Response:
    raw_body = await request.body()
    sig = request.headers.get("x-purelymail-signature")

    secret = os.environ.get(EMAIL_SECRET_ENV, "").strip()
    outcome = verify_purelymail_signature(
        signing_secret=secret,
        raw_body=raw_body,
        signature_header=sig,
    )
    if not outcome.ok:
        emit_webhook_dead_letter(
            source="email",
            reason=outcome.reason,
            headers=dict(request.headers),
            peer_ip=_peer_ip(request),
            request_id=request.headers.get("x-request-id"),
            body_bytes=len(raw_body),
        )
        return JSONResponse(
            {"error": outcome.reason},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # Real handler in Feature 3 — ST3 scaffolding just acknowledges.
    logger.info(
        "[kora.webhook.email] inbound accepted: bytes=%d", len(raw_body)
    )
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _peer_ip(request: Request) -> Optional[str]:
    """Best-effort client IP. Trusts X-Forwarded-For ONLY if the
    immediate peer is Fly's edge (we don't know that here, so
    return ``client.host`` and let operator-side log analysis
    handle X-Forwarded-For with caller knowledge)."""
    client = request.client
    return client.host if client else None


# ---------------------------------------------------------------------------
# Listener (uvicorn lifecycle)
# ---------------------------------------------------------------------------


class WebhookListener:
    """Owns the second uvicorn server task for the public webhook app."""

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._server = None  # uvicorn.Server | None
        self._serve_task: Optional[asyncio.Task] = None
        self._app: Optional[FastAPI] = None

    async def startup(self) -> None:
        import uvicorn

        # Per-startup app instance — fresh slowapi state.
        self._app = _build_webhook_app()

        config = uvicorn.Config(
            self._app,
            host=self._host,
            port=self._port,
            log_level="warning",
            # PUBLIC port — DO honor X-Forwarded-For headers Fly's
            # edge inserts. (The admin-port listener disables this
            # for loopback-only checks; this public listener wants
            # the real peer IP for rate limiting + dead-letter logs.)
            proxy_headers=True,
        )
        self._server = uvicorn.Server(config)
        self._serve_task = asyncio.create_task(
            self._server.serve(), name="webhook-listener:serve"
        )
        for _ in range(50):  # up to 1s
            if getattr(self._server, "started", False):
                break
            await asyncio.sleep(0.02)
        if not getattr(self._server, "started", False):
            raise RuntimeError(
                f"webhook listener: uvicorn did not enter started state "
                f"on {self._host}:{self._port} within 1s"
            )
        logger.info(
            "[kora.webhook] uvicorn bound on %s:%d (PUBLIC)",
            self._host,
            self._port,
        )

    async def shutdown(self) -> None:
        if self._server is None or self._serve_task is None:
            return
        self._server.should_exit = True
        try:
            await self._serve_task
        except asyncio.CancelledError:
            pass
        logger.info("[kora.webhook] uvicorn stopped")


def _factory():
    host = (
        os.environ.get("KORA_WEBHOOK_HOST", DEFAULT_WEBHOOK_HOST).strip()
        or DEFAULT_WEBHOOK_HOST
    )
    port_raw = os.environ.get("KORA_WEBHOOK_PORT", "").strip()
    try:
        port = int(port_raw) if port_raw else DEFAULT_WEBHOOK_PORT
    except ValueError as exc:
        raise SystemExit(
            f"KORA_WEBHOOK_PORT must be an int; got {port_raw!r}: {exc}"
        )
    listener = WebhookListener(host=host, port=port)
    return (listener.startup, listener.shutdown, DEFAULT_SHUTDOWN_TIMEOUT)


register_daemon_listener("webhooks", _factory)
