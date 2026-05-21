"""R4.1 §9.8 DR boot gates — gate 3 (substrate_contract_version) +
gate 3b (kora_known_epoch vs substrate_epoch — KR-P2-M ST3).

This module hosts the DR-related gates. ST1 (this file) ships only
gate 3; ST3 adds gate 3b alongside.

# Gate 3 — substrate_contract_version handshake (R4.1 §9.2 / §9.8)

Reads ``public.substrate_contract_version()`` (a STABLE accessor over
``public.kora_dr_epoch_substrate``) and compares against the
:data:`EXPECTED_SUBSTRATE_CONTRACT_VERSION` constant compiled into
this Kora runtime build. On mismatch: **INVARIANT FAIL** →
``kora.boot.failed`` chain event → ``BOOTING → STOPPED`` + non-zero
process exit (KR-P2-H wire-in handles the exit).

# How the version gets bumped

Substrate migrations that change the schema contract Kora handshakes
against (column renames, SECDEF signature changes, etc.) include a
bump of the ``substrate_contract_version`` column. Kora's
:data:`EXPECTED_SUBSTRATE_CONTRACT_VERSION` constant is updated in
the SAME release that adapts to the contract change, so a Kora boot
against an unfamiliar substrate fails loud rather than silently
running against a contract she wasn't compiled for.

If you bump this constant: also confirm the matching substrate
migration is staged (or already merged) BEFORE shipping the Kora
build. Otherwise prod Kora fails gate 3 at boot.

# Auth

``public.substrate_contract_version()`` is a STABLE SQL function with
no auth check — it returns the same value to any caller. Kora-runtime
reads it via the existing asyncpg pool (no SECDEF call needed).
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar, Final

from agent.boot_gates import (
    BootContext,
    Gate,
    GateClass,
    GateResult,
)
from agent.boot_gates_impl import _begin, _fail_result, _pass_result

logger = logging.getLogger(__name__)


# Compiled-in expected version. Bumped in the same Kora release that
# adapts to a substrate contract change. Substrate ships default = 1
# in migration ``top-level/0100_kora_dr_epoch_substrate.sql``; the
# initial value below matches.
#
# When this is updated, the matching substrate migration must be
# staged or merged BEFORE the new Kora build hits prod. Otherwise
# gate 3 fails loud at boot.
EXPECTED_SUBSTRATE_CONTRACT_VERSION: Final[int] = 1


# Read query — STABLE SQL function call. No params. Returns BIGINT.
_SELECT_SUBSTRATE_CONTRACT_VERSION_SQL: Final[str] = (
    "SELECT public.substrate_contract_version() AS version"
)


class SubstrateContractVersionGate(Gate):
    """Gate 3 — substrate_contract_version handshake.

    INVARIANT class — a mismatch means Kora is compiled for a
    contract she can't speak. There is no transient recovery; the
    process exits non-zero and the operator must align Kora's
    build with the deployed substrate (or vice versa).

    Failure modes surfaced as ``GateOutcome.FAIL`` (not raised):

      - memory_provider missing on BootContext
      - provider has no ``_connection``
      - substrate read raises (asyncpg / substrate-side error)
      - substrate value differs from
        :data:`EXPECTED_SUBSTRATE_CONTRACT_VERSION`
    """

    gate_id: ClassVar[str] = "3_substrate_contract_version"
    gate_class: ClassVar[GateClass] = GateClass.INVARIANT
    title: ClassVar[str] = "substrate_contract_version handshake"

    async def run(self, context: BootContext) -> GateResult:
        started_at, t0 = _begin()
        provider = context.memory_provider
        if provider is None:
            return _fail_result(
                self, started_at, t0, "memory_provider is not set on BootContext"
            )

        connection = getattr(provider, "_connection", None)
        if connection is None:
            return _fail_result(
                self, started_at, t0,
                "provider._connection is not initialized",
            )

        try:
            actual_version = connection.submit_and_wait(
                _read_substrate_contract_version(provider),
                timeout=5.0,
            )
        except Exception as exc:
            return _fail_result(
                self, started_at, t0,
                f"substrate_contract_version() read raised: {exc!r}",
            )

        if not isinstance(actual_version, int):
            return _fail_result(
                self, started_at, t0,
                f"substrate_contract_version() returned unexpected "
                f"shape: {type(actual_version).__name__} "
                f"(value={actual_version!r})",
            )

        if actual_version != EXPECTED_SUBSTRATE_CONTRACT_VERSION:
            return _fail_result(
                self, started_at, t0,
                f"substrate_contract_version mismatch: "
                f"expected={EXPECTED_SUBSTRATE_CONTRACT_VERSION} "
                f"actual={actual_version}. Kora build and substrate "
                f"migrations are out of sync — operator must align "
                f"versions before boot. See R4.1 §9.2 / §9.8.",
            )

        return _pass_result(
            self, started_at, t0,
            f"substrate_contract_version={actual_version} "
            f"(matches Kora build's EXPECTED={EXPECTED_SUBSTRATE_CONTRACT_VERSION})",
        )


async def _read_substrate_contract_version(provider: Any) -> int:
    """Async asyncpg fetch — returns the live substrate_contract_version.

    Runs on the provider's IO loop via ``submit_and_wait``; the public
    accessor :func:`public.substrate_contract_version()` is a STABLE
    function with no auth check, so a plain ``fetchrow`` against the
    existing pool works.
    """
    pool = provider._connection.get_pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_SELECT_SUBSTRATE_CONTRACT_VERSION_SQL)
    if row is None:
        # Defensive — the singleton row in kora_dr_epoch_substrate is
        # seeded by the migration; this branch is unreachable unless
        # the row was manually deleted post-deploy.
        raise RuntimeError(
            "substrate_contract_version() returned no row — "
            "kora_dr_epoch_substrate singleton may be missing"
        )
    return int(row["version"])
