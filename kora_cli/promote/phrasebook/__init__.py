"""Phrasebook promotion loop — KR-PROMOTE-PHRASEBOOK-FOUNDATION.

# Loop shape

  1. :mod:`.observer` — collect last-N-days of slack_dm_log entries
     where Kora's reasoning engine ran (NOT short-circuit hits;
     short-circuits already work).
  2. :mod:`.proposer` — cluster the observations + generate one
     phrasebook proposal per cohesive cluster meeting the
     min_cluster_size / cohesion / answer-consistency thresholds.
  3. :mod:`.store` — persist proposals as JSON files under
     ``${KORA_HOME}/promotions/phrasebook/{pending,approved,rejected}/``
     plus emit the corresponding promotion.* audit row.
  4. :mod:`.cycle` — orchestrator called by the cron task.
  5. Operator reviews via cockpit; approve / reject endpoints
     transition status + (on approve) PUT to the live phrasebook
     using the existing #177 editor with
     actor="kora_proposal_approved".

# Cost discipline

Per ``feedback-promotion-loops-self-improving-subsystems``: target
~$0.01-0.05/day. The embedder is lexical ($0); the proposer
optionally synthesizes one Haiku-driven reply template per
proposal (~$0.001 each). With min_cluster_size=5 + typical
operator-DM volume, expect ≤5 proposals per cycle → ≤$0.005/day.
Lots of headroom for future loops.
"""
