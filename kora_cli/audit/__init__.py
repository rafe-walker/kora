"""**LOCAL** audit — Kora-CLI-side JSONL sink + reader.

Single helper :func:`emit_audit` that dual-writes:

  1. The existing ``[kora.<seam>]`` structured-log line (preserved
     for operator grep workflows — no breaking change).
  2. A JSONL row to ``<KORA_HOME>/kora_audit_log.jsonl`` for panel
     consumption.

Substrate-backed promotion (filed as coord ask 2026-05-22) will
turn this into a triple-writer; panels continue reading the same
shape.

⚠ **NOT substrate audit.** This subdir handles LOCAL-file audit
only. For substrate chain-event emit, use
``isokron_client.events.emit_kora_event``. The renamed module
:mod:`kora_cli.audit.local_jsonl_sink` (was ``jsonl_sink``
pre-KR-KORA-PIP-RESTRUCTURE-PHASE-1B 2026-05-24) makes the
distinction explicit. A back-compat shim at
``kora_cli/audit/jsonl_sink.py`` re-exports the public surface
so existing callers keep working unchanged.
"""

from kora_cli.audit.local_jsonl_sink import (
    AuditEntry,
    DEFAULT_TENANT_ID,
    TENANT_ID_QUERY_PARAM_NAME,
    emit_audit,
)

__all__ = [
    "AuditEntry",
    "DEFAULT_TENANT_ID",
    "TENANT_ID_QUERY_PARAM_NAME",
    "emit_audit",
]
