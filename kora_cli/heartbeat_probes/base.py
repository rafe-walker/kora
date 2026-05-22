"""ServiceProbe Protocol + shared helpers (KR-FEAT-HEARTBEAT ST1).

The :class:`ServiceProbe` contract: each probe has a ``name`` +
``async def check() -> ServiceHealthSnapshot``. Implementations
live in sibling modules (one per service).

Shared helpers:

  - :func:`resolve_env` — read an env var; treat empty/whitespace
    as unset (matches KR-MCP-1 catalog convention).
  - :func:`snapshot_for_auth_missing` — short-circuit snapshot when
    an auth env var is unset; never makes the API call.
  - :func:`snapshot_for_timeout` — snapshot for the 10s wall-clock
    timeout overshoot.
  - :func:`snapshot_for_unexpected_error` — last-resort wrapper for
    unexpected exception types; runs the exception type/message
    through :func:`sanitize_error`.
  - :func:`sanitize_error` — strip any known auth-token values from
    a string before exposing in a snapshot's ``error`` field.

# Probe-wide timeout

``PROBE_TIMEOUT_SECONDS = 10.0`` enforced per :func:`with_timeout`
wrapper. The §4 Q1 ruling on cadence (5 min) gives plenty of
headroom — a 10s ceiling means even with all 5 probes serialized
worst-case, we still leave 4.5 min before the next cycle.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, Protocol, runtime_checkable

from kora_cli.heartbeat_probes.types import ServiceHealthSnapshot

logger = logging.getLogger(__name__)


PROBE_TIMEOUT_SECONDS: float = 10.0


@runtime_checkable
class ServiceProbe(Protocol):
    """Contract every backend-service probe implements."""

    name: str

    async def check(self) -> ServiceHealthSnapshot: ...


# ---------------------------------------------------------------------------
# Env resolution
# ---------------------------------------------------------------------------


def resolve_env(name: str) -> Optional[str]:
    """Read ``os.environ[name]``; treat empty / whitespace-only as
    unset. Matches the KR-MCP-1 catalog ``check_endpoint_health``
    convention — Doppler sometimes injects empty values.

    Returns the stripped value or ``None`` when unset/empty.
    """
    raw = os.environ.get(name, "").strip()
    return raw or None


# ---------------------------------------------------------------------------
# Token sanitization (preserve security contract)
# ---------------------------------------------------------------------------


def sanitize_error(text: str, *tokens: Optional[str]) -> str:
    """Strip any of ``tokens`` from ``text`` before exposing it.

    The error field of a snapshot is operator-visible. A probe must
    NEVER leak its auth token there — even on partial-auth
    failures (4xx responses, transport errors carrying the token
    in URL fragments, etc.).

    Replaces every occurrence of each non-empty token value with
    ``"<REDACTED>"``. Empty / ``None`` tokens are skipped.
    """
    if not text:
        return text
    out = text
    for token in tokens:
        if token:
            out = out.replace(token, "<REDACTED>")
    return out


# ---------------------------------------------------------------------------
# Snapshot constructors for common no-call / failure paths
# ---------------------------------------------------------------------------


def snapshot_for_auth_missing(
    *, name: str, env_var: str, extra_envs: tuple[str, ...] = ()
) -> ServiceHealthSnapshot:
    """Return a snapshot for the auth-env-missing case.

    Status = ``unknown``. Error string lists the env var name(s) —
    NOT any value. The probe MUST short-circuit here before
    issuing any outbound traffic.
    """
    all_envs = (env_var,) + extra_envs
    listing = ", ".join(repr(e) for e in all_envs)
    return ServiceHealthSnapshot(
        name=name,
        status="unknown",
        latency_ms=None,
        last_check_at=datetime.now(timezone.utc),
        details={},
        error=f"auth env unset or empty: {listing}",
    )


def snapshot_for_timeout(*, name: str) -> ServiceHealthSnapshot:
    return ServiceHealthSnapshot(
        name=name,
        status="unknown",
        latency_ms=None,
        last_check_at=datetime.now(timezone.utc),
        details={},
        error=f"probe timed out after {PROBE_TIMEOUT_SECONDS:.0f}s",
    )


def snapshot_for_unexpected_error(
    *,
    name: str,
    exc: BaseException,
    auth_tokens: tuple[Optional[str], ...] = (),
) -> ServiceHealthSnapshot:
    """Last-resort snapshot for an unexpected exception type.

    The runner catches per-probe exceptions to enforce isolation +
    delegates here to build the snapshot. Error text is sanitized
    against ``auth_tokens`` so we never leak even on weird-path
    failures (e.g., httpx error message containing the URL with
    bearer in a query string).
    """
    raw = f"{type(exc).__name__}: {exc}"
    return ServiceHealthSnapshot(
        name=name,
        status="unknown",
        latency_ms=None,
        last_check_at=datetime.now(timezone.utc),
        details={},
        error=sanitize_error(raw, *auth_tokens),
    )


# ---------------------------------------------------------------------------
# Per-probe timeout wrapper
# ---------------------------------------------------------------------------


async def with_timeout(
    coro: Awaitable[ServiceHealthSnapshot],
    *,
    name: str,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ServiceHealthSnapshot:
    """Wrap a probe's ``check()`` in :func:`asyncio.wait_for`.

    On :class:`asyncio.TimeoutError`, returns a
    ``snapshot_for_timeout`` instead of raising — the runner's
    isolation contract requires a snapshot per probe per cycle.
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(
            "[kora.heartbeat_probes] %s timed out after %.1fs", name, timeout
        )
        return snapshot_for_timeout(name=name)


# ---------------------------------------------------------------------------
# Latency timing helper
# ---------------------------------------------------------------------------


def now_ms_monotonic() -> float:
    """Wall-clock-free monotonic milliseconds for latency
    measurement. Use the delta between two calls — not the absolute
    value."""
    import time

    return time.monotonic() * 1000.0


# ---------------------------------------------------------------------------
# HTTP error → status mapping (shared across probes)
# ---------------------------------------------------------------------------


def status_from_http_response(
    status_code: int, *, healthy_codes: tuple[int, ...] = (200,)
) -> str:
    """Map an HTTP response code onto our 4-value status enum.

    ``healthy_codes`` → ``healthy``; everything else → ``unhealthy``
    (4xx/5xx). The probe layer may override based on response body
    (e.g. Sentry's unresolved-issues count → ``degraded``).
    """
    return "healthy" if status_code in healthy_codes else "unhealthy"


# Convenience re-exports so callers don't have to know about
# datetime / timezone construction quirks.

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# Type alias for the snapshot-producing callable each probe ships.
SnapshotProducer = Callable[[], Awaitable[ServiceHealthSnapshot]]


# Silence "Any unused" warnings
__all_dummy__: tuple[Any, ...] = ()
