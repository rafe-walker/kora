"""Cost-ladder wire-in helpers (KR-P2-K ST2).

Bridges the inference dispatch sites to the
:class:`agent.cost_state_holder.CostStateHolder` singleton. Two surfaces:

  - :func:`record_inference_from_response` — extracts ``response.usage``,
    normalizes via :func:`agent.usage_pricing.normalize_usage`, feeds the
    holder's :meth:`record_inference`. Fail-soft: every failure path
    logs DEBUG and returns; the inference response handler must never
    crash because of the cost-ladder estimator.

  - :func:`record_rate_limit_pulse_from_response` — extracts the
    ``anthropic-ratelimit-{requests,tokens}-{limit,remaining,reset}``
    headers from a direct-Anthropic SDK response, builds a
    :class:`RateLimitPulse`, feeds the holder's
    :meth:`record_rate_limit_pulse`. Best-effort secondary signal per
    R4.1 §9.6: only the 2 direct-Anthropic dispatch sites in this
    codebase surface these headers; OpenAI-compat responses don't.

# Asymmetry recap (per the B1 verification ruling)

The Joshua billing memo requires the **primary signal** (per-call
$-burn against the $200 pool) to cover ALL inference paths. The
**secondary signal** (rate-limit headers) is best-effort — only
direct-Anthropic SDK responses carry them.

This module covers:

  - Primary signal via :func:`record_inference_from_response`: called
    from BOTH the main agent's :mod:`agent.conversation_loop`
    chokepoint (post-:func:`normalize_usage`) AND from
    :mod:`agent.auxiliary_client`'s ``_validate_llm_response`` /
    ``_record_and_validate`` wrappers, covering compression / vision /
    web-extract / session-search / skills-hub / MCP / title-generation
    side tasks.

  - Secondary signal via :func:`record_rate_limit_pulse_from_response`:
    called from the 2 direct-Anthropic sites
    (``run_agent.py:_anthropic_messages_create`` wrapper +
    ``auxiliary_client.py:AnthropicAuxiliaryClient`` chat-completions
    shim).

# Note on the "3 sites" framing

The KR-P2-K bucket spec ST2 referenced "3 SDK call sites" from the
verification report. In practice the actual inference dispatch in
this codebase funnels through 2 chokepoints
(``conversation_loop.normalize_usage`` + ``auxiliary_client.call_llm``)
which together cover all dispatch paths. The third "site" identified
in the verification report (``agent_runtime_helpers.py:1275``) is
actually the OpenAI client construction site — the inference call
dispatched through that client flows back through the
``conversation_loop`` chokepoint. Coverage is intact; the framing
was site-count vs chokepoint-count.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from agent.cost_state_holder import (
    RateLimitAxis,
    RateLimitPulse,
    get_cost_holder,
)
from agent.usage_pricing import normalize_usage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Primary signal — record_inference (per-call $-burn)
# ---------------------------------------------------------------------------


def record_inference_from_response(
    response: Any,
    *,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_mode: Optional[str] = None,
    route: str = "unknown",
    escalated_to_opus: bool = False,
    escalation_reason: Optional[str] = None,
) -> None:
    """Feed the cost-ladder estimator from an inference response.

    Args:
        response: SDK response object. Must expose ``response.usage``
            in a shape :func:`normalize_usage` can consume (Anthropic
            ``input_tokens``/``output_tokens``/``cache_*_input_tokens``
            shape OR OpenAI ``prompt_tokens``/``completion_tokens``/
            ``input_tokens_details`` shape).
        model: Model identifier — typically from ``response.model``.
            Falls back to ``getattr(response, "model", "")``.
        provider: Provider hint forwarded to
            :func:`agent.usage_pricing.normalize_usage` +
            :func:`agent.cost_state_holder.CostStateHolder.record_inference`.
        base_url: Endpoint base URL hint, forwarded the same way.
        api_mode: API mode (``"anthropic_messages"`` / ``"openai"`` /
            etc.) forwarded to :func:`normalize_usage` for shape
            disambiguation.

    Fail-soft: any failure path (no holder, no usage attribute,
    normalize raises, record_inference raises) logs at DEBUG and
    returns. The caller's inference response handler must not see
    estimator failures.
    """
    try:
        holder = get_cost_holder()
        if holder is None:
            return

        raw_usage = getattr(response, "usage", None)
        if not raw_usage:
            return

        canonical_usage = normalize_usage(
            raw_usage, provider=provider, api_mode=api_mode
        )

        resolved_model = model or getattr(response, "model", "") or ""
        if not resolved_model:
            # Without a model name there's no pricing route to resolve.
            return

        holder.record_inference(
            canonical_usage,
            model_name=resolved_model,
            provider=provider,
            base_url=base_url,
            route=route,
            escalated_to_opus=escalated_to_opus,
            escalation_reason=escalation_reason,
        )
    except Exception as exc:
        # Fail-soft per the contract — estimator failures must not
        # crash the inference response handler.
        logger.debug(
            "[kora.cost_ladder] record_inference_from_response failed: %r",
            exc,
        )


# ---------------------------------------------------------------------------
# Secondary signal — record_rate_limit_pulse (Anthropic-only)
# ---------------------------------------------------------------------------


def record_rate_limit_pulse_from_response(response: Any) -> None:
    """Capture Anthropic SDK rate-limit headers from a direct-Anthropic
    response.

    Anthropic responses carry six headers (three per axis, two axes):

      ``anthropic-ratelimit-requests-limit``
      ``anthropic-ratelimit-requests-remaining``
      ``anthropic-ratelimit-requests-reset``
      ``anthropic-ratelimit-tokens-limit``
      ``anthropic-ratelimit-tokens-remaining``
      ``anthropic-ratelimit-tokens-reset``

    The ``-reset`` values are ISO-8601 timestamps. ``-limit`` and
    ``-remaining`` are non-negative integers.

    Best-effort: if the response doesn't expose ``response.headers``
    (some test doubles + non-Anthropic shapes), or any header is
    missing/malformed, the helper logs DEBUG and returns without
    touching the holder. Only the 2 direct-Anthropic dispatch sites
    surface these headers in this codebase; the OpenAI-compat path
    doesn't.
    """
    try:
        holder = get_cost_holder()
        if holder is None:
            return

        headers = _extract_headers(response)
        if headers is None:
            return

        requests_axis = _parse_axis(headers, axis="requests")
        tokens_axis = _parse_axis(headers, axis="tokens")
        if requests_axis is None or tokens_axis is None:
            return

        pulse = RateLimitPulse(
            requests=requests_axis,
            tokens=tokens_axis,
            captured_at=datetime.now(timezone.utc),
        )
        holder.record_rate_limit_pulse(pulse)
    except Exception as exc:
        logger.debug(
            "[kora.cost_ladder] record_rate_limit_pulse_from_response "
            "failed: %r",
            exc,
        )


def _extract_headers(response: Any) -> Optional[Any]:
    """Return a header-lookup object from the response, or ``None``.

    Anthropic SDK responses expose headers via either
    ``response.headers`` (dict-like) OR
    ``response.http_response.headers`` (httpx Response). Try both;
    fail-soft if neither.
    """
    headers = getattr(response, "headers", None)
    if headers is not None:
        return headers
    http_response = getattr(response, "http_response", None)
    if http_response is not None:
        return getattr(http_response, "headers", None)
    return None


def _parse_axis(headers: Any, *, axis: str) -> Optional[RateLimitAxis]:
    """Build a :class:`RateLimitAxis` from a header set.

    Returns ``None`` if any required header is missing or malformed.
    """
    limit_key = f"anthropic-ratelimit-{axis}-limit"
    remaining_key = f"anthropic-ratelimit-{axis}-remaining"
    reset_key = f"anthropic-ratelimit-{axis}-reset"

    try:
        limit_str = _get_header(headers, limit_key)
        remaining_str = _get_header(headers, remaining_key)
        reset_str = _get_header(headers, reset_key)
    except KeyError:
        return None
    if limit_str is None or remaining_str is None or reset_str is None:
        return None

    try:
        limit = int(limit_str)
        remaining = int(remaining_str)
        reset_at = _parse_iso8601(reset_str)
    except (ValueError, TypeError):
        return None
    if reset_at is None:
        return None

    return RateLimitAxis(limit=limit, remaining=remaining, reset_at=reset_at)


def _get_header(headers: Any, key: str) -> Optional[str]:
    """Look up ``key`` in headers, handling case-insensitive dict-like
    and httpx-Headers shapes uniformly. Returns ``None`` if missing."""
    # httpx.Headers + most dict-like header objects support .get
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(key)
        if value is None:
            value = getter(key.lower())
        return value
    # Last-resort: bracket access
    try:
        return headers[key]
    except (KeyError, TypeError):
        try:
            return headers[key.lower()]
        except (KeyError, TypeError):
            return None


def _parse_iso8601(value: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp string into a UTC-aware datetime.

    Anthropic's ``-reset`` headers are RFC3339 / ISO-8601 with a ``Z``
    suffix; Python's :meth:`datetime.fromisoformat` handles the
    common cases. Returns ``None`` on parse failure.
    """
    try:
        # Python 3.11+ fromisoformat handles trailing Z + offsets natively.
        # Replace 'Z' with '+00:00' for the older format too just in case.
        normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (ValueError, AttributeError):
        return None
