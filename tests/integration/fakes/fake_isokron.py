"""In-memory IsoKron substrate fakes (KR-P2-INT-TESTS shared fixtures).

Provides ``InMemorySubstrate`` (the shared store), ``FakeMCPClient``
(records MCP tool invocations, lets tests script responses),
``FakeKoraOperationLedger`` (same API as the real ledger writer but
backed by the in-memory store), ``FakeIsoKronConnection`` (provides
``submit_and_wait`` + ``get_pg_pool`` + ``get_mcp_client``), and
``FakeIsoKronProvider`` (the top-level surface the runtime consumes
via ``IsoKronMemoryProvider``).

The store enforces the load-bearing invariants the runtime's
idempotency contract relies on:

  - ``kora_operation_id`` uniqueness (substrate-side
    ``event_log`` UNIQUE constraint mirrored here as a set check on
    allocate)
  - Status transitions (allocated → dispatched → committed; any →
    abandoned) match the real ledger's contract
  - Reread by composite key returns the live row state

Cross-session recovery is modeled by constructing TWO separate
``FakeIsoKronProvider`` instances pointing at the SAME
``InMemorySubstrate``. The store is process-shared but the
provider wrappers are independent.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


@dataclass
class _LedgerRow:
    work_attempt_id: str
    sequence_within_attempt: int
    kora_operation_id: str
    workspace_id: str
    ticket_id: str
    status: str
    tool_name: str
    dispatch_result: Optional[dict[str, Any]]
    dispatch_error: Optional[str]
    created_at: datetime
    updated_at: datetime


@dataclass
class InMemorySubstrate:
    """Process-shared in-memory state for the fakes.

    Pointed at by ``FakeIsoKronProvider``; multiple providers can
    share one store to model cross-session recovery.

    Public mutable state for test introspection / setup:
      - ``ledger_rows`` — list of allocated rows (most-recent first
        via insertion-order iteration; tests can introspect)
      - ``chain_events`` — list of (workspace_id, event_type,
        payload) tuples for emit assertions
      - ``observed_kora_operation_ids`` — set used to enforce
        substrate-side UNIQUE constraint on the next allocate
    """

    ledger_rows: list[_LedgerRow] = field(default_factory=list)
    chain_events: list[tuple[str, str, dict[str, Any]]] = field(
        default_factory=list
    )
    observed_kora_operation_ids: set[str] = field(default_factory=set)

    # Sea_Ticket rows for the consumer-loop scenarios. Populated by
    # tests via ``seed_sea_ticket`` (or read directly).
    sea_tickets: dict[str, dict[str, Any]] = field(default_factory=dict)

    # kora_control rows for ST4 (STOP-KORA levels).
    kora_control_active_command: Optional[dict[str, Any]] = None

    # DR state for ST2 (PITR simulation).
    substrate_epoch: int = 1
    kora_known_epoch: Optional[int] = None

    def find_row_by_kora_operation_id(
        self, kora_operation_id: str
    ) -> Optional[_LedgerRow]:
        for row in self.ledger_rows:
            if row.kora_operation_id == kora_operation_id:
                return row
        return None

    def find_row_by_composite_key(
        self, work_attempt_id: str, sequence_within_attempt: int
    ) -> Optional[_LedgerRow]:
        for row in self.ledger_rows:
            if (
                row.work_attempt_id == work_attempt_id
                and row.sequence_within_attempt == sequence_within_attempt
            ):
                return row
        return None

    def next_sequence_for_attempt(self, work_attempt_id: str) -> int:
        seqs = [
            row.sequence_within_attempt
            for row in self.ledger_rows
            if row.work_attempt_id == work_attempt_id
        ]
        return max(seqs) + 1 if seqs else 0


# ---------------------------------------------------------------------------
# Errors (mirrors of substrate-side errors the runtime catches)
# ---------------------------------------------------------------------------


class FakeUniqueViolation(Exception):
    """Mirrors asyncpg's UniqueViolationError + the substrate's
    ``event_log.kora_operation_id`` UNIQUE constraint.

    Raised by the fake when a write tries to claim a
    ``kora_operation_id`` already in
    :attr:`InMemorySubstrate.observed_kora_operation_ids`.
    """


# ---------------------------------------------------------------------------
# Ledger fake
# ---------------------------------------------------------------------------


class FakeKoraOperationLedger:
    """Same API as the real :class:`KoraOperationLedger`, backed by
    the in-memory store.

    Used by tests that want to exercise the runtime's ledger-state
    machine (idempotency reread + retry) without spinning up a real
    Postgres instance. Status transitions match the real ledger's
    contract:

      - ``allocate_operation`` inserts with status='allocated'
      - ``mark_dispatched`` requires status='allocated', moves to
        'dispatched'
      - ``mark_committed`` requires status='dispatched', moves to
        'committed'
      - ``mark_abandoned`` moves any state to 'abandoned'
      - ``reread_for_retry`` returns the live row or None
    """

    def __init__(self, store: InMemorySubstrate):
        self._store = store

    async def allocate_operation(
        self,
        *,
        work_attempt_id: str,
        workspace_id: str,
        ticket_id: str,
        tool_name: str,
    ) -> Any:
        """Allocate a new row. Returns a row object (mirrors
        :class:`KoraOperationRow` shape via attribute access)."""
        kora_operation_id = str(uuid.uuid4())
        if kora_operation_id in self._store.observed_kora_operation_ids:
            # Extremely unlikely with uuid4 — but mirror the substrate
            # contract.
            raise FakeUniqueViolation(
                f"kora_operation_id={kora_operation_id} already exists"
            )
        self._store.observed_kora_operation_ids.add(kora_operation_id)

        seq = self._store.next_sequence_for_attempt(work_attempt_id)
        now = datetime.now(timezone.utc)
        row = _LedgerRow(
            work_attempt_id=work_attempt_id,
            sequence_within_attempt=seq,
            kora_operation_id=kora_operation_id,
            workspace_id=workspace_id,
            ticket_id=ticket_id,
            status="allocated",
            tool_name=tool_name,
            dispatch_result=None,
            dispatch_error=None,
            created_at=now,
            updated_at=now,
        )
        self._store.ledger_rows.append(row)
        return row

    async def mark_dispatched(
        self,
        kora_operation_id: str,
        dispatch_result: Optional[dict[str, Any]] = None,
    ) -> Any:
        row = self._store.find_row_by_kora_operation_id(kora_operation_id)
        if row is None:
            raise RuntimeError(
                f"no ledger row at kora_operation_id={kora_operation_id}"
            )
        if row.status != "allocated":
            raise RuntimeError(
                f"mark_dispatched: row at {kora_operation_id} is "
                f"status={row.status!r}, expected 'allocated'"
            )
        row.status = "dispatched"
        row.dispatch_result = dispatch_result
        row.updated_at = datetime.now(timezone.utc)
        return row

    async def mark_committed(self, kora_operation_id: str) -> Any:
        row = self._store.find_row_by_kora_operation_id(kora_operation_id)
        if row is None:
            raise RuntimeError(
                f"no ledger row at kora_operation_id={kora_operation_id}"
            )
        if row.status != "dispatched":
            raise RuntimeError(
                f"mark_committed: row at {kora_operation_id} is "
                f"status={row.status!r}, expected 'dispatched'"
            )
        row.status = "committed"
        row.updated_at = datetime.now(timezone.utc)
        return row

    async def mark_abandoned(
        self, kora_operation_id: str, reason: str
    ) -> Any:
        row = self._store.find_row_by_kora_operation_id(kora_operation_id)
        if row is None:
            raise RuntimeError(
                f"no ledger row at kora_operation_id={kora_operation_id}"
            )
        row.status = "abandoned"
        row.dispatch_error = reason
        row.updated_at = datetime.now(timezone.utc)
        return row

    async def reread_for_retry(
        self,
        *,
        work_attempt_id: str,
        sequence_within_attempt: int,
    ) -> Optional[Any]:
        return self._store.find_row_by_composite_key(
            work_attempt_id, sequence_within_attempt
        )


# ---------------------------------------------------------------------------
# MCP client fake
# ---------------------------------------------------------------------------


class FakeMCPClient:
    """In-process MCP client stub.

    Each ``invoke(tool_name, args)`` call is recorded in
    :attr:`invocations`. Tests script per-tool responses via
    :meth:`set_response` (returns dict) or :meth:`set_response_sequence`
    (returns successive items from a list — useful for retry tests).

    A response can also be an Exception instance — the fake raises
    it from ``invoke``. Used to simulate substrate errors (network,
    SECDEF rejection, unique violation, etc.).
    """

    def __init__(self) -> None:
        self.invocations: list[tuple[str, dict[str, Any]]] = []
        self._responses: dict[str, Any] = {}
        self._sequences: dict[str, list[Any]] = {}

    def set_response(self, tool_name: str, response: Any) -> None:
        self._responses[tool_name] = response

    def set_response_sequence(
        self, tool_name: str, responses: list[Any]
    ) -> None:
        self._sequences[tool_name] = list(responses)

    async def invoke(
        self, tool_name: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        self.invocations.append((tool_name, dict(args)))

        if tool_name in self._sequences and self._sequences[tool_name]:
            response = self._sequences[tool_name].pop(0)
        elif tool_name in self._responses:
            response = self._responses[tool_name]
        else:
            response = {}

        if isinstance(response, Exception):
            raise response
        return response

    def invocations_of(self, tool_name: str) -> list[dict[str, Any]]:
        """Return only invocations of ``tool_name``."""
        return [args for name, args in self.invocations if name == tool_name]


# ---------------------------------------------------------------------------
# Connection fake
# ---------------------------------------------------------------------------


class FakeIsoKronConnection:
    """Stub :class:`IsoKronConnection` for fake-provider tests.

    Provides ``submit_and_wait`` (runs coroutines synchronously on
    the current loop via :class:`concurrent.futures.Future`) +
    ``get_mcp_client`` + ``_submit_async``. The agent-side runtime
    consumes these methods via the provider's ``_connection``
    attribute.
    """

    def __init__(
        self,
        *,
        mcp_client: FakeMCPClient,
        store: InMemorySubstrate,
    ) -> None:
        self._mcp_client = mcp_client
        self._store = store

    def get_mcp_client(self) -> FakeMCPClient:
        return self._mcp_client

    def get_pg_pool(self) -> "_FakePgPool":
        return _FakePgPool(self._store)

    def submit_and_wait(
        self, coro: Awaitable[Any], *, timeout: float = 5.0
    ) -> Any:
        """Synchronously drive ``coro`` to completion. Used by the
        runtime's cross-loop bridge."""
        return asyncio.get_event_loop().run_until_complete(coro)

    def _submit_async(self, coro: Awaitable[Any]) -> concurrent.futures.Future:
        """Run ``coro`` immediately + return a completed Future
        carrying the result. Matches the real connection's
        cross-thread submit signature."""
        try:
            result = asyncio.get_event_loop().run_until_complete(coro)
            future: concurrent.futures.Future = concurrent.futures.Future()
            future.set_result(result)
            return future
        except Exception as exc:
            future = concurrent.futures.Future()
            future.set_exception(exc)
            return future


class _FakePgPool:
    """Stub asyncpg pool — only used by the real ledger writer's
    transient code paths. Tests that exercise the ledger directly
    go through FakeKoraOperationLedger instead.
    """

    def __init__(self, store: InMemorySubstrate):
        self._store = store

    def acquire(self):
        class _Ctx:
            async def __aenter__(self_inner) -> Any:
                return _FakePgConn()

            async def __aexit__(self_inner, *exc: Any) -> bool:
                return False

        return _Ctx()


class _FakePgConn:
    async def execute(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def fetchrow(self, *args: Any, **kwargs: Any) -> Optional[Any]:
        return None

    def transaction(self):
        class _Tx:
            async def __aenter__(self_inner) -> Any:
                return self_inner

            async def __aexit__(self_inner, *exc: Any) -> bool:
                return False

        return _Tx()


# ---------------------------------------------------------------------------
# Provider fake
# ---------------------------------------------------------------------------


class FakeIsoKronProvider:
    """Stub :class:`IsoKronMemoryProvider`.

    Holds a reference to the shared store + a connection. Multiple
    provider instances against the same store model cross-session
    behavior.
    """

    def __init__(
        self,
        *,
        store: InMemorySubstrate,
        workspace_id: str = "org_test",
    ) -> None:
        self._store = store
        self._workspace_id = workspace_id
        self._mcp_client = FakeMCPClient()
        self._connection = FakeIsoKronConnection(
            mcp_client=self._mcp_client, store=store
        )
        self._constitution_cache: dict[str, tuple[Optional[str], Optional[str]]] = {}

    def _resolve_workspace_id(
        self, workspace_id: Optional[str] = None
    ) -> Optional[str]:
        return workspace_id or self._workspace_id

    @property
    def mcp_client(self) -> FakeMCPClient:
        """Convenience accessor for test setup."""
        return self._mcp_client

    @property
    def store(self) -> InMemorySubstrate:
        return self._store


# ---------------------------------------------------------------------------
# Helper — build a fully-wired (store, provider, ledger) trio
# ---------------------------------------------------------------------------


def make_fake_isokron_trio(
    *, workspace_id: str = "org_test"
) -> tuple[InMemorySubstrate, FakeIsoKronProvider, FakeKoraOperationLedger]:
    """Convenience factory for tests that want all three at once.

    Returns ``(store, provider, ledger)`` — all three wired together.
    """
    store = InMemorySubstrate()
    provider = FakeIsoKronProvider(store=store, workspace_id=workspace_id)
    ledger = FakeKoraOperationLedger(store)
    return store, provider, ledger
