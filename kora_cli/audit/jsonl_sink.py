"""Backward-compat shim — KR-KORA-PIP-RESTRUCTURE-PHASE-1B (2026-05-24).

The original module name ``jsonl_sink`` conflated this LOCAL-file
audit sink with the substrate chain-event emit
(``isokron_client.events.emit_kora_event``). The module was
renamed to :mod:`kora_cli.audit.local_jsonl_sink` to make the
"LOCAL file, NOT substrate" semantic explicit at the dotted-name
level.

This file is a re-export shim — the ~20 existing callers
(``kora_cli/promote/*``, ``kora_cli/probes/*``, ``kora_cli/tools/
*``, ``kora_cli/alerts/*``, ``kora_cli/reasoning/anthropic_engine.py``,
``kora_cli/intent/email_to_sea_ticket.py``,
``kora_cli/web_server.py``, etc.) continue working unchanged via
``from kora_cli.audit.jsonl_sink import X``. New code SHOULD
prefer ``from kora_cli.audit.local_jsonl_sink import X``.

No public surface change. ``import *`` plus an explicit re-export
of the load-bearing symbols so IDE jump-to-definition and static
analyzers resolve cleanly through the shim.
"""

from __future__ import annotations

from kora_cli.audit.local_jsonl_sink import *  # noqa: F401,F403

# Explicit re-exports of the load-bearing symbols Kora-internal
# callers import directly. ``import *`` above covers all the
# public (non-underscore) names but NOT the test-helper
# underscore-prefixed ones (``_reset_batching_for_tests``) that
# tests/conftest.py reaches into — those need explicit re-export.
# Keep in sync with the public surface in ``local_jsonl_sink.py``.
from kora_cli.audit.local_jsonl_sink import (  # noqa: F401
    AUDIT_LOG_FILENAME,
    BATCH_SIZE_ENV,
    DEFAULT_BATCH_SIZE,
    DEFAULT_FLUSH_INTERVAL_SECONDS,
    FLUSH_INTERVAL_ENV,
    LOG_PATH_ENV,
    AuditEntry,
    emit_audit,
    flush_for_tests,
    _resolve_log_path,
    _reset_batching_for_tests,
)
