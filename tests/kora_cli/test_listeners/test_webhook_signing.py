"""Tests for ``kora_cli.listeners.webhook_signing`` — KR-D-DAEMON ST3.

Vector tests for the Slack v0 + Purelymail HMAC-SHA256 verifiers.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from kora_cli.listeners.webhook_signing import (
    SLACK_TIMESTAMP_TOLERANCE_SECONDS,
    verify_purelymail_signature,
    verify_slack_signature,
)


# ---------------------------------------------------------------------------
# Helpers — generate valid signatures for inputs
# ---------------------------------------------------------------------------


def _slack_sig(secret: str, ts: int, body: bytes) -> str:
    base = f"v0:{ts}:".encode() + body
    digest = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return f"v0={digest}"


def _purelymail_sig(secret: str, body: bytes, prefixed: bool = True) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}" if prefixed else digest


# ---------------------------------------------------------------------------
# Slack — accept paths
# ---------------------------------------------------------------------------


def test_slack_valid_signature_accepts():
    secret = "test-secret"
    ts = 1_700_000_000
    body = b'{"type":"event_callback"}'
    sig = _slack_sig(secret, ts, body)
    outcome = verify_slack_signature(
        signing_secret=secret,
        raw_body=body,
        signature_header=sig,
        timestamp_header=str(ts),
        now=ts,
    )
    assert outcome.ok is True
    assert outcome.reason == "ok"


def test_slack_url_verification_handshake_signature():
    """URL-verification challenge body is a tiny JSON; signature
    verification works just like any other event."""
    secret = "s"
    ts = 1_700_000_100
    body = b'{"type":"url_verification","challenge":"abc"}'
    sig = _slack_sig(secret, ts, body)
    outcome = verify_slack_signature(
        signing_secret=secret,
        raw_body=body,
        signature_header=sig,
        timestamp_header=str(ts),
        now=ts,
    )
    assert outcome.ok is True


# ---------------------------------------------------------------------------
# Slack — reject paths
# ---------------------------------------------------------------------------


def test_slack_secret_unset_rejects():
    outcome = verify_slack_signature(
        signing_secret="",
        raw_body=b"{}",
        signature_header="v0=deadbeef",
        timestamp_header="1700000000",
        now=1_700_000_000,
    )
    assert outcome.ok is False
    assert outcome.reason == "slack_secret_unset"


def test_slack_missing_signature_header_rejects():
    outcome = verify_slack_signature(
        signing_secret="s",
        raw_body=b"{}",
        signature_header=None,
        timestamp_header="1700000000",
        now=1_700_000_000,
    )
    assert outcome.reason == "slack_signature_missing"


def test_slack_missing_timestamp_rejects():
    outcome = verify_slack_signature(
        signing_secret="s",
        raw_body=b"{}",
        signature_header="v0=deadbeef",
        timestamp_header=None,
        now=1_700_000_000,
    )
    assert outcome.reason == "slack_timestamp_missing"


def test_slack_malformed_timestamp_rejects():
    outcome = verify_slack_signature(
        signing_secret="s",
        raw_body=b"{}",
        signature_header="v0=deadbeef",
        timestamp_header="not-a-number",
        now=1_700_000_000,
    )
    assert outcome.reason == "slack_timestamp_malformed"


def test_slack_timestamp_too_old_rejects():
    """Replay-protection: timestamp older than 5min."""
    ts = 1_700_000_000
    secret = "s"
    body = b"{}"
    sig = _slack_sig(secret, ts, body)
    now = ts + SLACK_TIMESTAMP_TOLERANCE_SECONDS + 1
    outcome = verify_slack_signature(
        signing_secret=secret,
        raw_body=body,
        signature_header=sig,
        timestamp_header=str(ts),
        now=now,
    )
    assert outcome.reason == "slack_timestamp_too_old"


def test_slack_timestamp_too_new_also_rejects():
    """Skew protection covers BOTH directions of drift."""
    ts = 1_700_000_000
    secret = "s"
    body = b"{}"
    sig = _slack_sig(secret, ts, body)
    now = ts - SLACK_TIMESTAMP_TOLERANCE_SECONDS - 1
    outcome = verify_slack_signature(
        signing_secret=secret,
        raw_body=body,
        signature_header=sig,
        timestamp_header=str(ts),
        now=now,
    )
    assert outcome.reason == "slack_timestamp_too_old"


def test_slack_signature_bad_scheme_rejects():
    """Anything not starting with 'v0='."""
    outcome = verify_slack_signature(
        signing_secret="s",
        raw_body=b"{}",
        signature_header="v1=deadbeef",
        timestamp_header="1700000000",
        now=1_700_000_000,
    )
    assert outcome.reason == "slack_signature_bad_scheme"


def test_slack_signature_mismatch_rejects():
    """Right scheme, wrong HMAC."""
    outcome = verify_slack_signature(
        signing_secret="s",
        raw_body=b"{}",
        signature_header="v0=0000000000000000000000000000000000000000000000000000000000000000",
        timestamp_header="1700000000",
        now=1_700_000_000,
    )
    assert outcome.reason == "slack_signature_mismatch"


def test_slack_signature_mismatch_with_wrong_secret():
    """Same body + ts, but secret differs → mismatch."""
    ts = 1_700_000_000
    body = b'{"x":1}'
    sig = _slack_sig("right-secret", ts, body)
    outcome = verify_slack_signature(
        signing_secret="wrong-secret",
        raw_body=body,
        signature_header=sig,
        timestamp_header=str(ts),
        now=ts,
    )
    assert outcome.reason == "slack_signature_mismatch"


# ---------------------------------------------------------------------------
# Purelymail
# ---------------------------------------------------------------------------


def test_purelymail_valid_signature_with_prefix_accepts():
    secret = "email-secret"
    body = b'{"from":"alice@x"}'
    outcome = verify_purelymail_signature(
        signing_secret=secret,
        raw_body=body,
        signature_header=_purelymail_sig(secret, body, prefixed=True),
    )
    assert outcome.ok is True


def test_purelymail_valid_signature_without_prefix_accepts():
    """Forward-compat: bare hex accepted in case Purelymail's
    actual format omits the prefix."""
    secret = "s"
    body = b"{}"
    outcome = verify_purelymail_signature(
        signing_secret=secret,
        raw_body=body,
        signature_header=_purelymail_sig(secret, body, prefixed=False),
    )
    assert outcome.ok is True


def test_purelymail_secret_unset_rejects():
    outcome = verify_purelymail_signature(
        signing_secret="",
        raw_body=b"{}",
        signature_header="sha256=deadbeef",
    )
    assert outcome.reason == "purelymail_secret_unset"


def test_purelymail_signature_missing_rejects():
    outcome = verify_purelymail_signature(
        signing_secret="s",
        raw_body=b"{}",
        signature_header=None,
    )
    assert outcome.reason == "purelymail_signature_missing"


def test_purelymail_signature_mismatch_rejects():
    outcome = verify_purelymail_signature(
        signing_secret="right",
        raw_body=b"{}",
        signature_header=_purelymail_sig("wrong", b"{}"),
    )
    assert outcome.reason == "purelymail_signature_mismatch"
