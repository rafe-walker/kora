"""Audit seam vocabulary for the kora_hermes plugin's audit
sub-plugin.

The canonical ``SeamName`` literal type lives at
``kora_cli/audit/jsonl_sink.py``; that's the authoritative wire-
contract for the JSONL rows. This module names the subset of
seams that the Hermes-plugin-side writer uses (today: only
``reasoning.tool_called``). Future plugin-resident seams (e.g.
``hermes.post_llm_call``) get appended here.
"""

from __future__ import annotations

from typing import Final, Tuple


# Seams emitted from the Hermes-plugin side. Each entry must
# also appear in ``kora_cli.audit.jsonl_sink.SeamName`` — drift
# would cause Pydantic validation to reject the row.
AUDIT_SEAMS: Final[Tuple[str, ...]] = ("reasoning.tool_called",)


# Source field literal for reasoning-tool audit rows. Matches
# ``kora_cli.audit.jsonl_sink.SourceName`` ``"reasoning"`` entry.
AUDIT_SOURCE_REASONING: Final[str] = "reasoning"
