"""Constitution pre-screen middleware (KR-P2-A ST1).

Evaluates whether a tool call is permitted under the active
Constitution revision + capability matrix for the calling actor.
ST2 wires this into ``agent/tool_executor.py`` BEFORE
``_tool_guardrails.before_call`` — Constitution is the policy layer;
guardrails is the sandbox layer; Constitution-deny short-circuits
guardrails.

# Verdict outcomes

- ``PASS``: tool may execute. ``envelope`` carries the Constitution
  revision_id + rules_hash active at decision time so ST3 can attach
  it to the chain event for audit.
- ``FAIL``: pre-screen judged the call out-of-policy. ST2 raises
  :class:`KoraConstitutionRejectError`; ST3 emits a
  ``kora.constitution.disagreement_raised`` chain event.
- ``INCONCLUSIVE``: pre-screen could not reach a verdict (unknown
  tool, cap missing from C2 mirror, missing memory provider, etc.).
  ST2 raises :class:`KoraConstitutionEscalateError`; ST3 emits a
  ``kora.escalation.requested`` chain event so an operator can
  adjudicate (cockpit / Slack alert).

# Fail-CLOSED default

When infrastructure is missing (memory provider not loaded, ``cap_*``
not in the C2 mirror, tool not in the static map), the verdict is
INCONCLUSIVE — escalate, not silently allow. Honors
``feedback_fail_closed_by_default_security_infra``: a denied or
non-evaluable call must NOT proceed silently.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional

from plugins.memory.isokron.capability_check import actor_has_capability
from agent.tool_capability_map import (
    UNKNOWN_TOOL_SENTINEL,
    get_required_capability,
)

logger = logging.getLogger(__name__)


# Sentinel string stored in PreScreenEnvelope.required_capability for
# substrate-tier ``kora__*`` tools that the pre-screen passes through
# without capability lookup. Documented public string — downstream
# audit consumers (cockpit / Slack) may match on it.
SUBSTRATE_ENFORCED = "<substrate-enforced>"


class PreScreenOutcome(Enum):
    """Three possible verdicts from a pre-screen evaluation."""

    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class PreScreenEnvelope:
    """Audit envelope attached to chain events on a passed pre-screen.

    Holds the Constitution revision_id + rules_hash that the pre-screen
    used; if the tool call is later disputed, this envelope is the
    anchor for which rules were active at decision time.

    ``required_capability`` may equal :data:`SUBSTRATE_ENFORCED` for
    ``kora__*`` substrate-tier tools, in which case
    ``constitution_revision_id`` and ``rules_hash`` may be ``None``
    (the substrate-side check is the authoritative one and emits its
    own audit trail via ``kora__append_event``).
    """

    tool_name: str
    required_capability: str
    actor_id: str
    constitution_revision_id: Optional[str]
    rules_hash: Optional[str]


@dataclass(frozen=True, slots=True)
class PreScreenVerdict:
    """Outcome of one :func:`constitution_pre_screen` call.

    Construct via the :meth:`pass_` / :meth:`fail` / :meth:`inconclusive`
    factory classmethods rather than the bare dataclass.
    """

    outcome: PreScreenOutcome
    envelope: Optional[PreScreenEnvelope] = None
    reason: str = ""

    @classmethod
    def pass_(cls, envelope: PreScreenEnvelope) -> "PreScreenVerdict":
        return cls(outcome=PreScreenOutcome.PASS, envelope=envelope)

    @classmethod
    def fail(
        cls,
        reason: str,
        envelope: Optional[PreScreenEnvelope] = None,
    ) -> "PreScreenVerdict":
        return cls(
            outcome=PreScreenOutcome.FAIL,
            envelope=envelope,
            reason=reason,
        )

    @classmethod
    def inconclusive(cls, reason: str) -> "PreScreenVerdict":
        return cls(outcome=PreScreenOutcome.INCONCLUSIVE, reason=reason)


class KoraConstitutionRejectError(Exception):
    """Pre-screen verdict = FAIL. Tool must NOT execute.

    ST2 catches this in ``agent/tool_executor.py`` and short-circuits
    the tool call; ST3 emits a ``kora.constitution.disagreement_raised``
    chain event before propagation.
    """

    def __init__(
        self,
        tool_name: str,
        reason: str,
        envelope: Optional[PreScreenEnvelope] = None,
    ):
        self.tool_name = tool_name
        self.reason = reason
        self.envelope = envelope
        super().__init__(f"[constitution_reject] {tool_name}: {reason}")


class KoraConstitutionEscalateError(Exception):
    """Pre-screen verdict = INCONCLUSIVE. Tool must NOT execute.

    ST2 catches this in ``agent/tool_executor.py`` and short-circuits
    the tool call; ST3 emits a ``kora.escalation.requested`` chain
    event so an operator can adjudicate.
    """

    def __init__(self, tool_name: str, reason: str):
        self.tool_name = tool_name
        self.reason = reason
        super().__init__(f"[constitution_escalate] {tool_name}: {reason}")


def _extract_constitution_state(
    memory_provider: Any,
    workspace_id: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Best-effort read of ``(revision_id, rules_hash)`` from the
    provider's TTL-cached Constitution state.

    Returns ``(None, None)`` when the provider doesn't expose a usable
    cache yet — fresh boot before first prefetch, fresh workspace
    without an authored Constitution, or the sentinel ``(None, None)``
    value (provider.py:407 puts ``(revision_id_or_None,
    rules_hash_or_None)``). The caller treats ``None`` as audit-only
    missing-context rather than a policy decision.
    """

    if memory_provider is None or workspace_id is None:
        return (None, None)
    cache = getattr(memory_provider, "_constitution_cache", None)
    if cache is None:
        return (None, None)
    try:
        cached = cache.get(workspace_id)
    except Exception:  # pragma: no cover — defensive against fakes
        return (None, None)
    if cached is None:
        return (None, None)
    if isinstance(cached, tuple) and len(cached) == 2:
        return (cached[0], cached[1])
    return (None, None)


def constitution_pre_screen(
    tool_name: str,
    tool_args: Mapping[str, Any],
    actor_id: str,
    memory_provider: Optional[Any],
    *,
    workspace_id: Optional[str] = None,
) -> PreScreenVerdict:
    """Evaluate whether ``tool_name(tool_args)`` is permitted.

    Returns a :class:`PreScreenVerdict`. ST2 maps the outcome to either
    a passing flow (attach envelope to chain event), a reject (raise
    :class:`KoraConstitutionRejectError`), or an escalate (raise
    :class:`KoraConstitutionEscalateError`). The function never raises
    directly — every outcome (including programmer-error paths like an
    unknown cap_*) is encoded as a verdict.

    Synchronous: every check (capability lookup, TTLCache read, dict
    membership) is sync. ST2 calls this from the sync
    ``execute_tool_calls_concurrent`` pre-flight loop, which runs
    before the ThreadPoolExecutor fan-out — no event loop available.

    Args:
        tool_name: The name the model invoked (``registry.register``
            ``name=`` value).
        tool_args: Arguments dict from the model's tool call. Not used
            for the policy decision today; ST3 records it in the
            disagreement event payload for audit.
        actor_id: The caller's actor_id. Propagates into the envelope +
            disagreement event payload. The current
            ``actor_has_capability`` helper is Kora-implicit (the C2
            mirror IS Kora's row), so this argument is recorded but
            not consulted for the lookup.
        memory_provider: ``IsoKronMemoryProvider`` or ``None``. When
            ``None``, verdict is INCONCLUSIVE (fail-CLOSED).
        workspace_id: Optional workspace_id used to read the active
            Constitution revision from the provider's per-workspace
            cache. When ``None``, audit state defaults to
            ``(None, None)`` — the verdict still reaches a policy
            decision, but the envelope records no revision context.

    Decision order:
      (a) ``kora__*`` substrate-tier tools → PASS (substrate enforces).
      (b) Tool name not in :data:`TOOL_CAPABILITY_MAP` → INCONCLUSIVE.
      (c) ``memory_provider`` is ``None`` → INCONCLUSIVE.
      (d) Capability check raises ``KeyError`` (cap_* not in C2 mirror
          — today's expected state for infrastructure-tier caps) →
          INCONCLUSIVE.
      (e) Capability check returns ``False`` → FAIL.
      (f) Capability check returns ``True`` → PASS.
    """

    # tool_args is part of the documented contract for ST3 audit
    # payload pass-through but unused for today's policy decision.
    _ = tool_args

    # (a) Substrate-tier tools pass through; substrate MCP dispatch
    # is the authoritative gate (K-6 / K-7 / K-8 / K-9).
    if tool_name.startswith("kora__"):
        return PreScreenVerdict.pass_(
            PreScreenEnvelope(
                tool_name=tool_name,
                required_capability=SUBSTRATE_ENFORCED,
                actor_id=actor_id,
                constitution_revision_id=None,
                rules_hash=None,
            )
        )

    # (b) Unknown tool → escalate (fail-CLOSED).
    required = get_required_capability(tool_name)
    if required is UNKNOWN_TOOL_SENTINEL:
        return PreScreenVerdict.inconclusive(
            f"tool '{tool_name}' has no entry in "
            "agent/tool_capability_map.py TOOL_CAPABILITY_MAP; operator "
            "must add a mapping or approve the call out-of-band."
        )

    # (c) No memory provider → escalate (fail-CLOSED).
    if memory_provider is None:
        return PreScreenVerdict.inconclusive(
            "IsoKronMemoryProvider not loaded; cannot evaluate active "
            f"Constitution revision. Operator must approve '{tool_name}' "
            "out-of-band."
        )

    # (d/e/f) Capability check.
    try:
        has_cap = actor_has_capability(required)  # type: ignore[arg-type]
    except KeyError as exc:
        return PreScreenVerdict.inconclusive(
            f"capability '{required}' (required by tool '{tool_name}') "
            f"is not in the C2 capability matrix mirror: {exc}. Operator "
            "must approve out-of-band, or extend the C2 mirror to "
            "include this infrastructure-tier capability."
        )

    revision_id, rules_hash = _extract_constitution_state(
        memory_provider, workspace_id
    )
    envelope = PreScreenEnvelope(
        tool_name=tool_name,
        required_capability=required,  # type: ignore[arg-type]
        actor_id=actor_id,
        constitution_revision_id=revision_id,
        rules_hash=rules_hash,
    )

    if not has_cap:
        return PreScreenVerdict.fail(
            f"actor_id='{actor_id}' lacks capability '{required}' "
            f"required by tool '{tool_name}'.",
            envelope=envelope,
        )

    return PreScreenVerdict.pass_(envelope)
