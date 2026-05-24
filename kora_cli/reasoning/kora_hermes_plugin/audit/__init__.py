"""Audit sub-plugin — KR-PLUGIN-AUDIT.

Owns the Hermes-plugin-side audit-writer wrapper + the
``post_tool_call`` / ``post_llm_call`` hook handlers. The
canonical JSONL sink remains at ``kora_cli/audit/jsonl_sink.py``
(shared with ``mcp_tools._emit_audit`` and the alerts notifier);
this sub-plugin provides the **reasoning-tool-call audit-writer
helper** that previously lived in ``kora_cli/reasoning/
anthropic_engine.py`` so the engine no longer owns plugin-shape
audit logic.

Public surface (re-exported):
  - ``_emit_tool_called_audit`` — reasoning-tool dual-write helper
  - ``register`` — sub-plugin entry point
  - ``AUDIT_SEAMS`` — vocabulary literal list
"""

from kora_cli.reasoning.kora_hermes_plugin.audit.constants import (
    AUDIT_SEAMS,
)
from kora_cli.reasoning.kora_hermes_plugin.audit.plugin import (
    _post_llm_call,
    _post_tool_call,
    register,
)
from kora_cli.reasoning.kora_hermes_plugin.audit.writer import (
    _emit_tool_called_audit,
)

__all__ = [
    "AUDIT_SEAMS",
    "_emit_tool_called_audit",
    "_post_llm_call",
    "_post_tool_call",
    "register",
]
