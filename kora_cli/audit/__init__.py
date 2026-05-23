"""Audit sink — KR-AUDIT-JSONL-SINK.

Single helper :func:`emit_audit` that dual-writes:

  1. The existing ``[kora.<seam>]`` structured-log line (preserved
     for operator grep workflows — no breaking change).
  2. A JSONL row to ``<KORA_HOME>/kora_audit_log.jsonl`` for panel
     consumption.

Substrate-backed promotion (filed as coord ask 2026-05-22) will
turn this into a triple-writer; panels continue reading the same
shape. See ``kora_cli/audit/jsonl_sink.py``.
"""

from kora_cli.audit.jsonl_sink import AuditEntry, emit_audit

__all__ = ["AuditEntry", "emit_audit"]
