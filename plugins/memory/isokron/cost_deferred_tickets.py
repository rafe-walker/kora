"""Read helper: tickets in ``sea_status = 'deferred_cost_limit'``.

KR-P2-COST-FLIP needs a small dedicated read for the COST-PANEL's
``deferred_tickets`` projection. The existing
:func:`plugins.memory.isokron.assigned_sea_tickets.read_assigned_sea_tickets`
returns all assigned tickets bucketed into 4 status groups
(in_progress / queued / recently_resolved / failed_or_blocked) — none of
those groups is "deferred for cost reasons", so the COST panel needs a
status-specific read rather than post-filtering the bucketed output.

Mirrors the SQL shape + actor-id resolution pattern from
``assigned_sea_tickets.py`` but pivots the projection to the field set
COST-PANEL renders (id / title / criticality / deferred_at / reason).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Status-specific column projection. ``deferred_at`` is best-effort:
# the substrate doesn't carry a dedicated "deferred_at" timestamp, but
# the cost-defer write path sets ``next_eligible_at`` when it transitions
# the row, so that's the closest proxy. Falls back to ``created_at``
# when next_eligible_at is null (older rows).
_DEFERRED_COST_LIMIT_SQL = """
SELECT
    id::text                       AS id,
    COALESCE(title, '<untitled>')  AS title,
    sea_priority                   AS criticality,
    sea_status                     AS state,
    next_eligible_at::text         AS deferred_at,
    created_at::text               AS created_at
  FROM public.tickets
 WHERE sea_assigned_to_actor_id = $1::uuid
   AND kind = 'sea'
   AND sea_status = 'deferred_cost_limit'
 ORDER BY COALESCE(next_eligible_at, created_at) DESC
 LIMIT $2
"""


DEFAULT_DEFERRED_LIMIT = 10
"""Per COST-FLIP bucket §4: last N (10) sufficient. The cost-defer
state is intentionally noisy when the rung is high; pagination is
future work if operators ask for it."""


def _project_deferred_row(row: dict[str, Any]) -> dict[str, Any]:
    """Project a substrate row into the COST-PANEL deferred-ticket shape.

    ``reason`` is intentionally static — the substrate doesn't carry a
    per-row deferral reason column. The COST panel's existing FE shows
    a generic explainer text near the ticket, and operators can
    drill into the ticket detail to see the cost-ladder rung that
    triggered the defer. A richer reason source (e.g. a
    ``kora.ticket.deferred_cost`` chain event with structured payload)
    is future work.
    """
    deferred_at = row.get("deferred_at") or row.get("created_at")
    return {
        "id": row["id"],
        "title": row["title"],
        "criticality": row.get("criticality") or "normal",
        "state": "deferred_cost_limit",  # pinned — query filtered on this
        "deferred_at": deferred_at,
        "reason": (
            "Deferred by cost ladder — see Cost panel for the active "
            "rung; defer clears at next refresh or via operator override."
        ),
    }


async def read_deferred_cost_limit_tickets(
    *,
    actor_id: str,
    pool: Any,
    limit: int = DEFAULT_DEFERRED_LIMIT,
) -> list[dict[str, Any]]:
    """Read the actor's tickets in ``sea_status = 'deferred_cost_limit'``.

    Returns at most ``limit`` rows, ordered by best-effort
    deferral-time desc. Caller owns pool acquisition; this helper
    only owns the SQL + projection.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(_DEFERRED_COST_LIMIT_SQL, actor_id, limit)
    return [_project_deferred_row(dict(row)) for row in rows]
