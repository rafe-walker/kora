"""Tests for ``kora_cli.listeners.webhook_dead_letter`` — KR-D-DAEMON ST3."""

from __future__ import annotations

import logging

import pytest

from kora_cli.listeners import webhook_dead_letter
from kora_cli.listeners.webhook_dead_letter import (
    _summarize_headers,
    emit_webhook_dead_letter,
)


def test_summarize_headers_filters_to_allowlist():
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer SECRET",  # NOT logged
        "Cookie": "session=...",  # NOT logged
        "User-Agent": "Slackbot 1.0",
        "x-slack-request-timestamp": "1700000000",
    }
    out = _summarize_headers(headers)
    assert "content-type" in out
    assert "user-agent" in out
    assert "x-slack-request-timestamp" in out
    assert "authorization" not in out
    assert "cookie" not in out


def test_summarize_headers_truncates_signature_values():
    """Signature values surface presence + a 12-char prefix; never
    the full HMAC."""
    sig = "v0=" + "f" * 64
    headers = {"X-Slack-Signature": sig, "X-Purelymail-Signature": "sha256=" + "a" * 64}
    out = _summarize_headers(headers)
    assert out["x-slack-signature"].endswith("...(truncated)")
    assert len(out["x-slack-signature"]) < len(sig)
    assert out["x-purelymail-signature"].endswith("...(truncated)")


def test_summarize_headers_lowercases_names():
    """Operator-side analysis works with lowercase keys, regardless of
    the casing the upstream framework used."""
    out = _summarize_headers({"CONTENT-TYPE": "application/json"})
    assert "content-type" in out


def test_emit_writes_warning_log_with_marker(caplog):
    caplog.set_level(logging.WARNING, logger=webhook_dead_letter.logger.name)
    emit_webhook_dead_letter(
        source="slack",
        reason="slack_signature_mismatch",
        headers={"Content-Type": "application/json"},
        peer_ip="203.0.113.7",
        request_id="req-123",
        body_bytes=42,
        now=1_700_000_000.0,
    )
    matching = [r for r in caplog.records if "kora.webhook.dead_letter" in r.getMessage()]
    assert len(matching) == 1
    msg = matching[0].getMessage()
    assert "source=slack" in msg
    assert "reason=slack_signature_mismatch" in msg
    assert "peer_ip=203.0.113.7" in msg
    assert "request_id=req-123" in msg
    assert "body_bytes=42" in msg


def test_emit_handles_missing_optional_fields(caplog):
    """peer_ip / request_id / body_bytes are optional."""
    caplog.set_level(logging.WARNING, logger=webhook_dead_letter.logger.name)
    emit_webhook_dead_letter(
        source="email",
        reason="purelymail_signature_missing",
        headers={},
    )
    msg = next(
        r.getMessage()
        for r in caplog.records
        if "kora.webhook.dead_letter" in r.getMessage()
    )
    assert "peer_ip=-" in msg
    assert "request_id=-" in msg
    assert "body_bytes=-" in msg


def test_emit_never_contains_body_content(caplog):
    """Even though the helper has no body arg, double-check no
    accidental body data sneaks in via headers (the allow-list
    excludes anything that could carry user content)."""
    caplog.set_level(logging.WARNING, logger=webhook_dead_letter.logger.name)
    emit_webhook_dead_letter(
        source="slack",
        reason="slack_signature_mismatch",
        headers={
            "Content-Type": "application/json",
            # Hypothetical leaky header.
            "X-Original-Body-Echo": "from=alice@example.com&secret=hunter2",
        },
    )
    msg = next(
        r.getMessage()
        for r in caplog.records
        if "kora.webhook.dead_letter" in r.getMessage()
    )
    assert "hunter2" not in msg
    assert "alice@example.com" not in msg
