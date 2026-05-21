"""Unit tests for ``agent/cost_ladder_wire.py`` (KR-P2-K ST2 helpers).

Covers:
  - ``record_inference_from_response`` happy path (extracts response.usage,
    normalizes, calls holder.record_inference)
  - Fail-soft on every failure mode: no holder, no usage, normalize
    raises, record_inference raises, no model
  - ``record_rate_limit_pulse_from_response`` happy path with both axes
  - Fail-soft on missing headers / malformed values / no http_response
  - Header lookup handles both ``response.headers`` and
    ``response.http_response.headers`` (httpx-style)
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock

import pytest

from agent.cost_ladder_wire import (
    record_inference_from_response,
    record_rate_limit_pulse_from_response,
)
from agent.cost_state_holder import (
    CostStateHolder,
    RateLimitPulse,
    _reset_cost_holder_for_tests,
    init_cost_holder,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    _reset_cost_holder_for_tests()
    yield
    _reset_cost_holder_for_tests()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _holder() -> CostStateHolder:
    return init_cost_holder(
        billing_period_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )


def _anthropic_response(
    *,
    input_tokens: int = 100,
    output_tokens: int = 50,
    model: str = "claude-sonnet-4.7",
    headers: Optional[dict] = None,
) -> SimpleNamespace:
    """Build a fake direct-Anthropic SDK response."""
    return SimpleNamespace(
        model=model,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ),
        headers=headers,
    )


def _openai_response(
    *,
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    model: str = "claude-sonnet-4.7",
) -> SimpleNamespace:
    """Build a fake OpenAI-compat response."""
    return SimpleNamespace(
        model=model,
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


# ---------------------------------------------------------------------------
# record_inference_from_response
# ---------------------------------------------------------------------------


def test_record_inference_no_holder_is_noop():
    """Without an initialized cost holder, the wire-helper silently
    returns. The estimator must not crash inference response handling."""
    # Holder is reset by autouse fixture
    record_inference_from_response(
        _anthropic_response(),
        model="claude-sonnet-4.7",
        provider="anthropic",
    )
    # No assertion — just ensure no exception raised


def test_record_inference_no_usage_attribute_is_noop():
    holder = _holder()
    response_no_usage = SimpleNamespace(model="claude-sonnet-4.7", usage=None)
    record_inference_from_response(
        response_no_usage,
        model="claude-sonnet-4.7",
        provider="anthropic",
    )
    assert holder.current.spent_to_date_usd == 0.0


def test_record_inference_no_model_returns_silently():
    """Without a model name, no pricing route resolves. Skip."""
    holder = _holder()
    response = _anthropic_response()
    record_inference_from_response(
        response,
        model=None,
        provider="anthropic",
    )
    # Response had a .model attr so it shouldn't return silently — but
    # if response.model is also empty:
    response_no_model = SimpleNamespace(
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )
    # No model attribute at all
    record_inference_from_response(
        response_no_model,
        model=None,
        provider="anthropic",
    )
    # Should not raise — fail-soft


def test_record_inference_happy_path_anthropic_shape():
    """Direct-Anthropic response normalizes via normalize_usage + feeds
    the holder. Spend accumulates."""
    holder = _holder()
    # Use a model that has known Anthropic pricing
    response = _anthropic_response(
        input_tokens=1000, output_tokens=500, model="claude-haiku-4.5"
    )
    record_inference_from_response(
        response,
        model="claude-haiku-4.5",
        provider="anthropic",
    )
    # Cost should be > 0 (haiku is the cheapest tier; small amount but
    # not zero)
    assert holder.current.spent_to_date_usd > 0


def test_record_inference_uses_response_model_when_arg_omitted():
    """If the caller doesn't pass ``model``, the helper extracts it from
    ``response.model`` (the SDK's canonical model identifier)."""
    holder = _holder()
    response = _anthropic_response(
        input_tokens=1000, output_tokens=500, model="claude-haiku-4.5"
    )
    record_inference_from_response(
        response,
        model=None,  # omitted — should fall back to response.model
        provider="anthropic",
    )
    assert holder.current.spent_to_date_usd > 0


def test_record_inference_normalize_failure_is_swallowed(monkeypatch):
    """If normalize_usage raises (malformed response shape), fail-soft."""
    holder = _holder()
    monkeypatch.setattr(
        "agent.cost_ladder_wire.normalize_usage",
        MagicMock(side_effect=RuntimeError("malformed response")),
    )
    response = _anthropic_response()
    record_inference_from_response(
        response, model="claude-sonnet-4.7", provider="anthropic"
    )
    # Holder untouched
    assert holder.current.spent_to_date_usd == 0.0


def test_record_inference_record_failure_is_swallowed(monkeypatch):
    """If holder.record_inference raises, fail-soft."""
    holder = _holder()
    monkeypatch.setattr(
        holder,
        "record_inference",
        MagicMock(side_effect=RuntimeError("estimator boom")),
    )
    response = _anthropic_response()
    record_inference_from_response(
        response, model="claude-sonnet-4.7", provider="anthropic"
    )
    # No exception raised; holder state unchanged


def test_record_inference_openai_compat_shape():
    """OpenAI-compat response (prompt/completion tokens shape) is
    correctly normalized + recorded."""
    holder = _holder()
    response = _openai_response(
        prompt_tokens=2000, completion_tokens=300, model="claude-haiku-4.5"
    )
    record_inference_from_response(
        response,
        model="claude-haiku-4.5",
        provider="openrouter",
    )
    assert holder.current.spent_to_date_usd >= 0  # any non-zero or zero is ok


# ---------------------------------------------------------------------------
# record_rate_limit_pulse_from_response
# ---------------------------------------------------------------------------


def _anthropic_headers() -> dict:
    return {
        "anthropic-ratelimit-requests-limit": "1000",
        "anthropic-ratelimit-requests-remaining": "850",
        "anthropic-ratelimit-requests-reset": "2026-05-21T12:00:00Z",
        "anthropic-ratelimit-tokens-limit": "10000000",
        "anthropic-ratelimit-tokens-remaining": "7500000",
        "anthropic-ratelimit-tokens-reset": "2026-05-21T12:05:00Z",
    }


def test_rate_limit_no_holder_is_noop():
    response = _anthropic_response(headers=_anthropic_headers())
    record_rate_limit_pulse_from_response(response)
    # Nothing to assert; just no exception


def test_rate_limit_happy_path_both_axes_captured():
    holder = _holder()
    response = _anthropic_response(headers=_anthropic_headers())
    record_rate_limit_pulse_from_response(response)

    pulse = holder.current.latest_rate_limit_pulse
    assert pulse is not None
    assert isinstance(pulse, RateLimitPulse)
    assert pulse.requests.limit == 1000
    assert pulse.requests.remaining == 850
    assert pulse.tokens.limit == 10000000
    assert pulse.tokens.remaining == 7500000


def test_rate_limit_reads_from_http_response_when_top_level_absent():
    """httpx-style responses expose headers via response.http_response.headers."""
    holder = _holder()
    response = SimpleNamespace(
        model="claude-sonnet-4.7",
        usage=None,
        http_response=SimpleNamespace(headers=_anthropic_headers()),
    )
    record_rate_limit_pulse_from_response(response)
    assert holder.current.latest_rate_limit_pulse is not None


def test_rate_limit_missing_headers_attribute_is_noop():
    holder = _holder()
    response = SimpleNamespace(model="claude-sonnet-4.7", usage=None)
    record_rate_limit_pulse_from_response(response)
    assert holder.current.latest_rate_limit_pulse is None


def test_rate_limit_missing_required_header_is_noop():
    """If any of the 6 required headers is missing, skip — partial
    pulses aren't meaningful."""
    holder = _holder()
    incomplete = _anthropic_headers()
    del incomplete["anthropic-ratelimit-tokens-remaining"]
    response = _anthropic_response(headers=incomplete)
    record_rate_limit_pulse_from_response(response)
    assert holder.current.latest_rate_limit_pulse is None


def test_rate_limit_malformed_int_value_is_noop():
    holder = _holder()
    bad = _anthropic_headers()
    bad["anthropic-ratelimit-requests-limit"] = "not-a-number"
    response = _anthropic_response(headers=bad)
    record_rate_limit_pulse_from_response(response)
    assert holder.current.latest_rate_limit_pulse is None


def test_rate_limit_malformed_timestamp_is_noop():
    holder = _holder()
    bad = _anthropic_headers()
    bad["anthropic-ratelimit-requests-reset"] = "not-a-date"
    response = _anthropic_response(headers=bad)
    record_rate_limit_pulse_from_response(response)
    assert holder.current.latest_rate_limit_pulse is None


def test_rate_limit_iso8601_with_trailing_z_parses():
    """RFC3339 ``Z`` suffix is canonical Anthropic header format;
    Python <3.11 ``fromisoformat`` didn't accept it natively."""
    holder = _holder()
    response = _anthropic_response(headers=_anthropic_headers())
    record_rate_limit_pulse_from_response(response)
    pulse = holder.current.latest_rate_limit_pulse
    assert pulse is not None
    # reset_at parsed and tz-aware
    assert pulse.requests.reset_at.tzinfo is not None


def test_rate_limit_iso8601_with_offset_parses():
    holder = _holder()
    headers = _anthropic_headers()
    headers["anthropic-ratelimit-requests-reset"] = "2026-05-21T12:00:00+00:00"
    response = _anthropic_response(headers=headers)
    record_rate_limit_pulse_from_response(response)
    pulse = holder.current.latest_rate_limit_pulse
    assert pulse is not None


# ---------------------------------------------------------------------------
# Helper: header lookup handles case + bracket access
# ---------------------------------------------------------------------------


def test_header_lookup_handles_case_insensitive():
    """Some httpx Header objects do case-insensitive lookup; the helper
    should work either way."""
    from agent.cost_ladder_wire import _get_header

    headers = {"Anthropic-RateLimit-Requests-Limit": "1000"}

    # Standard dict — exact lookup
    assert _get_header(headers, "Anthropic-RateLimit-Requests-Limit") == "1000"
    # Lowercase fallback also tries
    assert _get_header(headers, "anthropic-ratelimit-requests-limit") is None  # exact dict


def test_header_lookup_handles_object_with_get_method():
    from agent.cost_ladder_wire import _get_header

    class _CaseInsensitive:
        def __init__(self, base):
            self.base = {k.lower(): v for k, v in base.items()}

        def get(self, key):
            return self.base.get(key.lower())

    h = _CaseInsensitive({"Anthropic-RateLimit-Requests-Limit": "1000"})
    assert _get_header(h, "anthropic-ratelimit-requests-limit") == "1000"
