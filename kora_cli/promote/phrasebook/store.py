"""Proposal persistence — KR-PROMOTE-PHRASEBOOK-FOUNDATION (Deliverable D persistence side).

Pending proposals live at
``${KORA_HOME}/promotions/phrasebook/pending/<proposal_id>.json``.
Approved/rejected proposals get moved into sibling ``approved/`` /
``rejected/`` directories on status transition (atomic rename).

Why files instead of substrate / SQLite: this loop runs at idle
cadence and produces a handful of proposals per day. File-per-
proposal makes operator triage trivial (curl the directory, jq
the JSONs, git-diff before/after operator edits) without
requiring substrate round-trips. The audit JSONL is the
forensic-truth stream; the files are the live working set.

# Path layout

```
${KORA_HOME}/promotions/
  phrasebook/
    pending/   <uuid>.json
    approved/  <uuid>.json
    rejected/  <uuid>.json
```

# Audit emission

Status transitions emit one of the three new ``promotion.*``
seams. Persistence + audit are separate concerns; the store
module owns persistence and the cycle / endpoints own audit.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .proposer import (
    PROPOSAL_STATUS_VALUES,
    PromotionProposal,
    proposal_from_dict,
    proposal_to_dict,
)

logger = logging.getLogger(__name__)


PROMOTIONS_ROOT_ENV = "KORA_PROMOTIONS_DIR"
_PROMOTIONS_RELATIVE = Path("promotions") / "phrasebook"


class ProposalNotFound(LookupError):
    """Raised when an endpoint references a proposal_id that
    doesn't exist in any of the status subdirectories."""


def _root() -> Path:
    """Return the promotions root for the phrasebook loop.

    Honors ``KORA_PROMOTIONS_DIR`` for tests that don't want to
    use the canonical KORA_HOME path; falls back to
    ``${KORA_HOME}/promotions/phrasebook`` otherwise.
    """
    override = os.environ.get(PROMOTIONS_ROOT_ENV, "").strip()
    if override:
        return Path(override) / "phrasebook"
    from kora_constants import get_kora_home

    return get_kora_home() / _PROMOTIONS_RELATIVE


def _status_dir(status: str) -> Path:
    if status not in PROPOSAL_STATUS_VALUES:
        raise ValueError(f"unknown proposal status: {status!r}")
    return _root() / status


def save_pending(proposal: PromotionProposal) -> Path:
    """Atomic write a pending proposal. Returns the file path."""
    if proposal.status != "pending":
        raise ValueError(
            f"save_pending called with status={proposal.status!r}"
        )
    target_dir = _status_dir("pending")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{proposal.proposal_id}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(proposal_to_dict(proposal), indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, target)
    return target


def list_pending() -> List[PromotionProposal]:
    """Read all pending proposals, sorted highest-confidence first
    (operator's primary review order)."""
    return _list_status("pending", sort_by_confidence=True)


def list_by_status(status: str) -> List[PromotionProposal]:
    """Operator-debug helper. Same order as list_pending for
    confidence comparability."""
    return _list_status(status, sort_by_confidence=True)


def _list_status(
    status: str, *, sort_by_confidence: bool = False
) -> List[PromotionProposal]:
    target_dir = _status_dir(status)
    if not target_dir.is_dir():
        return []
    out: List[PromotionProposal] = []
    for path in target_dir.iterdir():
        if not path.is_file() or path.suffix != ".json":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "[kora.promote.phrasebook.store] %s unreadable, "
                "skipped: %r",
                path,
                exc,
            )
            continue
        try:
            out.append(proposal_from_dict(payload))
        except Exception as exc:
            logger.warning(
                "[kora.promote.phrasebook.store] %s malformed, "
                "skipped: %r",
                path,
                exc,
            )
            continue
    if sort_by_confidence:
        out.sort(key=lambda p: (-p.confidence, -p.cluster_size))
    return out


def load(proposal_id: str) -> PromotionProposal:
    """Look up a proposal across all status directories. Raises
    :class:`ProposalNotFound` when no file matches."""
    for status in PROPOSAL_STATUS_VALUES:
        path = _status_dir(status) / f"{proposal_id}.json"
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                return proposal_from_dict(payload)
            except Exception as exc:
                raise ProposalNotFound(
                    f"proposal {proposal_id!r} exists at {path} but "
                    f"failed to load: {exc!r}"
                ) from exc
    raise ProposalNotFound(f"proposal {proposal_id!r} not found")


def transition(
    proposal_id: str,
    *,
    new_status: str,
    review_notes: str = "",
    overrides: Optional[dict] = None,
) -> PromotionProposal:
    """Move a proposal from its current status directory into
    ``new_status``'s directory. Optionally apply override fields
    (pattern / reply_template / category) the operator edited at
    approve-time.

    Returns the post-transition proposal. Raises
    :class:`ProposalNotFound` when the proposal_id is not
    present.

    Operation order: rehydrate → apply overrides → write to new
    directory → unlink old. The write-before-unlink shape means
    a crash mid-transition leaves BOTH files; recovery is
    operator-readable (list both directories).
    """
    if new_status not in PROPOSAL_STATUS_VALUES:
        raise ValueError(f"unknown proposal status: {new_status!r}")
    current_path: Optional[Path] = None
    current_status: Optional[str] = None
    for status in PROPOSAL_STATUS_VALUES:
        candidate = _status_dir(status) / f"{proposal_id}.json"
        if candidate.is_file():
            current_path = candidate
            current_status = status
            break
    if current_path is None or current_status is None:
        raise ProposalNotFound(f"proposal {proposal_id!r} not found")

    payload = json.loads(current_path.read_text(encoding="utf-8"))
    proposal = proposal_from_dict(payload)

    if overrides:
        # Whitelist of operator-editable fields. Anything else is
        # ignored — the proposer's other fields (cluster_size,
        # sample_questions, confidence, etc.) are read-only audit
        # context.
        editable = {"pattern", "reply_template", "category"}
        unknown = set(overrides.keys()) - editable
        if unknown:
            logger.debug(
                "[kora.promote.phrasebook.store] ignoring unknown "
                "override keys: %s",
                sorted(unknown),
            )
        pattern = overrides.get("pattern") or proposal.proposed_pattern
        reply_template = (
            overrides.get("reply_template")
            or proposal.proposed_reply_template
        )
        category = (
            overrides.get("category") or proposal.proposed_category
        )
        # Rebuild with overrides applied. The original
        # proposed_* fields persist as the audit-trail values
        # — overrides are recorded separately via the review_notes
        # field so the post-edit shape is recoverable.
        proposal = PromotionProposal(
            proposal_id=proposal.proposal_id,
            cluster_size=proposal.cluster_size,
            sample_questions=proposal.sample_questions,
            proposed_pattern=pattern,
            proposed_reply_template=reply_template,
            proposed_category=category,
            confidence=proposal.confidence,
            created_at=proposal.created_at,
            status=new_status,  # type: ignore[arg-type]
            review_notes=review_notes,
            cluster_caller_session_ids=proposal.cluster_caller_session_ids,
            haiku_synthesized=proposal.haiku_synthesized,
        )
    else:
        proposal = PromotionProposal(
            proposal_id=proposal.proposal_id,
            cluster_size=proposal.cluster_size,
            sample_questions=proposal.sample_questions,
            proposed_pattern=proposal.proposed_pattern,
            proposed_reply_template=proposal.proposed_reply_template,
            proposed_category=proposal.proposed_category,
            confidence=proposal.confidence,
            created_at=proposal.created_at,
            status=new_status,  # type: ignore[arg-type]
            review_notes=review_notes,
            cluster_caller_session_ids=proposal.cluster_caller_session_ids,
            haiku_synthesized=proposal.haiku_synthesized,
        )

    target_dir = _status_dir(new_status)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{proposal_id}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(proposal_to_dict(proposal), indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, target)
    if current_path != target:
        try:
            current_path.unlink()
        except OSError as exc:
            logger.warning(
                "[kora.promote.phrasebook.store] old path %s unlink "
                "failed: %r — operator can clean manually",
                current_path,
                exc,
            )
    return proposal


def expire_older_than(*, days: int) -> int:
    """Move pending proposals older than ``days`` to the
    ``expired/`` directory. Returns count moved. Intended for the
    cron task to call after generation so the pending list stays
    operator-actionable."""
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    pending = list_pending()
    expired = 0
    for proposal in pending:
        ts = proposal.created_at.timestamp()
        if ts < cutoff:
            transition(
                proposal.proposal_id,
                new_status="expired",
                review_notes=f"auto-expired after {days} days pending",
            )
            expired += 1
    return expired
