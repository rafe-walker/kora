"""**LOCAL** JSONL audit sink — KR-AUDIT-JSONL-SINK.

⚠ This module writes to a **LOCAL FILE** at
``<KORA_HOME>/kora_audit_log.jsonl``. It is **NOT** the substrate
audit emit path. For substrate chain-event emit (the
``kora__append_event`` SECDEF surface), use
``isokron_client.events.emit_kora_event`` instead.

The renamed module name (``local_jsonl_sink``, was ``jsonl_sink``
pre-KR-KORA-PIP-RESTRUCTURE-PHASE-1B 2026-05-24) reflects this
distinction — the original name conflated local-file and
substrate-event audit. Backward-compat: a thin shim file at
``kora_cli/audit/jsonl_sink.py`` re-exports the public surface
from here so the ~20 existing callers keep working unchanged.

Promotes the 5 existing structured-log audit seams to ALSO write
JSONL rows that operator panels can consume programmatically.

# The 4 audit seams (1 covers MCP read + mutating)

  - ``mcp.tool_called`` — distinguished read vs mutating via
    ``details.tool_kind = "read" | "mutating"``
  - ``webhook.dead_letter``
  - ``slack_dm.reply_failed``
  - ``reasoning.tool_called``

# Dual-write contract

:func:`emit_audit` ALWAYS writes both:

  1. The structured-log line ``[kora.<seam>] key=value ...`` so
     existing operator grep / flyctl logs workflows keep working
     (no breaking change to the operator surface).
  2. A JSONL row to ``<KORA_HOME>/kora_audit_log.jsonl`` (path
     override via ``KORA_AUDIT_LOG_PATH``) so panels can SELECT-
     style consume audit data without parsing log output.

JSONL write failures (OSError on disk full / volume unmounted /
etc.) WARN-log + continue — never crash the caller. The
structured-log line still emits.

# Security posture

The ``details`` dict is the seam-specific payload. Callers MUST
pass only safe identifiers + machine codes + counts — NEVER:

  - bearer tokens / API keys / signing secrets / SMTP passwords
  - raw Slack DM bodies
  - raw email bodies
  - email subjects (may carry PII via "Re: <something private>")

The :func:`emit_audit` helper does NOT filter — it trusts callers
to pass clean dicts. The test surface
(``tests/kora_cli/audit/test_jsonl_sink.py``) walks a synthetic
batch of all 5 seam shapes against token-shape regexes (xoxb-,
sk-ant-, Bearer, etc.) + PII regex (email addresses, raw user
text) to assert no leakage. Adding a new field to a seam's
``details`` requires updating the per-seam allow-list test.

# File rotation

NOT handled by code. Operator manages via standard tools
(logrotate / Fly log-tailing to an external store). Documented
in ``kora_runtime_first_deploy_runbook.md``'s "Operator
obligations (ongoing)" section.

# Substrate-backed promotion path

When IsoKron PM ships the audit-ledger contract (coord ask
2026-05-22), :func:`emit_audit` extends to ALSO write a substrate
event_log row. Panels continue reading the same JSONL shape OR
move to substrate reads. The structured-log seam stays as the
operator-grep escape hatch.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums (Literal types) — wire-stable strings shared with panels
# ---------------------------------------------------------------------------


SeamName = Literal[
    "mcp.tool_called",
    "webhook.dead_letter",
    "slack_dm.reply_failed",
    "reasoning.tool_called",
    # KR-ALERT-NOTIFY — alert push notifications (Slack DM /
    # email) dispatched by the alert_notifier_listener periodic
    # task. Each new-fire dispatch emits one entry regardless of
    # success/failure; failures are visible in the audit panel
    # alongside the alerts panel.
    "notification.dispatched",
    # KR-PROBE-AUDIT-AND-CONVERT — per-probe issue-detection wake
    # signal. Probe cron post-hook detects an issue criterion
    # crossing, writes one of these to flag that Kora's reasoning
    # SHOULD investigate. The consumer side (reasoning-engine
    # wake) is a follow-on bucket (KR-PROBE-WAKE-CONSUMER); v1
    # ships the emission for operator visibility via the audit
    # panel + alerts panel without invoking LLM.
    "probe.wake_requested",
    # KR-EMAIL-OUTBOUND-COMPOSE-TOOL — outbound counterpart to the
    # email-to-sea_ticket intent seam. Emitted by
    # ``kora__send_email_to_operator`` (the reasoning-loop-callable
    # outbound tool) for every invocation, with ``details``
    # capturing ``status`` (``sent`` / ``rejected`` /
    # ``smtp_failure``), rejection reason if applicable, subject /
    # body / attachment sizes (no body content), and
    # ``smtp_message_id`` on success. Recipient is always the
    # operator (pinned to ``KORA_EMAIL_JOSHUA_ADDRESS``) — never
    # caller-controllable, so the audit row doesn't need a
    # recipient field. Future KR-FE-EMAIL-INTENT-LOG-PANEL bucket
    # can render both inbound + outbound seams in the same
    # cockpit panel.
    "tool.email_to_operator_sent",
    # KR-PROBE-AUTOFIX-EXECUTION — Kora's reasoning loop attempted a
    # pre-approved fix action for a probe-detected issue. Emitted
    # by ``kora__attempt_probe_autofix`` for every invocation
    # (including rejections from envelope gates), with ``details``
    # capturing ``probe`` / ``action`` / ``target_id`` / operator-
    # facing ``reason_from_reasoning``, ``status`` (``attempted`` /
    # ``rejected`` / ``execution_failed``), rejection_reason on
    # rejects, before/after state on attempts, and
    # ``executor_duration_ms``. The reason field IS recorded
    # verbatim (unlike email body in tool.email_to_operator_sent)
    # because operator triage of "what did Kora decide and why" is
    # the primary use case. Source attribution is ``reasoning``
    # since invocations originate inside the reasoning loop.
    "tool.probe_autofix_attempted",
    # KR-PROBE-INVESTIGATION-DATA-COMPLETION — per-investigation
    # summary emitted by the wake consumer after reasoning + DM
    # complete (or fail). Closes the three V1NotesBanner gaps CC#2
    # surfaced in #171: dm_status (so the panel can render dm_sent
    # without joining slack_dm_log.jsonl in the FE), per-call cost
    # + model_used + token counts (so the panel doesn't have to
    # query CostTelemetry aggregates), and the investigation
    # summary text verbatim (operator-decision-relevant per the
    # #182 precedent — Kora-composed, no external-string leakage).
    # ``autofix_attempted`` is a back-reference to whether the
    # ``tool.probe_autofix_attempted`` seam fired with the same
    # caller_session_id during this investigation. Source is
    # ``reasoning`` since the emit happens inside the wake
    # consumer's reasoning flow.
    "probe.investigation_completed",
    # KR-INTENT-EMAIL-TO-SEA-TICKET — operator-driven Sea_Ticket
    # creation from inbound email. Emitted from the email-inbound
    # handler when intent recognition runs on a Joshua-authored
    # message: one entry per email evaluated, with ``details``
    # capturing the matched pattern + confidence + action taken
    # (``created`` / ``dry_run`` / ``logged_only`` /
    # ``cap_exceeded`` / ``failed``) and the resulting
    # ``ticket_id`` when a Sea_Ticket was written. Future
    # KR-FE-EMAIL-INTENT-LOG-PANEL surfaces this seam in the
    # cockpit.
    "intent.email_to_sea_ticket",
    # KR-FE-PHRASEBOOK-EDITOR-AND-CRUD — operator-driven phrasebook
    # edits via the cockpit PUT endpoint. Each successful write
    # emits one entry with entry_count_before / entry_count_after /
    # backup_filename so operator-attention triage can reconstruct
    # "when did the phrasebook change + did the change have a
    # backup to revert to." Future actor extension (e.g.
    # ``actor="kora_proposal_approved"`` from the promotion-loop
    # bucket) reuses this seam shape.
    "phrasebook.updated",
    # KR-PROMOTE-PHRASEBOOK-FOUNDATION — first promotion loop.
    # Three seams covering the propose → review → resolve lifecycle:
    #
    # ``promotion.proposed`` — proposer emits a new pending
    # phrasebook proposal after the daily clustering cycle.
    # Payload carries the full PromotionProposal projection
    # (proposal_id, cluster_size, sample_questions,
    # proposed_pattern, proposed_reply_template, proposed_category,
    # confidence, created_at) so operator can grep the JSONL for
    # proposal history without reading every proposal file. One
    # row per proposal; the per-cycle summary (count / total cost)
    # is logged via the structured-log line.
    "promotion.proposed",
    # ``promotion.approved`` — operator approves via the cockpit
    # endpoint. Payload: proposal_id + the committed phrasebook
    # entry shape (post any operator override edits). The
    # ``phrasebook.updated`` audit row that follows uses
    # actor="kora_proposal_approved" per #177 forward-compat —
    # so the promotion seam stays distinct from the editor audit
    # without the promotion-history view having to scan
    # ``phrasebook.updated`` for actor=proposal entries.
    "promotion.approved",
    # ``promotion.rejected`` — operator rejects. Payload:
    # proposal_id + ``review_notes`` (rejection rationale, written
    # verbatim — operator-decision-relevant per the #182 precedent
    # for reason fields). Proposal stays in the rejected/ store
    # directory for future promotion-loop tuning (clusters that
    # operator consistently rejects are signal to tune the
    # proposer thresholds).
    "promotion.rejected",
    # KR-PROMOTE-SNAPSHOT-EXPAND — second promotion loop. Observes
    # reasoning tool-calls during status-shaped queries + proposes
    # new snapshot fields that would have answered those queries at
    # $0 LLM cost. The single seam ``promotion.snapshot_field_added``
    # covers both the propose path (auto-apply OFF, v1 default) and
    # the apply path (auto-apply ON). Payload carries
    # proposal_id / proposed_field_path / proposed_collector_summary /
    # cluster_size / sample_tool_calls / action (one of
    # "proposed" / "auto_applied") / applier_diff_summary (only on
    # auto_applied). Source is ``reasoning`` since the cluster
    # input is reasoning audit.
    "promotion.snapshot_field_added",
    # KR-PROMOTE-ROUTER-TUNING — third promotion loop. Reads per-route
    # escalation counts from cost_telemetry + the rolling 24h /
    # monthly windows to surface routes whose Haiku-to-Opus
    # escalation rate suggests their trigger pattern could be tuned
    # (tightened to save Opus spend, OR loosened to avoid recurring
    # operator /opus overrides). Payload carries proposal_id /
    # route / escalation_count / total_calls / escalation_rate /
    # recommendation_kind ("tighten_review" | "loosen_review") /
    # rationale / created_at / status. Source is ``reasoning``.
    "promotion.router_trigger_proposed",
    # KR-PROMOTE-TOOL-TRIMMING — fourth promotion loop. Reads
    # ``reasoning.tool_called`` audit history per (route, tool_name)
    # over the observation window and proposes adding unused tools
    # to a route's drop-list (the pre_tool_list_finalized hook
    # consumer). Payload carries proposal_id / route /
    # unused_tools (list of names) / total_calls_for_route /
    # observation_window_days / created_at / status. Source is
    # ``reasoning``. v1 is propose-only; enforcement of the drop-
    # list lands in the future KR-PLUGIN-TOOL-DESC-TRIM bucket.
    "promotion.tool_trim_proposed",
    # KR-PROMOTE-PROBE-FIX-ENVELOPES — fifth promotion loop. Reads
    # ``tool.probe_autofix_attempted`` + ``probe.investigation_completed``
    # audits + clusters recurring probe failures whose investigation
    # summaries point at a consistent recommended fix. Proposes
    # adding a new envelope action to ``probes/fix_envelopes.py``.
    # HIGH-RISK: payload includes the cluster's recurring fix-text +
    # operator-facing blast radius description; auto-apply is
    # HARDCODED FALSE — operator MUST review and scaffold manually.
    # Payload: proposal_id / probe / fix_name_suggestion /
    # cluster_size / sample_investigation_ids /
    # recurring_recommendation_text / blast_radius_summary /
    # created_at / status. Source is ``reasoning``.
    "promotion.probe_envelope_action_proposed",
    # KR-ALERT-INVESTIGATION-WAKE-CONSUMER — per-investigation summary
    # for the alert wake consumer (parallels probe.investigation_completed).
    # Emitted by ``kora_cli/alerts/wake_consumer.py`` after reasoning +
    # DM dispatch complete. Payload: alert_id / category / severity /
    # model_used / input_tokens / output_tokens /
    # cache_creation_input_tokens / cache_read_input_tokens /
    # total_cost_usd / investigation_duration_ms /
    # investigation_summary_text / dm_status / autoaction_attempted
    # (v1 always false; reserved for future alert-envelope autoaction
    # parallel to probe autofix). Source is ``reasoning`` since the
    # emit happens inside the wake consumer's reasoning flow. CC#2
    # follow-on KR-FE-ALERT-INVESTIGATIONS-VIEWER reads this seam
    # alongside the existing notification.dispatched + slack_dm_log.jsonl
    # to render the 3-stream join (4 streams if/when autoaction lands).
    "alert.investigation_completed",
    # KR-PROMOTE-EMAIL-INTENT — 6th promotion loop. Reads
    # ``intent.email_to_sea_ticket`` rows with ``action="logged_only"``
    # (Joshua-authored emails that no existing intent pattern matched)
    # + clusters by subject text similarity. Proposes new regex
    # patterns to extend the email-intent registry. Payload:
    # proposal_id / cluster_size / sample_subjects (up to 3) /
    # proposed_pattern / proposed_action_kind ("save_note" |
    # "log_only" | "save_with_reply" — operator picks at approve) /
    # confidence / created_at / status / action ("proposed" or
    # "auto_applied"). Source is ``email``. Auto-apply OFF by
    # default per promotion-loop discipline.
    "promotion.email_intent_pattern_proposed",
    # KR-PROMOTE-ROUTER-LOOSEN-AUDIT-ROW — captures operator-level
    # "Haiku should have escalated here but didn't" signals. Emitted
    # by the engine pre-call when ``select_model_pre_call`` returned
    # ``reason in {opus_prefix, force_opus_env}`` on iteration 1 —
    # i.e. operator manually forced Opus on a call that Haiku-router
    # would have left on Haiku absent the override. Payload:
    # original_message_text (truncated to 240 chars) /
    # pre_call_decision_reason (verbatim from the router; v1
    # observed reasons are ``opus_prefix`` / ``force_opus_env``) /
    # override_source (``operator_prefix`` / ``force_env``) / route
    # (the message source). The router-tuning observer (#193)
    # consumes this seam to activate its dormant loosen-path
    # proposer; the loosen proposal flags routes where operator
    # overrode N+ times in the window.
    "opus_override.applied",
    # KR-FE-ALERT-INVESTIGATIONS-VIEWER (forward-compat from #198) — alert wake seam
    # mirrors probe.wake_requested for alert investigations. Reads return [] until
    # the alert wake consumer writes these rows.
    "alert.wake_requested",
    # KR-CC1-POLISH — auto-approve sweep for low-risk probe-fix-
    # envelope proposals. Emitted by the post-cycle auto-approve
    # sweep ONLY when:
    #   * The proposal's ``blast_radius_level == "low"`` (matches
    #     a known-narrow envelope action; see
    #     ``kora_cli/promote/probe_fix_envelopes/proposer.py``
    #     ``_KNOWN_LOW_RISK_PATTERNS``)
    #   * Operator opted in via
    #     ``KORA_PROMOTE_PROBE_FIX_AUTO_APPROVE_LOW_RISK=true``
    #   * The proposal has been pending ≥
    #     ``KORA_PROMOTE_PROBE_FIX_AUTO_APPROVE_WAIT_HOURS``
    #     (default 1h) — operator's window to manually reject
    # Two-tier gating preserved: this seam means "the proposal is
    # now in the envelope vocabulary"; actual fix-attempt execution
    # STILL requires ``KORA_PROBE_AUTOFIX_<NAME>_ENABLED=true``.
    # Payload mirrors ``promotion.probe_envelope_action_proposed``
    # + adds ``auto_approve_wait_hours`` (the actual wait the
    # sweep applied) + ``auto_approved_at`` (ISO ts) so operator
    # triage can reconstruct the timeline.
    "promotion.probe_envelope_action_auto_approved",
]

SourceName = Literal[
    "mcp_http",
    "slack_dm",
    "email",
    "cron",
    "reasoning",
]


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

LOG_PATH_ENV = "KORA_AUDIT_LOG_PATH"
AUDIT_LOG_FILENAME = "kora_audit_log.jsonl"
# KR-PER-TENANT-AUDIT-JSONL — sentinel for the single-tenant default
# install. ``tenant_id=None`` callers and ``tenant_id="default"``
# callers both land in the same legacy ``<KORA_HOME>/kora_audit_log.jsonl``
# so existing readers (audit panel, reasoning context loader) keep
# seeing the same file on single-tenant deployments. Anything else
# becomes a per-tenant subdirectory under ``<KORA_HOME>/audit/<id>/``.
DEFAULT_TENANT_ID = "default"
# Drift-guard pin: the query-param name BE endpoints accept for
# tenant scoping is sourced from this constant. The FE constant
# (``web/src/lib/audit.ts``) and the BE allowlist test
# (``tests/kora_cli/audit/test_per_tenant_audit_jsonl.py``)
# both import-pin against this — 3-source agreement enforced.
TENANT_ID_QUERY_PARAM_NAME = "tenant_id"


def _resolve_log_path(tenant_id: Optional[str] = None) -> Path:
    """Env override → per-tenant path → ``<KORA_HOME>/kora_audit_log.jsonl``.

    Path-resolution rules:
      * ``KORA_AUDIT_LOG_PATH`` env override always wins (test hook).
      * ``tenant_id`` is ``None`` or ``"default"`` → legacy single-file
        path ``<KORA_HOME>/kora_audit_log.jsonl`` (existing readers
        keep working unchanged on single-tenant deployments).
      * Any other ``tenant_id`` → per-tenant subdirectory:
        ``<KORA_HOME>/audit/<tenant_id>/kora_audit_log.jsonl``. The
        subdir is created on first write by ``_write_entries_sync``'s
        ``parent.mkdir(parents=True, exist_ok=True)``.

    Mirrors the resolver pattern used by ``slack_dm_handler``'s log
    path + the ST2 conversation context loader. Honors ``KORA_HOME``
    primary + legacy ``HERMES_HOME`` fallback via
    ``kora_constants.get_kora_home()``.
    """
    override = os.environ.get(LOG_PATH_ENV, "").strip()
    if override:
        return Path(override)
    from kora_constants import get_kora_home

    kora_home = get_kora_home()
    if tenant_id is None or tenant_id == DEFAULT_TENANT_ID:
        return kora_home / AUDIT_LOG_FILENAME
    # Defense against a caller passing a path-traversal-shaped tenant
    # (e.g. ``"../foo"`` or ``"foo/../bar"``) — refuse the per-tenant
    # routing entirely and fall back to the legacy default path. The
    # eventual upstream is ``IdentitySpec.identity_metadata["tenant_id"]``
    # which is operator-controlled at plugin registration time, but
    # defense in depth here keeps the audit/ subtree's filesystem
    # surface a flat one-dir-per-tenant tree.
    raw = tenant_id.strip()
    if not raw or raw in {".", ".."}:
        return kora_home / AUDIT_LOG_FILENAME
    if "/" in raw or "\\" in raw or ".." in raw or raw.startswith("."):
        return kora_home / AUDIT_LOG_FILENAME
    return kora_home / "audit" / raw / AUDIT_LOG_FILENAME


# ---------------------------------------------------------------------------
# AuditEntry — Pydantic model with extra="forbid"
# ---------------------------------------------------------------------------


class AuditEntry(BaseModel):
    """Wire shape for a single JSONL audit row.

    ``extra="forbid"`` so any caller passing unexpected top-level
    fields fails at construction (catches schema drift across the
    5 seam call sites).
    """

    model_config = ConfigDict(extra="forbid")

    emitted_at: datetime
    seam: SeamName
    details: Dict[str, Any] = Field(default_factory=dict)
    caller_session_id: Optional[str] = None
    source: Optional[SourceName] = None


# ---------------------------------------------------------------------------
# emit_audit — dual writer
# ---------------------------------------------------------------------------


def emit_audit(
    seam: str,
    details: Dict[str, Any],
    *,
    caller_session_id: Optional[str] = None,
    source: Optional[str] = None,
    log_path: Optional[Path] = None,
    tenant_id: Optional[str] = None,
) -> None:
    """Append one JSONL audit row.

    **Dual-write contract**: each caller retains its existing
    ``[kora.<seam>]`` structured-log line VERBATIM (preserves
    operator grep workflows — see module docstring) AND calls
    :func:`emit_audit` afterward to write the JSONL row. This
    function does NOT emit the structured-log line itself —
    keeping the structured-log format under each caller's control
    avoids byte-for-byte drift across the 4 audit surfaces that
    ship distinct line formats today.

    Args:
      seam: One of the :data:`SeamName` literals.
      details: Seam-specific structured fields. Callers must pre-
        filter — see module docstring's "Security posture."
      caller_session_id: When applicable (source-shaped stable id
        for log correlation). ``slack_dm`` seams:
        ``"{channel}:{ts}"``; ``mcp_http`` seams: caller identity
        from ``mcp_callers.yaml``; etc.
      source: One of :data:`SourceName` literals. ``None`` allowed
        when the seam is source-agnostic (rare).
      log_path: Override for tests. ``None`` resolves via env →
        kora_home → per-tenant subdir.
      tenant_id: Optional tenant scope. ``None`` or ``"default"``
        writes to the legacy single-file path (backward-compat:
        existing readers see the same file). Any other value routes
        to ``<KORA_HOME>/audit/<tenant_id>/kora_audit_log.jsonl``.
        Path source is the IdentitySpec metadata in the eventual
        KR-PER-TENANT-IDENTITY-WIRE bucket; this signature accepts
        the kwarg today so call sites can opt in once that lands.

    Behavior:
      - Best-effort writes the JSONL row to ``log_path``. OSError
        (full disk / unwritable volume / permission denied) →
        WARN-log + continue.
      - Pydantic ValidationError on a bad ``seam`` / ``source`` /
        ``details`` → emits a defensive ``[kora.audit.skipped]``
        WARN line + returns; never raises. The caller's already-
        emitted structured-log line remains the operator-visible
        signal in that path.
    """
    entry: Optional[AuditEntry] = None
    try:
        entry = AuditEntry(
            emitted_at=datetime.now(timezone.utc),
            seam=seam,  # type: ignore[arg-type] — Pydantic validates
            details=dict(details or {}),
            caller_session_id=caller_session_id,
            source=source,  # type: ignore[arg-type]
        )
    except Exception as exc:
        logger.warning(
            "[kora.audit.skipped] AuditEntry construction failed: %r "
            "seam=%r — caller's structured-log line still emitted",
            exc,
            seam,
        )
        return

    path = log_path or _resolve_log_path(tenant_id=tenant_id)
    # KR-CHEAP-AUDIT-BATCHING (R3-4 #9) — route through the batched
    # sink when batching is enabled (default). The per-emit write
    # path stays available as the fallback (BATCH_SIZE_ENV=0) and
    # the immediate-write path inside the sink itself for tests
    # that pass log_path explicitly + want sync semantics.
    if _is_batching_enabled():
        _enqueue_for_batched_flush(entry, path)
        return
    _write_entries_sync([entry], path)


# ---------------------------------------------------------------------------
# Batched flusher — KR-CHEAP-AUDIT-BATCHING (R3-4 #9)
# ---------------------------------------------------------------------------
#
# Original behavior: every ``emit_audit`` call opens the JSONL file,
# appends one line, closes. Fine for low volume but inefficient when
# the daemon is humming (every reasoning tool call, every probe
# wake, every promotion proposal emits a row). R3-4 #9 batches:
# flush at ``KORA_AUDIT_BATCH_SIZE`` events OR ``KORA_AUDIT_FLUSH_
# INTERVAL_SECONDS`` seconds, whichever first.
#
# Lifecycle (STOP-ASK §4 mitigation):
#   * Daemon process: background thread (daemon=True) ticks every
#     FLUSH_INTERVAL and drains the queue.
#   * CLI invocations: same thread starts lazily on the first
#     emit; atexit handler drains on shutdown so single-shot CLI
#     processes don't lose pending events.
#   * Tests that pass an explicit log_path or set BATCH_SIZE=0 stay
#     on the sync write path.
#
# Per-emit interface is UNCHANGED — callers still call ``emit_audit``
# synchronously; the queue is purely internal.

BATCH_SIZE_ENV = "KORA_AUDIT_BATCH_SIZE"
FLUSH_INTERVAL_ENV = "KORA_AUDIT_FLUSH_INTERVAL_SECONDS"

DEFAULT_BATCH_SIZE = 100
DEFAULT_FLUSH_INTERVAL_SECONDS = 5.0


_batch_lock = threading.RLock()
# (entry, log_path) tuples. The path is captured at enqueue-time so
# callers that pass a per-call log_path override still write to the
# right file even when batched.
_batch_queue: List[Tuple["AuditEntry", Path]] = []
_flusher_thread: Optional[threading.Thread] = None
_flusher_stop = threading.Event()
_atexit_registered = False


def _read_batch_size() -> int:
    raw = os.environ.get(BATCH_SIZE_ENV, "").strip()
    if not raw:
        return DEFAULT_BATCH_SIZE
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "[kora.audit.batching] %s=%r not int; using default %d",
            BATCH_SIZE_ENV,
            raw,
            DEFAULT_BATCH_SIZE,
        )
        return DEFAULT_BATCH_SIZE
    if value < 0:
        return DEFAULT_BATCH_SIZE
    return value


def _read_flush_interval_seconds() -> float:
    raw = os.environ.get(FLUSH_INTERVAL_ENV, "").strip()
    if not raw:
        return DEFAULT_FLUSH_INTERVAL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "[kora.audit.batching] %s=%r not numeric; using default %ss",
            FLUSH_INTERVAL_ENV,
            raw,
            DEFAULT_FLUSH_INTERVAL_SECONDS,
        )
        return DEFAULT_FLUSH_INTERVAL_SECONDS
    if value <= 0:
        return DEFAULT_FLUSH_INTERVAL_SECONDS
    return value


def _is_batching_enabled() -> bool:
    """Batching is on whenever BATCH_SIZE > 0 (default 100).
    Setting BATCH_SIZE=0 forces the legacy per-emit write path —
    useful for tests that want sync semantics."""
    return _read_batch_size() > 0


def _write_entries_sync(
    entries: List["AuditEntry"], path: Path
) -> None:
    """Write a list of entries to ``path`` in one open/close cycle.
    Fail-soft per OSError; never raises. Empty list is a no-op."""
    if not entries:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for entry in entries:
                f.write(entry.model_dump_json() + "\n")
    except OSError as exc:
        logger.warning(
            "[kora.audit.skipped] JSONL batched write failed (%s): %r — "
            "caller's structured-log line still emitted",
            path,
            exc,
        )


def _drain_queue_locked() -> List[Tuple["AuditEntry", Path]]:
    """Move every queued (entry, path) out of the queue. Caller
    holds ``_batch_lock``. Returns the drained items so the actual
    file writes can happen OUTSIDE the lock (writing to disk while
    holding the lock would block subsequent emits unnecessarily)."""
    drained = list(_batch_queue)
    _batch_queue.clear()
    return drained


def _flush_now() -> int:
    """Drain the queue + write each path's entries in one open/close.
    Returns total events flushed (0 when queue empty). Safe to call
    from any thread (the size-triggered flush calls from emit_audit
    + the interval-triggered flush from the background thread + the
    atexit handler all share this path)."""
    with _batch_lock:
        drained = _drain_queue_locked()
    if not drained:
        return 0
    # Group by path so each file is opened once per flush.
    by_path: Dict[Path, List["AuditEntry"]] = {}
    for entry, path in drained:
        by_path.setdefault(path, []).append(entry)
    for path, entries in by_path.items():
        _write_entries_sync(entries, path)
    return len(drained)


def _flusher_loop() -> None:
    """Background-thread entry. Sleeps the configured interval +
    flushes; exits when ``_flusher_stop`` is set."""
    interval = _read_flush_interval_seconds()
    while not _flusher_stop.is_set():
        # ``wait(interval)`` returns True if the stop event was
        # set during the wait → exit promptly. Otherwise it
        # returns False after the interval and we flush.
        if _flusher_stop.wait(interval):
            break
        try:
            _flush_now()
        except Exception as exc:
            logger.warning(
                "[kora.audit.batching] background flush raised %r — "
                "queue retried on next tick",
                exc,
            )
    # Final drain on stop so atexit-initiated shutdown captures
    # whatever the background thread had pending at the moment
    # ``_flusher_stop`` was set.
    try:
        _flush_now()
    except Exception as exc:
        logger.warning(
            "[kora.audit.batching] final drain raised %r", exc
        )


def _atexit_flush() -> None:
    """atexit hook — drain the queue + signal the background thread
    to exit. Best-effort; never raises (atexit handlers that raise
    are surfaced by the runtime as ugly tracebacks)."""
    try:
        _flusher_stop.set()
        _flush_now()
    except Exception as exc:
        logger.warning(
            "[kora.audit.batching] atexit flush raised %r — events "
            "may be lost",
            exc,
        )


def _ensure_flusher_started() -> None:
    """Lazy thread start on first batched emit. Idempotent — safe
    to call from every emit_audit. The thread is ``daemon=True`` so
    a process exit doesn't block on it (the atexit handler does the
    final drain regardless)."""
    global _flusher_thread, _atexit_registered
    with _batch_lock:
        if _flusher_thread is not None and _flusher_thread.is_alive():
            return
        _flusher_stop.clear()
        _flusher_thread = threading.Thread(
            target=_flusher_loop,
            name="kora-audit-flusher",
            daemon=True,
        )
        _flusher_thread.start()
        if not _atexit_registered:
            atexit.register(_atexit_flush)
            _atexit_registered = True


def _enqueue_for_batched_flush(entry: "AuditEntry", path: Path) -> None:
    """Append one (entry, path) to the queue + flush immediately if
    the size threshold is hit. Otherwise the background thread
    handles the time-based flush."""
    _ensure_flusher_started()
    size_threshold = _read_batch_size()
    should_flush_immediately = False
    with _batch_lock:
        _batch_queue.append((entry, path))
        if len(_batch_queue) >= size_threshold:
            should_flush_immediately = True
    if should_flush_immediately:
        _flush_now()


def flush_for_tests() -> int:
    """Test surface: synchronously drain the queue. Returns number of
    events flushed. Production code should NOT call this — the
    automatic size + time + atexit triggers cover the production
    flush points."""
    return _flush_now()


def _reset_batching_for_tests() -> None:
    """Test surface: drain the queue (mirrors atexit shutdown
    semantics), stop the flusher thread, and reset state so the
    next test starts clean. Lets the dedicated batching-tests assert
    "shutdown drains pending events" by calling this helper as the
    shutdown stand-in.
    """
    global _flusher_thread, _atexit_registered
    # Drain BEFORE stopping the thread so an in-flight queue
    # reaches disk — atexit's contract is "events written".
    try:
        _flush_now()
    except Exception:
        # Best-effort; production atexit handler also swallows.
        pass
    _flusher_stop.set()
    if _flusher_thread is not None:
        _flusher_thread.join(timeout=2.0)
    _flusher_thread = None
    with _batch_lock:
        _batch_queue.clear()
    # We intentionally leave _atexit_registered True because Python's
    # atexit API has no unregister-by-function for once-registered
    # callbacks; the callback short-circuits on a clean queue so
    # leaving it registered is harmless.
