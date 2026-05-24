"""Integration tests for KR-HAIKU-ROUTER wiring inside the
reasoning engine.

Covers spec §2 engine acceptance:
  - Default-Haiku: simple message + iteration 1 + normal rung →
    SDK call goes out with model=Haiku
  - Opus prefix: SDK call goes out with Opus + prompt has prefix
    STRIPPED so the routing instruction doesn't leak
  - Tool-use iteration 2: first call Haiku; second call Opus
  - Cost-rung clamp: warn_75 / downshift_90 keeps Haiku
  - Post-call low-confidence escalation: end_turn + uncertainty
    marker → second SDK call to Opus with Haiku's response in
    the messages array as context
  - High-confidence Haiku response: only ONE SDK call
  - Tool-use response on iteration 1 does NOT trigger post-call
    escalation (tool_use stop_reason is its own signal; iteration
    2 will use Opus via the iteration earning signal)
  - hard_stop_100 rung returned via context: respond() short-
    circuits with cost_ladder_halted (existing behavior preserved)
  - Telemetry: get_telemetry().record_call invoked per API call
    with correct route + escalated_to_opus flag
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from kora_cli.reasoning.anthropic_engine import (
    MAX_TOOL_USE_ITERATIONS,
    OAUTH_TOKEN_ENV,
    AnthropicReasoningEngine,
)
from kora_cli.reasoning.engine import (
    ConversationContext,
    IncomingMessage,
)
from kora_cli.router import (
    DEFAULT_HAIKU_MODEL,
    DEFAULT_OPUS_MODEL,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _text_block(text: str):
    b = MagicMock()
    b.type = "text"
    b.text = text
    return b


def _tool_use_block(tool_id: str, name: str, tool_input: Dict[str, Any]):
    b = MagicMock()
    b.type = "tool_use"
    b.id = tool_id
    b.name = name
    b.input = tool_input
    return b


def _response(
    content: List[Any],
    stop_reason: str = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 50,
):
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    usage.cache_creation_input_tokens = 0
    usage.cache_read_input_tokens = 0
    r = MagicMock()
    r.content = content
    r.stop_reason = stop_reason
    r.model = DEFAULT_HAIKU_MODEL
    r.usage = usage
    return r


def _make_client(responses):
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=list(responses))
    client.close = AsyncMock()
    return client


def _msg(text: str = "hi") -> IncomingMessage:
    return IncomingMessage(
        text=text,
        source="slack_dm",
        received_at=datetime.now(timezone.utc),
        metadata={},
    )


def _ctx(rung: str = "normal", state: str = "ready") -> ConversationContext:
    return ConversationContext(
        recent_messages=[],
        current_operational_state=state,
        current_cost_ladder_rung=rung,
    )


@pytest.fixture
def system_prompt_path(tmp_path):
    p = tmp_path / "kora_system_prompt.md"
    p.write_text("You are Kora.\n", encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _oauth(monkeypatch):
    monkeypatch.setenv(OAUTH_TOKEN_ENV, "sk-ant-oat-test")


@pytest.fixture(autouse=True)
def _clean_router_env(monkeypatch):
    monkeypatch.delenv("KORA_FORCE_OPUS", raising=False)
    monkeypatch.delenv("KORA_OPUS_TRIGGER_PATTERNS", raising=False)
    monkeypatch.delenv("KORA_OPUS_PREFIX", raising=False)
    monkeypatch.delenv("KORA_HAIKU_LOW_CONFIDENCE_PATTERNS", raising=False)


@pytest.fixture
def telemetry_spy(monkeypatch):
    """Capture every record_call invocation in a list."""
    calls: list[dict] = []

    fake = MagicMock()

    def _capture(**kwargs):
        calls.append(kwargs)

    fake.record_call = _capture
    monkeypatch.setattr(
        "kora_cli.telemetry.cost_telemetry.get_telemetry", lambda: fake
    )
    # Also patch the re-export.
    monkeypatch.setattr(
        "kora_cli.telemetry.get_telemetry", lambda: fake
    )
    return calls


# ---------------------------------------------------------------------------
# Pre-call routing — model on the SDK call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_message_uses_haiku(
    monkeypatch, system_prompt_path, telemetry_spy
):
    """Trivial message → SDK call uses Haiku model."""
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)
    client = _make_client([_response([_text_block("hello")])])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg("hi"), _ctx())
    kwargs = client.messages.create.await_args_list[0].kwargs
    assert kwargs["model"] == DEFAULT_HAIKU_MODEL


@pytest.mark.asyncio
async def test_decision_language_message_uses_opus(
    monkeypatch, system_prompt_path, telemetry_spy
):
    """Decision-language phrase triggers Opus on iteration 1."""
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)
    client = _make_client([_response([_text_block("yes ship it")])])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg("should i ship this?"), _ctx())
    kwargs = client.messages.create.await_args_list[0].kwargs
    assert kwargs["model"] == DEFAULT_OPUS_MODEL


@pytest.mark.asyncio
async def test_opus_prefix_strips_from_prompt_and_uses_opus(
    monkeypatch, system_prompt_path, telemetry_spy
):
    """The /opus routing prefix is stripped before the SDK call so
    the model never sees the routing instruction."""
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)
    client = _make_client([_response([_text_block("ok")])])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg("/opus tell me about the migration"), _ctx())
    kwargs = client.messages.create.await_args_list[0].kwargs
    assert kwargs["model"] == DEFAULT_OPUS_MODEL
    # Walk every message content for the /opus prefix marker.
    user_messages = [
        m for m in kwargs["messages"] if m.get("role") == "user"
    ]
    last_user = user_messages[-1]["content"]
    if isinstance(last_user, str):
        assert "/opus" not in last_user.lower()
        assert "tell me about the migration" in last_user.lower()
    else:
        # list-of-blocks shape
        text_segs = [
            b.get("text", "")
            for b in last_user
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        joined = " ".join(text_segs).lower()
        assert "/opus" not in joined


@pytest.mark.asyncio
async def test_warn_75_clamps_to_haiku_despite_decision_language(
    monkeypatch, system_prompt_path, telemetry_spy
):
    """Cost backstop overrides the decision-language signal."""
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)
    client = _make_client([_response([_text_block("haiku reply")])])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg("should i ship?"), _ctx(rung="warn_75"))
    kwargs = client.messages.create.await_args_list[0].kwargs
    assert kwargs["model"] == DEFAULT_HAIKU_MODEL


@pytest.mark.asyncio
async def test_hard_stop_rung_short_circuits_engine(
    monkeypatch, system_prompt_path
):
    """respond() already handles hard_stop_100 with cost_ladder_
    halted before the router runs. Verified by zero SDK calls."""
    client = _make_client([])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(
        _msg("anything"), _ctx(rung="hard_stop_100")
    )
    assert result.error == "cost_ladder_halted"
    assert client.messages.create.await_count == 0


# ---------------------------------------------------------------------------
# Tool-loop iteration routing — iteration 2 uses Opus
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iteration_two_switches_to_opus(
    monkeypatch, system_prompt_path, telemetry_spy
):
    """First call Haiku; tool_use response triggers iter 2; second
    call must be Opus."""
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)
    iter1 = _response(
        [_tool_use_block("toolu_a", "kora__get_operational_state", {})],
        stop_reason="tool_use",
    )
    iter2 = _response([_text_block("based on the data: ready")])
    client = _make_client([iter1, iter2])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg("status?"), _ctx())

    assert client.messages.create.await_count == 2
    call1 = client.messages.create.await_args_list[0].kwargs
    call2 = client.messages.create.await_args_list[1].kwargs
    assert call1["model"] == DEFAULT_HAIKU_MODEL
    assert call2["model"] == DEFAULT_OPUS_MODEL


# ---------------------------------------------------------------------------
# Post-call escalation — Haiku → Opus with context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_low_confidence_haiku_escalates_to_opus(
    monkeypatch, system_prompt_path, telemetry_spy
):
    """Haiku returns end_turn with an uncertainty marker → engine
    re-issues to Opus with Haiku's response in the messages array."""
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)

    haiku_text = "I'm not sure about that — possibly X but I can't verify."
    haiku_resp = _response([_text_block(haiku_text)])
    opus_resp = _response(
        [_text_block("Based on the data I can see, X is correct because…")]
    )
    client = _make_client([haiku_resp, opus_resp])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(_msg("what's the deal here?"), _ctx())

    # Two SDK calls: Haiku then Opus.
    assert client.messages.create.await_count == 2
    call1 = client.messages.create.await_args_list[0].kwargs
    call2 = client.messages.create.await_args_list[1].kwargs
    assert call1["model"] == DEFAULT_HAIKU_MODEL
    assert call2["model"] == DEFAULT_OPUS_MODEL

    # Opus call carries Haiku's response as conversation context.
    msgs = call2["messages"]
    # Find the assistant message we injected.
    assistant_messages = [m for m in msgs if m.get("role") == "assistant"]
    assert any(
        haiku_text in str(m.get("content", ""))
        for m in assistant_messages
    ), "Opus re-issue must include Haiku's response as assistant context"
    # And a follow-up user turn asking for a review.
    assert any(
        "review" in str(m.get("content", "")).lower()
        for m in msgs
        if m.get("role") == "user"
    )

    # Final result reflects Opus's reply.
    assert "Based on the data" in result.text


@pytest.mark.asyncio
async def test_high_confidence_haiku_does_not_escalate(
    monkeypatch, system_prompt_path, telemetry_spy
):
    """Haiku returns a substantive end_turn response → no Opus
    re-issue; exactly one SDK call."""
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)

    client = _make_client(
        [
            _response(
                [
                    _text_block(
                        "Daemon is ready. Three active alerts. No "
                        "blockers right now."
                    )
                ]
            )
        ]
    )
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg("status?"), _ctx())

    assert client.messages.create.await_count == 1
    assert (
        client.messages.create.await_args_list[0].kwargs["model"]
        == DEFAULT_HAIKU_MODEL
    )


@pytest.mark.asyncio
async def test_tool_use_response_does_not_trigger_post_call_escalation(
    monkeypatch, system_prompt_path, telemetry_spy
):
    """Iteration 1 with stop_reason=tool_use is NOT a candidate
    for post-call escalation — tool_use is its own signal; the
    next iteration's earning-signal (iteration >= 2) handles Opus
    selection."""
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)

    iter1 = _response(
        [
            # Even if there's a text block with low-confidence
            # words, the tool_use stop_reason short-circuits the
            # escalation check.
            _text_block("I'm not sure, let me look it up"),
            _tool_use_block("toolu_a", "kora__get_operational_state", {}),
        ],
        stop_reason="tool_use",
    )
    iter2 = _response([_text_block("ready")])
    client = _make_client([iter1, iter2])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg("status?"), _ctx())

    # Exactly TWO calls (iter1 + iter2 via the loop), NOT three.
    # If escalation fired wrongly, we'd see iter1 + Opus_escalation
    # + iter2 = 3.
    assert client.messages.create.await_count == 2


@pytest.mark.asyncio
async def test_escalation_uses_warm_cache_kwargs(
    monkeypatch, system_prompt_path, telemetry_spy
):
    """Opus re-issue must reuse the SAME system + tools blocks as
    the Haiku call so the prompt cache stays warm. Differences
    would burn the cache and triple our input cost."""
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)
    haiku_resp = _response([_text_block("I don't know.")])
    opus_resp = _response([_text_block("here's why X is true")])
    client = _make_client([haiku_resp, opus_resp])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg("explain something"), _ctx())

    call1 = client.messages.create.await_args_list[0].kwargs
    call2 = client.messages.create.await_args_list[1].kwargs
    assert call1["system"] == call2["system"]
    # tools structure preserved across the re-issue (cache key).
    assert call1.get("tools") == call2.get("tools")


# ---------------------------------------------------------------------------
# Telemetry — record_call invoked per API call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telemetry_records_haiku_call_with_slack_route(
    monkeypatch, system_prompt_path, telemetry_spy
):
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)
    client = _make_client([_response([_text_block("ok")])])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg("hi"), _ctx())

    assert len(telemetry_spy) >= 1
    call = telemetry_spy[0]
    assert call["route"] == "slack_dm"
    assert call["model"] == DEFAULT_HAIKU_MODEL
    assert call["escalated_to_opus"] is False


@pytest.mark.asyncio
async def test_telemetry_records_escalation_with_escalated_flag(
    monkeypatch, system_prompt_path, telemetry_spy
):
    """Post-call escalation produces two telemetry calls — the
    initial Haiku (escalated=False) and the Opus re-issue
    (escalated=True)."""
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)
    haiku_resp = _response([_text_block("i'm not sure")])
    opus_resp = _response([_text_block("here's the answer")])
    client = _make_client([haiku_resp, opus_resp])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg("explain something"), _ctx())

    assert len(telemetry_spy) == 2
    assert telemetry_spy[0]["model"] == DEFAULT_HAIKU_MODEL
    assert telemetry_spy[0]["escalated_to_opus"] is False
    assert telemetry_spy[1]["model"] == DEFAULT_OPUS_MODEL
    assert telemetry_spy[1]["escalated_to_opus"] is True


@pytest.mark.asyncio
async def test_telemetry_iteration_two_uses_tool_loop_route(
    monkeypatch, system_prompt_path, telemetry_spy
):
    """Iteration 2's record_call uses ROUTE_TOOL_LOOP_ITERATION
    so the cockpit can break out iteration-driven Opus spend from
    direct Opus calls."""
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)
    iter1 = _response(
        [_tool_use_block("toolu_a", "kora__get_operational_state", {})],
        stop_reason="tool_use",
    )
    iter2 = _response([_text_block("ready")])
    client = _make_client([iter1, iter2])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg("status?"), _ctx())

    assert len(telemetry_spy) == 2
    assert telemetry_spy[0]["route"] == "slack_dm"
    assert telemetry_spy[1]["route"] == "tool_loop_iteration"
    assert telemetry_spy[1]["model"] == DEFAULT_OPUS_MODEL
    assert telemetry_spy[1]["escalated_to_opus"] is True


@pytest.mark.parametrize(
    "source,expected_route",
    [
        ("slack_dm", "slack_dm"),
        ("email", "email_inbound"),
        ("mcp", "mcp_tool"),
        # KR-PROMOTE-EXPAND-AND-TELEMETRY-WIRES — the four sources
        # now extended in the bypass-path mapping. probe_investigation
        # was previously falling through to ROUTE_UNKNOWN despite the
        # wake_consumer setting source="probe_investigation".
        ("probe_investigation", "probe_investigation"),
        ("alert_investigation", "alert_investigation"),
        ("email_outbound_compose", "email_outbound_compose"),
        ("scheduled_task", "scheduled_task"),
        ("nope_unknown_source", "unknown"),
    ],
)
@pytest.mark.asyncio
async def test_telemetry_route_mapping_covers_all_sources(
    monkeypatch,
    system_prompt_path,
    telemetry_spy,
    source,
    expected_route,
):
    """Bypass-path source→route map covers every reserved source."""
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)
    client = _make_client([_response([_text_block("ok")])])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    message = IncomingMessage(
        text="hi",
        source=source,  # type: ignore[arg-type]
        received_at=datetime.now(timezone.utc),
        metadata={},
    )
    await engine.respond(message, _ctx())
    assert len(telemetry_spy) >= 1
    assert telemetry_spy[0]["route"] == expected_route
