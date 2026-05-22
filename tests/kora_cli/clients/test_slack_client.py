"""Tests for ``kora_cli.clients.slack_client`` — KR-FEAT-SLACK-DM ST2.

Covers:
  - Fail-CLOSED on missing/whitespace bot token env
  - Successful post_dm → response dict with ts
  - Slack API-level error (ok: false) → SlackAPIError
  - HTTP 429 → respect Retry-After → 1 retry → success
  - HTTP 429 on BOTH attempts → SlackTransportError(last_status=429)
  - HTTP 500 → 1 retry → success
  - HTTP 500 on both attempts → SlackTransportError(last_status=500)
  - HTTP 4xx other than 429 (e.g. 401) → SlackTransportError, NO retry
  - Transport exception (timeout) on attempt 1 → 1 retry
  - Auth header is Bearer + bot token (verified via captured request)
  - Bot token NEVER appears in error messages or logged output
"""

from __future__ import annotations

import logging

import httpx
import pytest

from kora_cli.clients import slack_client as sc_mod
from kora_cli.clients.slack_client import (
    BOT_TOKEN_ENV,
    SlackAPIError,
    SlackClient,
    SlackClientNotConfigured,
    SlackTransportError,
)


# ---------------------------------------------------------------------------
# Helpers — httpx MockTransport recipe
# ---------------------------------------------------------------------------


def _ok_response(ts: str = "1700000000.123"):
    return httpx.Response(
        200,
        json={"ok": True, "ts": ts, "channel": "D01", "message": {}},
    )


def _api_error_response(err: str):
    return httpx.Response(200, json={"ok": False, "error": err})


def _429_response(retry_after: str | None = "0.01"):
    headers = {"Retry-After": retry_after} if retry_after else {}
    return httpx.Response(
        429, json={"ok": False, "error": "rate_limited"}, headers=headers
    )


def _500_response():
    return httpx.Response(500, text="internal error")


def _make_transport(responses):
    """Build a MockTransport that yields ``responses`` in sequence.
    Raises if the test runs more requests than expected (catches retry bugs)."""
    iter_responses = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        try:
            return next(iter_responses)
        except StopIteration as exc:
            raise AssertionError(
                f"unexpected extra request: {request.method} {request.url}"
            ) from exc

    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _bot_token_set(monkeypatch):
    monkeypatch.setenv(BOT_TOKEN_ENV, "xoxb-test-bot-token-do-not-leak")


# ---------------------------------------------------------------------------
# Construction — fail-CLOSED
# ---------------------------------------------------------------------------


def test_fail_closed_on_missing_token(monkeypatch):
    monkeypatch.delenv(BOT_TOKEN_ENV, raising=False)
    with pytest.raises(SlackClientNotConfigured, match=BOT_TOKEN_ENV):
        SlackClient()


def test_fail_closed_on_whitespace_token(monkeypatch):
    monkeypatch.setenv(BOT_TOKEN_ENV, "   ")
    with pytest.raises(SlackClientNotConfigured):
        SlackClient()


def test_constructor_reads_token_from_env():
    """Token is read at construction; not lazily on first call."""
    client = SlackClient()
    # Private attribute used by post_dm; verified indirectly via the
    # Authorization header in test_auth_header_carries_bearer_token.
    assert client._token == "xoxb-test-bot-token-do-not-leak"


# ---------------------------------------------------------------------------
# Successful post_dm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_dm_success_returns_response_dict():
    transport = _make_transport([_ok_response(ts="1700000000.001")])
    client = SlackClient(transport=transport)
    result = await client.post_dm(
        channel_id="D01", text="hi", thread_ts=None
    )
    assert result["ok"] is True
    assert result["ts"] == "1700000000.001"


@pytest.mark.asyncio
async def test_post_dm_includes_thread_ts_in_payload():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = request.read().decode("utf-8")
        return _ok_response()

    client = SlackClient(transport=httpx.MockTransport(handler))
    await client.post_dm(
        channel_id="D01", text="reply", thread_ts="1700.000"
    )
    assert '"thread_ts":"1700.000"' in captured["json"].replace(" ", "")


@pytest.mark.asyncio
async def test_post_dm_omits_thread_ts_when_none():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = request.read().decode("utf-8")
        return _ok_response()

    client = SlackClient(transport=httpx.MockTransport(handler))
    await client.post_dm(channel_id="D01", text="reply", thread_ts=None)
    assert "thread_ts" not in captured["json"]


@pytest.mark.asyncio
async def test_auth_header_carries_bearer_token():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization", "")
        captured["ct"] = request.headers.get("content-type", "")
        return _ok_response()

    client = SlackClient(transport=httpx.MockTransport(handler))
    await client.post_dm(channel_id="D01", text="hi", thread_ts=None)
    assert captured["auth"] == "Bearer xoxb-test-bot-token-do-not-leak"
    assert "application/json" in captured["ct"]


# ---------------------------------------------------------------------------
# Slack API-level errors (2xx + ok: false)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_api_error_raises_with_error_code():
    transport = _make_transport([_api_error_response("channel_not_found")])
    client = SlackClient(transport=transport)
    with pytest.raises(SlackAPIError) as exc_info:
        await client.post_dm(channel_id="DBAD", text="x", thread_ts=None)
    assert exc_info.value.slack_error == "channel_not_found"


@pytest.mark.asyncio
async def test_slack_api_error_no_retry():
    """ok: false is NOT retryable — only 1 request hit."""
    transport = _make_transport([_api_error_response("invalid_auth")])
    client = SlackClient(transport=transport)
    with pytest.raises(SlackAPIError):
        await client.post_dm(channel_id="D01", text="x", thread_ts=None)


# ---------------------------------------------------------------------------
# 429 retry behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_429_then_success_retries_once():
    transport = _make_transport(
        [_429_response(retry_after="0.01"), _ok_response()]
    )
    client = SlackClient(transport=transport)
    result = await client.post_dm(channel_id="D01", text="x")
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_429_both_attempts_raises_transport_error_with_status():
    transport = _make_transport(
        [_429_response(retry_after="0.01"), _429_response(retry_after="0.01")]
    )
    client = SlackClient(transport=transport)
    with pytest.raises(SlackTransportError) as exc_info:
        await client.post_dm(channel_id="D01", text="x")
    assert exc_info.value.last_status == 429


@pytest.mark.asyncio
async def test_429_missing_retry_after_uses_default():
    """Slack 429 without Retry-After header → default ``DEFAULT_RETRY_AFTER_SECONDS``
    is used. Hard to assert the exact sleep, but the retry must succeed."""
    transport = _make_transport([_429_response(retry_after=None), _ok_response()])
    client = SlackClient(transport=transport)
    # Patch the default to a tiny value so the test is fast.
    import kora_cli.clients.slack_client as scm

    original = scm.DEFAULT_RETRY_AFTER_SECONDS
    scm.DEFAULT_RETRY_AFTER_SECONDS = 0.001
    try:
        result = await client.post_dm(channel_id="D01", text="x")
        assert result["ok"] is True
    finally:
        scm.DEFAULT_RETRY_AFTER_SECONDS = original


# ---------------------------------------------------------------------------
# 5xx retry behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_500_then_success_retries_once():
    transport = _make_transport([_500_response(), _ok_response()])
    client = SlackClient(transport=transport)
    result = await client.post_dm(channel_id="D01", text="x")
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_500_both_attempts_raises_transport_error():
    transport = _make_transport([_500_response(), _500_response()])
    client = SlackClient(transport=transport)
    with pytest.raises(SlackTransportError) as exc_info:
        await client.post_dm(channel_id="D01", text="x")
    assert exc_info.value.last_status == 500


@pytest.mark.asyncio
async def test_503_treated_as_5xx_retryable():
    transport = _make_transport(
        [httpx.Response(503, text="overloaded"), _ok_response()]
    )
    client = SlackClient(transport=transport)
    result = await client.post_dm(channel_id="D01", text="x")
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# Non-retryable 4xx
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_401_no_retry():
    """Slack returning HTTP 401 (rare; ok:false is the usual path) —
    NO retry. Only 1 request is allowed by the MockTransport."""
    transport = _make_transport([httpx.Response(401, text="unauthorized")])
    client = SlackClient(transport=transport)
    with pytest.raises(SlackTransportError) as exc_info:
        await client.post_dm(channel_id="D01", text="x")
    assert exc_info.value.last_status == 401


@pytest.mark.asyncio
async def test_403_no_retry():
    transport = _make_transport([httpx.Response(403)])
    client = SlackClient(transport=transport)
    with pytest.raises(SlackTransportError):
        await client.post_dm(channel_id="D01", text="x")


@pytest.mark.asyncio
async def test_404_no_retry():
    transport = _make_transport([httpx.Response(404)])
    client = SlackClient(transport=transport)
    with pytest.raises(SlackTransportError):
        await client.post_dm(channel_id="D01", text="x")


# ---------------------------------------------------------------------------
# Transport exception (timeout / connect failure)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_on_attempt_1_retries(monkeypatch):
    """A TimeoutException on attempt 1 → retry once → success."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise httpx.TimeoutException("simulated timeout")
        return _ok_response()

    # Speed up the retry backoff so the test is fast.
    import kora_cli.clients.slack_client as scm

    monkeypatch.setattr(scm, "RETRY_5XX_BACKOFF_SECONDS", 0.001)

    client = SlackClient(transport=httpx.MockTransport(handler))
    result = await client.post_dm(channel_id="D01", text="x")
    assert result["ok"] is True
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_timeout_on_both_attempts_raises_transport_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated timeout")

    import kora_cli.clients.slack_client as scm

    monkeypatch.setattr(scm, "RETRY_5XX_BACKOFF_SECONDS", 0.001)

    client = SlackClient(transport=httpx.MockTransport(handler))
    with pytest.raises(SlackTransportError) as exc_info:
        await client.post_dm(channel_id="D01", text="x")
    assert exc_info.value.last_status is None  # no HTTP-status on transport-level


# ---------------------------------------------------------------------------
# Bot token NEVER appears in error messages or logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bot_token_not_in_error_messages(caplog):
    """Diverse failure modes — none of the error messages should
    embed the bot token value."""
    caplog.set_level(logging.WARNING)
    secret = "xoxb-test-bot-token-do-not-leak"

    # ok: false (api error)
    client = SlackClient(transport=_make_transport([_api_error_response("invalid_auth")]))
    try:
        await client.post_dm(channel_id="D01", text="x")
    except SlackAPIError as exc:
        assert secret not in str(exc)
        assert secret not in repr(exc)

    # 401 (transport error w/ status)
    client = SlackClient(transport=_make_transport([httpx.Response(401)]))
    try:
        await client.post_dm(channel_id="D01", text="x")
    except SlackTransportError as exc:
        assert secret not in str(exc)
        assert secret not in repr(exc)

    # 429 retry-exhaustion
    client = SlackClient(
        transport=_make_transport(
            [_429_response(retry_after="0.001"), _429_response(retry_after="0.001")]
        )
    )
    try:
        await client.post_dm(channel_id="D01", text="x")
    except SlackTransportError as exc:
        assert secret not in str(exc)

    # All captured log messages must also exclude the secret.
    all_log_text = " ".join(r.getMessage() for r in caplog.records)
    assert secret not in all_log_text
