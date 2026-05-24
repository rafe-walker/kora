"""JSONL audit reader — KR-AUDIT-PANEL-ENDPOINTS.

Companion to ``jsonl_sink.py``: shared reader used by the 3 panel
endpoints that flip from stubs to live audit-JSONL reads
(agent-activity / reasoning / webhook-events).

Mirrors the projection-pattern discipline established by
KR-SLACK-DM-PANEL-FLIP (#137):

  * Tolerate malformed lines (log + skip; never crash)
  * Tolerate missing file (return empty list — fresh daemon)
  * Newest-first ordering by ``emitted_at`` descending
  * Per-call ``get_kora_home()`` lookup so monkeypatch in tests
    works without ContextVar plumbing

The 200-line cap is the caller's responsibility (each endpoint
applies its own cap after projection so grouping logic — e.g.
reasoning's by-session aggregation — operates on the unfiltered
set first).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from kora_cli.audit.local_jsonl_sink import (
    AUDIT_LOG_FILENAME,
    DEFAULT_TENANT_ID,
    LOG_PATH_ENV,
    AuditEntry,
)

logger = logging.getLogger(__name__)


def _resolve_log_path(tenant_id: Optional[str] = None) -> Path:
    """Env override → per-tenant path → ``<KORA_HOME>/kora_audit_log.jsonl``.

    Re-resolves on every call so monkeypatch.setattr of
    ``get_kora_home`` in tests takes effect. Mirrors the resolution
    in ``jsonl_sink._resolve_log_path`` so writer + reader always
    agree on which file holds which tenant's audit rows.
    """
    override = os.environ.get(LOG_PATH_ENV, "").strip()
    if override:
        return Path(override)
    # Local import — same reason the sink does it: lets the test
    # fixture patch get_kora_home in its own module namespace.
    from kora_constants import get_kora_home

    kora_home = get_kora_home()
    if tenant_id is None or tenant_id == DEFAULT_TENANT_ID:
        return kora_home / AUDIT_LOG_FILENAME
    raw = tenant_id.strip()
    if not raw or raw in {".", ".."}:
        return kora_home / AUDIT_LOG_FILENAME
    if "/" in raw or "\\" in raw or ".." in raw or raw.startswith("."):
        return kora_home / AUDIT_LOG_FILENAME
    return kora_home / "audit" / raw / AUDIT_LOG_FILENAME


def read_audit_entries(
    seam: Optional[str] = None,
    limit: Optional[int] = None,
    since: Optional[datetime] = None,
    *,
    tenant_id: Optional[str] = None,
) -> List[AuditEntry]:
    """Read + project the audit JSONL into a newest-first list.

    Args:
      seam: When non-None, only entries matching this seam string
        are returned. The Pydantic SeamName Literal does the value
        validation at AuditEntry construction; an unknown seam
        string passed here simply matches nothing.
      limit: When non-None, cap the returned list to the newest N
        entries. Callers that group by session-id should call
        WITHOUT a limit and apply their own cap after grouping.
      since: When non-None, drop entries with
        ``emitted_at < since``. Naive datetimes are assumed UTC.
      tenant_id: Optional tenant scope. ``None`` or ``"default"``
        reads the legacy single-file path; any other value reads
        ``<KORA_HOME>/audit/<tenant_id>/kora_audit_log.jsonl``. Used
        by the audit BE endpoints to honor the ``?tenant_id=…``
        query param the cockpit picker sets.

    Behavior:
      * Missing file → empty list (fresh daemon; no error).
      * Malformed JSON line → ``[kora.audit.read]`` WARN-log +
        skipped; other lines still parsed.
      * Pydantic ValidationError on a single entry → WARN-log +
        skipped; doesn't poison the whole read.
      * Non-dict JSON line (e.g. an array) → skipped.
      * Blank lines → skipped.
    """
    path = _resolve_log_path(tenant_id=tenant_id)
    if not path.is_file():
        return []

    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    entries: List[AuditEntry] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for lineno, raw_line in enumerate(f, start=1):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "[kora.audit.read] line %d malformed JSON, "
                        "skipped: %r",
                        lineno,
                        exc,
                    )
                    continue
                if not isinstance(obj, dict):
                    logger.warning(
                        "[kora.audit.read] line %d not a JSON object, "
                        "skipped",
                        lineno,
                    )
                    continue
                try:
                    entry = AuditEntry.model_validate(obj)
                except Exception as exc:
                    # Pydantic ValidationError or anything else from
                    # construction — log + skip the single line.
                    logger.warning(
                        "[kora.audit.read] line %d failed AuditEntry "
                        "validation, skipped: %r",
                        lineno,
                        exc,
                    )
                    continue
                if seam is not None and entry.seam != seam:
                    continue
                if since is not None and entry.emitted_at < since:
                    continue
                entries.append(entry)
    except OSError as exc:
        logger.warning(
            "[kora.audit.read] failed to read %s: %r", path, exc
        )
        return []

    # Newest-first ordering by emitted_at descending.
    entries.sort(key=lambda e: e.emitted_at, reverse=True)

    if limit is not None and limit > 0:
        entries = entries[:limit]

    return entries
