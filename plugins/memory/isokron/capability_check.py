"""Python ``actorHasCapability`` mirror for Kora (KR-6).

Replaces the KR-3 ST1 ``assert_kora_can_perform`` stub with a real
check against the C2 capability matrix mirror. Every ``iso_node_*`` /
``iso_link_*`` tool handler asserts its capability at the start of
work; denied calls raise ``CapabilityDeniedError`` rather than
silently proceeding (the KR-3 ST1 stub behavior).

# Forward stability

The helper consumes the resolved Kora-row dict
(``ACTOR_CAPABILITY_MATRIX_KORA_COLUMN``) from
``capability_matrix_mirror``. When K-7 ships the Sea MCP tool
``kora__read_kora_capability_row`` and a follow-on KR-N swap replaces
the C2 mirror with a fresh-per-call MCP fetch, only the import line
on line 1 changes — public surface (``actor_has_capability`` /
``assert_kora_can_perform`` / ``CapabilityDeniedError``) stays
identical. Tested by ``test_capability_check.py::test_module_has_no_
network_or_db_imports_at_load_time``.

# TypeScript reference

Mirrors the behavior of ``actorHasCapability`` in
``packages/sea-mcp-server/src/capability-matrix.ts:657``:

    export function actorHasCapability(
      actorKind: HumanOrAiActorKind,
      capability: ActorCapability,
    ): boolean {
      const row = ACTOR_CAPABILITY_MATRIX[capability];
      if (!row) return false;
      return row[actorKind] === true;
    }

Note: the TS helper is fail-closed on unknown capabilities (returns
``false``); the Python helper is fail-LOUD (raises ``KeyError``).
Rationale: Python callers are always inside Kora's own codebase, so
an unknown capability is a programmer error (typo) we want to surface
loudly during testing. TS callers ride through a Sea MCP boundary
where unknown caps can arrive from JSON payloads — fail-closed there
prevents privilege escalation via typo.
"""

from __future__ import annotations

from typing import Optional

from .capability_matrix_mirror import ACTOR_CAPABILITY_MATRIX_KORA_COLUMN


class CapabilityDeniedError(Exception):
    """Raised when Kora lacks the requested capability.

    ``capability`` carries the cap_* name; ``reason`` is a
    human-readable message (default form names the capability).
    Callers surfacing this to the model can include both fields in
    a structured error envelope so the model can reason about which
    capability was denied + why.
    """

    def __init__(self, capability: str, *, reason: str = ""):
        self.capability = capability
        self.reason = reason or (
            f"actor_kind='kora' lacks capability '{capability}'"
        )
        super().__init__(self.reason)


def actor_has_capability(capability: str) -> bool:
    """Return True iff Kora has ``capability`` per the C2 mirror.

    Implicitly Kora-only — the C2 mirror IS Kora's row of
    ``ACTOR_CAPABILITY_MATRIX``. A future bucket may extend this to
    other actor_kinds if ``claude_pm`` / ``oracle`` ever ship Python
    runtime helpers.

    Raises:
        KeyError: ``capability`` not in the mirror. Likely a typo;
            the parity test against the TS source catches drift on
            both sides, so an unknown cap here means the caller passed
            a name that exists nowhere.
    """
    if capability not in ACTOR_CAPABILITY_MATRIX_KORA_COLUMN:
        raise KeyError(
            f"Unknown capability '{capability}' — not in C2 mirror "
            f"(49 entries). Either it's a typo, or the TS source added "
            f"the cap but the parity test has skipped (KORA_ISOKRON_REPO "
            f"env var not set in this environment)."
        )
    return ACTOR_CAPABILITY_MATRIX_KORA_COLUMN[capability]


def assert_kora_can_perform(
    capability: str,
    *,
    reason: Optional[str] = None,
) -> None:
    """Raise :class:`CapabilityDeniedError` if Kora lacks ``capability``.

    Use at the start of any operation that requires a specific
    capability. The check is a fast O(1) dict lookup; KR-6's helper
    has no IO. When K-7 → KR-N swaps to MCP-fetched matrix, callers
    don't change — only the underlying import.

    Args:
        capability: A ``cap_*`` name from the C2 mirror.
        reason: Optional override for the error message (callers wanting
            to surface why the capability was needed in the denial).

    Raises:
        CapabilityDeniedError: Kora's column for ``capability`` is False.
        KeyError: ``capability`` is not in the mirror.
    """
    if not actor_has_capability(capability):
        raise CapabilityDeniedError(capability, reason=reason or "")
