"""Read helper for ``/api/kora-control/observed-state`` (KR-P2-CLEANUP ST3).

Returns ALL kora_control rows for the active workspace, grouped by
lifecycle position:

  * ``active``           — open commands (lifecycle ∈
                           {created, visible_to_runtime, acknowledged,
                            enforcing})
  * ``recently_enforced`` — last N enforced commands (operator-facing
                           "did the runtime actually act?")
  * ``history``          — older terminal-state commands (enforced
                           beyond the recent cap; superseded, expired,
                           failed, escalated)

# Why a separate module from kora_control_reader.py

``KoraControlReader`` is the operational-path reader (per-claim STOP-KORA
check) and is constructed with a ``kora_actor_id`` because it calls
``transition_kora_control`` (which requires the runtime actor identity).
The panel-side read is workspace-scoped only — no actor_id needed, no
lifecycle transitions performed. Keeping it in its own module avoids
forcing the panel endpoint to fabricate a fake actor_id just to
instantiate the reader.

A thin :meth:`KoraControlReader.get_all_observed_commands` wrapper
exists for symmetry with the bucket spec wording; production callers
typically use :func:`get_observed_state_via_provider` directly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final, Optional

from .kora_control_reader import (
    KORA_CONTROL_WORKSPACE_GUC,
)

logger = logging.getLogger(__name__)


# Limits — keep the panel payload trim. Active set is operationally
# small (one or two open commands at a time during normal ops); the
# enforced cap surfaces the most-recent operator actions.
_RECENTLY_ENFORCED_LIMIT: Final[int] = 10
_HISTORY_LIMIT: Final[int] = 30


_ACTIVE_LIFECYCLE_STATES: Final[frozenset[str]] = frozenset(
    {"created", "visible_to_runtime", "acknowledged", "enforcing"}
)


_SELECT_ALL_KORA_CONTROL_SQL: Final[str] = """
    SELECT
        command_id::text             AS command_id,
        workspace_id                 AS workspace_id,
        issuer_session_id            AS issuer_session_id,
        issuer_actor_id::text        AS issuer_actor_id,
        issuer_actor_kind            AS issuer_actor_kind,
        level                        AS level,
        kind                         AS kind,
        reason                       AS reason,
        target_session               AS target_session,
        sequence                     AS sequence,
        lifecycle_state              AS lifecycle_state,
        created_at::text             AS created_at,
        visible_to_runtime_at::text  AS visible_to_runtime_at,
        expires_at::text             AS expires_at,
        observed_at::text            AS observed_at,
        acknowledged_at::text        AS acknowledged_at,
        enforced_at::text            AS enforced_at
      FROM public.kora_control
     WHERE kind = 'stop'
     ORDER BY created_at DESC
     LIMIT 200
"""


def _project_row(row: Any) -> dict[str, Any]:
    """Project an asyncpg row to the panel's dict shape."""
    return {
        "command_id": row["command_id"],
        "level": int(row["level"]),
        "kind": row["kind"],
        "reason": row["reason"],
        "issuer": _format_issuer(row),
        "sequence": int(row["sequence"]),
        "created_at": row.get("created_at"),
        "visible_to_runtime_at": row.get("visible_to_runtime_at"),
        "observed_at": row.get("observed_at"),
        "acknowledged_at": row.get("acknowledged_at"),
        "enforced_at": row.get("enforced_at"),
        "lifecycle_state": row["lifecycle_state"],
        "expires_at": row.get("expires_at"),
        "target_session": row.get("target_session"),
    }


def _format_issuer(row: Any) -> str:
    """Human-readable issuer descriptor for the panel.

    Mirrors the v1 stub format: ``"<actor_kind/id> (cockpit session <session_id>)"``.
    The substrate ``kora_control_issuer_actor_kind_not_kora`` CHECK
    guarantees ``issuer_actor_kind != 'kora'``; the cockpit-facing
    label is always a non-Kora actor.
    """
    actor_kind = row.get("issuer_actor_kind") or "operator"
    actor_id = row.get("issuer_actor_id") or "<unknown>"
    session_id = row.get("issuer_session_id") or "<unknown>"
    return f"{actor_kind}/{actor_id} (cockpit session {session_id})"


def _classify_row(row: dict[str, Any]) -> str:
    """Return panel bucket name. Mutually exclusive."""
    state = row.get("lifecycle_state")
    if state in _ACTIVE_LIFECYCLE_STATES:
        return "active"
    if state == "enforced":
        return "recently_enforced"
    return "history"


async def read_observed_kora_control(
    *, pool: Any, workspace_id: str
) -> dict[str, list[dict[str, Any]]]:
    """Run the SQL against ``pool`` (asyncpg.Pool) inside a transaction
    that sets the ``app.workspace_id`` GUC for RLS scoping. Returns
    the panel-shaped grouped dict.

    Caller owns acquisition; this helper only owns the GUC + SQL +
    projection + Python-side grouping. Limits are applied during the
    grouping pass — the SQL fetches up to 200 rows and we slice in
    Python so the bucket counts can independently honor different
    limits without multiple round trips.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config($1, $2, true)",
                KORA_CONTROL_WORKSPACE_GUC,
                workspace_id,
            )
            rows = await conn.fetch(_SELECT_ALL_KORA_CONTROL_SQL)

    active: list[dict[str, Any]] = []
    recently_enforced: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []

    for raw_row in rows:
        projected = _project_row(raw_row)
        bucket = _classify_row(projected)
        if bucket == "active":
            active.append(projected)
        elif bucket == "recently_enforced":
            if len(recently_enforced) < _RECENTLY_ENFORCED_LIMIT:
                recently_enforced.append(projected)
            else:
                # Once recently_enforced is saturated, additional enforced
                # rows spill into history so they're still visible to
                # operators scrolling further back.
                if len(history) < _HISTORY_LIMIT:
                    history.append(projected)
        else:  # history
            if len(history) < _HISTORY_LIMIT:
                history.append(projected)

    return {
        "active": active,
        "recently_enforced": recently_enforced,
        "history": history,
    }


async def get_observed_state_via_provider(
    *, provider: Any
) -> Optional[dict[str, list[dict[str, Any]]]]:
    """Resolve ``provider``'s workspace_id + pool + run the read.

    Returns ``None`` on:
      * ``provider`` is ``None``
      * ``_connection`` missing
      * workspace_id unresolvable
      * the query raises

    Caller falls back to the stub shape + ``error`` field.
    """
    if provider is None:
        logger.debug(
            "[observed_kora_control] provider is None; returning None"
        )
        return None

    connection = getattr(provider, "_connection", None)
    if connection is None:
        logger.warning(
            "[observed_kora_control] provider has no _connection; "
            "returning None"
        )
        return None

    try:
        workspace_id = provider._resolve_workspace_id()
    except Exception:
        logger.exception(
            "[observed_kora_control] workspace_id resolution raised"
        )
        return None
    if not workspace_id:
        logger.warning(
            "[observed_kora_control] no workspace_id resolvable; "
            "returning None"
        )
        return None

    try:
        pool = connection.get_pg_pool()
        future = connection._submit_async(
            read_observed_kora_control(pool=pool, workspace_id=workspace_id)
        )
        return await asyncio.wrap_future(future)
    except Exception:
        logger.exception(
            "[observed_kora_control] read raised; returning None"
        )
        return None
