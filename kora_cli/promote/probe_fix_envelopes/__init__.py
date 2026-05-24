"""Probe-fix-envelope promotion loop — KR-PROMOTE-PROBE-FIX-ENVELOPES.

Fifth and final promotion loop. Observes recurring probe failures
where Kora's investigation summaries point at a consistent
recommended fix, and proposes adding a new envelope action to
``probes/fix_envelopes.py``.

# Loop shape

  1. :mod:`.observer` — read ``probe.investigation_completed``
     (#184) + ``tool.probe_autofix_attempted`` (#182) audit rows.
     Cluster by ``probe`` + ``issue_category`` over the
     observation window; carry ``investigation_summary_text`` and
     fix-attempt outcomes so the proposer can see the pattern.
  2. :mod:`.proposer` — for each cluster ≥ min_cluster_size,
     propose a new envelope action with:
       * ``fix_name_suggestion`` — derived from probe + category
       * ``recurring_recommendation_text`` — extracted from the
         cluster's investigation summaries
       * ``blast_radius_summary`` — operator-facing risk
         description (defaults to "operator must review")
  3. Store + audit + endpoint follow the phrasebook (#186) shape.
  4. :mod:`.plugin` — orchestrator + listener wiring.

# Cost discipline

$0 LLM. Pure audit-log scan + lexical cluster + text projection.

# Auto-apply (HARDCODED FALSE per spec)

Per spec §2 deliverable C: auto-apply is HARDCODED FALSE for v1.
Adding operator-authorized fix actions to Kora's envelope file
mutates what Kora is permitted to do to production infra without
operator-in-the-loop. Per
``feedback-fail-closed-by-default-for-security-infra`` and
``feedback-promotion-loops-self-improving-subsystems``: never
auto-apply high-risk loops.

# Approval path (manual scaffolding — STOP-ASK §4 mitigation)

Per spec STOP-ASK §4: writing to declarative ``fix_envelopes.py``
via codegen is fragile. The approve endpoint transitions the
proposal status + emits ``promotion.approved`` — it does NOT
modify ``fix_envelopes.py``. Operator manually scaffolds the
approved envelope into the file (the proposal payload carries
the suggested ``FixEnvelope(...)`` shape verbatim so the copy-
paste is mechanical). Approved proposal sits in the
``promotions/probe_fix_envelopes/approved/`` directory as the
audit trail.
"""
