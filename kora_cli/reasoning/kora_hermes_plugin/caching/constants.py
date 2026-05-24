"""Cache-control marker shapes per Anthropic's prompt-caching docs.

Per Anthropic API docs:
https://platform.claude.com/docs/en/agents-and-tools/prompt-caching

Cache breakpoints are marked with
``cache_control: {"type": "ephemeral"}``. The marker on a block
caches everything UP TO AND INCLUDING that block. Kora uses TWO
breakpoints (API allows up to 4):

  1. The system prompt — static across all calls.
  2. The tool list — static unless the tool registry mutates.

Cache TTL is ~5 minutes on Anthropic's side. Active reasoning
sessions hit the warm cache repeatedly (~90% discount on the
cached portion). Idle agent pays the cache-write premium (~25%
above base rate) on the first call post-idle, then reads cheap
until idle again. Net expected effect: ~50% input-cost
reduction on warm sessions.
"""

from __future__ import annotations

from typing import Final


# The cache_control marker the API expects on a content block /
# tool descriptor. ``ephemeral`` is the only marker Anthropic
# supports today.
CACHE_CONTROL_EPHEMERAL: Final[dict] = {"type": "ephemeral"}
