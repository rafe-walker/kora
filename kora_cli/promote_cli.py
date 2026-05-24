"""``kora promote`` operator CLI commands — KR-CC1-POLISH (#198).

Adds an ergonomic surface so operator can inspect + ad-hoc-run
the 6 promotion loops from the terminal without opening the
cockpit:

  * ``kora promote status``               — per-loop pending /
    approved / rejected counts + last cycle timestamp.
  * ``kora promote run-once <loop>``     — invoke one cycle of a
    specific loop ad-hoc. Returns the cycle's summary dict (the
    same shape each loop's heartbeat tick logs at INFO).
  * ``kora promote history <loop>``      — last 30 days of audit
    rows for the loop (proposed / approved / rejected, etc).
  * ``kora promote pending <loop>``      — JSON dump of currently-
    pending proposals for the loop.

# Loop registry

The 6 loop names — kept in sync with the on-disk store layout +
the cycle entry points each loop's plugin.py exposes:

  | Loop name              | Store layout (under promotions/)   |
  | ---------------------- | ---------------------------------- |
  | phrasebook             | pending/approved/rejected/expired/ |
  | snapshot_expand        | applied/  (auto-apply variant)     |
  | router_tuning          | pending/approved/rejected/expired/ |
  | tool_trimming          | pending/approved/rejected/expired/ |
  | probe_fix_envelopes    | pending/approved/rejected/expired/ |
  | email_intent           | pending/approved/rejected/expired/ |

Snapshot-expand's audit-only variant is treated specially: it
has no pending/approved/rejected statuses (the loop is
audit-only by default + auto-apply persists to ``applied/`` only).
For that loop, ``status`` shows applied-record counts only;
``pending`` errors out with a clear "this loop doesn't use the
pending/approved/rejected store" message.

# Output discipline

All commands print JSON to stdout — operator pipes through ``jq``
or similar for ad-hoc queries. Errors print a single-line JSON
shape ``{"error": "<msg>"}`` + exit code 1.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Loop registry
# ---------------------------------------------------------------------------


# Audit seam each loop's propose-side row uses. Looked up by
# ``kora promote history`` to project the loop's recent events.
# Approve/reject/expire share ``promotion.approved`` /
# ``promotion.rejected`` across all loops (via the shared
# endpoint transition helper from #193); auto-approve uses
# ``promotion.probe_envelope_action_auto_approved`` for the
# probe-fix loop (KR-CC1-POLISH).
_LOOP_AUDIT_SEAMS: Dict[str, Tuple[str, ...]] = {
    "phrasebook": (
        "promotion.proposed",
        "promotion.approved",
        "promotion.rejected",
    ),
    "snapshot_expand": ("promotion.snapshot_field_added",),
    "router_tuning": (
        "promotion.router_trigger_proposed",
        "promotion.approved",
        "promotion.rejected",
    ),
    "tool_trimming": (
        "promotion.tool_trim_proposed",
        "promotion.approved",
        "promotion.rejected",
    ),
    "probe_fix_envelopes": (
        "promotion.probe_envelope_action_proposed",
        "promotion.probe_envelope_action_auto_approved",
        "promotion.approved",
        "promotion.rejected",
    ),
    "email_intent": (
        "promotion.email_intent_pattern_proposed",
        "promotion.approved",
        "promotion.rejected",
    ),
}


# All 6 loops in their conventional dispatch order. Used by
# ``status`` (iterates all) + as the allowlist for the per-loop
# subcommands.
LOOP_NAMES: Tuple[str, ...] = tuple(_LOOP_AUDIT_SEAMS.keys())


# Loops whose proposals live under the standard
# ``promotions/<loop>/{pending,approved,rejected,expired}/``
# layout (i.e. all loops EXCEPT snapshot_expand).
_STANDARD_STORE_LOOPS = frozenset(
    name for name in LOOP_NAMES if name != "snapshot_expand"
)


def _promotions_root() -> Path:
    """Resolve the promotions root the same way the _shared store
    does (env override → KORA_HOME/promotions). Re-implemented
    here so the CLI doesn't import the store module just to read
    file counts (keeps the CLI fast at startup)."""
    override = os.environ.get("KORA_PROMOTIONS_DIR", "").strip()
    if override:
        return Path(override)
    from kora_constants import get_kora_home

    return get_kora_home() / "promotions"


# ---------------------------------------------------------------------------
# Loop cycle dispatch (run-once)
# ---------------------------------------------------------------------------


def _resolve_cycle_callable(loop_name: str) -> Callable[..., Any]:
    """Import + return the async cycle function for a loop.

    Lazy import: keeps ``kora promote`` startup quick (the loop
    modules pull in clustering / pricing helpers that aren't
    needed for the read-only subcommands).
    """
    if loop_name == "phrasebook":
        from kora_cli.promote.phrasebook.cycle import (
            run_phrasebook_promotion_cycle,
        )

        return run_phrasebook_promotion_cycle
    if loop_name == "snapshot_expand":
        from kora_cli.promote.snapshot_expand.cycle import (
            run_snapshot_expand_cycle,
        )

        return run_snapshot_expand_cycle
    if loop_name == "router_tuning":
        from kora_cli.promote.router_tuning.plugin import (
            run_router_tuning_cycle,
        )

        return run_router_tuning_cycle
    if loop_name == "tool_trimming":
        from kora_cli.promote.tool_trimming.plugin import (
            run_tool_trimming_cycle,
        )

        return run_tool_trimming_cycle
    if loop_name == "probe_fix_envelopes":
        from kora_cli.promote.probe_fix_envelopes.plugin import (
            run_probe_fix_envelopes_cycle,
        )

        return run_probe_fix_envelopes_cycle
    if loop_name == "email_intent":
        from kora_cli.promote.email_intent.plugin import (
            run_email_intent_cycle,
        )

        return run_email_intent_cycle
    raise ValueError(f"unknown loop: {loop_name!r}")


# ---------------------------------------------------------------------------
# Status accessor (per-loop file counts)
# ---------------------------------------------------------------------------


def _count_files_in(path: Path) -> int:
    """Count ``*.json`` files in a status subdir. Missing dir → 0."""
    if not path.is_dir():
        return 0
    return sum(
        1 for child in path.iterdir() if child.is_file() and child.suffix == ".json"
    )


def _newest_mtime(path: Path) -> Optional[float]:
    """Return the newest mtime among ``*.json`` files in ``path``,
    or None when the dir is empty / missing. Used as a proxy for
    "last activity in this status bucket"."""
    if not path.is_dir():
        return None
    candidates = [
        child.stat().st_mtime
        for child in path.iterdir()
        if child.is_file() and child.suffix == ".json"
    ]
    if not candidates:
        return None
    return max(candidates)


def _format_iso_from_ts(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _loop_status_dict(loop_name: str) -> Dict[str, Any]:
    """Per-loop status projection. Standard-layout loops surface
    pending/approved/rejected/expired counts; snapshot_expand
    surfaces applied count only."""
    root = _promotions_root() / loop_name
    if loop_name == "snapshot_expand":
        applied = root / "applied"
        applied_count = _count_files_in(applied)
        return {
            "loop": loop_name,
            "store_layout": "applied_only",
            "applied_count": applied_count,
            "last_activity_at": _format_iso_from_ts(
                _newest_mtime(applied)
            ),
        }
    statuses = ("pending", "approved", "rejected", "expired")
    counts = {s: _count_files_in(root / s) for s in statuses}
    # Newest activity across any status — operator-grep helper.
    newest_per_status = [_newest_mtime(root / s) for s in statuses]
    newest = max((ts for ts in newest_per_status if ts is not None), default=None)
    return {
        "loop": loop_name,
        "store_layout": "standard",
        "counts": counts,
        "last_activity_at": _format_iso_from_ts(newest),
    }


# ---------------------------------------------------------------------------
# History accessor (audit JSONL projection)
# ---------------------------------------------------------------------------


def _loop_history(loop_name: str, *, days: int = 30) -> List[Dict[str, Any]]:
    """Project audit rows belonging to this loop into a JSON-safe
    list. Reads via ``kora_cli.audit.jsonl_reader.read_audit_entries``.

    Per-loop seam filter is applied so cross-loop rows (e.g. the
    shared ``promotion.approved`` seam) don't get attributed to
    every loop — we match on the ``caller_session_id`` prefix
    ``promotion:<loop>:`` which the per-loop emit sites all use.
    """
    try:
        from kora_cli.audit.jsonl_reader import read_audit_entries
    except Exception as exc:
        logger.warning(
            "[kora.promote_cli.history] audit reader import failed: %r",
            exc,
        )
        return []

    seams = _LOOP_AUDIT_SEAMS.get(loop_name, ())
    if not seams:
        return []
    since = datetime.now(timezone.utc) - timedelta(days=days)
    csid_prefix = f"promotion:{loop_name}:"
    out: List[Dict[str, Any]] = []
    for seam in seams:
        try:
            rows = read_audit_entries(seam=seam, since=since)
        except Exception as exc:
            logger.warning(
                "[kora.promote_cli.history] read_audit_entries(%s) "
                "raised %r",
                seam,
                exc,
            )
            continue
        for row in rows:
            csid = getattr(row, "caller_session_id", "") or ""
            # promotion.proposed (phrasebook) + the per-loop
            # proposed seams use ``promotion:<loop>:<id>`` so the
            # prefix filter scopes correctly. For the shared
            # promotion.approved / promotion.rejected seams the
            # prefix filter is the disambiguator.
            if seam in (
                "promotion.proposed",
                "promotion.approved",
                "promotion.rejected",
            ) and not csid.startswith(csid_prefix):
                continue
            out.append(
                {
                    "emitted_at": (
                        getattr(row, "emitted_at", None)
                        .isoformat()
                        if getattr(row, "emitted_at", None) is not None
                        else None
                    ),
                    "seam": seam,
                    "caller_session_id": csid or None,
                    "details": dict(getattr(row, "details", {}) or {}),
                }
            )
    out.sort(
        key=lambda r: r.get("emitted_at") or "",
        reverse=True,
    )
    return out


# ---------------------------------------------------------------------------
# Pending accessor (per-loop)
# ---------------------------------------------------------------------------


def _loop_pending(loop_name: str) -> List[Dict[str, Any]]:
    """Project the loop's pending/ directory into a JSON-safe list.

    Snapshot-expand has no pending/ directory by design — the
    caller (subcommand) surfaces that as a structured error.
    """
    if loop_name == "snapshot_expand":
        raise ValueError(
            "snapshot_expand has no pending/ status — this loop is "
            "audit-only by default (auto-apply persists to applied/ "
            "directly). Use ``kora promote history snapshot_expand`` "
            "to view recent activity."
        )
    pending_dir = _promotions_root() / loop_name / "pending"
    if not pending_dir.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for path in sorted(pending_dir.iterdir()):
        if not path.is_file() or path.suffix != ".json":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "[kora.promote_cli.pending] %s unreadable: %r — skipped",
                path,
                exc,
            )
            continue
        if isinstance(payload, dict):
            out.append(payload)
    # Match the cockpit panel ordering (highest-confidence first
    # falls back to filesystem name for loops without a confidence
    # field).
    out.sort(
        key=lambda p: (
            -float(p.get("confidence") or 0.0),
            -int(p.get("cluster_size") or 0),
        )
    )
    return out


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------


def _emit_json(payload: Any) -> None:
    """Write a JSON object to stdout + newline. Stable shape for
    operator piping through ``jq``."""
    json.dump(payload, sys.stdout, indent=2, sort_keys=False, default=str)
    sys.stdout.write("\n")


def _emit_error(message: str) -> int:
    _emit_json({"error": message})
    return 1


def cmd_promote(args: Any) -> Optional[int]:
    """Top-level dispatcher for ``kora promote <subcommand>``.

    Matches the cli.py convention: each subcommand has a
    ``args.promote_command`` value set by argparse; we dispatch to
    a per-subcommand handler returning an int exit code. Bare
    ``kora promote`` with no subcommand surfaces a usage line.
    """
    sub = getattr(args, "promote_command", None)
    if sub is None:
        _emit_json(
            {
                "error": "missing subcommand",
                "subcommands": [
                    "status",
                    "run-once",
                    "history",
                    "pending",
                ],
                "loops": list(LOOP_NAMES),
            }
        )
        return 1
    if sub == "status":
        return _cmd_promote_status(args)
    if sub == "run-once":
        return _cmd_promote_run_once(args)
    if sub == "history":
        return _cmd_promote_history(args)
    if sub == "pending":
        return _cmd_promote_pending(args)
    return _emit_error(f"unknown subcommand: {sub!r}")


def _cmd_promote_status(args: Any) -> int:
    rows = [_loop_status_dict(name) for name in LOOP_NAMES]
    _emit_json({"loops": rows})
    return 0


def _cmd_promote_run_once(args: Any) -> int:
    loop_name = getattr(args, "loop", None)
    if not loop_name or loop_name not in LOOP_NAMES:
        return _emit_error(
            f"loop must be one of {list(LOOP_NAMES)} "
            f"(got {loop_name!r})"
        )
    try:
        cycle = _resolve_cycle_callable(loop_name)
    except Exception as exc:
        return _emit_error(
            f"unable to resolve cycle for {loop_name!r}: {exc!r}"
        )
    try:
        summary = asyncio.run(cycle())
    except Exception as exc:
        return _emit_error(
            f"cycle raised {type(exc).__name__}: {exc}"
        )
    _emit_json({"loop": loop_name, "summary": summary})
    return 0


def _cmd_promote_history(args: Any) -> int:
    loop_name = getattr(args, "loop", None)
    if not loop_name or loop_name not in LOOP_NAMES:
        return _emit_error(
            f"loop must be one of {list(LOOP_NAMES)} "
            f"(got {loop_name!r})"
        )
    days = int(getattr(args, "days", None) or 30)
    rows = _loop_history(loop_name, days=days)
    _emit_json({"loop": loop_name, "days": days, "rows": rows})
    return 0


def _cmd_promote_pending(args: Any) -> int:
    loop_name = getattr(args, "loop", None)
    if not loop_name or loop_name not in LOOP_NAMES:
        return _emit_error(
            f"loop must be one of {list(LOOP_NAMES)} "
            f"(got {loop_name!r})"
        )
    try:
        rows = _loop_pending(loop_name)
    except ValueError as exc:
        return _emit_error(str(exc))
    _emit_json({"loop": loop_name, "pending": rows})
    return 0
