"""Audit-writer helper for the Hermes plugin's audit sub-plugin.

Moved verbatim from ``kora_cli/reasoning/anthropic_engine.py`` per
KR-PLUGIN-EXTRACTIONS-BATCH-2 (Deliverable A). The engine's three
in-file callers continue to work via a re-import shim at the old
location — they `from kora_runtime.audit.
writer import _emit_tool_called_audit` is the canonical import
path now.

# Dual-write contract (unchanged from engine-resident form)

This helper preserves the existing ``[kora.reasoning.tool_called]``
structured-log line VERBATIM (operator grep workflows depend on
the exact format) AND calls :func:`kora_cli.audit.emit_audit` to
append the JSONL row that the cockpit's audit panel consumes.

NEVER logs tool input/output bodies (those may contain privileged
operator data). Names + status codes only — same posture as
``mcp_tools._emit_audit``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from kora_runtime.audit.constants import (
    AUDIT_SOURCE_REASONING,
)

logger = logging.getLogger(__name__)


def _emit_tool_called_audit(
    *,
    tool_name: str,
    triggered_by: str,
    caller_session_id: str,
    tool_duration_ms: int,
    tool_status: str,
    exc_type: Optional[str] = None,
) -> None:
    """Stable audit per reasoning-tool call — KR-AUDIT-JSONL-SINK.

    **Dual-write**: existing ``[kora.reasoning.tool_called]``
    structured-log line preserved VERBATIM (operator grep workflows
    keep working) + :func:`emit_audit` writes a JSONL row to
    ``kora_audit_log.jsonl`` (panel consumption).

    NEVER logs tool input/output bodies (those may contain
    privileged operator data). Names + status codes only.
    """
    if exc_type is not None:
        logger.info(
            "[kora.reasoning.tool_called] tool=%s triggered_by=%s "
            "caller_session_id=%s tool_duration_ms=%d tool_status=%s "
            "exc_type=%s",
            tool_name,
            triggered_by,
            caller_session_id,
            tool_duration_ms,
            tool_status,
            exc_type,
        )
    else:
        logger.info(
            "[kora.reasoning.tool_called] tool=%s triggered_by=%s "
            "caller_session_id=%s tool_duration_ms=%d tool_status=%s",
            tool_name,
            triggered_by,
            caller_session_id,
            tool_duration_ms,
            tool_status,
        )

    # KR-AUDIT-JSONL-SINK — JSONL bridge to panels.
    from kora_cli.audit import emit_audit

    details: Dict[str, Any] = {
        "tool_name": tool_name,
        "triggered_by": triggered_by,
        "tool_duration_ms": tool_duration_ms,
        "tool_status": tool_status,
    }
    if exc_type is not None:
        details["exc_type"] = exc_type

    emit_audit(
        seam="reasoning.tool_called",
        details=details,
        caller_session_id=caller_session_id,
        source=AUDIT_SOURCE_REASONING,
    )
