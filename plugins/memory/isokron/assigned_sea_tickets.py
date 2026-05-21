"""Read helper for the ``/api/sea-tickets/kora-assigned`` panel (KR-P2-CLEANUP ST2).

Queries ``public.tickets`` for all Sea_Tickets currently assigned to
Kora and groups them into the four panel buckets:

  * ``in_progress``     — claim_fence_token is set (Kora's working it)
  * ``queued``          — assigned + no active claim
  * ``recently_resolved`` — resolved within the last N rows (most recent first)
  * ``failed_or_blocked`` — failed_terminal / blocked_needs_operator

# Honest scope (panel-side richness deferred)

The stub returned a few cockpit-friendly fields the substrate doesn't
directly carry on ``tickets``:

  * ``criticality`` — surfaced as ``sea_priority`` ("low" / "normal" / "high" /
    "frontier") today; the panel may want a CASE-aware mapping later.
  * ``model_tier_used`` — comes from a ``kora.sea_ticket.resolved`` chain
    event's payload (KR-P2-E ST4). Joining ``event_log`` per row would
    multiply the query cost; ST2 leaves this ``None`` and the panel
    renders "unknown" in the absence. A future "denormalize last
    model_tier onto tickets" change can backfill it.
  * ``failure_count_by_reason`` — also requires aggregating event_log
    rows by ``reason``. Same story; ST2 returns an empty dict.

The shape (top-level keys + per-entry key set) matches the v1 stub
exactly so the cockpit panel renders without changes.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Column projection sized to what panel renders today. Extra fields
# (chain-event joins for model_tier_used, failure aggregations) land in
# follow-on iterations.
_ASSIGNED_TICKETS_SQL = """
SELECT
    id::text                       AS id,
    COALESCE(title, '<untitled>')  AS title,
    sea_status                     AS sea_status,
    sea_priority                   AS sea_priority,
    sea_active_claim_token::text   AS active_claim_token,
    sea_active_claim_actor_id::text AS active_claim_actor,
    claim_count                    AS claim_count,
    work_attempt_count             AS work_attempt_count,
    next_eligible_at::text         AS next_eligible_at,
    created_at::text               AS created_at
  FROM public.tickets
 WHERE sea_assigned_to_actor_id = $1::uuid
   AND kind = 'sea'
 ORDER BY created_at DESC
 LIMIT 200
"""


_RECENTLY_RESOLVED_LIMIT = 20


_RESOLVED_STATUSES = frozenset({"completed", "released", "failed_retryable"})
_FAILED_OR_BLOCKED_STATUSES = frozenset(
    {"failed_terminal", "blocked_needs_operator"}
)


def _classify_row(row: dict[str, Any]) -> str:
    """Return the panel bucket name for a row.

    Buckets are mutually exclusive — a ticket falls into exactly one.
    Tiebreakers: an in-progress ticket (active claim token) ALWAYS goes
    to ``in_progress`` even if its sea_status would otherwise put it
    elsewhere.
    """
    has_active_claim = row.get("active_claim_token") is not None
    sea_status = row.get("sea_status")

    if has_active_claim:
        return "in_progress"
    if sea_status in _FAILED_OR_BLOCKED_STATUSES:
        return "failed_or_blocked"
    if sea_status in _RESOLVED_STATUSES:
        return "recently_resolved"
    return "queued"


def _to_in_progress_entry(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "criticality": row.get("sea_priority") or "normal",
        "claimed_at": row.get("created_at"),  # closest available; tickets
        # don't carry a dedicated "claimed_at" column today
        "claim_count": int(row.get("claim_count") or 0),
        "work_attempt_count": int(row.get("work_attempt_count") or 0),
    }


def _to_queued_entry(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "criticality": row.get("sea_priority") or "normal",
        "assigned_at": row.get("created_at"),
        "next_eligible_at": row.get("next_eligible_at"),
    }


def _to_resolved_entry(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "criticality": row.get("sea_priority") or "normal",
        "resolved_at": row.get("created_at"),
        "resolution": row.get("sea_status") or "released",
        # model_tier_used requires a kora.sea_ticket.resolved chain-event
        # JOIN; deferred. Return None — the panel renders "unknown".
        "model_tier_used": None,
    }


def _to_failed_entry(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "criticality": row.get("sea_priority") or "normal",
        "state": row.get("sea_status") or "blocked_needs_operator",
        # failure_count_by_reason requires an event_log aggregation;
        # deferred. Return empty dict so the panel still renders.
        "failure_count_by_reason": {},
    }


async def read_assigned_sea_tickets(
    *,
    actor_id: str,
    pool: Any,
) -> dict[str, list[dict[str, Any]]]:
    """Run the assigned-Sea_Tickets read against ``pool`` (asyncpg.Pool).

    Returns the four-bucket grouped dict in panel-shape. Caller is
    responsible for pool acquisition / connection setup; this helper
    only owns the SQL + projection.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(_ASSIGNED_TICKETS_SQL, actor_id)

    in_progress: list[dict[str, Any]] = []
    queued: list[dict[str, Any]] = []
    recently_resolved: list[dict[str, Any]] = []
    failed_or_blocked: list[dict[str, Any]] = []

    for raw_row in rows:
        row = dict(raw_row)
        bucket = _classify_row(row)
        if bucket == "in_progress":
            in_progress.append(_to_in_progress_entry(row))
        elif bucket == "queued":
            queued.append(_to_queued_entry(row))
        elif bucket == "recently_resolved":
            if len(recently_resolved) < _RECENTLY_RESOLVED_LIMIT:
                recently_resolved.append(_to_resolved_entry(row))
        else:  # failed_or_blocked
            failed_or_blocked.append(_to_failed_entry(row))

    return {
        "in_progress": in_progress,
        "queued": queued,
        "recently_resolved": recently_resolved,
        "failed_or_blocked": failed_or_blocked,
    }


async def get_assigned_sea_tickets_via_provider(
    *,
    provider: Any,
    actor_id: Optional[str] = None,
) -> Optional[dict[str, list[dict[str, Any]]]]:
    """Wrapper that resolves the asyncpg pool from ``provider`` + runs the
    read.

    Returns ``None`` on any of:
      * ``provider`` is ``None``
      * ``provider._connection`` is missing or not started
      * The actor_id cannot be resolved (caller passed None and the
        substrate has no ``actor_registry`` row for ``actor_kind='kora'``
        in the configured workspace)
      * The query raises

    On ``None``, the caller (typically the web_server endpoint) falls
    back to the stub shape + ``error`` field. Errors are logged loudly
    so operators can grep.
    """
    if provider is None:
        logger.debug(
            "[assigned_sea_tickets] provider is None; returning None"
        )
        return None

    connection = getattr(provider, "_connection", None)
    if connection is None:
        logger.warning(
            "[assigned_sea_tickets] provider has no _connection; "
            "returning None"
        )
        return None

    resolved_actor = actor_id
    if resolved_actor is None:
        try:
            resolved_actor = await _resolve_kora_actor_id(provider)
        except Exception:
            logger.exception(
                "[assigned_sea_tickets] kora actor_id resolution raised"
            )
            return None
    if resolved_actor is None:
        logger.warning(
            "[assigned_sea_tickets] no kora actor_id resolvable for the "
            "configured workspace; returning None"
        )
        return None

    try:
        import asyncio

        pool = connection.get_pg_pool()
        future = connection._submit_async(
            read_assigned_sea_tickets(actor_id=resolved_actor, pool=pool)
        )
        return await asyncio.wrap_future(future)
    except Exception:
        logger.exception(
            "[assigned_sea_tickets] read raised; returning None"
        )
        return None


# ---------------------------------------------------------------------------
# Internal: kora actor_id resolution (mirrors the poller's
# ``_resolve_kora_actor_id``; small enough to inline rather than
# extracting yet another helper)
# ---------------------------------------------------------------------------


_RESOLVE_KORA_ACTOR_SQL = """
SELECT actor_id::text AS actor_id
  FROM public.actor_registry
 WHERE workspace_id = $1
   AND actor_kind = 'kora'
 LIMIT 1
"""


async def _resolve_kora_actor_id(provider: Any) -> Optional[str]:
    workspace_id = provider._resolve_workspace_id()
    if not workspace_id:
        return None
    connection = provider._connection
    pool = connection.get_pg_pool()

    async def _q() -> Optional[str]:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_RESOLVE_KORA_ACTOR_SQL, workspace_id)
        return None if row is None else row["actor_id"]

    import asyncio

    future = connection._submit_async(_q())
    return await asyncio.wrap_future(future)
