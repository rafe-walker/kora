"""Proposal applier — KR-PROMOTE-SNAPSHOT-EXPAND.

For each :class:`SnapshotFieldProposal` produced by the proposer,
this module:

  * ALWAYS emits a ``promotion.snapshot_field_added`` audit row.
    The ``action`` field distinguishes ``"proposed"`` (v1 default
    — operator reviews via the audit JSONL or a future cockpit
    endpoint, then manually adds the collector) from
    ``"auto_applied"`` (env-gated — the loop would write a stub
    collector + bump SCHEMA_VERSION).
  * When ``KORA_PROMOTE_SNAPSHOT_EXPAND_AUTO_APPLY=true``, ALSO:
    persists a minimal proposal record under
    ``${KORA_HOME}/promotions/snapshot_expand/applied/<proposal_id>.json``
    so the operator has an at-rest artifact + emits
    ``action="auto_applied"`` in the audit row.

# Safety posture

Per STOP-ASK §4 of the bucket spec, schema-bumping at runtime is
fragile. The auto-apply path in v1 ONLY writes a stub record —
it does NOT modify ``state_snapshot.py`` or bump
``SCHEMA_VERSION`` itself. A separate operator-driven step (a
future cockpit "approve + scaffold" endpoint, or a manual code
edit) is still required to actually add the collector. The
auto_applied action is "we've recorded this; please scaffold."
This is the conservative interpretation of "auto-apply with
audit trail" — the audit trail is unconditional, the actual code
change stays operator-gated.

# Why not write the collector directly via codegen

Writing into ``state_snapshot.py`` from a cron task would mean:
  * Running code that mutates its own runtime imports.
  * Bumping SCHEMA_VERSION mid-process (cache invalidation; FE
    type drift; consumer assumptions about stable shape).
  * No code review on the generated collector.
The bucket spec's docstring template promised codegen as
"future-when-trusted." v1 takes the safer first step: the audit
trail establishes the loop's value over weeks, and operator
approves the scaffolding manually.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

from .proposer import SnapshotFieldProposal, proposal_to_dict

logger = logging.getLogger(__name__)


AUTO_APPLY_ENV = "KORA_PROMOTE_SNAPSHOT_EXPAND_AUTO_APPLY"
PROMOTIONS_ROOT_ENV = "KORA_PROMOTIONS_DIR"
_APPLIED_RELATIVE = Path("promotions") / "snapshot_expand" / "applied"


def _is_auto_apply_enabled() -> bool:
    """Read the env. Default ``false`` per v1 spec — flip to true
    only after operator builds trust with the loop."""
    raw = os.environ.get(AUTO_APPLY_ENV, "false").strip().lower()
    return raw in {"true", "1", "yes", "on"}


def _applied_dir() -> Path:
    """Resolve the applied-proposal store directory.

    Mirrors the phrasebook store's env-override pattern so tests
    can redirect via ``KORA_PROMOTIONS_DIR`` without touching
    KORA_HOME.
    """
    override = os.environ.get(PROMOTIONS_ROOT_ENV, "").strip()
    if override:
        return Path(override) / "snapshot_expand" / "applied"
    from kora_constants import get_kora_home

    return get_kora_home() / _APPLIED_RELATIVE


def _persist_applied(proposal: SnapshotFieldProposal) -> None:
    """Atomic-write the proposal as an applied record. Best-effort
    — OSError logged + swallowed (audit row is the canonical
    artifact)."""
    try:
        target_dir = _applied_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{proposal.proposal_id}.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(proposal_to_dict(proposal), indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, target)
    except OSError as exc:
        logger.warning(
            "[kora.promote.snapshot_expand.applier] persist failed for "
            "%s: %r — audit row still emitted",
            proposal.proposal_id,
            exc,
        )


def _emit_audit(
    proposal: SnapshotFieldProposal, *, action: str
) -> None:
    """Emit the ``promotion.snapshot_field_added`` audit row.

    Payload shape:
      * Full proposal projection (proposal_id / cluster_size /
        proposed_field_path / proposed_collector_summary /
        source_tool_name / sample_caller_session_ids /
        confidence / created_at)
      * ``action`` — one of ``"proposed"`` (auto-apply OFF) or
        ``"auto_applied"`` (auto-apply ON; v1 means audit + stub
        persist, NOT live schema mutation — see module docstring).
    """
    try:
        from kora_cli.audit.jsonl_sink import emit_audit
    except Exception as exc:
        logger.warning(
            "[kora.promote.snapshot_expand.applier] audit import failed: "
            "%r — promotion.snapshot_field_added skipped",
            exc,
        )
        return
    payload: Dict[str, Any] = proposal_to_dict(proposal)
    payload["action"] = action
    try:
        emit_audit(
            "promotion.snapshot_field_added",
            payload,
            caller_session_id=(
                f"promotion:snapshot_expand:{proposal.proposal_id}"
            ),
            source="reasoning",
        )
    except Exception as exc:
        logger.warning(
            "[kora.promote.snapshot_expand.applier] emit_audit raised "
            "%r — proposal lost from the audit stream",
            exc,
        )


def apply_proposal(proposal: SnapshotFieldProposal) -> str:
    """Apply one proposal. Returns the ``action`` string emitted in
    the audit row (``"proposed"`` or ``"auto_applied"``)."""
    if _is_auto_apply_enabled():
        _persist_applied(proposal)
        _emit_audit(proposal, action="auto_applied")
        return "auto_applied"
    _emit_audit(proposal, action="proposed")
    return "proposed"
