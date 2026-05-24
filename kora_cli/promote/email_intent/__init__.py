"""Email-intent promotion loop — KR-PROMOTE-EMAIL-INTENT.

Sixth promotion loop (after phrasebook / snapshot-expand /
router-tuning / tool-trimming / probe-fix-envelopes). Observes
``intent.email_to_sea_ticket`` audit rows with
``action="logged_only"`` — Joshua-authored emails that no
existing intent regex matched — and proposes new regex patterns
to extend the email-intent registry.

# Loop shape

  1. :mod:`.observer` — read ``intent.email_to_sea_ticket`` rows
     where ``action="logged_only"``; extract the subject text +
     pattern_matched hint + reason.
  2. :mod:`.proposer` — embed subjects via the shared lexical
     embedder + cluster by similarity; for clusters meeting the
     min-size threshold, derive a candidate regex from common
     tokens + propose a default ``proposed_action_kind`` of
     ``"save_note"`` (operator picks at approve-time).
  3. :mod:`.plugin` — orchestrator + listener wiring + audit emit
     via ``promotion.email_intent_pattern_proposed``.

# Why subject-only clustering

The ``intent.email_to_sea_ticket`` audit payload intentionally
carries the subject text but NOT the body — PII discipline per
the existing module's security posture. v1 clusters on subjects
which is sufficient signal for "operator routinely sends emails
with this subject shape" — the proposer's pattern is a regex
the operator REVIEWS, so coarse signal is fine.

# Cost discipline

$0 LLM. The proposer uses the shared lexical embedder + greedy
clustering + token-frequency-based pattern derivation. Per
cycle: $0. Combined with the other 5 loops: still ≤$0.005/day
(phrasebook's Haiku synthesis remains the only LLM cost).

# Auto-apply

DEFAULT FALSE. Adding regex patterns to the intent registry
changes how Kora interprets operator-from emails (could
inadvertently auto-Sea_Ticket emails the operator didn't mean to
trigger). v1 is propose-only — operator manually scaffolds
approved patterns into ``kora_cli/intent/email_to_sea_ticket.py``
(follows the probe-fix-envelopes precedent from #193).
"""
