"""Tests for KR-CHEAP-PROMPT-CACHING — ephemeral cache markers +
cost-ladder accuracy on cache tokens.

Covers spec §2 + §3 acceptance:

  - kwargs["system"] is a content-block list with cache_control:
    {"type": "ephemeral"} on the only block (not a bare string)
  - kwargs["tools"]'s LAST tool carries cache_control: ephemeral;
    earlier tools do NOT (the marker covers everything up to and
    including the marked block, per Anthropic API semantics)
  - Empty tool list → tools kwarg omitted (the original wrapper
    behavior is preserved; no empty list ever sent)
  - Tool list is NOT mutated in place (caller's registry structure
    untouched between calls)
  - SAME system + tools structures sent on every iteration so the
    cache key matches across the tool-use loop
  - ResponseResult surfaces cache_creation_input_tokens +
    cache_read_input_tokens accumulated across iterations
  - Handler's _record_inference_to_cost_ladder passes the cache
    fields through to CanonicalUsage (cache_write_tokens +
    cache_read_tokens) so the cost-ladder bills at the correct
    rate (~1.25x for writes, ~0.1x for reads per the
    PricingEntry's cache_*_cost_per_million)
  - A pure-cache-read call (input_tokens=0 but cache_read_tokens>0)
    still bills — the early-return guard checks ALL token buckets
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from kora_cli.reasoning.anthropic_engine import (
    MODEL_OPUS,
    OAUTH_TOKEN_ENV,
    AnthropicReasoningEngine,
    _wrap_system_as_cacheable,
    _wrap_tools_as_cacheable,
)
from kora_cli.reasoning.engine import (
    ConversationContext,
    IncomingMessage,
    ResponseResult,
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
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
):
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    usage.cache_creation_input_tokens = cache_creation_input_tokens
    usage.cache_read_input_tokens = cache_read_input_tokens
    r = MagicMock()
    r.content = content
    r.stop_reason = stop_reason
    r.model = MODEL_OPUS
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
    p.write_text("You are Kora. Be useful.\n", encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _oauth(monkeypatch):
    monkeypatch.setenv(OAUTH_TOKEN_ENV, "sk-ant-oat-test")


# ---------------------------------------------------------------------------
# Wrapper unit tests — pure functions
# ---------------------------------------------------------------------------


def test_wrap_system_as_cacheable_returns_content_block_list():
    out = _wrap_system_as_cacheable("hello world")
    assert isinstance(out, list)
    assert len(out) == 1
    assert out[0] == {
        "type": "text",
        "text": "hello world",
        "cache_control": {"type": "ephemeral"},
    }


def test_wrap_tools_as_cacheable_marks_only_last_tool():
    """The cache marker on the last block caches everything UP TO
    AND INCLUDING that block — so we only mark the last."""
    tools_in = [
        {"name": "a", "description": "A", "input_schema": {}},
        {"name": "b", "description": "B", "input_schema": {}},
        {"name": "c", "description": "C", "input_schema": {}},
    ]
    out = _wrap_tools_as_cacheable(tools_in)
    assert len(out) == 3
    assert "cache_control" not in out[0]
    assert "cache_control" not in out[1]
    assert out[2]["cache_control"] == {"type": "ephemeral"}
    # Other fields preserved on the last tool.
    assert out[2]["name"] == "c"
    assert out[2]["description"] == "C"


def test_wrap_tools_as_cacheable_does_not_mutate_input():
    """Caller (the registry) holds a reference to the source list;
    we must NOT add cache_control to its dicts."""
    tools_in = [{"name": "a", "description": "A", "input_schema": {}}]
    _wrap_tools_as_cacheable(tools_in)
    assert "cache_control" not in tools_in[0]


def test_wrap_tools_as_cacheable_empty_returns_empty():
    """Empty tool list → empty wrapper (caller skips the tools=
    kwarg entirely; some SDK versions reject empty arrays)."""
    assert _wrap_tools_as_cacheable([]) == []


def test_wrap_tools_single_tool_gets_marker():
    """Edge case: only one tool — it IS the last."""
    out = _wrap_tools_as_cacheable(
        [{"name": "solo", "description": "S", "input_schema": {}}]
    )
    assert out[0]["cache_control"] == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# Engine integration — system block + tools block shape on SDK call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_sends_system_as_cacheable_content_block(
    monkeypatch, system_prompt_path
):
    """First SDK call's kwargs["system"] must be a list of content
    blocks with cache_control on the last (here: only) block — NOT
    a bare string."""
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)
    iter1 = _response([_text_block("hi")], stop_reason="end_turn")
    client = _make_client([iter1])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg(), _ctx())

    kwargs = client.messages.create.await_args_list[0].kwargs
    assert isinstance(kwargs["system"], list), (
        "system must be a content-block list (not a bare string) "
        "for cache_control to attach"
    )
    assert len(kwargs["system"]) == 1
    block = kwargs["system"][0]
    assert block["type"] == "text"
    assert block["text"] == "You are Kora. Be useful.\n"
    assert block["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_engine_marks_last_tool_with_cache_control(
    monkeypatch, system_prompt_path
):
    """tools= kwarg's last entry carries cache_control: ephemeral.
    Verifies the engine's wrap is engaged — not just the helper."""
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)
    iter1 = _response([_text_block("hi")], stop_reason="end_turn")
    client = _make_client([iter1])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg(), _ctx())

    kwargs = client.messages.create.await_args_list[0].kwargs
    tools = kwargs["tools"]
    assert len(tools) >= 1
    # Last tool marked.
    assert tools[-1]["cache_control"] == {"type": "ephemeral"}
    # Earlier tools NOT marked (if more than one).
    for t in tools[:-1]:
        assert "cache_control" not in t


@pytest.mark.asyncio
async def test_engine_omits_tools_kwarg_when_registry_empty(
    monkeypatch, system_prompt_path
):
    """Registry fails to load → tools=[] → SDK call must NOT include
    tools= at all (some SDK versions reject empty arrays). The
    cacheable wrapper preserves this contract."""
    monkeypatch.setattr(
        "kora_cli.reasoning.tool_registry.get_reasoning_available_tools",
        lambda: (_ for _ in ()).throw(RuntimeError("registry down")),
    )
    iter1 = _response([_text_block("hi")], stop_reason="end_turn")
    client = _make_client([iter1])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg(), _ctx())

    kwargs = client.messages.create.await_args_list[0].kwargs
    assert "tools" not in kwargs


@pytest.mark.asyncio
async def test_engine_sends_same_system_and_tools_each_iteration(
    monkeypatch, system_prompt_path
):
    """For the cache to HIT across iterations of the tool-use loop,
    the engine must send byte-identical system + tools structures
    on every iteration. The wrap is built ONCE outside the loop."""
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)
    iter1 = _response(
        [
            _tool_use_block("toolu_a", "kora__get_operational_state", {}),
        ],
        stop_reason="tool_use",
    )
    iter2 = _response([_text_block("done")], stop_reason="end_turn")
    client = _make_client([iter1, iter2])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    await engine.respond(_msg(), _ctx())

    call1_kwargs = client.messages.create.await_args_list[0].kwargs
    call2_kwargs = client.messages.create.await_args_list[1].kwargs
    # Same Python list object isn't required (the engine could
    # build per-iter) but the SHAPE + CONTENT must match.
    assert call1_kwargs["system"] == call2_kwargs["system"], (
        "system must be identical across iterations or the cache "
        "key won't match + we pay the cache-write premium per iter"
    )
    assert call1_kwargs["tools"] == call2_kwargs["tools"]


# ---------------------------------------------------------------------------
# Cache-token accounting — ResponseResult surfaces totals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_response_result_carries_cache_token_totals(
    monkeypatch, system_prompt_path
):
    """A response with cache_creation + cache_read in usage →
    ResponseResult.cache_creation_input_tokens +
    cache_read_input_tokens are populated."""
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)
    iter1 = _response(
        content=[_text_block("hi")],
        stop_reason="end_turn",
        input_tokens=200,
        output_tokens=50,
        cache_creation_input_tokens=1000,
        cache_read_input_tokens=500,
    )
    client = _make_client([iter1])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(_msg(), _ctx())
    assert result.cache_creation_input_tokens == 1000
    assert result.cache_read_input_tokens == 500
    assert result.input_tokens == 200
    assert result.output_tokens == 50


@pytest.mark.asyncio
async def test_cache_tokens_accumulate_across_iterations(
    monkeypatch, system_prompt_path
):
    """Tool-use loop with 3 iterations: cache_creation paid once
    on iter 1; cache_read on iters 2 + 3. Totals sum correctly."""
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)
    iter1 = _response(
        [_tool_use_block("toolu_a", "kora__get_operational_state", {})],
        stop_reason="tool_use",
        input_tokens=300,
        output_tokens=20,
        cache_creation_input_tokens=800,  # write — first call
        cache_read_input_tokens=0,
    )
    iter2 = _response(
        [_tool_use_block("toolu_b", "kora__get_health_rollup", {})],
        stop_reason="tool_use",
        input_tokens=350,
        output_tokens=15,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=800,  # read — cache hit
    )
    iter3 = _response(
        [_text_block("done")],
        stop_reason="end_turn",
        input_tokens=400,
        output_tokens=30,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=800,  # read — cache hit
    )
    client = _make_client([iter1, iter2, iter3])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(_msg(), _ctx())

    assert result.cache_creation_input_tokens == 800
    assert result.cache_read_input_tokens == 1600  # 800 + 800
    assert result.input_tokens == 1050  # 300 + 350 + 400


@pytest.mark.asyncio
async def test_cache_tokens_default_zero_when_usage_missing_attrs(
    monkeypatch, system_prompt_path
):
    """Older SDK versions OR uncached calls may leave the cache
    attrs absent on usage. getattr-defaults to 0 → ResponseResult
    fields default to 0 (no AttributeError)."""
    from kora_cli.listeners import mcp_tools

    monkeypatch.setattr(mcp_tools, "_get_active_provider", lambda: None)
    # Build a usage object WITHOUT the cache attrs.
    usage = MagicMock(spec=["input_tokens", "output_tokens"])
    usage.input_tokens = 100
    usage.output_tokens = 25
    r = MagicMock()
    r.content = [_text_block("hi")]
    r.stop_reason = "end_turn"
    r.model = MODEL_OPUS
    r.usage = usage

    client = _make_client([r])
    engine = AnthropicReasoningEngine(
        system_prompt_path=system_prompt_path, client=client
    )
    result = await engine.respond(_msg(), _ctx())
    assert result.cache_creation_input_tokens == 0
    assert result.cache_read_input_tokens == 0


# ---------------------------------------------------------------------------
# Handler cost-ladder integration — cache tokens flow through
# ---------------------------------------------------------------------------


def test_record_inference_passes_cache_tokens_to_canonical_usage():
    """When reasoning_meta carries cache_creation +
    cache_read, the handler constructs CanonicalUsage with
    BOTH cache_write_tokens AND cache_read_tokens populated —
    not just input/output."""
    from unittest.mock import MagicMock, patch

    # The cost-ladder helper is a @staticmethod on SlackDMHandler;
    # import via the module.
    from kora_cli.handlers import slack_dm_handler as h_mod

    fake_holder = MagicMock()
    captured_usage = []

    def _capture(usage, *, model_name, provider):
        captured_usage.append(usage)

    fake_holder.record_inference = _capture

    with patch.object(
        h_mod, "get_cost_holder", return_value=fake_holder, create=True
    ) as _mocked:
        # The helper imports lazily; we monkeypatch get_cost_holder
        # in the namespace it imports from.
        with patch(
            "agent.cost_state_holder.get_cost_holder",
            return_value=fake_holder,
        ):
            h_mod.SlackDMHandler._record_inference_to_cost_ladder(
                {
                    "model_used": "claude-opus-4-7",
                    "input_tokens": 200,
                    "output_tokens": 50,
                    "cache_creation_input_tokens": 1000,
                    "cache_read_input_tokens": 500,
                }
            )

    assert len(captured_usage) == 1
    u = captured_usage[0]
    assert u.input_tokens == 200
    assert u.output_tokens == 50
    assert u.cache_write_tokens == 1000
    assert u.cache_read_tokens == 500


def test_record_inference_handles_none_cache_fields():
    """Engine errored (cache_* in meta is None) → record_inference
    still bills the non-cache tokens; cache fields default to 0."""
    from unittest.mock import MagicMock, patch

    from kora_cli.handlers import slack_dm_handler as h_mod

    fake_holder = MagicMock()
    captured = []
    fake_holder.record_inference = lambda usage, **kw: captured.append(usage)

    with patch(
        "agent.cost_state_holder.get_cost_holder",
        return_value=fake_holder,
    ):
        h_mod.SlackDMHandler._record_inference_to_cost_ladder(
            {
                "model_used": "claude-opus-4-7",
                "input_tokens": 100,
                "output_tokens": 25,
                "cache_creation_input_tokens": None,
                "cache_read_input_tokens": None,
            }
        )

    assert len(captured) == 1
    assert captured[0].cache_write_tokens == 0
    assert captured[0].cache_read_tokens == 0
    assert captured[0].input_tokens == 100


def test_record_inference_bills_pure_cache_read_calls():
    """A call that's ENTIRELY served from cache (input_tokens=0,
    cache_read_tokens>0) must still bill — the early-return guard
    bailed only when EVERY token bucket was 0."""
    from unittest.mock import MagicMock, patch

    from kora_cli.handlers import slack_dm_handler as h_mod

    fake_holder = MagicMock()
    called = []
    fake_holder.record_inference = lambda usage, **kw: called.append(usage)

    with patch(
        "agent.cost_state_holder.get_cost_holder",
        return_value=fake_holder,
    ):
        h_mod.SlackDMHandler._record_inference_to_cost_ladder(
            {
                "model_used": "claude-opus-4-7",
                "input_tokens": 0,
                "output_tokens": 50,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 1500,
            }
        )

    assert len(called) == 1
    assert called[0].cache_read_tokens == 1500


def test_record_inference_skips_when_every_bucket_zero():
    """All token buckets 0 → skip the call (no real inference)."""
    from unittest.mock import MagicMock, patch

    from kora_cli.handlers import slack_dm_handler as h_mod

    fake_holder = MagicMock()
    called = []
    fake_holder.record_inference = lambda usage, **kw: called.append(usage)

    with patch(
        "agent.cost_state_holder.get_cost_holder",
        return_value=fake_holder,
    ):
        h_mod.SlackDMHandler._record_inference_to_cost_ladder(
            {
                "model_used": "claude-opus-4-7",
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            }
        )

    assert called == []
