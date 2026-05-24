"""Tool-trimming promotion loop — KR-PROMOTE-TOOL-TRIMMING.

Fourth promotion loop. Reads ``reasoning.tool_called`` audit rows
per (route, tool_name) over an observation window and proposes
adding unused tools to a route's drop-list — so the LLM doesn't
spend tokens on tool descriptions it never invokes for that route.

# Loop shape

  1. :mod:`.observer` — tally tool calls by (route, tool_name) over
     the last N days from the audit JSONL.
  2. :mod:`.proposer` — for each route with ≥ min_total_calls,
     identify tools registered for the route that had ZERO calls in
     the window. Propose adding them to a drop-list. The list of
     tools registered per route is sourced from a snapshot of the
     reasoning engine's tool registry (or — when unavailable —
     the union of all tool names observed across routes).
  3. Store + audit + endpoint follow the phrasebook (#186) shape
     via :mod:`kora_cli.promote._shared.proposal_store`.
  4. :mod:`.plugin` — orchestrator + listener wiring.

# Cost discipline

$0 LLM. Pure audit-log scan + set diff. Per cycle: $0.

# Enforcement (deferred)

v1 is propose-only. Actual enforcement of the per-route drop-list
lands in the future KR-PLUGIN-TOOL-DESC-TRIM bucket, which will
read the approved set from the operator-curated config and
respect it inside the ``pre_tool_list_finalized`` hook (which is
a no-op today — see ``kora_cli/reasoning/kora_hermes_plugin/
plugin.py::_pre_tool_list_finalized``).
"""
