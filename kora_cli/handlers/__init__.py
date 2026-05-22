"""Per-source webhook handlers (KR-FEAT-SLACK-DM, KR-FEAT-EMAIL, ...).

Distinct from ``kora_cli/listeners/`` (which owns the daemon-coordinator
lifecycle + transport-layer routing). A handler module takes a
verified-payload dict from a listener + drives Kora-specific logic:
identity filtering, state-gating, persistence, downstream emits.
"""
