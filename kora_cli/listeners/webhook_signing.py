"""HMAC verification helpers for inbound webhooks (KR-D-DAEMON ST3).

Pure functions — no FastAPI, no logging, no side effects. Lets the
verifiers be unit-tested with vector inputs + reused across routes.

# Slack

Slack signs requests with a v0 scheme:
  - header ``X-Slack-Signature``: ``v0=<HMAC-SHA256-hex>``
  - header ``X-Slack-Request-Timestamp``: unix seconds
  - base string: ``v0:<timestamp>:<raw_body>``
  - HMAC-SHA256 with the app's signing secret over the base string
  - reject if timestamp older than ``SLACK_TIMESTAMP_TOLERANCE_SECONDS``
    (5 minutes, replay protection)

# Purelymail

The actual Purelymail webhook signing scheme is NOT well-documented
publicly. The bucket spec asked us to research + flag any divergence
from "shared secret HMAC-SHA256 over body". Implementation here is
the conservative default — operator MUST verify against Purelymail's
current docs at integration time + adjust header name / encoding as
needed.

Default assumption:
  - header ``X-Purelymail-Signature``: ``sha256=<HMAC-SHA256-hex>``
    OR a plain hex digest (we accept both for forward-compat)
  - HMAC-SHA256 with the shared secret over the raw body bytes

Flag pinned in the PR body + the R2 amendment doc — when Feature 3
lands real handler logic, this verifier may need a one-line update
to match Purelymail's actual scheme.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass


SLACK_TIMESTAMP_TOLERANCE_SECONDS: int = 5 * 60  # 5 minutes


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    """Result of verifying a webhook signature.

    ``ok`` is the bottom-line accept/reject. ``reason`` is a short
    machine-readable code suitable for inclusion in the dead-letter
    record (no user content; no secrets).
    """

    ok: bool
    reason: str


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


def verify_slack_signature(
    *,
    signing_secret: str,
    raw_body: bytes,
    signature_header: str | None,
    timestamp_header: str | None,
    now: float | None = None,
    tolerance_seconds: int = SLACK_TIMESTAMP_TOLERANCE_SECONDS,
) -> VerificationOutcome:
    """Verify a Slack Events webhook signature.

    Returns ``VerificationOutcome(ok=False, reason="...")`` on any
    failure path — the caller maps to the appropriate HTTP status
    (401 for signature mismatch, 408 for timestamp drift).

    ``now`` is injectable for tests; defaults to ``time.time()``.
    """
    if not signing_secret:
        return VerificationOutcome(False, "slack_secret_unset")
    if not signature_header:
        return VerificationOutcome(False, "slack_signature_missing")
    if not timestamp_header:
        return VerificationOutcome(False, "slack_timestamp_missing")

    try:
        ts = int(timestamp_header)
    except (TypeError, ValueError):
        return VerificationOutcome(False, "slack_timestamp_malformed")

    now_ts = now if now is not None else time.time()
    if abs(now_ts - ts) > tolerance_seconds:
        return VerificationOutcome(False, "slack_timestamp_too_old")

    # Slack's v0 scheme.
    if not signature_header.startswith("v0="):
        return VerificationOutcome(False, "slack_signature_bad_scheme")
    expected_digest = signature_header[len("v0=") :]

    base_string = f"v0:{ts}:".encode("utf-8") + raw_body
    computed = hmac.new(
        signing_secret.encode("utf-8"), base_string, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed, expected_digest):
        return VerificationOutcome(False, "slack_signature_mismatch")
    return VerificationOutcome(True, "ok")


# ---------------------------------------------------------------------------
# Purelymail (default assumption — see module docstring)
# ---------------------------------------------------------------------------


def verify_purelymail_signature(
    *,
    signing_secret: str,
    raw_body: bytes,
    signature_header: str | None,
) -> VerificationOutcome:
    """Verify an inbound-email webhook signature using the conservative
    default scheme (HMAC-SHA256 over raw body).

    Accepts both ``sha256=<hex>`` and bare hex digest in the header
    for forward compatibility — Purelymail's exact format may vary.
    """
    if not signing_secret:
        return VerificationOutcome(False, "purelymail_secret_unset")
    if not signature_header:
        return VerificationOutcome(False, "purelymail_signature_missing")

    presented = signature_header.strip()
    if presented.startswith("sha256="):
        presented = presented[len("sha256=") :]

    computed = hmac.new(
        signing_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed, presented):
        return VerificationOutcome(False, "purelymail_signature_mismatch")
    return VerificationOutcome(True, "ok")
