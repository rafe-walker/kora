"""Caching markers — pure functions for wrapping system + tools
with Anthropic's ``cache_control: ephemeral`` breakpoints.

Moved verbatim from ``kora_cli/reasoning/anthropic_engine.py``
per KR-PLUGIN-EXTRACTIONS-BATCH-2 (Deliverable B). The engine
retains a re-import shim so existing callers (the engine's own
``_make_request_kwargs`` at line ~481 + any external consumer)
keep resolving the symbols.

# Why pure functions live here

``_wrap_system_as_cacheable`` + ``_wrap_tools_as_cacheable`` have
zero plugin-context dependency — they're just dict shape
transforms. Living in the plugin package lets the cost-ladder
hook (and a future split-out caching hook) import them as
sibling-plugin code instead of reaching into the engine module
for them.
"""

from __future__ import annotations

from kora_cli.reasoning.kora_hermes_plugin.caching.constants import (
    CACHE_CONTROL_EPHEMERAL,
)


def _wrap_system_as_cacheable(system_prompt: str) -> list[dict]:
    """Convert a bare-string system prompt to a content-block list
    with ``cache_control: ephemeral`` on the last block (here:
    the only block).

    The SDK accepts ``system: str`` OR
    ``system: list[{type: "text", text: str, cache_control?: ...}]``.
    The list form is required to attach the cache marker.
    """
    return [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": CACHE_CONTROL_EPHEMERAL,
        }
    ]


def _wrap_tools_as_cacheable(tools: list[dict]) -> list[dict]:
    """Return a NEW list of tool descriptors with
    ``cache_control: ephemeral`` on the FINAL tool.

    The marker on the last tool covers the entire tool block
    (system prompt's tool-system additions + every preceding
    tool's schema). We never mutate the input list — caller
    holds a reference to the registry's structures and we don't
    want to surprise them with a side-effect.

    Empty input → empty output (caller skips ``tools=`` kwarg).
    """
    if not tools:
        return []
    wrapped: list[dict] = []
    for i, tool in enumerate(tools):
        if i == len(tools) - 1:
            # Last tool — attach the cache marker. Copy the dict so
            # we don't mutate the registry's source structure.
            new_tool = dict(tool)
            new_tool["cache_control"] = CACHE_CONTROL_EPHEMERAL
            wrapped.append(new_tool)
        else:
            wrapped.append(tool)
    return wrapped
