"""Router-tuning promotion loop — KR-PROMOTE-ROUTER-TUNING.

Third promotion loop. Reads per-route escalation telemetry +
proposes operator review for routes whose Haiku-to-Opus escalation
pattern suggests trigger tuning.

# Loop shape

  1. :mod:`.observer` — read ``cost_telemetry.snapshot()`` per-route
     ``escalation_count`` / ``calls_count`` for the rolling 24h
     window. Per-route quality data ("was the Opus reply materially
     better than Haiku?") isn't exposed today — see ``plugin`` for
     the v1 scope decision.
  2. :mod:`.proposer` — score each route by escalation_rate +
     volume. High-rate routes get a ``tighten_review``
     recommendation; routes with notably-low escalation despite
     repeated operator ``/opus`` overrides (future-data) would get a
     ``loosen_review``. v1 only ships ``tighten_review`` since the
     override-observation data isn't yet collected.
  3. Store + audit + endpoint follow the phrasebook (#186)
     pending/approve/reject template via
     :mod:`kora_cli.promote._shared.proposal_store`.
  4. :mod:`.plugin` — orchestrator + listener wiring.

# Cost discipline

$0 LLM. The proposer reads telemetry counters + emits proposals
purely from threshold math. Per cycle: $0. Combined with the other
4 loops, still inside the [[feedback-promotion-loops-self-improving-
subsystems]] $0.01-0.05/day target.

# Auto-apply

DEFAULT FALSE. Router-tuning changes trigger patterns that affect
every reasoning call; operator MUST review before any change.
"""
