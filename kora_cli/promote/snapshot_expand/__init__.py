"""Snapshot-expand promotion loop — KR-PROMOTE-SNAPSHOT-EXPAND.

Second promotion loop (after :mod:`kora_cli.promote.phrasebook`).
Observes which tool calls fire during status-shaped reasoning queries
and proposes new snapshot fields that would have answered those
queries at $0 LLM cost.

# Loop shape

  1. :mod:`.observer` — read recent ``reasoning.tool_called`` audit
     rows; cluster by tool_name (proxy for "what was being asked").
  2. :mod:`.proposer` — for each cluster ≥ min_cluster_size, propose
     a new snapshot field whose collector would have returned the
     same answer.
  3. :mod:`.applier` — when AUTO_APPLY is OFF (v1 default), emit a
     ``promotion.snapshot_field_added`` audit row with
     ``action="proposed"``. When ON, also write a stub collector
     entry + audit ``action="auto_applied"``.
  4. :mod:`.cycle` — orchestrator the periodic-task listener calls.

# Cost discipline

Per ``feedback-promotion-loops-self-improving-subsystems``: target
~$0.01-0.05/day combined across promotion loops. This loop is
fully lexical (no LLM): clustering by tool_name groups exact-name
matches, and the proposer derives field names + summaries from
the cluster shape directly. Per cycle: **$0**. Daily ceiling
absorbed entirely by the phrasebook loop's ≤$0.005/day.

# Auto-apply safety

Per STOP-ASK §4 of the bucket spec: schema-bumping at runtime is
fragile. v1 ships with ``KORA_PROMOTE_SNAPSHOT_EXPAND_AUTO_APPLY=false``
by default — proposals land in the audit JSONL only, and operator
review (via a future cockpit endpoint or by reading the audit
seam directly) is the gate before any schema change. Operator
can flip the env once trust is built.
"""
