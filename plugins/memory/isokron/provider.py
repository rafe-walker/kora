"""IsoKronMemoryProvider — Kora's substrate-backed MemoryProvider.

KR-2 closes here at ST4: every ABC method has a real implementation,
no ``NotImplementedError`` stubs remain. Surface summary:

* **Role Charter** — ``read_active_role_charter`` against
  ``public.kora_role_charter`` with SHA-256 integrity check (ST2).
* **Capability matrix Kora row** — C2 Python mirror of
  ``ACTOR_CAPABILITY_MATRIX`` (parity test guards drift; K-7 will swap
  to a Sea MCP tool; ST2).
* **Policy registry** — ``read_kora_policy_registry`` against
  ``kora_policy_registry`` with RLS GUC; 31-row sanity warn-on-drift (ST2).
* **Scratchpad** — own + cross-agent reads against
  ``kronicle.agent_scratchpad_entries`` (ST3). Writes deferred behind
  ``ScratchpadWriteNotAvailableError`` until K-8 ships the Sea MCP tool.
* **Recent chain events** — ``read_recent_kora_events`` against
  ``hivex_foundation.event_log`` (tenant_id-keyed; JOIN tenant on
  clerk_org_id) filtered ``LIKE 'kora.%'`` (ST4).
* **Active Constitution revision** — ``read_active_constitution_revision``
  against ``kronicle.workspace_constitution_revisions``; hex-encoded
  ``rules_hash`` for K-6 Constitution pre-screen middleware (ST4).
* **Chain event emit** — KR-7 swap to ``kora__append_event`` MCP call
  via the KR-7a-wired :class:`IsoKronMCPClient`. K-9 shipped the
  substrate tool (`f8487059`); BUILD_DEVIATIONS
  ``D-kr2-st4-no-chain-emit-mcp-tool`` closed in KR-7. Substrate-side
  failures surface as ``IsoKronMCPInvocationError``; lifecycle hooks
  catch + log at ERROR so the session stays alive.
* **Session context** — ``assemble_session_context`` returns a
  ``KoraSessionContext`` mirroring the TS-side
  ``packages/sea-mcp-server/src/kora/context-assembler/types.ts:130``
  shape; six load-bearing reads + two identity fields, fanned out via
  ``asyncio.gather``.

Selected via ``memory.provider: isokron`` in ``~/.kora/config.yaml``.
Replaces Hermes' flat MEMORY.md / USER.md once configured; ``MemoryManager``
enforces the one-external-provider invariant so the legacy files are
left as read-only fallback per the KR-2 bucket spec.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

from .cache import TTLCache
from .config import ISOKRON_CONFIG_SCHEMA, IsoKronProviderConfig
from .connection import IsoKronConnection
from .events import (
    RecentChainEvent,
    emit_kora_event,
)
from .mcp_client import IsoKronMCPInvocationError
from .models import (
    KoraCapabilityRow,
    PolicyRegistryEntry,
    RoleCharter,
)
from .reads import (
    policies_as_mapping,
    read_active_role_charter,
    read_kora_capability_row,
    read_kora_policy_registry,
)
from .scratchpad import (
    ScratchpadEntry,
    ScratchpadKind,
    ScratchpadWriteNotAvailableError,
    VisibilityScope,
    read_cross_agent_scratchpad,
    read_own_scratchpad,
    write_scratchpad_entry,
)
from .session_context import KoraSessionContext, assemble_session_context

logger = logging.getLogger(__name__)


# Policy paths surfaced verbatim in the system prompt's "Active policy
# values" section. Pulled from the 31 canonical entries in migration
# 0078; the list is the 3-5 most load-bearing for Kora's behavior.
# Operator additions to the registry are NOT auto-included — kept
# tight so the system prompt stays compact.
SYSTEM_PROMPT_POLICY_PATHS = (
    "policy.kora_disabled",
    "policy.kora_max_nonsecurity_overrides_per_24h",
    "policy.kora_output_size_cap_pull",
    "policy.kora_rate_limit_per_minute",
    "policy.kora_context_assembler_token_ceiling.opus",
)

# Verbatim string injected at the bottom of system_prompt_block per
# ST2 spec § "Rule-6 honest-label". Operators grep this string to
# confirm a session's system prompt actually got the IsoKron block.
RULE_6_HONEST_LABEL = (
    "This identity block was assembled by IsoKronMemoryProvider on "
    "session start. The substrate is the source of truth; if anything "
    "below conflicts with a fresh substrate query, the substrate query "
    "wins."
)


class IsoKronMemoryProvider(MemoryProvider):
    """Substrate-backed MemoryProvider for the Kora runtime.

    Resolves Kora's memory against the IsoKron typed-graph substrate:

    * Reads (asyncpg, direct Postgres):
        - ``public.kora_role_charter`` per-workspace charter body + integrity hash
        - ``ACTOR_CAPABILITY_MATRIX`` Kora row (via Sea MCP read tool or
          generated manifest; STOP-gate at ST2 picks the approach)
        - ``public.kora_policy_registry`` policy rows
        - ``kronicle.agent_scratchpad_entries`` own + cross-agent reads
        - ``hivex_foundation.event_log`` recent ``kora.*`` events

    * Writes (MCP, Sea MCP server tool surface):
        - ``kora__write_agent_scratchpad`` for scratchpad inserts
          (Plan 04 ``cap_write_agent_scratchpad`` capability gate)
        - ``append_event`` (or substrate-side helper) for ``kora.*``
          chain event emission

    KR-2 ST1 ships only the structural skeleton; ST2-ST4 land the real
    paths.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Instantiate the provider with an unvalidated config dict.

        ``config`` is the plugin's ``plugins.entries.isokron`` block from
        ``~/.kora/config.yaml`` (already env-expanded by the plugin
        loader). We validate via ``IsoKronProviderConfig`` and stash the
        parsed object. The ``IsoKronConnection`` is created but NOT
        started until ``initialize()`` runs.

        Raises:
            pydantic.ValidationError when required keys are missing or
            field validators reject the input. Surfaces in the plugin
            loader logs at startup; operators see "isokron provider
            failed to construct" with a useful traceback.
        """
        self._raw_config = config or {}
        self._config: Optional[IsoKronProviderConfig] = None
        self._connection: Optional[IsoKronConnection] = None
        self._session_id: Optional[str] = None
        self._initialized: bool = False

        # Per-workspace TTL caches for the three ST2 reads. TTL defaults
        # to 60s, overridable via config.cache_ttl_seconds. Cache lives
        # on the provider (per-session) so a /reset clears state.
        ttl = (
            self._raw_config.get("cache_ttl_seconds")
            if isinstance(self._raw_config, dict)
            else None
        ) or 60.0
        self._charter_cache: TTLCache[RoleCharter] = TTLCache(ttl_seconds=ttl)
        self._policy_cache: TTLCache[List[PolicyRegistryEntry]] = TTLCache(
            ttl_seconds=ttl
        )
        self._capability_cache: TTLCache[KoraCapabilityRow] = TTLCache(
            ttl_seconds=ttl
        )
        # Scratchpad caches — separate for own vs cross-agent so a Kora
        # write invalidates only her own cache and not Critic/Oracle reads.
        self._own_scratchpad_cache: TTLCache[List[ScratchpadEntry]] = TTLCache(
            ttl_seconds=ttl
        )
        self._cross_agent_scratchpad_cache: TTLCache[List[ScratchpadEntry]] = TTLCache(
            ttl_seconds=ttl
        )
        # Recent chain events + active Constitution revision (ST4 reads).
        self._events_cache: TTLCache[List[RecentChainEvent]] = TTLCache(
            ttl_seconds=ttl
        )
        # Constitution revision cache holds Optional[tuple[str, str]] —
        # (revision_id, rules_hash_hex) — or the sentinel ``(None, None)``
        # for fresh workspaces with no revisions. Using a tuple keeps the
        # TTLCache invariant (cached value cannot be None for "miss").
        self._constitution_cache: TTLCache[
            tuple[Optional[str], Optional[str]]
        ] = TTLCache(ttl_seconds=ttl)

        # Validate config eagerly so a typo surfaces at construct time
        # rather than at first turn. ``is_available`` checks this.
        if config:
            self._config = IsoKronProviderConfig(**config)
            self._connection = IsoKronConnection(self._config)

    # -- Required: identity + availability -----------------------------------

    @property
    def name(self) -> str:
        return "isokron"

    def is_available(self) -> bool:
        """Return True when config is parsed + dependencies importable.

        Does NOT open connections (per ABC contract — "should not make
        network calls — just check config and installed deps"). The
        actual substrate handshake happens in ``initialize()``.
        """
        if self._config is None:
            logger.debug(
                "[kora.isokron] is_available=False — config not parsed; "
                "configure the plugins.entries.isokron block in config.yaml."
            )
            return False

        try:
            import asyncpg  # noqa: F401
        except ImportError:
            logger.warning(
                "[kora.isokron] is_available=False — asyncpg not installed. "
                "Run `uv sync --extra isokron`."
            )
            return False

        try:
            import mcp  # noqa: F401
        except ImportError:
            logger.warning(
                "[kora.isokron] is_available=False — mcp client not installed. "
                "Run `uv sync --extra isokron`."
            )
            return False

        return True

    # -- Lifecycle -----------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        """Open the connection + refresh the capability matrix from MCP.

        KR-7b: replaces the hand-mirrored capability matrix with the
        authoritative substrate fetch (``kora__read_kora_capability_row``).
        On fetch failure the hand-mirrored fallback stays in place + a
        WARNING is logged so dev/test ergonomics survive substrate
        downtime. Operators grep ``[kora.capability_matrix.fallback]``
        in production logs to confirm the fetch is succeeding.
        """
        if self._connection is None:
            raise RuntimeError(
                "[kora.isokron] initialize called before construct — "
                "the plugin loader must instantiate with a valid config."
            )
        self._connection.start()
        self._session_id = session_id
        self._initialized = True
        self._refresh_capability_matrix_from_mcp()
        logger.info(
            "[kora.isokron] initialize OK. session_id=%s actor_kind=%s",
            session_id,
            self._config.actor_kind if self._config else "<unset>",
        )

    def _refresh_capability_matrix_from_mcp(self) -> None:
        """Try to fetch the capability matrix from MCP; fall back on failure.

        Production: fetch succeeds → matrix is authoritative.
        Dev/test (or substrate down): fetch raises → fallback intact +
        WARNING logged.

        Same production-test posture as KR-7's chain-emit closure —
        code shape ships green; substrate-team dispatch tier (queued)
        un-stubs the K-7 handler. Operators grep
        ``[kora.capability_matrix.fallback]`` to confirm production
        fetch health.
        """
        from .capability_matrix_mirror import populate_capability_matrix_from_mcp

        if self._connection is None:  # pragma: no cover — guarded above
            return
        try:
            mcp_client = self._connection.get_mcp_client()
        except Exception as exc:
            logger.warning(
                "[kora.capability_matrix.fallback] could not reach MCP "
                "client (%s); using hand-mirrored fallback. Production "
                "deploys must verify substrate dispatch tier is live.",
                exc,
            )
            return
        try:
            self._connection.submit_and_wait(
                populate_capability_matrix_from_mcp(mcp_client),
                timeout=10.0,
            )
        except Exception as exc:
            logger.warning(
                "[kora.capability_matrix.fallback] MCP fetch of "
                "kora__read_kora_capability_row failed (%s); using "
                "hand-mirrored fallback. Production deploys must verify "
                "substrate dispatch tier is live.",
                exc,
            )

    def shutdown(self) -> None:
        """Tear down the connection (idempotent, safe on partial init)."""
        if self._connection is not None:
            self._connection.close()
        self._initialized = False
        logger.info("[kora.isokron] shutdown OK")

    # -- Static metadata --------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return the model-facing tool schemas this provider exposes.

        KR-3 ST1: ``iso_node_*`` family (4 tools).
        KR-3 ST2: ``iso_link_*`` family (3 tools).
        ST3 wires registration polish.
        """
        from .tools import ISO_TYPED_GRAPH_TOOL_SCHEMAS

        return list(ISO_TYPED_GRAPH_TOOL_SCHEMAS)

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return ISOKRON_CONFIG_SCHEMA

    # -- ST2 reads + system prompt block -------------------------------------

    def _resolve_workspace_id(self, **kwargs: Any) -> Optional[str]:
        """Pick the workspace_id for this session.

        Precedence:
            1. ``kwargs['workspace_id']`` (explicit per-call override)
            2. ``self._config.default_workspace_id`` (config-level)
            3. ``None`` — caller logs a warning and skips substrate reads

        Per-session resolution via gateway/CLI context is the path
        future sub-tasks fill in (see ``initialize`` kwargs in the
        ``MemoryProvider`` ABC). For ST2 we accept the explicit
        override + config default; cron jobs without per-session
        context use the config default.
        """
        explicit = kwargs.get("workspace_id")
        if isinstance(explicit, str) and explicit:
            return explicit
        if self._config and self._config.default_workspace_id:
            return self._config.default_workspace_id
        return None

    def _prefetch_all(self, workspace_id: str) -> None:
        """Block on a parallel ``asyncio.gather`` of all session reads.

        Each result populates its TTL cache so the subsequent
        ``system_prompt_block`` call hits warm cache. Integrity errors
        (RoleCharterIntegrityError, NoActiveRoleCharterError) surface
        as exceptions per spec § "fail-closed"; the policy 31-row
        sanity + scratchpad BLAKE3 drift are non-fatal WARNINGs.

        Seven reads in parallel: Role Charter, policy registry,
        capability matrix, own scratchpad, cross-agent scratchpad,
        recent ``kora.*`` chain events, active Constitution revision.

        Called by ``on_turn_start`` (per spec acceptance) and as a
        cache-warm step from ``system_prompt_block`` when the cache
        is cold.
        """
        if self._connection is None:
            raise RuntimeError(
                "[kora.isokron] _prefetch_all called before construct"
            )
        pool = self._connection.get_pg_pool()

        # We deliberately gather all 7 reads in one shot rather than
        # call ``assemble_session_context`` — the latter doesn't fetch
        # the policy registry (not part of KoraSessionContext), and
        # we want a single gather for round-trip latency.
        from .events import read_recent_kora_events
        from .scratchpad import (
            read_cross_agent_scratchpad as _read_cross,
            read_own_scratchpad as _read_own,
        )
        from .constitution import read_active_constitution_revision

        async def _gather() -> Any:
            return await asyncio.gather(
                read_active_role_charter(workspace_id, pool),
                read_kora_policy_registry(workspace_id, pool),
                read_kora_capability_row(pool),
                _read_own(workspace_id, pool),
                _read_cross(workspace_id, pool),
                read_recent_kora_events(workspace_id, pool),
                read_active_constitution_revision(workspace_id, pool),
            )

        (
            charter,
            policies,
            caps,
            own_entries,
            cross_entries,
            recent_events,
            constitution,
        ) = self._connection.submit_and_wait(_gather(), timeout=20.0)
        self._charter_cache.put(workspace_id, charter)
        self._policy_cache.put(workspace_id, policies)
        self._capability_cache.put(workspace_id, caps)
        self._own_scratchpad_cache.put(workspace_id, own_entries)
        self._cross_agent_scratchpad_cache.put(workspace_id, cross_entries)
        self._events_cache.put(workspace_id, recent_events)
        self._constitution_cache.put(
            workspace_id,
            (
                (constitution.revision_id, constitution.rules_hash)
                if constitution is not None
                else (None, None)
            ),
        )

    def session_context(
        self, *, workspace_id: Optional[str] = None
    ) -> Optional[KoraSessionContext]:
        """Return the assembled session context for the workspace.

        Reads from the post-prefetch caches; returns ``None`` if any
        load-bearing cache is cold (call ``on_turn_start`` first to
        warm). Mirrors the TS-side ``KoraSessionContext`` shape at
        ``packages/sea-mcp-server/src/kora/context-assembler/types.ts:130``.

        Public API — consumers wanting just the typed context object
        (e.g. K-6 Constitution pre-screen middleware in Python) call
        this rather than touching individual caches.
        """
        from datetime import datetime, timezone

        ws = self._resolve_workspace_id(workspace_id=workspace_id)
        if ws is None:
            return None
        charter = self._charter_cache.get(ws)
        capabilities = self._capability_cache.get(ws)
        own = self._own_scratchpad_cache.get(ws)
        cross = self._cross_agent_scratchpad_cache.get(ws)
        events = self._events_cache.get(ws)
        constitution = self._constitution_cache.get(ws)
        if (
            charter is None
            or capabilities is None
            or own is None
            or cross is None
            or events is None
            or constitution is None
        ):
            return None
        rev_id, rules_hash = constitution
        return KoraSessionContext(
            workspace_id=ws,
            assembled_at=datetime.now(timezone.utc).isoformat(),
            role_charter=charter,
            capability_matrix_row=capabilities,
            own_scratchpad=tuple(own),
            cross_agent_scratchpad=tuple(cross),
            recent_chain_events=tuple(events),
            active_constitution_revision_id=rev_id,
            active_constitution_rules_hash=rules_hash,
        )

    def system_prompt_block(self) -> str:
        """Return the assembled identity prompt block.

        Sections (in order):
            §1 Identity — from ``content_md`` / sections.identity
            §2 CAN bullets — sections.authority_can_do
            §3 CANNOT bullets — sections.authority_cannot_do
            §4 Active policy values — selected 5 load-bearing rows
            §5 Capability matrix Kora-row summary — granted cap names
            §6 Recent ``kora.*`` activity — last few chain events
            Rule-6 honest-label — verbatim

        If the substrate is unreachable or the cache is cold and the
        workspace_id can't be resolved, returns an empty string and
        logs a warning (matches the ``MemoryProvider.system_prompt_block``
        contract: "Return empty string to skip").
        """
        workspace_id = self._resolve_workspace_id()
        if workspace_id is None:
            logger.warning(
                "[kora.isokron] system_prompt_block skipped — no workspace_id "
                "resolvable (set default_workspace_id in config.yaml or pass "
                "via session context). Kora session will run without the "
                "Role Charter identity block."
            )
            return ""

        # Warm the cache on cold start. Integrity errors propagate;
        # connection errors propagate — the spec is fail-closed.
        if (
            self._charter_cache.get(workspace_id) is None
            or self._policy_cache.get(workspace_id) is None
            or self._capability_cache.get(workspace_id) is None
            or self._events_cache.get(workspace_id) is None
        ):
            self._prefetch_all(workspace_id)

        charter = self._charter_cache.get(workspace_id)
        policies = self._policy_cache.get(workspace_id)
        capabilities = self._capability_cache.get(workspace_id)
        events = self._events_cache.get(workspace_id) or []
        # The four ST2/ST4 caches are populated post-_prefetch_all; the
        # ``is None`` guards are defensive (e.g. zero-TTL test config).
        assert charter is not None, "charter cache miss after prefetch"
        assert policies is not None, "policy cache miss after prefetch"
        assert capabilities is not None, "capability cache miss after prefetch"

        return _assemble_system_prompt_block(
            charter, policies, capabilities, events
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall context for the upcoming turn — no-op in ST2.

        ST3 wires scratchpad-based prefetch. The ABC's default is
        empty string; we explicitly return empty here so the contract
        is obvious (rather than inheriting the default silently).
        """
        del query, session_id
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Queue background recall for the next turn — no-op in ST2."""
        del query, session_id

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
    ) -> None:
        """Persist a Kora-action summary to the scratchpad (if any happened).

        Heuristic per spec § ST3 §4: any turn whose assistant output
        references a ``cap_*`` token is treated as a Kora action that
        gets a ``reasoning_trail`` scratchpad entry.

        Write goes through :func:`scratchpad.write_scratchpad_entry`
        which currently raises ``ScratchpadWriteNotAvailableError``
        (BUILD_DEVIATIONS ``D-kr2-st3-no-scratchpad-write-mcp-tool``).
        We catch that one error + log a one-line warning so sessions
        stay alive while substrate-team ships the MCP tool. Any other
        exception propagates.
        """
        del session_id, user_content
        if not _looks_like_kora_action(assistant_content):
            return
        workspace_id = self._resolve_workspace_id()
        if workspace_id is None:
            logger.debug(
                "[kora.isokron] sync_turn: Kora action detected but no "
                "workspace_id resolvable — scratchpad write skipped."
            )
            return
        self._attempt_scratchpad_write(
            workspace_id=workspace_id,
            scratchpad_kind=ScratchpadKind.REASONING_TRAIL,
            visibility_scope=VisibilityScope.AGENT_PRIVATE,
            content=_summarize_for_scratchpad(assistant_content),
            origin="sync_turn",
        )

    def handle_tool_call(
        self,
        tool_name: str,
        args: Dict[str, Any],
        **kwargs: Any,
    ) -> str:
        """Route a tool call to the right typed-graph handler.

        KR-3 ST1 dispatches ``iso_node_*``. ST2 adds ``iso_link_*``.
        Unknown tool names fall through to the ABC default which
        raises a clear "Provider isokron does not handle tool X" error.
        """
        del kwargs
        if tool_name.startswith("iso_node_"):
            from .tools import handle_iso_node_tool_call

            return handle_iso_node_tool_call(self, tool_name, args)
        if tool_name.startswith("iso_link_"):
            from .tools import handle_iso_link_tool_call

            return handle_iso_link_tool_call(self, tool_name, args)
        return super().handle_tool_call(tool_name, args)

    # -- Scratchpad reads (sync wrappers around the async reads) -----------

    def read_own_scratchpad(
        self,
        *,
        workspace_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[ScratchpadEntry]:
        """Return Kora's own scratchpad entries (cached 60s per workspace).

        Sync — uses the connection's dedicated IO loop. ``workspace_id``
        falls back to ``default_workspace_id`` via ``_resolve_workspace_id``;
        a missing workspace_id returns empty list and logs a warning.
        """
        ws = self._resolve_workspace_id(workspace_id=workspace_id)
        if ws is None:
            logger.warning(
                "[kora.isokron] read_own_scratchpad — no workspace_id "
                "resolvable; returning empty list."
            )
            return []
        cached = self._own_scratchpad_cache.get(ws)
        if cached is not None:
            return cached
        if self._connection is None:
            raise RuntimeError("[kora.isokron] read_own_scratchpad before construct")
        pool = self._connection.get_pg_pool()
        entries = self._connection.submit_and_wait(
            read_own_scratchpad(ws, pool, limit=limit), timeout=10.0
        )
        self._own_scratchpad_cache.put(ws, entries)
        return entries

    def read_cross_agent_scratchpad(
        self,
        *,
        workspace_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[ScratchpadEntry]:
        """Return cross-agent dereferenceable entries (cached 60s)."""
        ws = self._resolve_workspace_id(workspace_id=workspace_id)
        if ws is None:
            logger.warning(
                "[kora.isokron] read_cross_agent_scratchpad — no "
                "workspace_id resolvable; returning empty list."
            )
            return []
        cached = self._cross_agent_scratchpad_cache.get(ws)
        if cached is not None:
            return cached
        if self._connection is None:
            raise RuntimeError(
                "[kora.isokron] read_cross_agent_scratchpad before construct"
            )
        pool = self._connection.get_pg_pool()
        entries = self._connection.submit_and_wait(
            read_cross_agent_scratchpad(ws, pool, limit=limit), timeout=10.0
        )
        self._cross_agent_scratchpad_cache.put(ws, entries)
        return entries

    def _attempt_scratchpad_write(
        self,
        *,
        workspace_id: str,
        scratchpad_kind: ScratchpadKind,
        visibility_scope: VisibilityScope,
        content: str,
        origin: str,
    ) -> None:
        """Common write-attempt path: catch the deferred-write error gracefully.

        Invalidates the own_scratchpad cache for this workspace whether
        or not the write succeeds. When the MCP tool lands, this code
        path keeps working without changes — only ``write_scratchpad_entry``
        body changes.
        """
        if self._connection is None:
            raise RuntimeError(
                f"[kora.isokron] _attempt_scratchpad_write ({origin}) before construct"
            )
        # Pre-invalidate so a successful write isn't masked by stale cache;
        # a failed write means there's no fresh data to hide either, so
        # invalidation is safe in both branches.
        self._own_scratchpad_cache.invalidate(workspace_id)
        try:
            self._connection.submit_and_wait(
                write_scratchpad_entry(
                    workspace_id=workspace_id,
                    scratchpad_kind=scratchpad_kind,
                    visibility_scope=visibility_scope,
                    content=content,
                    mcp_client=None,  # ST3 deferred; ST4 wires the MCP client
                ),
                timeout=10.0,
            )
        except ScratchpadWriteNotAvailableError as exc:
            logger.warning(
                "[kora.isokron] %s scratchpad write skipped — %s",
                origin,
                exc,
            )

    def on_turn_start(
        self,
        turn_number: int,
        message: str,
        **kwargs: Any,
    ) -> None:
        """Pre-fetch the three reads in parallel via ``asyncio.gather``.

        Per spec acceptance: this hook fires once per turn and warms
        the Role Charter / policy registry / capability matrix caches
        so the system prompt assembly and downstream policy checks
        hit warm cache.

        Integrity errors propagate (fail-closed). Connection errors
        propagate. A missing workspace_id logs a warning and skips
        prefetch (the session can still run with empty system prompt
        block, but no policy enforcement happens).
        """
        del turn_number, message
        workspace_id = self._resolve_workspace_id(**kwargs)
        if workspace_id is None:
            logger.warning(
                "[kora.isokron] on_turn_start prefetch skipped — no "
                "workspace_id resolvable. Configure default_workspace_id "
                "or pass workspace_id via session kwargs."
            )
            return
        self._prefetch_all(workspace_id)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Emit ``kora.session.ended`` chain event with turn count.

        Through the deferred-emit path until K-9 (or equivalent Sea
        MCP ``kora__append_event`` tool) lands. The event would carry
        ``{session_id, turn_count, ended_at}``; the catch + log
        pattern preserves session lifecycle reliability regardless.
        """
        workspace_id = self._resolve_workspace_id()
        if workspace_id is None:
            logger.debug(
                "[kora.isokron] on_session_end: no workspace_id — chain event skipped."
            )
            return
        self._attempt_chain_event_emit(
            workspace_id=workspace_id,
            event_type="kora.session.ended",
            payload={
                "session_id": self._session_id or "",
                "turn_count": len(messages),
            },
            origin="on_session_end",
        )

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        **kwargs: Any,
    ) -> None:
        """Update stashed session_id + invalidate caches on a hard reset.

        ``/resume`` / ``/branch`` / compression keep the logical
        conversation alive — leave the caches as-is; new session_id
        is the only state to rotate.

        ``/reset`` / ``/new`` (``reset=True``) starts a fresh
        conversation. Flush the per-workspace caches so the next turn
        re-reads the current substrate state rather than serving
        stale entries from a different logical session.
        """
        del parent_session_id, kwargs
        self._session_id = new_session_id
        if reset:
            self._charter_cache.clear()
            self._policy_cache.clear()
            self._capability_cache.clear()
            self._own_scratchpad_cache.clear()
            self._cross_agent_scratchpad_cache.clear()
            self._events_cache.clear()
            self._constitution_cache.clear()
            logger.info(
                "[kora.isokron] session reset to %s — all caches flushed",
                new_session_id,
            )

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Provider-extracted insights to preserve through compression.

        The IsoKron substrate is the system of record for everything
        that should survive compression (Role Charter, scratchpad,
        chain events). Conversation-history compression doesn't need
        an isokron-side contribution; substrate-side data is
        re-fetched on next turn.
        """
        del messages
        return ""

    def on_delegation(
        self,
        task: str,
        result: str,
        *,
        child_session_id: str = "",
        **kwargs: Any,
    ) -> None:
        """Record a subagent delegation as scratchpad + chain event.

        Subagents (e.g. claude_pm / oracle / critic) emit on their own
        substrate identity; parent Kora records the observation via a
        ``reasoning_trail`` scratchpad entry (so her own context shows
        what she handed off + what came back) and a
        ``kora.handoff.to_claude_pm`` chain event.

        Both paths are catch-and-continue (deferred MCP tools).
        """
        del kwargs
        workspace_id = self._resolve_workspace_id()
        if workspace_id is None:
            return
        summary = _summarize_for_scratchpad(
            f"[delegation child={child_session_id}] task={task!r} result={result!r}"
        )
        self._attempt_scratchpad_write(
            workspace_id=workspace_id,
            scratchpad_kind=ScratchpadKind.REASONING_TRAIL,
            visibility_scope=VisibilityScope.AGENT_PRIVATE,
            content=summary,
            origin="on_delegation",
        )
        self._attempt_chain_event_emit(
            workspace_id=workspace_id,
            event_type="kora.handoff.to_claude_pm",
            payload={
                "child_session_id": child_session_id,
                "task_preview": task[:200],
                "result_preview": result[:200],
            },
            origin="on_delegation",
        )

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mirror Hermes' built-in memory writes to the scratchpad.

        Per ABC: "Use to mirror built-in memory writes to your backend."
        We project each memory write as a ``reasoning_trail`` scratchpad
        entry. ``action`` and ``target`` are encoded in the summary so
        operators can trace which built-in write produced which
        scratchpad row.

        Write attempts go through the same deferred path as ``sync_turn``;
        ``ScratchpadWriteNotAvailableError`` is caught + logged.
        """
        del metadata
        workspace_id = self._resolve_workspace_id()
        if workspace_id is None:
            logger.debug(
                "[kora.isokron] on_memory_write: no workspace_id resolvable — "
                "scratchpad mirror skipped (built-in memory write itself is "
                "unaffected; this hook only mirrors)."
            )
            return
        summary = f"[memory.{action} → {target}] {_summarize_for_scratchpad(content)}"
        self._attempt_scratchpad_write(
            workspace_id=workspace_id,
            scratchpad_kind=ScratchpadKind.REASONING_TRAIL,
            visibility_scope=VisibilityScope.AGENT_PRIVATE,
            content=summary,
            origin="on_memory_write",
        )

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """No-op: the IsoKron provider is configured via env vars + the
        ``plugins.entries.isokron`` YAML block in ``config.yaml``.

        Per ABC: "Providers that use only env vars can leave the default
        (no-op)". The ``kora memory setup`` walkthrough writes the YAML
        block + an ``.env`` entry directly via the secret-handling path;
        the provider itself has no native config file to maintain.
        """
        del values, hermes_home

    # -- Chain event emit (KR-7: real Sea MCP call) --------------------------

    def _attempt_chain_event_emit(
        self,
        *,
        workspace_id: str,
        event_type: str,
        payload: Dict[str, Any],
        origin: str,
    ) -> None:
        """Emit a ``kora.*`` chain event via the Sea MCP ``kora__append_event``
        tool. Mirrors :meth:`_attempt_scratchpad_write`'s
        attempt-then-log pattern but for the chain-emit surface.

        Substrate-side failures (``IsoKronMCPInvocationError``) get
        logged at ERROR — chain-event-emit failure is operator-visible
        per PM-lean ("substrate-side issue worth surfacing"). Session
        lifecycle hooks (``on_session_end`` / ``on_delegation`` /
        ``sync_turn``) keep running regardless so a single bad emit
        doesn't crash the session. Operators grep
        ``[kora.chain.emit.failed]`` in logs to find dropped events.

        Successful emits invalidate the per-workspace events cache so
        the next ``system_prompt_block`` §6 re-reads.
        """
        if self._connection is None:
            raise RuntimeError(
                f"[kora.isokron] _attempt_chain_event_emit ({origin}) before construct"
            )
        try:
            mcp_client = self._connection.get_mcp_client()
        except Exception as exc:
            logger.error(
                "[kora.chain.emit.failed] %s emit (%s) — MCP client unavailable: %s",
                origin,
                event_type,
                exc,
            )
            return
        try:
            event_id = self._connection.submit_and_wait(
                emit_kora_event(
                    workspace_id=workspace_id,
                    event_type=event_type,
                    payload=payload,
                    mcp_client=mcp_client,
                ),
                timeout=10.0,
            )
            logger.info(
                "[kora.chain.emit] %s emit %s → event_id=%s",
                origin,
                event_type,
                event_id,
            )
            self._events_cache.invalidate(workspace_id)
        except IsoKronMCPInvocationError as exc:
            # PM-lean: propagate is the function-level default, but the
            # provider-level wrapper catches at the lifecycle boundary
            # so session hooks stay alive. ERROR (not WARNING) so the
            # failure is operator-visible.
            logger.error(
                "[kora.chain.emit.failed] %s emit (%s) — %s",
                origin,
                event_type,
                exc,
            )
        except Exception as exc:
            # Defensive: any other unexpected exception from the MCP
            # boundary (transport reset, timeout, etc.) — same
            # error-log-but-continue treatment.
            logger.error(
                "[kora.chain.emit.failed] %s emit (%s) — unexpected: %s",
                origin,
                event_type,
                exc,
            )


# ---------------------------------------------------------------------------
# Kora-action heuristic + scratchpad summarizer (module-level)
# ---------------------------------------------------------------------------


import re as _re

_KORA_ACTION_PATTERN = _re.compile(r"\bcap_[a-z0-9_]+\b")
"""Match capability tokens like ``cap_write_agent_scratchpad`` in text.

A capability mention in assistant output is the spec § ST3 §4 heuristic
for "a Kora action happened this turn". Crude but PM-approved as the
ST3 starting point — refined when ST4 wires real chain-event detection.
"""

_SCRATCHPAD_SUMMARY_MAX_CHARS = 2000
"""Cap inline scratchpad content at 2KB to stay under the eventual
``content_inline TEXT`` payload limits + keep audit-log noise bounded.
Longer content should go through object storage via ``content_uri``."""


def _looks_like_kora_action(assistant_content: str) -> bool:
    """True iff the assistant output references at least one ``cap_*`` token.

    Public so tests + the sync_turn heuristic share one definition.
    """
    return bool(_KORA_ACTION_PATTERN.search(assistant_content))


def _summarize_for_scratchpad(content: str) -> str:
    """Trim ``content`` to fit a scratchpad ``content_inline`` row.

    Truncates with a clear marker rather than silently chopping so
    downstream readers know the entry was lossy.
    """
    if len(content) <= _SCRATCHPAD_SUMMARY_MAX_CHARS:
        return content
    head = content[: _SCRATCHPAD_SUMMARY_MAX_CHARS - 24]
    return f"{head}…[truncated by isokron]"


# ---------------------------------------------------------------------------
# System prompt block assembler (module-level pure function)
# ---------------------------------------------------------------------------


def _format_policy_value(value: Any) -> str:
    """Format a JSONB policy_value compactly for the system prompt.

    Scalars render as-is (``true`` / ``false`` / ``5`` / ``"text"``);
    objects render as JSON. Keeps the prompt readable without bloating
    on operator-extended structured values.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    import json as _json

    return _json.dumps(value, separators=(",", ":"), sort_keys=True)


def _assemble_system_prompt_block(
    charter: RoleCharter,
    policies: List[PolicyRegistryEntry],
    capabilities: KoraCapabilityRow,
    recent_events: List[RecentChainEvent],
) -> str:
    """Render the identity + policy + capability + activity block.

    Pure function — extracted from ``IsoKronMemoryProvider`` so tests
    can drive it with synthetic shapes without mocking the connection.
    """
    sections = charter.sections
    can_bullets = "\n".join(f"  - {item}" for item in sections.authority_can_do)
    cannot_bullets = "\n".join(
        f"  - {item}" for item in sections.authority_cannot_do
    )

    policy_lookup = policies_as_mapping(policies)
    policy_lines: List[str] = []
    for path in SYSTEM_PROMPT_POLICY_PATHS:
        if path in policy_lookup:
            policy_lines.append(
                f"  - {path} = {_format_policy_value(policy_lookup[path])}"
            )
        else:
            # Surface missing entries explicitly so an operator who
            # pruned the registry sees the absence reflected in Kora's
            # session rather than silently dropped.
            policy_lines.append(f"  - {path} = <not seeded>")
    policy_section = "\n".join(policy_lines)

    granted_sorted = sorted(capabilities.granted)
    cap_bullets = "\n".join(f"  - {cap}" for cap in granted_sorted)

    blocks = [
        f"§1 Identity (Kora Role Charter v{charter.charter_version})",
        sections.identity,
        "",
        "§2 You CAN:",
        can_bullets,
        "",
        "§3 You CANNOT:",
        cannot_bullets,
        "",
        "§4 Active policy values",
        policy_section,
        "",
        f"§5 Granted capabilities ({len(granted_sorted)} of "
        f"{len(capabilities.granted) + len(capabilities.denied)}):",
        cap_bullets,
        "",
        _render_typed_graph_tool_surface(),
        "",
        _render_recent_activity(recent_events),
        "",
        RULE_6_HONEST_LABEL,
    ]
    return "\n".join(blocks)


def _render_typed_graph_tool_surface() -> str:
    """Render the §6a 'Typed-graph tools' section.

    KR-3 ST3 extension. The model sees the 7-tool surface with one-line
    guidance + the Hermes-deprecation note so it knows to prefer the
    typed-graph tools over the flat ``memory.*`` family.

    Numbered 6a rather than renumbering everything because
    ``test_reads.py`` + ``test_provider_end_to_end.py`` (already on
    main) assert ``§6 Recent kora.* activity`` — keeping 6a keeps
    those tests stable while the new content lands.
    """
    # Tool names sourced from the combined typed-graph schemas so any
    # future rename or addition flows through naturally.
    from .tools import ISO_TYPED_GRAPH_TOOL_SCHEMAS

    names = [s["name"] for s in ISO_TYPED_GRAPH_TOOL_SCHEMAS]
    name_line = ", ".join(names)
    return (
        "§6a Typed-graph tools — your working memory is the IsoKron graph.\n"
        f"You have {len(names)} tools: {name_line}.\n"
        "\n"
        "Each node has a typed kind (one of 18 canonical IsoKron entity\n"
        "kinds) — prefer the most specific kind over Concept.\n"
        "Edges are typed — declare relationships when you observe them.\n"
        "Hermes' flat memory.set / memory.add / memory.replace / "
        "memory.remove tool is deprecated; iso_node_* is richer."
    )


# Number of recent events to surface in the §6 prompt section. Keeps
# prompt size bounded — the full set lives in event_log and is
# queryable via read_recent_kora_events directly.
_SYSTEM_PROMPT_RECENT_EVENT_LIMIT = 5


def _render_recent_activity(events: List[RecentChainEvent]) -> str:
    """Render §6 'Recent kora.* activity'.

    Returns a "§6" section even when the list is empty so the section
    structure stays consistent across sessions (operators inspecting
    the prompt see the same anchors regardless of activity volume).
    """
    if not events:
        return "§6 Recent kora.* activity\n  - <no recent chain events>"
    head = events[:_SYSTEM_PROMPT_RECENT_EVENT_LIMIT]
    bullets = []
    for evt in head:
        # First line of payload_summary gives operators the shape of
        # the event without the full pretty-printed JSON in-prompt.
        first_line = evt.payload_summary.splitlines()[0] if evt.payload_summary else ""
        bullets.append(
            f"  - {evt.occurred_at} {evt.event_type} {first_line}".rstrip()
        )
    if len(events) > _SYSTEM_PROMPT_RECENT_EVENT_LIMIT:
        bullets.append(
            f"  - …{len(events) - _SYSTEM_PROMPT_RECENT_EVENT_LIMIT} older "
            f"event(s) in event_log"
        )
    return f"§6 Recent kora.* activity ({len(events)} cached):\n" + "\n".join(bullets)
