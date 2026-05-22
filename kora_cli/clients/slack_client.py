"""Outbound Slack Web API client — KR-FEAT-SLACK-DM ST2.

Minimal async client targeting Slack's ``chat.postMessage`` endpoint
for Kora's reply path. NOT a full Slack SDK wrapper; we ship only
what the daemon's Slack handler needs + grow the surface in
follow-on buckets (e.g. ``users.info`` for live display-name
resolution, ``conversations.open`` for proactive DMs).

# Why httpx, not aiohttp

The ``slack`` pyproject extra ships ``aiohttp`` for the legacy
``gateway/platforms/slack.py`` Bolt-app. The daemon already uses
``httpx`` extensively (heartbeat probes, web_server outbound calls,
auth) and ``httpx[socks]==0.28.1`` is a CORE dependency — using
``aiohttp`` would force every daemon deploy to install the ``slack``
extra. Choosing ``httpx`` keeps the daemon's outbound HTTP surface
uniform + drops one extra requirement.

# Auth + token handling

``KORA_SLACK_BOT_TOKEN`` from Doppler ``kora-runtime-gateways``.
Read once at ``SlackClient.__init__``; **fail-CLOSED** on unset/
empty (raises ``SlackClientError`` — operator must configure
before daemon can reply). The token is NEVER logged, NEVER included
in dead-letter records, NEVER serialized into the JSONL outbound
audit (a unit test asserts this via diverse-sequence
substring-absence — same shape as ST1's signing-secret test).

# Retry policy

  - HTTP 429: respect ``Retry-After`` header (seconds; float ok).
    Default 1.0s if header missing. **One** retry attempt.
  - HTTP 5xx: short fixed backoff (0.5s). **One** retry attempt.
  - HTTP 2xx + ``ok: false`` (Slack API-level error like
    ``invalid_auth`` / ``channel_not_found``): NO retry; raise.
  - HTTP 4xx other than 429: NO retry; raise.

Total ceiling: 2 attempts per ``post_dm`` call. Per-call timeout:
10s (covers both attempts' transport time; matches the bucket spec).

# Result mapping

Successful 2xx + ``ok: true`` → returns the raw response dict.
The handler reads ``ts`` for the JSONL outbound entry's
``slack_message_ts`` field. Failure modes surface as
``SlackClientError`` subclasses so the handler can branch on
retry-exhaustion vs API-error vs auth-failure.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BOT_TOKEN_ENV = "KORA_SLACK_BOT_TOKEN"
SLACK_API_BASE = "https://slack.com/api"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_RETRY_AFTER_SECONDS = 1.0
RETRY_5XX_BACKOFF_SECONDS = 0.5


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SlackClientError(RuntimeError):
    """Base class. Concrete subclasses below distinguish failure modes
    so the caller can branch on retry-exhaustion vs API-level vs auth.
    """


class SlackClientNotConfigured(SlackClientError):
    """``KORA_SLACK_BOT_TOKEN`` is unset or empty — fail-CLOSED on
    construction. Operator must set the env via Doppler."""


class SlackAPIError(SlackClientError):
    """Slack returned a 2xx HTTP response with ``ok: false``.

    Carries the Slack-side ``error`` code (``invalid_auth`` /
    ``channel_not_found`` / etc.) so the operator can triage.
    """

    def __init__(self, slack_error: str, raw_response: Dict[str, Any]):
        self.slack_error = slack_error
        self.raw_response = raw_response
        super().__init__(f"slack API error: {slack_error}")


class SlackTransportError(SlackClientError):
    """Network / timeout / retry-exhaustion failure.

    Includes a ``last_status`` (HTTP code of the final attempt) when
    applicable, ``None`` otherwise (connection refused, DNS, etc.).
    """

    def __init__(self, reason: str, last_status: Optional[int] = None):
        self.reason = reason
        self.last_status = last_status
        super().__init__(reason)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class SlackClient:
    """Thin async wrapper around Slack Web API endpoints Kora calls."""

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        """Read bot token from env; fail-CLOSED on missing.

        Args:
          timeout_seconds: Per-call timeout (applies to each attempt
            including retries). Defaults to 10s.
          transport: Test seam — inject ``httpx.MockTransport`` for
            unit tests. Production code leaves this ``None``.

        Raises:
          SlackClientNotConfigured: ``KORA_SLACK_BOT_TOKEN`` env is
            unset or whitespace-only.
        """
        token = os.environ.get(BOT_TOKEN_ENV, "").strip()
        if not token:
            raise SlackClientNotConfigured(
                f"{BOT_TOKEN_ENV} env unset or empty — daemon cannot "
                f"reply to Slack DMs. Set via Doppler "
                f"(kora-runtime-gateways project)."
            )
        # Stored as a private attr; never logged. ``_token`` underscore
        # prefix discourages accidental serialization via __dict__-walking
        # debuggers (also a separate test asserts it doesn't leak into
        # the JSONL log file).
        self._token = token
        self._timeout = timeout_seconds
        self._transport = transport

    async def post_dm(
        self,
        *,
        channel_id: str,
        text: str,
        thread_ts: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Call ``chat.postMessage`` with retry policy.

        Args:
          channel_id: Slack channel ID (``D...`` for an IM, ``C...``
            for a public channel).
          text: Message body. Slack truncates at 40k chars; the
            handler caller is responsible for any earlier truncation
            (KR-FEAT-SLACK-DM ST2 echo format caps at 200).
          thread_ts: Optional. Set to reply inside an existing
            thread. The handler defaults to ``event.thread_ts or
            event.ts`` so all Kora replies thread under the
            originating DM.

        Returns:
          The full Slack response dict on success. Notable fields:
          ``ts`` (the new message's timestamp — recorded in the
          outbound JSONL).

        Raises:
          SlackAPIError: Slack returned ``ok: false``.
          SlackTransportError: Network failure or retry exhaustion.
        """
        payload: Dict[str, Any] = {"channel": channel_id, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts

        # Two-attempt loop. Variable-name hygiene: ``attempt`` is
        # 1-indexed (the first attempt is attempt 1, the retry is
        # attempt 2). ``last_response`` carries the response of the
        # first attempt into the retry-decision logic.
        last_response: Optional[httpx.Response] = None
        last_error_reason: Optional[str] = None

        async with self._make_client() as client:
            for attempt in (1, 2):
                try:
                    response = await client.post(
                        f"{SLACK_API_BASE}/chat.postMessage",
                        json=payload,
                        headers={
                            # Bearer + Content-Type. Both Slack-required.
                            "Authorization": f"Bearer {self._token}",
                            "Content-Type": "application/json; charset=utf-8",
                        },
                    )
                except (httpx.TimeoutException, httpx.HTTPError) as exc:
                    # Transport-level failure (timeout, connection
                    # refused, DNS). If this is attempt 1, retry once.
                    last_error_reason = f"{type(exc).__name__}: {exc}"
                    if attempt == 1:
                        await asyncio.sleep(RETRY_5XX_BACKOFF_SECONDS)
                        continue
                    raise SlackTransportError(
                        f"slack POST transport failed after 2 attempts: "
                        f"{last_error_reason}",
                        last_status=None,
                    )

                last_response = response

                # 2xx: parse + check ok flag.
                if 200 <= response.status_code < 300:
                    return self._handle_2xx(response)

                # 429: retry once with respect-retry-after.
                if response.status_code == 429:
                    if attempt == 1:
                        delay = self._parse_retry_after(response)
                        await asyncio.sleep(delay)
                        continue
                    raise SlackTransportError(
                        f"slack rate-limited after retry "
                        f"(429 on both attempts)",
                        last_status=429,
                    )

                # 5xx: retry once with fixed backoff.
                if 500 <= response.status_code < 600:
                    if attempt == 1:
                        await asyncio.sleep(RETRY_5XX_BACKOFF_SECONDS)
                        continue
                    raise SlackTransportError(
                        f"slack 5xx after retry: "
                        f"{response.status_code}",
                        last_status=response.status_code,
                    )

                # Other 4xx (401, 403, 404, etc.) — NO retry.
                raise SlackTransportError(
                    f"slack HTTP {response.status_code} "
                    f"(non-retryable)",
                    last_status=response.status_code,
                )

        # Loop fell through — should be unreachable but defensive.
        raise SlackTransportError(
            f"slack post_dm exhausted retries without raise; "
            f"last_status={last_response.status_code if last_response else None}",
            last_status=last_response.status_code if last_response else None,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_client(self) -> httpx.AsyncClient:
        kwargs: Dict[str, Any] = {"timeout": self._timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    def _handle_2xx(self, response: httpx.Response) -> Dict[str, Any]:
        """Slack ALWAYS returns 200 OK + JSON; the API-level success
        is in ``ok`` (bool). Non-ok → SlackAPIError."""
        try:
            body = response.json()
        except ValueError as exc:
            raise SlackTransportError(
                f"slack returned 2xx with non-JSON body: {exc}",
                last_status=response.status_code,
            )
        if not isinstance(body, dict):
            raise SlackTransportError(
                "slack returned 2xx with non-dict JSON body",
                last_status=response.status_code,
            )
        if body.get("ok") is True:
            return body
        slack_error = str(body.get("error") or "unknown_slack_error")
        # Don't include the raw response in the WARN log if it
        # might carry echoed secrets — but Slack's error responses
        # are documented to carry only ``ok``/``error``/``warning``,
        # so logging is safe.
        logger.warning(
            "[kora.slack_client] chat.postMessage ok=false error=%s",
            slack_error,
        )
        raise SlackAPIError(slack_error, raw_response=body)

    def _parse_retry_after(self, response: httpx.Response) -> float:
        """Slack 429 sends ``Retry-After: <seconds>``. Default to
        ``DEFAULT_RETRY_AFTER_SECONDS`` if missing/malformed."""
        raw = response.headers.get("retry-after", "").strip()
        if not raw:
            return DEFAULT_RETRY_AFTER_SECONDS
        try:
            return float(raw)
        except ValueError:
            return DEFAULT_RETRY_AFTER_SECONDS
