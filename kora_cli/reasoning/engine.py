"""ReasoningEngine protocol + canonical value classes.

Defines the abstract surface a Kora reasoning implementation must
expose. ST1 ships :class:`AnthropicReasoningEngine` against this
protocol (``kora_cli/reasoning/anthropic_engine.py``); future
buckets may swap providers (Bedrock / Vertex / a different model
family) without touching the handler-side call site.

# Why a Protocol vs an ABC

Protocol composes more cleanly with the existing test seam (the
handler accepts an injected engine instance; tests pass a
``MagicMock(spec=ReasoningEngine)`` and don't need to subclass).
ABCs would force a synthetic test class per case.

# Field-naming convention (K-DG locked)

Every typed structure in this module ships its actual field names
verbatim in this docstring + the dataclass definitions. Per the
2026-05-22 PM-locked standing rule
(``feedback_k_dg_substrate_field_names_in_specs``), the bucket
spec's paraphrased shapes are NOT authoritative; this module is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Protocol


# ---------------------------------------------------------------------------
# Source enum (literal, not Enum — keeps JSON serialization trivial)
# ---------------------------------------------------------------------------


MessageSource = Literal["slack_dm", "email", "mcp", "probe_investigation"]

# Cost-ladder rung as Kora's reasoning sees it. These are the
# ``.value`` strings of ``agent.cost_state_holder.CostRung`` — the
# canonical-name mapping is one-shot at the listener boundary so the
# rest of the reasoning code consumes the canonical strings.
#
# Per K-DG check on KR-P2-K cascade: actual CostRung values are
# "normal" / "warn_75" / "downshift_90" / "hard_stop_100"
# (NOT the bucket spec's "normal" / "warned" / "constrained" / "halted").
CostLadderRungName = Literal[
    "normal", "warn_75", "downshift_90", "hard_stop_100", "unknown"
]


# ---------------------------------------------------------------------------
# Value classes — inbound side
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """A message arriving at Kora's reasoning layer.

    Fields:

      - ``text``: full inbound message text (NO truncation here;
        the reasoning engine may want context).
      - ``source``: which surface the message arrived on.
      - ``received_at``: wall-clock when the listener accepted it.
        UTC-tagged. The handler sets this from inbound JSONL's
        ``received_at`` (round-trippable ISO 8601).
      - ``metadata``: source-specific bag. For ``slack_dm``:
        ``{channel_id, thread_ts, user_id, event_ts}``. For
        ``email``: ``{subject, from, message_id}``. For ``mcp``:
        ``{caller_actor_kind, tool_name}``. Stable per-source
        schema — but typed as ``dict[str, Any]`` to keep this
        module source-agnostic.
    """

    text: str
    source: MessageSource
    received_at: datetime
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """One prior message in the same thread, in chronological order.

    The context loader pulls these from the inbound/outbound JSONL
    sliced to (channel_id, thread_ts) + most-recent N.
    """

    direction: Literal["inbound", "outbound"]
    text: str
    at: datetime


@dataclass(frozen=True, slots=True)
class ConversationContext:
    """Per-call context the reasoning engine consumes alongside the
    fresh ``IncomingMessage``.

    Fields:

      - ``recent_messages``: prior turns in the SAME thread,
        oldest→newest. Bounded at the loader (default 10) — the
        engine may further truncate if its token budget requires.
      - ``current_operational_state``: lowercase string of the
        active ``PrimaryState`` (``"booting"`` / ``"ready"`` /
        ``"active"`` / ``"paused"`` / ``"stopped"`` — verified
        against ``agent/operational_state.py:70-74``).
      - ``current_cost_ladder_rung``: lowercase string of the active
        ``CostRung`` (see ``CostLadderRungName``).
      - ``extra``: future-extension bag — currently unused; engine
        impls may ignore.
    """

    recent_messages: List[ConversationTurn] = field(default_factory=list)
    current_operational_state: str = "unknown"
    current_cost_ladder_rung: CostLadderRungName = "unknown"
    extra: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Value classes — response side
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResponseResult:
    """The reasoning engine's output.

    The handler checks ``error`` first; if non-None, the engine
    failed (cost-ladder halt / paused / SDK exception / etc.). The
    handler falls back to a canned response and records the error
    in JSONL.

    On success (``error is None``):

      - ``text``: the response body to send back via the source's
        outbound channel.
      - ``model_used``: actual model string from the SDK response
        (e.g. ``"claude-opus-4-7"``). Cost-ladder downshift means
        this may be a different model than the ladder ``normal``
        default.
      - ``input_tokens`` + ``output_tokens``: from the SDK response
        usage block; handler passes these into the cost-ladder
        ``record_inference()`` write.
      - ``reasoning_duration_ms``: wall-clock from the engine's
        own timer (NOT the SDK's; includes prompt assembly +
        network).

    On failure (``error`` set):

      - ``error``: short machine-readable code:
        ``"cost_ladder_halted"`` / ``"operational_state_paused"`` /
        ``"sdk_timeout"`` / ``"sdk_auth"`` / ``"sdk_rate_limited"``
        / ``"sdk_5xx"`` / ``"sdk_4xx_<code>"`` /
        ``"sdk_transport"`` / ``"unconfigured"``.
      - Other fields may be partially populated (e.g.
        ``reasoning_duration_ms`` is set even on failure to surface
        slow-failure paths).
    """

    text: str
    model_used: str
    input_tokens: int
    output_tokens: int
    reasoning_duration_ms: int
    error: Optional[str] = None
    # KR-FEAT-AGENTIC-REASONING ST1 — tool-use audit trail. List of
    # tool names Kora actually invoked during this response
    # (oldest→newest, may contain duplicates if the same tool was
    # called across iterations). Empty list when the response used
    # no tools (pure chat-completion path) or when the engine
    # short-circuited on a refuse-path (paused / cost-halted / etc.).
    #
    # Handler surfaces this in the outbound JSONL ``tools_used``
    # field (KR-FEAT-AGENTIC-REASONING ST2 wires the persistence).
    # ``field(default_factory=list)`` keeps the dataclass
    # backwards-compatible — existing ResponseResult construction
    # without this kwarg still works.
    tools_used: List[str] = field(default_factory=list)
    # KR-CHEAP-PROMPT-CACHING — cache-token totals across all
    # iterations of the tool-use loop. Both are 0 when no caching
    # was used (uncached call, engine refused, or model didn't
    # surface cache usage). Default 0 keeps the dataclass
    # backwards-compatible. Handler reads these to bill against
    # the cost-ladder's cache_read / cache_write rates rather than
    # the full input-token rate.
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


# ---------------------------------------------------------------------------
# Protocol — what a reasoning engine must implement
# ---------------------------------------------------------------------------


class ReasoningEngine(Protocol):
    """The single method handlers call.

    Implementations:

      - Read the cost-ladder rung + operational state from
        ``context`` (NOT directly from the holders — keeps the
        engine pure-functional + testable; listener boundary
        wraps the holders).
      - Refuse to call when the rung is ``hard_stop_100`` or the
        operational state is ``paused`` / ``stopped`` (returns a
        ResponseResult with ``error`` set; the handler falls back).
      - Sanitize all errors so credentials don't leak.

    ``respond`` is the only required method. ``close`` is optional
    (defaults to a no-op via the Protocol's structural typing).
    """

    async def respond(
        self,
        message: IncomingMessage,
        context: ConversationContext,
    ) -> ResponseResult:
        ...

    async def close(self) -> None:
        ...
