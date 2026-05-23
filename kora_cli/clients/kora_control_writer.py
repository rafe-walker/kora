"""KoraControlWriter — Python wrapper around ``public.issue_kora_control``.

Mirror of :class:`plugins.memory.isokron.kora_control_reader.KoraControlReader`
on the WRITE side. The reader documents (lines 16-26) that there is
NO MCP wrapper for the kora_control SECDEFs in ``rafe-walker/isokron@main``
— ``packages/sea-mcp-server/src/tools/`` registers only K-7/8/9/10
(append-event, capability-row, relationlink, scratchpad-write). The
canonical access path is direct asyncpg through the IsoKron pool, same
as the reader (``constitution.py``, ``reads.py``, ``scratchpad.py``
patterns).

# Substrate SECDEF this wraps

Substrate migration ``packages/db/migrations/0090_kora_control_secdefs.sql``
(commit ``2abf095``, May 21 2026) ships:

  public.issue_kora_control(
    p_workspace_id      TEXT,
    p_issuer_session_id TEXT,
    p_issuer_actor_id   UUID,
    p_level             SMALLINT,
    p_kind              TEXT,
    p_reason            TEXT          DEFAULT NULL,
    p_target_session    TEXT          DEFAULT NULL,
    p_expires_at        TIMESTAMPTZ   DEFAULT NULL
  ) RETURNS TABLE (
    out_command_id              UUID,
    out_sequence                BIGINT,
    out_lifecycle_state         TEXT,
    out_superseded_command_ids  UUID[],
    out_chain_event_id          UUID
  )

# Load-bearing substrate-side rejections (informational for callers)

The SECDEF rejects fail-CLOSED at function entry — Kora's runtime
does not duplicate these checks (substrate is the source of truth),
but knowing them helps callers form valid requests:

  * ``actor_kind = 'kora'`` → ``42501 insufficient_privilege`` —
    R4.1 §9.3 / §10 load-bearing audit invariant. Kora cannot issue
    her own kora_control. Any non-Kora actor_id is fine (operator,
    cockpit, claude_pm_*, drone, etc.).
  * level=0 (reset) requires ``actor_kind = 'operator'`` —
    ``42501 insufficient_privilege``. The MCP layer enforces this
    upstream by restricting the ``kora__request_stop`` tool to
    levels 1 + 2, but it's worth knowing the substrate would reject
    L0 from any other caller anyway.
  * ``issuer_actor_id`` not in ``actor_registry`` OR belongs to a
    different workspace → ``22023 invalid_parameter_value``.
  * ``level`` not in 0-5 OR ``kind`` not in {'pause', 'drain',
    'abort', 'kill', 'reset'} OR (level, kind) mismatch →
    ``22023 invalid_parameter_value``.

# Why this is the only legitimate write path

The 0089 ``kora_control`` table has NO INSERT/UPDATE/DELETE RLS
policies — direct INSERT fails for non-service_role connections.
``issue_kora_control`` is the SECURITY DEFINER function granted to
``app_authenticated`` (0090 grants block). service_role bypass is
reserved for migrations + platform-emit; agent-side writes must go
through this SECDEF.

# Fail-CLOSED on missing actor_id

This module raises :class:`MissingActorIdError` BEFORE touching the
substrate when ``issuer_actor_id`` is ``None``. The substrate would
reject anyway (the SECDEF requires a UUID), but failing earlier
saves a roundtrip and gives the caller a typed exception with a
clear message ("supply caller.actor_id from mcp_callers.yaml").

# Dry-run mode

``dry_run=True`` returns a predicted-row shape (same keys as the
real return) without invoking the substrate. The predicted UUIDs
are placeholder strings (``"<dry-run>"``); ``out_sequence`` is
``-1``. Callers serializing this for an MCP ``tool_result`` should
mark ``dry_run: true`` in the envelope so the operator doesn't
mistake the prediction for a real-write outcome.

# 10-second client-side timeout

Per ST2 §4 Q3: tight 5-10s window on stop calls. We wrap the
substrate call in :func:`asyncio.wait_for` with a 10s deadline.
The SECDEF itself sets ``statement_timeout = '60s'``; the
client-side wait is the FIRST gate to trip and surfaces as
:class:`asyncio.TimeoutError` → :class:`KoraControlWriterTimeout`
in the wrapper. 10s leaves room for a healthy roundtrip (typical
SECDEF completes <100ms) while bounding the operator's wait on a
flaky link.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Literal, Optional

# asyncpg is the canonical Postgres driver but only required when the
# IsoKron memory provider is actually wired up. Mirror the lazy-import
# pattern used by ``plugins/memory/isokron/kora_operation_ledger.py``:
# catch the broad postgres-error class inside the call site rather than
# importing at module-load time. That keeps this module importable in
# test contexts (and on stripped-down deploys) that don't carry asyncpg.

logger = logging.getLogger(__name__)


# Client-side ceiling for issue_kora_control roundtrips. SECDEF's
# own statement_timeout is 60s; this is the first gate to trip.
ISSUE_KORA_CONTROL_TIMEOUT_SECONDS = 10.0


# Level + kind literals — match substrate 0089 CHECK constraint vocab.
# Source: 0090 SECDEF docstring + 0089 table CHECK.
StopLevel = Literal[1, 2, 3, 4, 5]
StopKind = Literal["pause", "drain", "abort", "kill"]


# (level, kind) pairs the substrate accepts. The 0089 table CHECK
# enforces: level=0 ↔ kind='reset'; L1 ↔ 'pause'; L2 ↔ 'drain';
# L3 ↔ 'abort'; L4/L5 ↔ 'kill'. The MCP tool layer restricts to
# L1/L2 only; we keep the full table here for completeness and
# error-message clarity.
_VALID_LEVEL_KIND: dict[int, str] = {
    1: "pause",
    2: "drain",
    3: "abort",
    4: "kill",
    5: "kill",
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class KoraControlWriterError(RuntimeError):
    """Base class for writer-side errors."""


class MissingActorIdError(KoraControlWriterError):
    """``issuer_actor_id`` was ``None``. Fail-CLOSED before substrate."""


class InvalidLevelKindError(KoraControlWriterError):
    """``(level, kind)`` doesn't match the substrate CHECK. Fail-CLOSED."""


class PoolUnavailableError(KoraControlWriterError):
    """No asyncpg pool — memory_provider not initialized."""


class KoraControlWriterTimeout(KoraControlWriterError):
    """Substrate roundtrip exceeded ``ISSUE_KORA_CONTROL_TIMEOUT_SECONDS``."""


class SubstrateRejected(KoraControlWriterError):
    """Substrate raised PostgresError. ``sqlstate`` + ``message`` preserved.

    Common cases:
      * ``42501`` insufficient_privilege — actor_kind='kora' or
        L0-reset by non-operator.
      * ``22023`` invalid_parameter_value — issuer_actor_id not in
        actor_registry, workspace mismatch, level/kind mismatch.
      * ``23514`` check_violation — table CHECK rejected the row
        (rare — duplicates the function-entry checks).
    """

    def __init__(self, sqlstate: str, message: str) -> None:
        super().__init__(f"substrate rejected ({sqlstate}): {message}")
        self.sqlstate = sqlstate
        self.substrate_message = message


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IssueKoraControlResult:
    """Projection of the SECDEF's RETURNS TABLE row.

    All five columns surfaced verbatim. ``dry_run`` is a wrapper-side
    discriminator (the substrate doesn't know about dry-run; we
    don't call it in dry-run mode and instead synthesize this row
    with placeholder UUIDs and ``out_sequence=-1``).
    """

    command_id: str
    sequence: int
    lifecycle_state: str
    superseded_command_ids: list[str]
    chain_event_id: str
    dry_run: bool


_DRY_RUN_PLACEHOLDER_UUID = "00000000-0000-0000-0000-000000000000"


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class KoraControlWriter:
    """Writer for ``public.issue_kora_control`` via the IsoKron asyncpg pool.

    Construct one per process (or per memory_provider) — the
    underlying pool is shared. Mirrors :class:`KoraOperationLedger`'s
    construction pattern (``connection`` is an :class:`IsoKronConnection`
    obtained from ``IsoKronMemoryProvider._connection``).

    For the MCP tool layer, see :func:`current_kora_control_writer`
    below for a process-wide accessor that uses the active provider.
    """

    def __init__(self, connection: Any) -> None:
        """``connection`` is an :class:`IsoKronConnection` (already
        started). Caller obtains via ``provider._connection``."""
        self._connection = connection

    async def issue_command(
        self,
        *,
        workspace_id: str,
        issuer_session_id: str,
        issuer_actor_id: Optional[str],
        level: StopLevel,
        kind: StopKind,
        reason: Optional[str] = None,
        target_session: Optional[str] = None,
        expires_at: Optional[Any] = None,
        dry_run: bool = False,
    ) -> IssueKoraControlResult:
        """Invoke ``public.issue_kora_control`` (or simulate it in
        ``dry_run`` mode).

        Args:
            workspace_id: substrate workspace key.
            issuer_session_id: session-id string the substrate
                logs into ``issuer_session_id`` for cross-correlation.
                For MCP-tool callers, the active daemon session_id.
            issuer_actor_id: caller's UUID in ``actor_registry``.
                **Required** — None raises :class:`MissingActorIdError`.
                Must NOT be Kora's actor_id (substrate enforces) and
                must belong to ``workspace_id`` (substrate enforces).
            level: 1-5. MCP tool layer should constrain to 1-2.
            kind: 'pause' / 'drain' / 'abort' / 'kill'. Substrate
                enforces (level, kind) consistency per 0089 CHECK.
            reason: free-form operator reason. NEVER include
                caller-supplied text in audit logs (Q4 ruling).
            target_session: optional session-id to target. Default
                ``None`` = workspace-wide.
            expires_at: optional substrate TIMESTAMPTZ. Default
                ``None`` = no expiry.
            dry_run: when ``True``, returns a synthetic
                :class:`IssueKoraControlResult` without invoking
                substrate. Validation still runs.

        Raises:
            MissingActorIdError: when ``issuer_actor_id`` is ``None``.
            InvalidLevelKindError: ``(level, kind)`` doesn't match
                substrate CHECK.
            PoolUnavailableError: ``memory_provider._connection`` has
                no live asyncpg pool.
            KoraControlWriterTimeout: substrate roundtrip exceeded
                :data:`ISSUE_KORA_CONTROL_TIMEOUT_SECONDS`.
            SubstrateRejected: substrate raised ``asyncpg.PostgresError``
                (insufficient_privilege / invalid_parameter_value /
                etc.).
        """
        # Pre-validate fail-CLOSED.
        if issuer_actor_id is None:
            raise MissingActorIdError(
                "issuer_actor_id is required — caller must populate "
                "actor_id in mcp_callers.yaml. No anonymous "
                "kora_control writes."
            )
        expected_kind = _VALID_LEVEL_KIND.get(int(level))
        if expected_kind is None:
            raise InvalidLevelKindError(
                f"level={level} is not in 1-5 (substrate would reject "
                f"with 22023)."
            )
        if kind != expected_kind:
            raise InvalidLevelKindError(
                f"(level={level}, kind={kind!r}) doesn't match "
                f"substrate CHECK; expected kind={expected_kind!r}."
            )

        if dry_run:
            return IssueKoraControlResult(
                command_id=_DRY_RUN_PLACEHOLDER_UUID,
                sequence=-1,
                lifecycle_state="created",
                superseded_command_ids=[],
                chain_event_id=_DRY_RUN_PLACEHOLDER_UUID,
                dry_run=True,
            )

        async def _call() -> Any:
            pool = self._connection.get_pg_pool()
            if pool is None:
                raise PoolUnavailableError(
                    "IsoKron asyncpg pool is unavailable; "
                    "memory_provider._connection not initialized"
                )
            async with pool.acquire() as conn:
                return await conn.fetchrow(
                    """
                    SELECT
                      out_command_id,
                      out_sequence,
                      out_lifecycle_state,
                      out_superseded_command_ids,
                      out_chain_event_id
                    FROM public.issue_kora_control(
                      $1::text,
                      $2::text,
                      $3::uuid,
                      $4::smallint,
                      $5::text,
                      $6::text,
                      $7::text,
                      $8::timestamptz
                    )
                    """,
                    workspace_id,
                    issuer_session_id,
                    issuer_actor_id,
                    int(level),
                    kind,
                    reason,
                    target_session,
                    expires_at,
                )

        # Lazy asyncpg import for PostgresError catch — only needed when
        # actually invoking the SECDEF. Module loads in environments
        # without asyncpg (test isolation, etc.).
        try:
            import asyncpg
            postgres_error_cls: type = asyncpg.PostgresError
        except ImportError:
            # asyncpg absent: define a never-raised sentinel that's
            # still ``except``-able. Must inherit from BaseException so
            # Python accepts it in the ``except`` clause; will never
            # actually match a raised exception in this branch.
            class _NoPostgresErrorAvailable(Exception):
                pass

            postgres_error_cls = _NoPostgresErrorAvailable

        try:
            future = self._connection._submit_async(_call())
            row = await asyncio.wait_for(
                asyncio.wrap_future(future),
                timeout=ISSUE_KORA_CONTROL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise KoraControlWriterTimeout(
                f"issue_kora_control exceeded "
                f"{ISSUE_KORA_CONTROL_TIMEOUT_SECONDS}s client-side "
                f"deadline (substrate may still apply the write — "
                f"reconcile via kora_control read)"
            ) from exc
        except postgres_error_cls as exc:
            sqlstate = getattr(exc, "sqlstate", "<unknown>")
            raise SubstrateRejected(
                sqlstate=sqlstate or "<unknown>",
                message=str(exc),
            ) from exc
        except PoolUnavailableError:
            raise
        except Exception as exc:
            raise KoraControlWriterError(
                f"issue_kora_control raised non-postgres error: {exc!r}"
            ) from exc

        if row is None:
            # Substrate function returns exactly one row; None is
            # unreachable in normal operation. Surface as a writer
            # error rather than a silent success.
            raise KoraControlWriterError(
                "issue_kora_control returned no row — substrate "
                "function contract violation"
            )

        return IssueKoraControlResult(
            command_id=str(row["out_command_id"]),
            sequence=int(row["out_sequence"]),
            lifecycle_state=str(row["out_lifecycle_state"]),
            superseded_command_ids=[
                str(cid) for cid in (row["out_superseded_command_ids"] or [])
            ],
            chain_event_id=str(row["out_chain_event_id"]),
            dry_run=False,
        )


# ---------------------------------------------------------------------------
# Process-wide accessor
# ---------------------------------------------------------------------------


def current_kora_control_writer() -> Optional[KoraControlWriter]:
    """Return a writer bound to the gateway's active IsoKron provider.

    Returns ``None`` when:
      * No active provider is registered (gateway hasn't booted yet,
        or test isolation cleared the singleton).
      * The active provider has no ``_connection`` attribute
        (uninitialized).

    Constructs a fresh :class:`KoraControlWriter` per call — the
    writer is stateless except for its connection reference, so
    re-constructing is cheap (no pool re-acquisition). Callers who
    want to memoize can hold the returned instance.
    """
    # Lazy import — keeps non-stop paths fast and avoids any boot-order
    # circular-import risk with the memory plugin.
    from plugins.memory.isokron.active_provider import get_active_provider

    provider = get_active_provider()
    if provider is None:
        return None
    connection = getattr(provider, "_connection", None)
    if connection is None:
        return None
    return KoraControlWriter(connection)
