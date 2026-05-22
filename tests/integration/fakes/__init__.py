"""Shared fakes for KR-P2-INT-TESTS integration suite.

These fakes simulate the IsoKron substrate surface (MCP client +
kora_operation_ledger + connection submit_and_wait) without requiring
a real Postgres instance or MCP server. They preserve the SHAPES
the runtime expects so multi-component flows (poller + holder +
ledger + cost-state + dr-handler) can be exercised end-to-end.

# Scope vs real substrate

These fakes test the RUNTIME-side state machine. They do NOT
exercise:
  - Substrate SECDEF behavior (canonical-actor invariant, claim
    fence-token enforcement, kora_operation_id uniqueness against
    event_log)
  - Chain-event durability guarantees
  - asyncpg pool behavior under contention

Scenarios that genuinely require real substrate (KR-P2-INT-TESTS
ST3 KR-7, parts of ST1 sub-4/5, ST6 threshold transition) ship as
contract-tests that pin the runtime-side response to substrate
signals (mock the substrate's response, verify the runtime's
handling). Each affected PR body documents what real-substrate
validation would add.
"""

from tests.integration.fakes.fake_isokron import (
    FakeIsoKronConnection,
    FakeIsoKronProvider,
    FakeKoraOperationLedger,
    FakeMCPClient,
    FakeUniqueViolation,
    InMemorySubstrate,
    make_fake_isokron_trio,
)

__all__ = [
    "FakeIsoKronConnection",
    "FakeIsoKronProvider",
    "FakeKoraOperationLedger",
    "FakeMCPClient",
    "FakeUniqueViolation",
    "InMemorySubstrate",
    "make_fake_isokron_trio",
]
