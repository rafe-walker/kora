"""Chain event emit + recent events read (KR-2 ST4).

Two halves:

- :func:`read_recent_kora_events` — direct asyncpg read against
  ``hivex_foundation.event_log`` filtered to ``event_type LIKE 'kora.%'``.
  ``event_log`` is **tenant_id UUID keyed** (the one genuine substrate
  exception to the workspace_id TEXT pattern, per foundation/0003); the
  query resolves the caller's ``workspace_id`` (Clerk ``org_*`` TEXT)
  to ``tenant_id`` via ``JOIN hivex_foundation.tenant ON
  t.clerk_org_id = $1``. Matches the TS reader at
  ``packages/sea-mcp-server/src/kora/context-assembler/index.ts:287``.

- :func:`emit_kora_event` — deferred write surface. Chain events go
  through the substrate's ``_emit_chain_event`` SECDEF (which sets
  ``prev_event_hash`` / ``this_event_hash`` to maintain chain witness
  integrity); calling it from runtime Python without the SECDEF wrapper
  would break the witness chain. The path is a Sea MCP tool
  (working name ``kora__append_event``); as of substrate main
  ``28ff4f78``, no such tool is registered. Raises
  :class:`ChainEventEmitNotAvailableError` until the tool ships.
  BUILD_DEVIATIONS ``D-kr2-st4-no-chain-emit-mcp-tool``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ChainEventEmitNotAvailableError(RuntimeError):
    """Raised by :func:`emit_kora_event` until the Sea MCP tool ships.

    Same pattern as :class:`scratchpad.ScratchpadWriteNotAvailableError`
    — runtime callers MUST NOT bypass with direct INSERT or
    ``_emit_chain_event`` calls (would break chain witness integrity).
    """

    DEFAULT_MESSAGE = (
        "[kora.isokron.todo] chain event emit deferred — Sea MCP server "
        "does not yet expose kora__append_event (or equivalent). Tracked "
        "in BUILD_DEVIATIONS.md as D-kr2-st4-no-chain-emit-mcp-tool. "
        "Direct INSERT into hivex_foundation.event_log bypasses the "
        "prev_event_hash / this_event_hash chain — do NOT do that; the "
        "MCP tool wraps the substrate's _emit_chain_event SECDEF which "
        "preserves chain witness integrity."
    )

    def __init__(self, message: Optional[str] = None):
        super().__init__(message or self.DEFAULT_MESSAGE)


# ---------------------------------------------------------------------------
# Typed shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecentChainEvent:
    """One ``kora.*`` chain event from ``hivex_foundation.event_log``.

    Mirrors TS-side ``KoraRecentChainEvent`` in
    ``packages/sea-mcp-server/src/kora/context-assembler/types.ts:72``.
    ``payload_summary`` is ``jsonb_pretty(payload)`` truncated to 300
    chars + ellipsis — keeps system prompt bullets readable without
    bloating the prompt on operator-extended payloads.
    """

    event_id: str
    event_type: str
    occurred_at: str  # ISO-8601 from TIMESTAMPTZ
    payload_summary: str


# ---------------------------------------------------------------------------
# Read SQL — matches TS reader verbatim
# ---------------------------------------------------------------------------

SELECT_RECENT_KORA_CHAIN_EVENTS_SQL = """
    SELECT
      el.event_id::text                AS event_id,
      el.event_type                    AS event_type,
      el.occurred_at                   AS occurred_at,
      jsonb_pretty(el.payload)::text   AS payload_text
    FROM hivex_foundation.event_log el
    JOIN hivex_foundation.tenant t ON t.tenant_id = el.tenant_id
    WHERE t.clerk_org_id = $1
      AND el.event_type LIKE 'kora.%'
    ORDER BY el.occurred_at DESC
    LIMIT $2
"""

DEFAULT_RECENT_EVENT_LIMIT = 50
"""Per spec § ST4 + TS-side ``DEFAULT_RECENT_EVENT_LIMIT``."""

RECENT_EVENT_PAYLOAD_TRUNCATE_LENGTH = 300
"""Per TS-side ``RECENT_EVENT_PAYLOAD_TRUNCATE_LENGTH``. Truncated
``jsonb_pretty`` output preserves the leading structural lines so an
operator skimming the system prompt can still see what each event
carried, without bloating long payloads."""


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------


def _truncate_payload(payload_text: str) -> str:
    if len(payload_text) <= RECENT_EVENT_PAYLOAD_TRUNCATE_LENGTH:
        return payload_text
    return payload_text[:RECENT_EVENT_PAYLOAD_TRUNCATE_LENGTH] + "…"


def _project_event(row: dict[str, Any]) -> RecentChainEvent:
    occurred_at = row["occurred_at"]
    if hasattr(occurred_at, "isoformat"):
        occurred_at_iso = occurred_at.isoformat()
    else:
        occurred_at_iso = str(occurred_at)
    return RecentChainEvent(
        event_id=row["event_id"],
        event_type=row["event_type"],
        occurred_at=occurred_at_iso,
        payload_summary=_truncate_payload(row["payload_text"] or ""),
    )


async def read_recent_kora_events(
    workspace_id: str,
    pool: Any,
    *,
    limit: int = DEFAULT_RECENT_EVENT_LIMIT,
) -> list[RecentChainEvent]:
    """Read recent ``kora.*`` chain events for the workspace.

    Note: ``hivex_foundation.event_log`` keys off ``tenant_id UUID``,
    not ``workspace_id TEXT`` like the other Kora tables. The JOIN
    resolves the workspace_id (Clerk ``org_*``) to tenant_id via
    ``hivex_foundation.tenant.clerk_org_id``. The TS reader at
    ``packages/sea-mcp-server/src/kora/context-assembler/index.ts:287``
    uses the same JOIN; this is the substrate-team's established
    resolution pattern.

    No RLS GUC needed here: ``event_log``'s row-security model uses
    different machinery (per-partition partial indexes + foreign-key
    constraints to tenant); the JOIN itself enforces tenant isolation
    via the workspace_id binding.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            SELECT_RECENT_KORA_CHAIN_EVENTS_SQL, workspace_id, limit
        )
    return [_project_event(dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# Emit path (deferred — see BUILD_DEVIATIONS D-kr2-st4-no-chain-emit-mcp-tool)
# ---------------------------------------------------------------------------


async def emit_kora_event(
    *,
    workspace_id: str,
    event_type: str,
    payload: Any,
    mcp_client: Any = None,
) -> str:
    """Emit a ``kora.*`` chain event via the Sea MCP tool surface.

    Raises ``ChainEventEmitNotAvailableError`` until
    ``kora__append_event`` (or equivalent) lands in the Sea MCP
    server. Caller signature matches the future MCP-backed
    implementation; when the tool ships the body switches to an
    ``mcp_client.invoke('kora__append_event', ...)`` call without
    any caller-side refactor.

    ``event_type`` must start with ``kora.`` and appear in the
    ``event_log_event_type_check`` constraint set (foundation/0136 +
    foundation/0138 ship the canonical vocabulary). Validation is
    enforced substrate-side by the MCP tool — runtime callers pass
    the literal through.
    """
    del workspace_id, event_type, payload, mcp_client
    raise ChainEventEmitNotAvailableError()
