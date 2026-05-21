"""R4.1 §9.2 boot gate concrete implementations (KR-P2-H ST2).

Seven gates: 1, 4, 5, 6, 7, 8, 10 (per the bucket spec scope table —
gates 2 / 3 / 3b / 9 are handled by other buckets).

Each gate subclasses :class:`agent.boot_gates.Gate`. Failure modes are
returned as ``GateOutcome.FAIL`` results (not raised) — the runner
classifies transient vs. invariant and decides retry vs. short-circuit.

# Substrate-loop bridging

The provider's asyncpg pool + MCP client live on a dedicated background
asyncio loop in :class:`IsoKronConnection`. Gates run on the runner's
event loop. Cross-loop calls use ``provider._connection.submit_and_wait``
(same pattern as :mod:`agent.stop_kora_pre_flight` and
:mod:`agent.constitution_audit`).
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, ClassVar, Optional

from agent.boot_gates import (
    BootContext,
    Gate,
    GateClass,
    GateOutcome,
    GateResult,
)

logger = logging.getLogger(__name__)


# Anthropic API key env var. Mirrors the entrypoint validation from
# KR-P2-F-pre ST2 (the shell-level fail-closed); this gate is the
# Python-level in-process equivalent.
_ANTHROPIC_API_KEY_ENV: str = "ANTHROPIC_API_KEY"

# Kora service token env var. Used by the dispatch tier for Layer-A
# wsk_* auth.
_KORA_SERVICE_TOKEN_ENV: str = "KORA_SERVICE_TOKEN"


# ---------------------------------------------------------------------------
# Helpers — build PASS / FAIL GateResult
# ---------------------------------------------------------------------------


def _pass_result(
    gate: Gate, started_at: datetime, t0: float, detail: str
) -> GateResult:
    completed_at = datetime.now(timezone.utc)
    return GateResult(
        gate_id=gate.gate_id,
        gate_class=gate.gate_class,
        outcome=GateOutcome.PASS,
        detail=detail,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
        started_at=started_at,
        completed_at=completed_at,
    )


def _fail_result(
    gate: Gate, started_at: datetime, t0: float, detail: str
) -> GateResult:
    completed_at = datetime.now(timezone.utc)
    return GateResult(
        gate_id=gate.gate_id,
        gate_class=gate.gate_class,
        outcome=GateOutcome.FAIL,
        detail=detail,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
        started_at=started_at,
        completed_at=completed_at,
    )


def _begin() -> tuple[datetime, float]:
    """Convenience: snapshot started_at + monotonic clock for elapsed_ms."""
    return datetime.now(timezone.utc), time.monotonic()


# ---------------------------------------------------------------------------
# Gate 1 — Claude auth valid
# ---------------------------------------------------------------------------


class ClaudeAuthGate(Gate):
    """Gate 1 — verify the Anthropic API key env var is set + plausible.

    TRANSIENT: an env-var-not-set state can occur during a config
    rotation; the retry budget absorbs brief gaps. A persistent miss
    exhausts the budget → STOPPED.

    Intentional non-scope: this gate does NOT make a network probe call
    to verify the key works. The cost (latency + credit on every boot)
    isn't worth it for a boot gate; runtime first-tool-call surfaces
    invalid-key errors loudly enough. Future extension may add an
    optional ``--strict-auth`` mode that makes a no-op probe call.
    """

    gate_id: ClassVar[str] = "1_claude_auth"
    gate_class: ClassVar[GateClass] = GateClass.TRANSIENT
    title: ClassVar[str] = "Claude auth valid (ANTHROPIC_API_KEY set)"

    async def run(self, context: BootContext) -> GateResult:
        started_at, t0 = _begin()
        api_key = os.environ.get(_ANTHROPIC_API_KEY_ENV, "")
        if not api_key:
            return _fail_result(
                self,
                started_at,
                t0,
                f"{_ANTHROPIC_API_KEY_ENV} env var is unset or empty",
            )
        # Minimum sanity: real Anthropic keys are well over 30 chars.
        # 20 is a loose lower bound to catch trivially-truncated values
        # (e.g. ``sk-ant-xxx`` truncated by a misconfigured Doppler
        # secret reference).
        if len(api_key) < 20:
            return _fail_result(
                self,
                started_at,
                t0,
                f"{_ANTHROPIC_API_KEY_ENV} appears truncated "
                f"(length={len(api_key)}; expected >= 20)",
            )
        return _pass_result(
            self,
            started_at,
            t0,
            f"{_ANTHROPIC_API_KEY_ENV} set (length={len(api_key)})",
        )


# ---------------------------------------------------------------------------
# Gate 4 — kora_runtime role perms (read OK + RLS deny on raw write)
# ---------------------------------------------------------------------------


class KronicleRolePermsGate(Gate):
    """Gate 4 — verify the kora_runtime DB role has expected perms.

    Two probes:
      1. **Known-pass read**: ``SELECT 1 FROM public.actor_registry``.
         Confirms the role can SELECT from a workspace-agnostic table.
      2. **Known-deny write**: raw ``UPDATE public.tickets`` against a
         sentinel UUID. RLS denies non-SECDEF writes for the role;
         a *successful* update would mean RLS is misconfigured.

    Gate passes only when BOTH probes match expectations.
    """

    gate_id: ClassVar[str] = "4_kora_runtime_role_perms"
    gate_class: ClassVar[GateClass] = GateClass.TRANSIENT
    title: ClassVar[str] = "kora_runtime role perms (SELECT OK + RLS write-deny)"

    async def run(self, context: BootContext) -> GateResult:
        started_at, t0 = _begin()
        provider = context.memory_provider
        if provider is None:
            return _fail_result(
                self, started_at, t0, "memory_provider is not set on BootContext"
            )

        # Probe 1: known-pass read.
        try:
            read_ok = provider._connection.submit_and_wait(
                _probe_actor_registry_select(provider),
                timeout=5.0,
            )
        except Exception as exc:
            return _fail_result(
                self, started_at, t0,
                f"actor_registry read probe raised: {exc!r}",
            )
        if not read_ok:
            return _fail_result(
                self, started_at, t0,
                "actor_registry SELECT returned empty (table may be unseeded)",
            )

        # Probe 2: known-deny raw write to tickets.
        try:
            deny_observed = provider._connection.submit_and_wait(
                _probe_tickets_write_denied(provider),
                timeout=5.0,
            )
        except Exception as exc:
            return _fail_result(
                self, started_at, t0,
                f"tickets write-deny probe raised unexpectedly: {exc!r}",
            )
        if not deny_observed:
            return _fail_result(
                self, started_at, t0,
                "RAW UPDATE on public.tickets succeeded — RLS deny is missing! "
                "kora_runtime role has unexpected write perms; substrate "
                "config drift.",
            )

        return _pass_result(
            self, started_at, t0,
            "actor_registry SELECT OK; tickets RAW UPDATE correctly RLS-denied",
        )


async def _probe_actor_registry_select(provider: Any) -> bool:
    """Returns True if at least one row exists in actor_registry."""
    pool = provider._connection.get_pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM public.actor_registry LIMIT 1"
        )
    return row is not None


async def _probe_tickets_write_denied(provider: Any) -> bool:
    """Returns True if a raw UPDATE on public.tickets is denied
    (RLS / insufficient_privilege). Returns False if the UPDATE
    succeeded (which would be a substrate misconfiguration)."""
    sentinel_id = "00000000-0000-0000-0000-000000000000"
    pool = provider._connection.get_pg_pool()
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                "UPDATE public.tickets "
                "SET updated_at = NOW() "
                "WHERE id = $1::uuid",
                sentinel_id,
            )
        except Exception as exc:
            # Any exception classifies as "write denied." Specific
            # asyncpg PostgresError subclasses (InsufficientPrivilegeError,
            # InsufficientPrivilege) all surface as exceptions; RLS denies
            # via "permission denied" / "row-level security violation."
            msg = str(exc).lower()
            if (
                "permission denied" in msg
                or "row-level security" in msg
                or "insufficient" in msg
                or "rls" in msg
            ):
                return True
            # Any OTHER exception (e.g. table missing) is unexpected;
            # the caller wraps as a FAIL with detail.
            raise
    # UPDATE succeeded silently — RLS is missing. The sentinel UUID
    # doesn't exist so no rows were touched, but the SQL ran without
    # raising — that's the failure mode.
    return False


# ---------------------------------------------------------------------------
# Gate 5 — kronicle-mcp reachable (MCP client opens successfully)
# ---------------------------------------------------------------------------


class KronicleMCPReachableGate(Gate):
    """Gate 5 — verify the MCP transport opens against the dispatch tier.

    "Reachability" is operationally defined as "can the runtime open the
    IsoKron MCP client without a transport error?" The bucket spec
    suggested an HTTP GET to ``kronicle-mcp.internal:8443/health`` but
    the actual endpoint URL is not in substrate source today; the MCP
    client's lazy ``start()`` is the equivalent reachability probe and
    matches the existing K-7/8/9/10 transport.

    Failures from this gate: connection refused, DNS resolution
    failure, transport handshake timeout. Auth failures (invalid
    wsk_* token, 401) surface from Gate 6 (which is ordered next).
    """

    gate_id: ClassVar[str] = "5_kronicle_mcp_reachable"
    gate_class: ClassVar[GateClass] = GateClass.TRANSIENT
    title: ClassVar[str] = "kronicle-mcp transport reachable"

    async def run(self, context: BootContext) -> GateResult:
        started_at, t0 = _begin()
        provider = context.memory_provider
        if provider is None:
            return _fail_result(
                self, started_at, t0, "memory_provider is not set on BootContext"
            )

        try:
            # get_mcp_client() lazily opens the transport on first call.
            mcp_client = provider._connection.get_mcp_client()
        except Exception as exc:
            return _fail_result(
                self, started_at, t0,
                f"IsoKron MCP client open raised: {exc!r}",
            )
        if mcp_client is None:
            return _fail_result(
                self, started_at, t0,
                "IsoKron MCP client returned None (transport not started)",
            )
        return _pass_result(
            self, started_at, t0,
            "IsoKron MCP client opened successfully",
        )


# ---------------------------------------------------------------------------
# Gate 6 — wsk_* token valid (authenticated MCP call succeeds)
# ---------------------------------------------------------------------------


class WskTokenValidGate(Gate):
    """Gate 6 — verify KORA_SERVICE_TOKEN is set + authenticates against
    the dispatch tier.

    Probe: invoke the K-7 read-only tool ``kora__read_kora_capability_row``.
    If the dispatch tier accepts the wsk_* token + responds with
    capability data, the token is valid. If the call returns 401 / auth
    error, the token is invalid or expired.
    """

    gate_id: ClassVar[str] = "6_wsk_token_valid"
    gate_class: ClassVar[GateClass] = GateClass.TRANSIENT
    title: ClassVar[str] = "wsk_* service token valid"

    async def run(self, context: BootContext) -> GateResult:
        started_at, t0 = _begin()
        token = os.environ.get(_KORA_SERVICE_TOKEN_ENV, "")
        if not token:
            return _fail_result(
                self, started_at, t0,
                f"{_KORA_SERVICE_TOKEN_ENV} env var is unset or empty",
            )

        provider = context.memory_provider
        if provider is None:
            return _fail_result(
                self, started_at, t0, "memory_provider is not set on BootContext"
            )

        try:
            mcp_client = provider._connection.get_mcp_client()
            result = provider._connection.submit_and_wait(
                mcp_client.invoke("kora__read_kora_capability_row", {}),
                timeout=10.0,
            )
        except Exception as exc:
            return _fail_result(
                self, started_at, t0,
                f"kora__read_kora_capability_row probe raised: {exc!r}",
            )

        if not isinstance(result, dict):
            return _fail_result(
                self, started_at, t0,
                f"kora__read_kora_capability_row returned unexpected shape: "
                f"{type(result).__name__}",
            )

        return _pass_result(
            self, started_at, t0,
            f"wsk_* token authenticated; capability row returned "
            f"({len(result)} top-level keys)",
        )


# ---------------------------------------------------------------------------
# Gate 7 — canonical kora actor row exists (INVARIANT)
# ---------------------------------------------------------------------------


class CanonicalKoraActorGate(Gate):
    """Gate 7 — verify ``actor_registry`` has the canonical kora actor
    row for the active workspace.

    INVARIANT: Kora cannot operate without her own identity row in
    actor_registry. A missing row means the workspace was provisioned
    without the kora actor (Plan 01 / migration 0076 seed step
    skipped). This is a substrate-config error — the runtime should
    halt and surface the failure for operator triage.

    Side-effect: on PASS, the resolved actor UUID is stored in
    ``BootContext.kora_actor_uuid`` for Gate 10 (and downstream
    code) to consume without re-querying.
    """

    gate_id: ClassVar[str] = "7_canonical_kora_actor"
    gate_class: ClassVar[GateClass] = GateClass.INVARIANT
    title: ClassVar[str] = "canonical kora actor row exists"

    async def run(self, context: BootContext) -> GateResult:
        started_at, t0 = _begin()
        provider = context.memory_provider
        if provider is None:
            return _fail_result(
                self, started_at, t0, "memory_provider is not set on BootContext"
            )

        try:
            workspace_id = provider._resolve_workspace_id()
        except Exception as exc:
            return _fail_result(
                self, started_at, t0,
                f"workspace_id resolution raised: {exc!r}",
            )
        if not workspace_id:
            return _fail_result(
                self, started_at, t0,
                "workspace_id is None/empty after resolution; "
                "IsoKronProviderConfig.default_workspace_id may be unset",
            )
        context.workspace_id = workspace_id

        try:
            actor_uuid = provider._connection.submit_and_wait(
                _query_canonical_kora_actor(provider, workspace_id),
                timeout=5.0,
            )
        except Exception as exc:
            return _fail_result(
                self, started_at, t0,
                f"actor_registry query raised: {exc!r}",
            )
        if not actor_uuid:
            return _fail_result(
                self, started_at, t0,
                f"no actor_kind='kora' row in actor_registry for "
                f"workspace_id='{workspace_id}'. Substrate is missing the "
                f"Plan 01 / migration 0076 seed step for this workspace.",
            )

        # Cache on context for downstream gates (Gate 10).
        context.kora_actor_uuid = actor_uuid
        return _pass_result(
            self, started_at, t0,
            f"canonical kora actor resolved: {actor_uuid} "
            f"(workspace={workspace_id})",
        )


async def _query_canonical_kora_actor(
    provider: Any, workspace_id: str
) -> Optional[str]:
    """Look up Kora's UUID in actor_registry for the workspace."""
    pool = provider._connection.get_pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT actor_id::text AS actor_id
              FROM public.actor_registry
             WHERE workspace_id = $1
               AND actor_kind = 'kora'
               AND deactivated_at IS NULL
             LIMIT 1
            """,
            workspace_id,
        )
    return row["actor_id"] if row else None


# ---------------------------------------------------------------------------
# Gate 8 — Charter + capability matrix load
# ---------------------------------------------------------------------------


class CharterCapabilityMatrixGate(Gate):
    """Gate 8 — eager-load the active Constitution + capability matrix.

    The IsoKronMemoryProvider lazy-loads these on first read (e.g.
    when KR-P2-A's pre-screen runs). Gate 8 forces the load at boot
    so an operator sees the load failure here rather than at first
    tool call.

    Uses :meth:`provider._prefetch_all` which does a single
    asyncio.gather of all session reads (Role Charter, policy
    registry, capability matrix, scratchpad, recent events, active
    Constitution revision).
    """

    gate_id: ClassVar[str] = "8_charter_capability_matrix_load"
    gate_class: ClassVar[GateClass] = GateClass.TRANSIENT
    title: ClassVar[str] = "Charter + capability matrix eager-loaded"

    async def run(self, context: BootContext) -> GateResult:
        started_at, t0 = _begin()
        provider = context.memory_provider
        if provider is None:
            return _fail_result(
                self, started_at, t0, "memory_provider is not set on BootContext"
            )

        workspace_id = (
            context.workspace_id
            or _safe_resolve_workspace_id(provider)
        )
        if not workspace_id:
            return _fail_result(
                self, started_at, t0, "workspace_id unresolved; cannot prefetch"
            )

        try:
            provider._prefetch_all(workspace_id)
        except Exception as exc:
            return _fail_result(
                self, started_at, t0,
                f"_prefetch_all raised: {exc!r}",
            )

        # Sanity-check the caches populated.
        try:
            cap_row = provider._capability_cache.get(workspace_id)
            constitution = provider._constitution_cache.get(workspace_id)
        except Exception as exc:
            return _fail_result(
                self, started_at, t0,
                f"cache inspection after _prefetch_all raised: {exc!r}",
            )

        # Constitution may legitimately be None for fresh workspaces
        # (no active revision authored yet); capability row should
        # always be present after a successful prefetch.
        if cap_row is None:
            return _fail_result(
                self, started_at, t0,
                "capability matrix cache empty after _prefetch_all; "
                "capability_matrix_mirror may not have populated",
            )

        return _pass_result(
            self, started_at, t0,
            f"prefetch completed; capability row cached "
            f"(constitution present: {constitution is not None})",
        )


def _safe_resolve_workspace_id(provider: Any) -> Optional[str]:
    try:
        return provider._resolve_workspace_id()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Gate 10 — KR-7 boot smoke (read-only attribution check)
# ---------------------------------------------------------------------------


class KR7BootSmokeGate(Gate):
    """Gate 10 — KR-7 invariant smoke: the dispatch tier attributes a
    ``kora__*`` call to the canonical 0076 actor.

    Probe: invoke the K-7 read-only tool ``kora__read_kora_capability_row``.
    A successful response means:
      - Dispatch tier accepted the Layer-A wsk_* token
      - Dispatch tier resolved Layer-B attribution to ``actor_kind='kora'``
        (the K-7 tool returns the Kora capability row; the dispatch
        tier's RLS-equivalent filter is implicit)
      - Substrate-side `actor_registry` has the canonical row for the
        attributed workspace + actor

    INVARIANT: if the dispatch tier can't attribute the runtime's
    token to the canonical kora actor, no kora__* tool will work; the
    process should halt for operator triage.

    NO durable write per the bucket-spec § ST2 table — this is a
    read-only K-7 invocation. The ``kora.kr7.smoke`` chain-event
    literal in ``foundation/0159`` exists for FUTURE periodic
    health probes; not emitted by this gate.
    """

    gate_id: ClassVar[str] = "10_kr7_boot_smoke"
    gate_class: ClassVar[GateClass] = GateClass.INVARIANT
    title: ClassVar[str] = "KR-7 boot smoke (read-only attribution invariant)"

    async def run(self, context: BootContext) -> GateResult:
        started_at, t0 = _begin()
        provider = context.memory_provider
        if provider is None:
            return _fail_result(
                self, started_at, t0, "memory_provider is not set on BootContext"
            )

        try:
            mcp_client = provider._connection.get_mcp_client()
            result = provider._connection.submit_and_wait(
                mcp_client.invoke("kora__read_kora_capability_row", {}),
                timeout=10.0,
            )
        except Exception as exc:
            return _fail_result(
                self, started_at, t0,
                f"KR-7 smoke invoke raised: {exc!r}",
            )

        if not isinstance(result, dict) or not result:
            return _fail_result(
                self, started_at, t0,
                f"KR-7 returned unexpected/empty shape: {type(result).__name__}",
            )

        return _pass_result(
            self, started_at, t0,
            f"KR-7 smoke OK; dispatch tier attributed kora__* call "
            f"successfully (response had {len(result)} top-level keys)",
        )


# ---------------------------------------------------------------------------
# Canonical gate sequence (R4.1 §9.2 order)
# ---------------------------------------------------------------------------


def build_default_gate_sequence() -> list[Gate]:
    """Return the canonical R4.1 §9.2 gate sequence (the 7 gates this
    bucket ships, in order).

    Order matters per R4.1 §9.2:
      Gate 1 → 4 → 5 → 6 → 7 → 8 → 10

    Gates 2 / 3 / 3b / 9 are intentionally absent — handled in other
    buckets (2 by KR-P2-F-pre entrypoint; 3 / 3b by KR-P2-M; 9 by
    KR-P2-K).
    """
    return [
        ClaudeAuthGate(),
        KronicleRolePermsGate(),
        KronicleMCPReachableGate(),
        WskTokenValidGate(),
        CanonicalKoraActorGate(),
        CharterCapabilityMatrixGate(),
        KR7BootSmokeGate(),
    ]
