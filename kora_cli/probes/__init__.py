"""Probe issue-detection + fix-attempt envelope declarations + wake-event
emission — KR-PROBE-AUDIT-AND-CONVERT (Lock R3-8 (b)).

Lives ALONGSIDE the existing ``kora_cli/heartbeat_probes/`` package
(the cheap cron observers). This package is the issue-detection +
wake layer: pure functions that classify ServiceHealthSnapshot
observations into operator-attention Issue objects, declarative
fix-attempt envelopes (all default OFF per fail-CLOSED discipline),
and the wake-event emitter that writes ``probe.wake_requested``
audit rows.

Per spec §2 Phase 1 audit: all 5 probes
(supabase / fly / vercel / sentry / doppler) are already cheap-
cron-only ($0 LLM cost). No conversion was needed in Phase 2 —
the package ships the issue-detection + envelope + wake layer
on top of the existing cheap probes.

Public surface:
  * :class:`Issue` — typed probe issue (per-probe criteria firing)
  * :func:`detect_issues` — pure function: snapshots → issues
  * :class:`FixEnvelope` — declarative fix-attempt envelope
  * ``ENVELOPES`` — per-probe envelope table (env-gated; default OFF)
  * :func:`is_envelope_enabled` — env-gated enable check
  * :func:`emit_wake_event` — audit-row emitter
"""

from kora_cli.probes.fix_envelopes import (
    ENVELOPES,
    FixEnvelope,
    is_envelope_enabled,
)
from kora_cli.probes.issue_detector import (
    Issue,
    IssueSeverity,
    detect_issue_for_snapshot,
    detect_issues,
)
from kora_cli.probes.wake_consumer import (
    ProbeWakeConsumer,
    WakeConsumeOutcome,
    format_fallback_text,
    format_investigation_prompt,
    format_operator_dm,
)
from kora_cli.probes.wake_emitter import emit_wake_event

__all__ = [
    "ENVELOPES",
    "FixEnvelope",
    "Issue",
    "IssueSeverity",
    "ProbeWakeConsumer",
    "WakeConsumeOutcome",
    "detect_issue_for_snapshot",
    "detect_issues",
    "emit_wake_event",
    "format_fallback_text",
    "format_investigation_prompt",
    "format_operator_dm",
    "is_envelope_enabled",
]
