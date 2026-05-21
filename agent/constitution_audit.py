"""Constitution pre-screen audit chain-event emission (KR-P2-A ST3).

Emits the ``kora.constitution.disagreement_raised`` (on FAIL) or
``kora.escalation.requested`` (on INCONCLUSIVE) chain event via the
existing ``kora__append_event`` Sea MCP tool (KR-7 path). ST2's
tool-executor wire-in calls this helper BEFORE assembling the
model-facing ``block_result``, so the audit-trail entry lands before
the tool denial reaches the model.

# Fail-LOUD policy

If the emit itself fails (substrate down, MCP client unavailable,
workspace_id unresolved, IsoKron connection not initialized, etc.),
this module logs at ERROR and raises :class:`ConstitutionAuditEmitError`.
Per the pre-screen's audit-trail tenet: a denied call that can't be
audited must NOT be silently dropped — operator intervention is
required.

This is intentionally stricter than the provider's
``_attempt_chain_event_emit`` (which logs-but-doesn't-raise so that
session lifecycle hooks like ``on_session_end`` stay alive across
emit failures). The Constitution audit is policy-critical: failing to
emit means the system silently allowed a denied tool to be denied
without record — and the *next* operator triage step (cockpit alert,
Slack page) doesn't fire.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from agent.constitution_pre_screen import (
    PreScreenOutcome,
    PreScreenVerdict,
)

logger = logging.getLogger(__name__)


# Chain event-type literals (must match the
# ``event_log_event_type_check`` constraint set on substrate — both
# literals were added in foundation/0136 + foundation/0138).
CONSTITUTION_DISAGREEMENT_EVENT = "kora.constitution.disagreement_raised"
ESCALATION_REQUESTED_EVENT = "kora.escalation.requested"

# Escalation-kind sub-discriminator inside the
# ``kora.escalation.requested`` payload. Other escalation kinds
# (operator-page, deadline-miss, etc.) will land via other paths;
# pre-screen-INCONCLUSIVE escalations stamp this string.
ESCALATION_KIND_CONSTITUTION_INCONCLUSIVE = "constitution_inconclusive"


class ConstitutionAuditEmitError(RuntimeError):
    """Raised when the audit chain-event emit fails for any reason.

    Carries the intended ``event_type`` and the ``tool_name`` of the
    denied call for operator triage. The original exception (if any)
    is chained via ``__cause__``.
    """

    def __init__(self, event_type: str, tool_name: str, cause: str):
        self.event_type = event_type
        self.tool_name = tool_name
        self.cause = cause
        super().__init__(
            f"[kora.constitution.audit.failed] {event_type} for tool "
            f"'{tool_name}' could not be emitted: {cause}. Refusing to "
            "proceed silently — operator must adjudicate."
        )


def emit_constitution_audit_event(
    agent,
    tool_name: str,
    tool_args: Mapping[str, Any],
    verdict: PreScreenVerdict,
) -> None:
    """Emit the audit chain event for a Constitution block.

    Called by ``agent/tool_executor.py`` for verdicts in
    ``{FAIL, INCONCLUSIVE}``. A PASS verdict is a no-op (defensive —
    the caller already filters; PASS leaves no audit footprint at
    this layer because the chain event for the *tool result itself*
    lands later in the tool-dispatch path via the existing K-7
    chain-event emit).

    Raises:
        ConstitutionAuditEmitError: substrate-side failure, missing
            infra, or unexpected verdict shape. Per ST3 spec, callers
            should let this propagate; the agent's tool batch fails
            rather than silently dropping the audit.
    """
    if verdict.outcome is PreScreenOutcome.PASS:
        return

    if verdict.outcome is PreScreenOutcome.FAIL:
        event_type = CONSTITUTION_DISAGREEMENT_EVENT
    elif verdict.outcome is PreScreenOutcome.INCONCLUSIVE:
        event_type = ESCALATION_REQUESTED_EVENT
    else:  # pragma: no cover — exhaustive enum; defensive
        raise ConstitutionAuditEmitError(
            "<unknown>",
            tool_name,
            f"unexpected verdict outcome {verdict.outcome!r}",
        )

    memory_manager = getattr(agent, "_memory_manager", None)
    isokron_provider = (
        memory_manager.get_provider("isokron") if memory_manager is not None else None
    )
    if isokron_provider is None:
        raise ConstitutionAuditEmitError(
            event_type,
            tool_name,
            "IsoKron provider not loaded; cannot emit audit event",
        )

    try:
        workspace_id = isokron_provider._resolve_workspace_id()
    except Exception as exc:
        raise ConstitutionAuditEmitError(
            event_type,
            tool_name,
            f"workspace_id resolution raised: {exc!r}",
        ) from exc
    if not workspace_id:
        raise ConstitutionAuditEmitError(
            event_type,
            tool_name,
            "workspace_id is None/empty after resolution",
        )

    connection = getattr(isokron_provider, "_connection", None)
    if connection is None:
        raise ConstitutionAuditEmitError(
            event_type,
            tool_name,
            "IsoKron connection not initialized",
        )

    payload = _build_payload(event_type, tool_name, tool_args, verdict)

    try:
        from plugins.memory.isokron.events import emit_kora_event

        mcp_client = connection.get_mcp_client()
        event_id = connection.submit_and_wait(
            emit_kora_event(
                workspace_id=workspace_id,
                event_type=event_type,
                payload=payload,
                mcp_client=mcp_client,
            ),
            timeout=10.0,
        )
    except ConstitutionAuditEmitError:
        raise
    except Exception as exc:
        logger.error(
            "[kora.constitution.audit.failed] %s for tool '%s' — emit raised: %r. "
            "Refusing to proceed silently.",
            event_type,
            tool_name,
            exc,
        )
        raise ConstitutionAuditEmitError(
            event_type,
            tool_name,
            f"substrate emit raised: {exc!r}",
        ) from exc

    logger.info(
        "[kora.constitution.audit] %s emitted for tool '%s' → event_id=%s",
        event_type,
        tool_name,
        event_id,
    )


def _build_payload(
    event_type: str,
    tool_name: str,
    tool_args: Mapping[str, Any],
    verdict: PreScreenVerdict,
) -> dict[str, Any]:
    """Build the chain event payload per bucket spec § ST3.

    Disagreement payload captures the policy snapshot (denied
    capability, active Constitution revision_id, rules_hash) so
    auditors can reconstruct the decision later. Escalation payload
    captures the operator-actionable context (escalation_kind +
    reason). Both include a ``snapshot_args`` copy of the tool call
    for forensic context.
    """
    envelope = verdict.envelope
    args_snapshot = dict(tool_args) if tool_args else {}
    actor_id = envelope.actor_id if envelope is not None else "kora"

    if event_type == CONSTITUTION_DISAGREEMENT_EVENT:
        return {
            "tool_name": tool_name,
            "denied_capability": (
                envelope.required_capability if envelope is not None else None
            ),
            "active_constitution_revision_id": (
                envelope.constitution_revision_id if envelope is not None else None
            ),
            "rules_hash": envelope.rules_hash if envelope is not None else None,
            "actor_id": actor_id,
            "snapshot_args": args_snapshot,
            "reason": verdict.reason,
        }
    # ESCALATION_REQUESTED_EVENT
    return {
        "tool_name": tool_name,
        "escalation_kind": ESCALATION_KIND_CONSTITUTION_INCONCLUSIVE,
        "escalation_reason": verdict.reason,
        "actor_id": actor_id,
        "snapshot_args": args_snapshot,
    }
