"""Probe wake-event emitter — KR-PROBE-AUDIT-AND-CONVERT.

Writes one ``probe.wake_requested`` audit row when a probe's
issue criterion fires (per :mod:`kora_cli.probes.issue_detector`).

# Architecture decision (proposed via STOP-ASK)

Spec §4 STOP-ASK #1 noted no existing wake-Kora-on-event mechanism
exists for non-alert events. The PR body's audit table proposes
this seam: an audit JSONL row is the conduit. A follow-on bucket
(KR-PROBE-WAKE-CONSUMER) will register a watcher on the audit
log that, when fresh ``probe.wake_requested`` rows appear AND the
relevant envelope is enabled, invokes the reasoning engine with
``route="probe_investigation"`` (the telemetry literal already
accepted by PR #161).

v1 ships JUST the emission. Benefits:

  * Operator-visible: the audit panel already renders this seam
    (rendering is shape-driven; ``probe.wake_requested`` rows
    appear alongside the other 5 seams).
  * Alerts integration: the snapshot's ``alerts.by_category``
    bucket will reflect probe-derived issues via the existing
    ``service_unhealthy`` aggregator rule (also unchanged).
  * No risk of cost surprises: zero LLM cost on the emission
    path; the consumer side is gated separately.

# Wake-event payload

Audit row ``details`` shape (consumed by the future wake-listener
+ surfaced in the audit panel):

  - ``probe``: which probe surfaced the issue
  - ``severity``: critical | warning | info
  - ``category``: matches alerts vocabulary (``service_unhealthy``)
  - ``title``: short title
  - ``detail``: longer detail (operator-readable)
  - ``snapshot_details``: the probe's own ``details`` dict
  - ``envelope_enabled``: bool — is the per-probe auto-fix
    envelope opted in? Lets the consumer branch on "investigate
    only" vs "investigate + attempt fix"
  - ``envelope_fix_name``: short id of the envelope (or "(none)")

# Telemetry route literal

When the future consumer side invokes reasoning in response to a
wake event, it bills via ``record_inference(route="probe_investigation")``
(already accepted by PR #161's taxonomy). This module does NOT
itself invoke reasoning — emission is $0 LLM cost.
"""

from __future__ import annotations

import logging
from typing import Optional

from kora_cli.probes.fix_envelopes import ENVELOPES, is_envelope_enabled
from kora_cli.probes.issue_detector import Issue

logger = logging.getLogger(__name__)


def emit_wake_event(issue: Issue) -> None:
    """Write one ``probe.wake_requested`` audit row for ``issue``.

    Fail-soft: an audit-write error logs + returns; never raises
    (the probe runner's post-hook can't crash the heartbeat
    scheduler).

    The audit row is the wake-event conduit. The consumer side
    (reasoning-engine wake listener) is a follow-on bucket; v1
    surfaces the event to the audit panel so operator visibility
    is the immediate observable.
    """
    try:
        from kora_cli.audit import emit_audit
    except Exception as exc:
        logger.warning(
            "[kora.probes.wake] audit import failed: %r — wake event "
            "not recorded for issue=%s",
            exc,
            issue.id,
        )
        return

    envelope = ENVELOPES.get(issue.probe)
    envelope_fix_name = envelope.fix_name if envelope is not None else "(none)"
    envelope_enabled = is_envelope_enabled(issue.probe)

    details = {
        "probe": issue.probe,
        "severity": issue.severity,
        "category": issue.category,
        "title": issue.title,
        "detail": issue.detail,
        "snapshot_details": dict(issue.details),
        "envelope_enabled": envelope_enabled,
        "envelope_fix_name": envelope_fix_name,
    }
    try:
        emit_audit(
            seam="probe.wake_requested",
            details=details,
            source=None,
        )
    except Exception as exc:
        logger.warning(
            "[kora.probes.wake] emit_audit raised %r — wake event "
            "not recorded for issue=%s",
            exc,
            issue.id,
        )
        return

    logger.info(
        "[kora.probes.wake] probe=%s severity=%s envelope_enabled=%s "
        "envelope=%s",
        issue.probe,
        issue.severity,
        envelope_enabled,
        envelope_fix_name,
    )
