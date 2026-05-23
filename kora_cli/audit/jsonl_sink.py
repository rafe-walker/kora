"""JSONL audit sink — KR-AUDIT-JSONL-SINK.

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

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Literal, Optional

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


def _resolve_log_path() -> Path:
    """Env override → ``<KORA_HOME>/kora_audit_log.jsonl``.

    Mirrors the resolver pattern used by ``slack_dm_handler``'s log
    path + the ST2 conversation context loader. Honors ``KORA_HOME``
    primary + legacy ``HERMES_HOME`` fallback via
    ``kora_constants.get_kora_home()``.
    """
    override = os.environ.get(LOG_PATH_ENV, "").strip()
    if override:
        return Path(override)
    from kora_constants import get_kora_home

    return get_kora_home() / AUDIT_LOG_FILENAME


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
        kora_home.

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

    path = log_path or _resolve_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")
    except OSError as exc:
        logger.warning(
            "[kora.audit.skipped] JSONL write failed (%s): %r — "
            "caller's structured-log line still emitted",
            path,
            exc,
        )
