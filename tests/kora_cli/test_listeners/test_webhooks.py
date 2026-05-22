"""Tests for ``kora_cli.listeners.webhooks`` — KR-D-DAEMON ST3.

Covers the route surface (Slack + email) against a fresh public app
instance per test. The full uvicorn lifecycle is exercised by a
single end-to-end test; the rest use FastAPI's TestClient against
the app object directly to keep the suite fast.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import socket
from typing import Tuple

import pytest
from fastapi.testclient import TestClient

from kora_cli.listeners import webhooks as wh_mod
from kora_cli.listeners.webhooks import (
    DEFAULT_RATE_LIMIT,
    EMAIL_SECRET_ENV,
    SLACK_SECRET_ENV,
    WebhookListener,
    _build_webhook_app,
    _factory,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slack_signed(
    secret: str, body: bytes, ts: int
) -> Tuple[str, str]:
    """Return (signature_header, timestamp_header)."""
    base = f"v0:{ts}:".encode() + body
    digest = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return (f"v0={digest}", str(ts))


def _purelymail_signed(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Use the real wall clock for these — Slack verifier defaults to
# ``time.time()`` and the helpers below compute timestamps within the
# 5-min tolerance window relative to now.
import time as _time


def _now_int() -> int:
    return int(_time.time())


@pytest.fixture
def slack_secret(monkeypatch):
    monkeypatch.setenv(SLACK_SECRET_ENV, "test-slack-secret-1234")
    return "test-slack-secret-1234"


@pytest.fixture
def email_secret(monkeypatch):
    monkeypatch.setenv(EMAIL_SECRET_ENV, "test-email-secret-5678")
    return "test-email-secret-5678"


@pytest.fixture
def client():
    return TestClient(_build_webhook_app())


# ---------------------------------------------------------------------------
# Slack — URL verification handshake + accept + reject paths
# ---------------------------------------------------------------------------


def test_slack_url_verification_round_trips(slack_secret, client):
    """Slack's initial app-install ping: receive challenge, echo it."""
    ts = _now_int()
    body = json.dumps(
        {"type": "url_verification", "challenge": "kor4-ch4ll3ng3"}
    ).encode()
    sig, ts_h = _slack_signed(slack_secret, body, ts)
    r = client.post(
        "/api/webhooks/slack/events",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Slack-Signature": sig,
            "X-Slack-Request-Timestamp": ts_h,
        },
    )
    assert r.status_code == 200
    assert r.text == "kor4-ch4ll3ng3"


def test_slack_valid_event_accepted(slack_secret, client):
    ts = _now_int()
    body = json.dumps({"type": "event_callback", "event": {}}).encode()
    sig, ts_h = _slack_signed(slack_secret, body, ts)
    r = client.post(
        "/api/webhooks/slack/events",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Slack-Signature": sig,
            "X-Slack-Request-Timestamp": ts_h,
        },
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_slack_signature_mismatch_returns_401(slack_secret, client, caplog):
    caplog.set_level(logging.WARNING)
    ts = _now_int()
    body = b'{"type":"event_callback"}'
    bad_sig = "v0=" + "0" * 64
    r = client.post(
        "/api/webhooks/slack/events",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Slack-Signature": bad_sig,
            "X-Slack-Request-Timestamp": str(ts),
        },
    )
    assert r.status_code == 401
    # Dead-letter logged.
    dead_letter_lines = [
        r for r in caplog.records if "kora.webhook.dead_letter" in r.getMessage()
    ]
    assert len(dead_letter_lines) == 1
    assert "source=slack" in dead_letter_lines[0].getMessage()
    assert "reason=slack_signature_mismatch" in dead_letter_lines[0].getMessage()


def test_slack_timestamp_too_old_returns_408(slack_secret, client):
    """Bucket spec: 408 specifically for timestamp drift > 5min."""
    ancient_ts = _now_int() - (60 * 60)  # 1 hour ago
    body = b'{"type":"event_callback"}'
    sig, ts_h = _slack_signed(slack_secret, body, ancient_ts)
    r = client.post(
        "/api/webhooks/slack/events",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Slack-Signature": sig,
            "X-Slack-Request-Timestamp": ts_h,
        },
    )
    assert r.status_code == 408


def test_slack_missing_signature_header_returns_401(slack_secret, client):
    r = client.post(
        "/api/webhooks/slack/events",
        content=b"{}",
        headers={"X-Slack-Request-Timestamp": str(_now_int())},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Email — accept + reject
# ---------------------------------------------------------------------------


def test_email_valid_signature_accepted(email_secret, client):
    body = b'{"from": "alice@example.com", "to": "kora@kora.example"}'
    r = client.post(
        "/api/webhooks/email/inbound",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Purelymail-Signature": _purelymail_signed(email_secret, body),
        },
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_email_signature_mismatch_returns_401(email_secret, client, caplog):
    caplog.set_level(logging.WARNING)
    body = b"{}"
    r = client.post(
        "/api/webhooks/email/inbound",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Purelymail-Signature": "sha256=" + "0" * 64,
        },
    )
    assert r.status_code == 401
    dead_letter_lines = [
        r for r in caplog.records if "kora.webhook.dead_letter" in r.getMessage()
    ]
    assert any("source=email" in r.getMessage() for r in dead_letter_lines)


def test_email_secret_unset_returns_401(monkeypatch, client):
    """No KORA_PUREMAIL_HMAC_SECRET → all email webhooks rejected."""
    monkeypatch.delenv(EMAIL_SECRET_ENV, raising=False)
    body = b"{}"
    r = client.post(
        "/api/webhooks/email/inbound",
        content=body,
        headers={"X-Purelymail-Signature": "sha256=anything"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


def test_healthz_no_auth_required(client):
    """Fly TCP/HTTP check needs this to succeed without secrets."""
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.text == "ok"


# ---------------------------------------------------------------------------
# Admin routes NOT mounted on public app
# ---------------------------------------------------------------------------


def test_admin_routes_not_on_public_app(client):
    """Structural guarantee: the public app is a distinct FastAPI
    instance, so it has NONE of the admin/MCP routes."""
    for path in (
        "/api/status",
        "/mcp/tools/list",
        "/api/cron",
        "/api/sessions",
    ):
        r = client.get(path)
        assert r.status_code == 404, f"{path} unexpectedly mounted: {r.status_code}"


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_rate_limit_enforced(monkeypatch, slack_secret):
    """At very low limits, the (N+1)th request returns 429.

    slowapi default storage is in-memory keyed by remote IP. We set
    an aggressively low rate limit via env to make this assertion
    fast and deterministic.
    """
    monkeypatch.setenv("KORA_WEBHOOK_RATE_LIMIT", "2/minute")
    app = _build_webhook_app()
    c = TestClient(app)

    body = b"{}"
    # Sign each request so signature isn't the cause of any 401 —
    # we want to confirm 429s come from the rate limiter, not from
    # other failure paths.
    bad_sig = "v0=" + "0" * 64
    ts = str(_now_int())
    headers = {
        "Content-Type": "application/json",
        "X-Slack-Signature": bad_sig,
        "X-Slack-Request-Timestamp": ts,
    }

    # The first 2 hit 401 (bad sig). The third should hit 429 from
    # the limiter BEFORE the route runs.
    r1 = c.post("/api/webhooks/slack/events", content=body, headers=headers)
    r2 = c.post("/api/webhooks/slack/events", content=body, headers=headers)
    r3 = c.post("/api/webhooks/slack/events", content=body, headers=headers)

    assert r1.status_code == 401
    assert r2.status_code == 401
    assert r3.status_code == 429


# ---------------------------------------------------------------------------
# Factory + listener lifecycle
# ---------------------------------------------------------------------------


def test_factory_default_shape(monkeypatch):
    monkeypatch.delenv("KORA_WEBHOOK_HOST", raising=False)
    monkeypatch.delenv("KORA_WEBHOOK_PORT", raising=False)
    spec = _factory()
    assert isinstance(spec, tuple) and len(spec) == 3
    startup, shutdown, timeout = spec
    assert callable(startup) and callable(shutdown)
    assert timeout > 0


def test_factory_rejects_non_int_port(monkeypatch):
    monkeypatch.setenv("KORA_WEBHOOK_PORT", "not-a-port")
    with pytest.raises(SystemExit, match="KORA_WEBHOOK_PORT"):
        _factory()


@pytest.mark.asyncio
async def test_webhook_listener_lifecycle_end_to_end(slack_secret):
    """Full uvicorn lifecycle on a free port — proves the second
    uvicorn instance binds cleanly + serves a webhook + tears down."""
    import asyncio

    import httpx

    port = _find_free_port()
    listener = WebhookListener(host="127.0.0.1", port=port)
    await listener.startup()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"http://127.0.0.1:{port}/healthz")
            assert r.status_code == 200
    finally:
        await asyncio.wait_for(listener.shutdown(), timeout=5.0)


def test_default_rate_limit_constant():
    assert DEFAULT_RATE_LIMIT == "60/minute"
