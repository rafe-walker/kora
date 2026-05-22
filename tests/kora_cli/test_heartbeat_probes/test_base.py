"""Base / helper tests (KR-FEAT-HEARTBEAT ST1)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from kora_cli.heartbeat_probes.base import (
    PROBE_TIMEOUT_SECONDS,
    resolve_env,
    sanitize_error,
    snapshot_for_auth_missing,
    snapshot_for_timeout,
    snapshot_for_unexpected_error,
    with_timeout,
)
from kora_cli.heartbeat_probes.types import (
    SERVICE_STATUSES,
    ServiceHealthSnapshot,
)


# ---------------------------------------------------------------------------
# Status enum + snapshot Pydantic shape
# ---------------------------------------------------------------------------


def test_service_statuses_includes_unknown():
    """KR-FEAT-HEARTBEAT extends the TS contract's 3-value enum with
    "unknown" (covers auth-missing + timeout + probe-loop crash)."""
    assert set(SERVICE_STATUSES) == {"healthy", "degraded", "unhealthy", "unknown"}


def test_snapshot_rejects_unknown_keys():
    """K-DG drift discipline — Pydantic ``extra="forbid"`` catches
    probe authors silently widening the shape without coordinating
    with the FE."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ServiceHealthSnapshot(
            name="x",
            status="healthy",
            last_check_at=datetime.now(timezone.utc),
            wat_typo=True,  # type: ignore[call-arg]
        )


def test_snapshot_status_literal_enforced():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ServiceHealthSnapshot(
            name="x",
            status="not-a-status",  # type: ignore[arg-type]
            last_check_at=datetime.now(timezone.utc),
        )


# ---------------------------------------------------------------------------
# resolve_env — empty/whitespace treated as unset (Doppler corner case)
# ---------------------------------------------------------------------------


def test_resolve_env_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("TEST_PROBE_VAR", raising=False)
    assert resolve_env("TEST_PROBE_VAR") is None


def test_resolve_env_returns_none_when_empty_string(monkeypatch):
    monkeypatch.setenv("TEST_PROBE_VAR", "")
    assert resolve_env("TEST_PROBE_VAR") is None


def test_resolve_env_returns_none_when_whitespace_only(monkeypatch):
    monkeypatch.setenv("TEST_PROBE_VAR", "   \n  ")
    assert resolve_env("TEST_PROBE_VAR") is None


def test_resolve_env_strips_and_returns_value(monkeypatch):
    monkeypatch.setenv("TEST_PROBE_VAR", "  ghp_real  \n")
    assert resolve_env("TEST_PROBE_VAR") == "ghp_real"


# ---------------------------------------------------------------------------
# sanitize_error — token redaction discipline
# ---------------------------------------------------------------------------


def test_sanitize_error_redacts_single_token():
    text = "auth failed: Bearer ghp_realvalue123 rejected"
    out = sanitize_error(text, "ghp_realvalue123")
    assert "ghp_realvalue123" not in out
    assert "<REDACTED>" in out


def test_sanitize_error_redacts_multiple_tokens():
    text = "auth1=token_a auth2=token_b"
    out = sanitize_error(text, "token_a", "token_b")
    assert "token_a" not in out
    assert "token_b" not in out


def test_sanitize_error_skips_none_and_empty_tokens():
    text = "harmless"
    out = sanitize_error(text, None, "", "  ")
    # Empty tokens skipped; whitespace token would replace whitespace
    # which is a bug — so only literally-empty + None are skipped.
    # Whitespace token "  " would replace " " in text but our text
    # has no whitespace to replace, so output equals input.
    assert "harmless" in out


def test_sanitize_error_preserves_unrelated_text():
    text = "endpoint unreachable: HTTP 503"
    out = sanitize_error(text, "secret_token")
    assert out == text


def test_sanitize_error_handles_empty_text():
    assert sanitize_error("", "secret_token") == ""


# ---------------------------------------------------------------------------
# snapshot_for_auth_missing
# ---------------------------------------------------------------------------


def test_snapshot_for_auth_missing_carries_env_var_names_not_values():
    """SECURITY: the error string lists ENV VAR NAMES never values.
    Even on auth-missing path, no token-shaped string appears."""
    snap = snapshot_for_auth_missing(name="vercel", env_var="KORA_VERCEL_API_TOKEN")
    assert snap.status == "unknown"
    assert snap.latency_ms is None
    assert "KORA_VERCEL_API_TOKEN" in snap.error
    # Sanity: no token-value-shaped substring
    assert "Bearer" not in snap.error
    assert "ghp_" not in snap.error


def test_snapshot_for_auth_missing_with_extra_envs():
    snap = snapshot_for_auth_missing(
        name="supabase",
        env_var="KORA_SUPABASE_ANON_KEY",
        extra_envs=("KORA_SUPABASE_URL",),
    )
    assert "KORA_SUPABASE_ANON_KEY" in snap.error
    assert "KORA_SUPABASE_URL" in snap.error


# ---------------------------------------------------------------------------
# snapshot_for_timeout + snapshot_for_unexpected_error
# ---------------------------------------------------------------------------


def test_snapshot_for_timeout_shape():
    snap = snapshot_for_timeout(name="vercel")
    assert snap.status == "unknown"
    assert snap.latency_ms is None
    assert "timed out" in snap.error.lower()


def test_snapshot_for_unexpected_error_redacts_token():
    """SECURITY: when an exception message happens to embed the
    auth token (e.g. httpx error with URL containing the token in
    a query string), the sanitize step strips it."""
    snap = snapshot_for_unexpected_error(
        name="fly",
        exc=RuntimeError("upstream rejected ghp_realtoken123 with 401"),
        auth_tokens=("ghp_realtoken123",),
    )
    assert snap.status == "unknown"
    assert "ghp_realtoken123" not in snap.error
    assert "<REDACTED>" in snap.error
    # The exception type prefix is preserved
    assert "RuntimeError" in snap.error


def test_snapshot_for_unexpected_error_handles_no_tokens():
    """When no tokens are passed (e.g. error is from probe internals
    that never touched auth), no redaction needed."""
    snap = snapshot_for_unexpected_error(
        name="fly",
        exc=ValueError("invalid response shape"),
    )
    assert snap.status == "unknown"
    assert "ValueError" in snap.error
    assert "invalid response shape" in snap.error


# ---------------------------------------------------------------------------
# with_timeout — 10s ceiling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_timeout_returns_snapshot_for_timeout_on_overshoot(monkeypatch):
    """A coroutine that takes longer than the timeout → returns a
    snapshot_for_timeout instead of raising. The runner relies on
    this contract."""

    async def _slow():
        await asyncio.sleep(0.5)
        return ServiceHealthSnapshot(
            name="x",
            status="healthy",
            last_check_at=datetime.now(timezone.utc),
        )

    snap = await with_timeout(_slow(), name="test", timeout=0.05)
    assert snap.status == "unknown"
    assert "timed out" in snap.error.lower()


@pytest.mark.asyncio
async def test_with_timeout_returns_snapshot_when_check_completes():
    """Fast check returns its snapshot unchanged."""
    expected = ServiceHealthSnapshot(
        name="x",
        status="healthy",
        last_check_at=datetime.now(timezone.utc),
    )

    async def _fast():
        return expected

    snap = await with_timeout(_fast(), name="test", timeout=1.0)
    assert snap is expected


def test_probe_timeout_seconds_is_10():
    """Spec pin: §4 Q1 implies 10s per-probe ceiling vs 5min cadence."""
    assert PROBE_TIMEOUT_SECONDS == 10.0
