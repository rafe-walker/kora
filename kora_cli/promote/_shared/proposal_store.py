"""Generic propose-then-approve file-backed store for promotion loops.

Extracted from the phrasebook (#186) store to avoid duplicating the
same pending/approved/rejected/expired filesystem layout across the
3 new propose-then-approve loops introduced in
KR-PROMOTE-LOOPS-COMPLETION-MEGABUCKET.

# Why per-loop subdirs (not one shared dir)

Each loop's proposal payload has a distinct shape; mixing them in one
directory would require a discriminator field + per-row type guards.
Per-loop subdirs keep operator triage trivial:

  ``${KORA_HOME}/promotions/<loop_name>/{pending,approved,rejected,expired}``

# Payload contract

The store is payload-agnostic: it accepts a JSON-serializable dict
keyed by ``proposal_id``. Each loop owns its own dataclass + (de)serialize
helpers; this module only handles atomic-write, list, status
transition, and expiry.

# Out of scope

  * Audit emission — each loop owns its own audit seam + emit call
    (the store is filesystem-only)
  * Operator-edit overrides — phrasebook (#186) needed a whitelist for
    pattern/reply_template/category overrides at approve-time; the
    new loops either don't take overrides (router-tuning, probe-
    envelopes) or take simpler ones (tool-trimming may take a
    per-tool retain decision but those land via the payload)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


PROMOTIONS_ROOT_ENV = "KORA_PROMOTIONS_DIR"
_PROMOTIONS_RELATIVE = Path("promotions")

STATUS_VALUES: Tuple[str, ...] = (
    "pending",
    "approved",
    "rejected",
    "expired",
)


class ProposalNotFound(LookupError):
    """Raised when an endpoint references a proposal_id that doesn't
    exist in any of the status subdirectories."""


def _root(loop_name: str) -> Path:
    """Resolve the loop's promotions root.

    Honors ``KORA_PROMOTIONS_DIR`` for tests that don't want to use
    the canonical KORA_HOME path; falls back to
    ``${KORA_HOME}/promotions/<loop_name>`` otherwise.
    """
    override = os.environ.get(PROMOTIONS_ROOT_ENV, "").strip()
    if override:
        return Path(override) / loop_name
    from kora_constants import get_kora_home

    return get_kora_home() / _PROMOTIONS_RELATIVE / loop_name


def _status_dir(loop_name: str, status: str) -> Path:
    if status not in STATUS_VALUES:
        raise ValueError(f"unknown proposal status: {status!r}")
    return _root(loop_name) / status


def save_pending(
    *, loop_name: str, proposal_id: str, payload: Dict[str, Any]
) -> Path:
    """Atomic-write a pending proposal. Returns the file path.

    ``payload`` is written verbatim. Caller is responsible for
    setting ``status="pending"`` inside it if the loop's shape
    carries a status field — the store doesn't enforce that.
    """
    target_dir = _status_dir(loop_name, "pending")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{proposal_id}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp, target)
    return target


def list_by_status(
    *, loop_name: str, status: str
) -> List[Dict[str, Any]]:
    """Return all proposals in a status directory (unordered).

    Caller can sort by whatever proposal-shape field it cares about
    (confidence / cluster_size / created_at). Loops with a wire-
    stable ordering convention apply it in their endpoint handler.
    """
    target_dir = _status_dir(loop_name, status)
    if not target_dir.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for path in sorted(target_dir.iterdir()):
        if not path.is_file() or path.suffix != ".json":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "[kora.promote.store] %s unreadable, skipped: %r",
                path,
                exc,
            )
            continue
        if isinstance(payload, dict):
            out.append(payload)
    return out


def load(*, loop_name: str, proposal_id: str) -> Tuple[str, Dict[str, Any]]:
    """Look up a proposal across all status directories.

    Returns ``(current_status, payload)``. Raises
    :class:`ProposalNotFound` when the file is missing.
    """
    for status in STATUS_VALUES:
        path = _status_dir(loop_name, status) / f"{proposal_id}.json"
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise ProposalNotFound(
                    f"proposal {proposal_id!r} exists at {path} but "
                    f"failed to load: {exc!r}"
                ) from exc
            if not isinstance(payload, dict):
                raise ProposalNotFound(
                    f"proposal {proposal_id!r} at {path} is not a JSON object"
                )
            return status, payload
    raise ProposalNotFound(f"proposal {proposal_id!r} not found")


def transition(
    *,
    loop_name: str,
    proposal_id: str,
    new_status: str,
    payload_mutator: Optional[Any] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Move a proposal from its current status to ``new_status``.

    Returns ``(old_status, post_transition_payload)``. Optionally
    runs ``payload_mutator(payload)`` to mutate the dict in place
    before persisting at the new status (callers use this to stamp
    review_notes / approver / etc.).

    Write-before-unlink shape — a crash mid-transition leaves BOTH
    files; recovery is operator-readable (list both directories).
    """
    if new_status not in STATUS_VALUES:
        raise ValueError(f"unknown proposal status: {new_status!r}")
    old_status, payload = load(loop_name=loop_name, proposal_id=proposal_id)
    if callable(payload_mutator):
        payload_mutator(payload)
    # The store-level status field is optional; we stamp it for
    # callers that don't bother in payload_mutator.
    payload.setdefault("_store_status", new_status)
    payload["_store_status"] = new_status

    target_dir = _status_dir(loop_name, new_status)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{proposal_id}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp, target)

    old_path = _status_dir(loop_name, old_status) / f"{proposal_id}.json"
    if old_path != target:
        try:
            old_path.unlink()
        except OSError as exc:
            logger.warning(
                "[kora.promote.store] old path %s unlink failed: %r — "
                "operator can clean manually",
                old_path,
                exc,
            )
    return old_status, payload


def expire_older_than(*, loop_name: str, days: int) -> int:
    """Move pending proposals older than ``days`` to ``expired/``.

    Returns count moved. Read ``created_at`` from the payload (ISO
    8601). Payloads without a parseable created_at are left in
    place (defensive; future loops may not carry that field).
    """
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    expired = 0
    for payload in list_by_status(loop_name=loop_name, status="pending"):
        proposal_id = str(payload.get("proposal_id") or "")
        if not proposal_id:
            continue
        ts_raw = payload.get("created_at")
        if not isinstance(ts_raw, str) or not ts_raw:
            continue
        try:
            if ts_raw.endswith("Z"):
                ts_raw_parsed = ts_raw[:-1] + "+00:00"
            else:
                ts_raw_parsed = ts_raw
            ts = datetime.fromisoformat(ts_raw_parsed).timestamp()
        except ValueError:
            continue
        if ts < cutoff:
            try:
                transition(
                    loop_name=loop_name,
                    proposal_id=proposal_id,
                    new_status="expired",
                    payload_mutator=lambda p: p.update(
                        {
                            "review_notes": (
                                f"auto-expired after {days} days pending"
                            )
                        }
                    ),
                )
                expired += 1
            except Exception as exc:
                logger.warning(
                    "[kora.promote.store] expire transition failed for "
                    "%s: %r",
                    proposal_id,
                    exc,
                )
    return expired
