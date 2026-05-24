"""Pure helpers for the post-call Opus escalation re-issue.

The hook handler in ``plugin.py`` is responsible for orchestrating
the re-issue; this module holds the pure functions it composes
with so they're independently testable.

Two responsibilities:

  1. :func:`extract_first_text` — pull the first text block out of
     an Anthropic ``Messages`` response. Used to feed
     :func:`should_escalate_post_call` from the cost_ladder
     selector + to build the Haiku-context assistant turn.
  2. :func:`build_opus_reissue_kwargs` — mutate a copy of the
     original ``api_kwargs`` into the Opus re-issue form: same
     conversation prefix + Haiku response as an assistant turn +
     a one-liner reviewer prompt + ``model`` swapped to Opus.

Both are pure functions (no I/O, no state). The hook handler is
where activation gating + telemetry side-effects live.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from kora_runtime.haiku_router.constants import (
    MODEL_OPUS,
    REISSUE_REVIEW_PROMPT,
)


def extract_first_text(response: Any) -> str:
    """Return the first text block from an Anthropic ``Messages``
    response. Empty string on any extraction failure — caller
    treats that as "no Haiku text to escalate from" and bails.

    The Anthropic SDK shape is ``response.content`` →
    ``list[ContentBlock]`` where each block has ``type`` and
    (for text blocks) ``text``. Models can return content lists
    interleaving text + tool_use; we only want the text. We
    concatenate ALL text blocks because some models emit the
    user-visible answer across multiple text blocks (e.g. when
    extended thinking is enabled the answer can split).
    """
    if response is None:
        return ""

    content = getattr(response, "content", None)
    if not isinstance(content, list):
        return ""

    parts: List[str] = []
    for block in content:
        # SDK content block (pydantic model).
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text = getattr(block, "text", "")
            if isinstance(text, str) and text:
                parts.append(text)
            continue
        # Dict fallback (Mock-friendly + non-SDK callers).
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if isinstance(text, str) and text:
                parts.append(text)

    return "\n".join(parts).strip()


def extract_last_user_text(api_kwargs: Dict[str, Any]) -> str:
    """Return the most recent user-turn text from
    ``api_kwargs["messages"]``. Used as ``original_message_text``
    when calling :func:`should_escalate_post_call`.

    The Anthropic ``messages`` shape is a list of
    ``{"role": "user"|"assistant", "content": str | list[block]}``.
    We walk backwards looking for the first user turn + flatten
    its content to text. Returns "" on any extraction failure.
    """
    if not isinstance(api_kwargs, dict):
        return ""

    messages = api_kwargs.get("messages")
    if not isinstance(messages, list):
        return ""

    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # Content-block list — flatten text parts.
            parts: List[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if isinstance(text, str):
                        parts.append(text)
            return "\n".join(parts).strip()
        return ""

    return ""


def build_opus_reissue_kwargs(
    *,
    api_kwargs: Dict[str, Any],
    haiku_response_text: str,
    opus_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the new api_kwargs for the Opus re-issue.

    Strategy (parallel-Claude's pattern, R3 origin): instead of
    a cold Opus call that redoes Haiku's work, include Haiku's
    response as an assistant turn and ask Opus to confirm-or-
    improve. Opus often returns a one-liner confirmation — ~30%
    cheaper escalations.

    Returns a NEW dict; the input ``api_kwargs`` is not mutated.
    The returned kwargs share top-level non-message refs with
    the input (system prompt, tools, max_tokens, etc.) — only
    ``model`` + ``messages`` are replaced.
    """
    new_kwargs = dict(api_kwargs)
    new_kwargs["model"] = opus_model or MODEL_OPUS

    original_messages = api_kwargs.get("messages")
    if not isinstance(original_messages, list):
        original_messages = []

    new_messages = list(original_messages)
    new_messages.append(
        {"role": "assistant", "content": haiku_response_text}
    )
    new_messages.append(
        {"role": "user", "content": REISSUE_REVIEW_PROMPT}
    )
    new_kwargs["messages"] = new_messages

    return new_kwargs


__all__ = [
    "build_opus_reissue_kwargs",
    "extract_first_text",
    "extract_last_user_text",
]
