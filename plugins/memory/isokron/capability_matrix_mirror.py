"""Python mirror of the Kora column of ``ACTOR_CAPABILITY_MATRIX``.

[kora.isokron.todo] **C2 INTERIM** — swap to ``kora__read_kora_capability_row``
MCP tool when K-7 (Sea MCP capability-row tool) lands. PM-decided
2026-05-20 STOP-gate: blocking CC#3 on a CC#1 dependency to ship KR-2
ST2 burns days; C2 mirror is the unblock path. A parity test
(``tests/plugins/memory/test_capability_matrix_parity.py``) guards
drift against the TS source.

Source of truth:
  ``packages/sea-mcp-server/src/capability-matrix.ts`` in the IsoKron
  substrate repo. Specifically the ``ACTOR_CAPABILITY_MATRIX`` const's
  Kora column for each entry in ``SEA_CAPABILITIES`` (24) +
  ``KORA_BROADER_CAPABILITIES`` (24).

BUILD_DEVIATIONS entry: ``D-kr2-st2-capability-matrix-mirror`` —
closes when K-7 lands.

Capability names are stable strings per the TS source header:
> "Capability names are stable strings — they appear in chain events,
> audit logs, error payloads, and bundle profile definitions. Renames
> are operator-direct schema changes (PolicyRegistry / chain-event-
> vocabulary tier of stability)."

So a Python mirror is safe across normal-velocity substrate evolution.
The parity test catches additions, removals, and Kora-column flips.
"""

from __future__ import annotations

from .models import KoraCapabilityRow


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

    Sync (no network IO) — this is the C2 interim path. Once K-7 lands
    the Sea MCP ``kora__read_kora_capability_row`` tool, the read path
    on ``provider.py`` swaps to call that tool (async), and this
    function becomes a fallback / test fixture only.

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
