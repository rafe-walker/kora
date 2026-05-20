"""Kora-column slice of ``ACTOR_CAPABILITY_MATRIX`` — KR-7b swap.

# Two paths, one dict

* **Production (post-KR-7b)** — ``IsoKronMemoryProvider.initialize()``
  calls :func:`populate_capability_matrix_from_mcp` which fetches
  ``kora__read_kora_capability_row`` via the KR-7a-wired
  :class:`IsoKronMCPClient` and replaces this module's dict contents.
  Substrate is the authoritative source; no hand-translation drift
  risk. Closes BUILD_DEVIATIONS ``D-kr2-st2-capability-matrix-mirror``.

* **Dev/test fallback (legacy C2)** — the hand-mirrored 49-entry dict
  below stays as the default at module import. If the MCP fetch fails
  at provider initialize (transport down, substrate unreachable, etc.),
  the provider logs a ``[kora.capability_matrix.fallback]`` WARNING and
  the fallback stays in place — sessions still run, capability checks
  still work against the (potentially stale) dev data. The parity test
  at ``tests/plugins/memory/test_capability_matrix_parity.py`` guards
  the fallback against TS-source drift so dev parity matches
  production-substrate parity.

# Production-test posture

Same as KR-7's chain-emit closure. K-7 (`ee730853`) shipped the
substrate-side MCP tool; substrate-team's dispatch tier (queued)
un-stubs the handler. KR-7b's code is sound; mock tests verify the
populate machinery; production deploys wait on the dispatch tier
landing. Operators grep ``[kora.capability_matrix.fallback]`` in logs
to confirm the production fetch is succeeding.

# Forward stability

When K-13 (capability-matrix tightening) ships and adds new entries
on the substrate side, the populate function picks them up
automatically at next provider start — no Python-side code change.
The hand-mirrored fallback below still needs to be bumped for dev
parity (the parity test catches it at CI), but the production path
is auto-current.

Capability names are stable strings per the TS source header:
> "Capability names are stable strings — they appear in chain events,
> audit logs, error payloads, and bundle profile definitions. Renames
> are operator-direct schema changes (PolicyRegistry / chain-event-
> vocabulary tier of stability)."
"""

from __future__ import annotations

import logging
from typing import Any

from .models import KoraCapabilityRow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SEA_CAPABILITIES — 24 entries, Kora's column from ACTOR_CAPABILITY_MATRIX.
# Order matches the TS const verbatim for parity-test stability.
# ---------------------------------------------------------------------------

SEA_CAPABILITIES_KORA_COLUMN: dict[str, bool] = {
    # §5.1 base set (REV3 baseline)
    "cap_sea_create": True,                          # Kora authors entities
    "cap_sea_edit_field": False,
    "cap_sea_link_authoring": True,                  # Kora authors links
    "cap_sea_set_status": False,
    "cap_sea_set_priority": False,
    "cap_sea_add_narrative": False,
    "cap_sea_claim_for_edit": False,
    "cap_sea_condense": False,                       # Oracle-domain
    "cap_sea_seed_intake": False,
    "cap_sea_modify_condensation_draft": False,      # operator-only
    "cap_sea_request_deletion": False,
    "cap_sea_approve_deletion": False,
    "cap_sea_read_private_notes": False,
    "cap_sea_bulk_import": False,
    "cap_sea_propose_actor_window_rollback": False,
    "cap_sea_confirm_actor_window_rollback": False,
    "cap_sea_read_view": True,                       # Kora reads Sea views

    # REV4 additions (UBC-R3-A, UBC-R3-B, UBC-R3-L)
    "cap_sea_claim_pair_for_link": False,
    "cap_sea_force_release_claim": False,
    "cap_critic_flag_advisory_link_type": False,

    # REV5 additions (UBC-R4-G, UBC-R4-B)
    "cap_run_full_advisory_pass": False,
    "cap_cancel_condensation_session": False,

    # REV5.1 additions (UBC-R5-B, UBC-R5-C)
    "cap_cancel_advisory_pass": False,
    "cap_resume_rollback": False,
}


# ---------------------------------------------------------------------------
# KORA_BROADER_CAPABILITIES — 24 entries, Kora's column.
# Three logical groups per the TS source: reads, writes, governance.
# Order matches the TS const verbatim.
# ---------------------------------------------------------------------------

KORA_BROADER_CAPABILITIES_KORA_COLUMN: dict[str, bool] = {
    # Reads
    "cap_read_unfiltered_relationlink": True,
    "cap_read_precommit_scratchpad": True,
    "cap_read_cross_agent_scratchpad": True,         # pointer-deref into Critic/Oracle
    "cap_read_ticket_attachment": True,
    "cap_read_escalation_queue": True,               # own escalation queue (Plan 08)

    # Writes (scratchpad + class-2 + author surface)
    "cap_write_agent_scratchpad": True,              # primary writer (Plan 02)
    "cap_propose_class2": True,
    "cap_author_operations": True,

    # 3-cell Critic-override split (Plan 04 §"Step 1" + §"Step 2")
    "cap_request_critic_reconsideration": True,
    "cap_override_nonsecurity_critic_verdict": True,  # Cell B — 6 firewall preconditions
    "cap_override_security_or_policy_verdict": False, # Cell C — operator-only

    # Governance proposals (Kora proposes; operator approves)
    "cap_propose_policy_change": True,               # Plan 06 ships the tool
    "cap_propose_convention": True,                  # Plan 13 — primary author
    "cap_submit_declaration": True,                  # kora_authored_self_build (Plan 14)

    # Routing + safety
    "cap_route_intent": True,                        # Kora-only routing classifier
    "cap_release_poison_pill": True,
    "cap_quarantine_package": True,
    "cap_request_human_escalation": True,

    # HNAO surface (v1.1+ feature-flagged off in v0.1)
    "cap_observe_hnao": True,

    # Pre-screen runner cap (Plan 11 PM-Q2)
    "cap_run_pre_screen": True,

    # Operator-direct admin caps (operator-ONLY)
    "cap_operator_approve_policy_change": False,
    "cap_operator_bless_convention": False,
    "cap_unbless_convention": False,  # operator-only un-bless path
    "cap_operator_ack_escalation": False,
    "cap_operator_update_policy": False,
}


# ---------------------------------------------------------------------------
# Combined Kora column — used by read_kora_capability_row().
# ---------------------------------------------------------------------------

ACTOR_CAPABILITY_MATRIX_KORA_COLUMN: dict[str, bool] = {
    **SEA_CAPABILITIES_KORA_COLUMN,
    **KORA_BROADER_CAPABILITIES_KORA_COLUMN,
}
"""Full Kora column — 48 capabilities total.

Order: 24 SEA_CAPABILITIES first, then 24 KORA_BROADER_CAPABILITIES,
matching the TS ``ACTOR_CAPABILITIES = [...SEA, ...KORA_BROADER]``
spread. Parity test asserts insertion order is preserved.
"""


def read_kora_capability_row() -> KoraCapabilityRow:
    """Return the Kora row of ``ACTOR_CAPABILITY_MATRIX`` as a typed shape.

    Sync (no network IO). Reads the module-level dict, which is either
    the hand-mirrored fallback (default at import) or the MCP-fetched
    authoritative data (after ``populate_capability_matrix_from_mcp``
    runs at provider initialize).

    Returns:
        ``KoraCapabilityRow`` with ``granted`` = frozenset of cap names
        where Kora's column is True; ``denied`` = the complement.
    """
    granted = frozenset(
        cap for cap, allowed in ACTOR_CAPABILITY_MATRIX_KORA_COLUMN.items() if allowed
    )
    denied = frozenset(
        cap for cap, allowed in ACTOR_CAPABILITY_MATRIX_KORA_COLUMN.items() if not allowed
    )
    return KoraCapabilityRow(actor_kind="kora", granted=granted, denied=denied)


# ---------------------------------------------------------------------------
# KR-7b — MCP-backed population at provider initialize
# ---------------------------------------------------------------------------


CAPABILITY_MATRIX_MCP_TOOL = "kora__read_kora_capability_row"
"""Name of the K-7 Sea MCP tool that returns the Kora-row matrix."""


async def populate_capability_matrix_from_mcp(mcp_client: Any) -> int:
    """Fetch ``kora__read_kora_capability_row`` + replace this module's dict.

    Mutates ``ACTOR_CAPABILITY_MATRIX_KORA_COLUMN`` in place so all
    callers that imported it by reference see the fresh data on next
    access. ``capability_check.actor_has_capability`` consumes the
    dict by name; no caller-side refactor needed.

    Args:
        mcp_client: a started :class:`IsoKronMCPClient` (typically from
            ``IsoKronConnection.get_mcp_client()``).

    Returns:
        Number of entries written (e.g. 49 today; K-13 will bump to 51).

    Raises:
        Any exception from ``mcp_client.invoke`` (e.g.
        :class:`IsoKronMCPInvocationError`) — caller decides whether to
        fail-closed or fall back to the hand-mirrored data.
        ``RuntimeError`` if the response shape doesn't match
        ``{"capability_matrix": {...}}``.
    """
    if mcp_client is None:
        raise ValueError(
            "populate_capability_matrix_from_mcp: mcp_client is required "
            "(resolve via IsoKronConnection.get_mcp_client() before calling)"
        )
    result = await mcp_client.invoke(CAPABILITY_MATRIX_MCP_TOOL, {})
    fetched = (
        result.get("capability_matrix") if isinstance(result, dict) else None
    )
    if not isinstance(fetched, dict):
        raise RuntimeError(
            f"{CAPABILITY_MATRIX_MCP_TOOL} returned unexpected shape: "
            f"{result!r}; expected {{'capability_matrix': {{...}}}}"
        )
    # Defensive: ensure every value is a bool. K-7's TS-side Zod schema
    # should guarantee this, but a substrate-side regression that ships
    # non-bools would silently break ``actor_has_capability`` lookups.
    bad = [
        k for k, v in fetched.items()
        if not isinstance(v, bool) or not isinstance(k, str)
    ]
    if bad:
        raise RuntimeError(
            f"{CAPABILITY_MATRIX_MCP_TOOL} returned non-bool / non-str "
            f"entries: {bad!r}"
        )
    ACTOR_CAPABILITY_MATRIX_KORA_COLUMN.clear()
    ACTOR_CAPABILITY_MATRIX_KORA_COLUMN.update(fetched)
    logger.info(
        "[kora.capability_matrix] populated from %s — %d entries",
        CAPABILITY_MATRIX_MCP_TOOL,
        len(fetched),
    )
    return len(fetched)
