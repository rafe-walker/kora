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
from typing import Any, ClassVar, Final, Optional

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


# ---------------------------------------------------------------------------
# Gate 3b — kora_known_epoch vs substrate_epoch (DR check)
# ---------------------------------------------------------------------------


class Gate3bEpochCheck(Gate):
    """Gate 3b — R4.1 §9.8 DR epoch check.

    Reads both ``public.substrate_epoch()`` and
    ``public.kora_known_epoch()``. Branches:

      - ``kora_known_epoch IS NULL`` (Kora's first boot): PASS — no
        history to compare against. ST4 writes the initial value at
        end-of-boot.
      - ``substrate_epoch == kora_known_epoch``: PASS — Kora is on
        the same timeline as her last boot.
      - ``substrate_epoch != kora_known_epoch``: epoch mismatch.
        Invokes :mod:`agent.dr_handler` to emit ``kora.dr.observed``
        + transition the holder to ``PAUSED{substrate}``, then returns
        a FAIL ``GateResult`` with class ``INVARIANT_PAUSE``. The
        coordinator detects this class and routes to
        ``BootResult.PAUSED`` (NOT ``STOPPED``); the wire-in skips
        ``sys.exit`` so the process stays running for operator
        clearance.

    INVARIANT_PAUSE class — fail-fast, no retry, but the coordinator
    routes to PAUSED instead of STOPPED. The substrate_epoch could
    have advanced because of a legitimate post-PITR DR-runbook bump,
    so a "STOPPED+alert" posture would be too aggressive — operator
    clearance via cockpit ``kora_control`` reset is the right path.
    """

    gate_id: ClassVar[str] = "3b_epoch_dr_check"
    gate_class: ClassVar[GateClass] = GateClass.INVARIANT_PAUSE
    title: ClassVar[str] = "kora_known_epoch vs substrate_epoch (R4.1 §9.8)"

    async def run(self, context: BootContext) -> GateResult:
        started_at, t0 = _begin()
        provider = context.memory_provider
        holder = context.holder
        if provider is None:
            return _fail_result(
                self, started_at, t0,
                "memory_provider is not set on BootContext",
            )
        if holder is None:
            return _fail_result(
                self, started_at, t0,
                "holder is not set on BootContext (required for "
                "PAUSED transition on mismatch)",
            )

        connection = getattr(provider, "_connection", None)
        if connection is None:
            return _fail_result(
                self, started_at, t0,
                "provider._connection is not initialized",
            )

        try:
            substrate_epoch = connection.submit_and_wait(
                _read_substrate_epoch_async(provider),
                timeout=5.0,
            )
            known_epoch = connection.submit_and_wait(
                _read_kora_known_epoch_async(provider),
                timeout=5.0,
            )
        except Exception as exc:
            return _fail_result(
                self, started_at, t0,
                f"epoch read raised: {exc!r}",
            )

        # First-boot case: nothing to compare against. PASS.
        if known_epoch is None:
            return _pass_result(
                self, started_at, t0,
                f"first boot — no kora_known_epoch yet "
                f"(substrate_epoch={substrate_epoch}); ST4 will write "
                f"the initial value at end-of-boot",
            )

        # Match: PASS.
        if substrate_epoch == known_epoch:
            return _pass_result(
                self, started_at, t0,
                f"epochs match (both={substrate_epoch}); same timeline "
                f"as last boot",
            )

        # Mismatch — invoke handler to emit + transition holder.
        # The handler is best-effort on the emit; the transition fires
        # regardless. If the handler raises unexpectedly, we still
        # return FAIL so the coordinator routes to PAUSED.
        try:
            from agent.dr_handler import handle_epoch_mismatch

            await handle_epoch_mismatch(
                memory_provider=provider,
                holder=holder,
                observed_substrate_epoch=substrate_epoch,
                last_known_epoch=known_epoch,
            )
        except Exception as exc:
            logger.error(
                "[kora.dr_handler] handle_epoch_mismatch raised: %r — "
                "gate 3b still returns FAIL; coordinator routes to "
                "PAUSED. Holder state may be partially advanced.",
                exc,
            )

        return _fail_result(
            self, started_at, t0,
            f"epoch mismatch — observed substrate_epoch="
            f"{substrate_epoch} vs kora_known_epoch={known_epoch}. "
            f"R4.1 §9.8 DR signal — holder transitioned to "
            f"PAUSED{{substrate}}; operator must clear via cockpit "
            f"kora_control reset.",
        )


async def _read_substrate_epoch_async(provider: Any) -> int:
    """Wrapper that pulls the pool from provider + calls the ST2 helper.

    Defined here (not in dr_epoch.py) so the gate file is the single
    place that talks to ``provider._connection``; ST2's
    :mod:`plugins.memory.isokron.dr_epoch` takes only a pool and
    stays substrate-side / provider-agnostic.
    """
    from plugins.memory.isokron.dr_epoch import read_substrate_epoch

    pool = provider._connection.get_pg_pool()
    return await read_substrate_epoch(pool)


async def _read_kora_known_epoch_async(provider: Any) -> Optional[int]:
    """Wrapper that pulls the pool + calls the ST2 helper."""
    from plugins.memory.isokron.dr_epoch import read_kora_known_epoch

    pool = provider._connection.get_pg_pool()
    return await read_kora_known_epoch(pool)
