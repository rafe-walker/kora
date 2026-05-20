"""IsoKronMemoryProvider — Kora's substrate-backed MemoryProvider.

KR-2 ST1 shipped this as a structural skeleton. KR-2 ST2 wires the
read paths:

* **Role Charter** — ``read_active_role_charter`` against
  ``public.kora_role_charter`` with SHA-256 integrity check.
* **Capability matrix Kora row** — C2 Python mirror of
  ``ACTOR_CAPABILITY_MATRIX`` (parity test guards drift; K-7 will swap
  to a Sea MCP tool).
* **Policy registry** — ``read_kora_policy_registry`` against
  ``kora_policy_registry`` with RLS GUC set inside a transaction;
  31-row sanity warned-on-drift.

Subsequent sub-tasks fill in:

* **ST3** — scratchpad reads + writes (Plan 02 schema). Wires
  ``sync_turn`` / ``on_memory_write``.
* **ST4** — chain event emission (``kora.*`` event types) + recent
  events read. Removes the remaining stubs.

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


def _not_yet_implemented(method: str, sub_task: str) -> NotImplementedError:
    """Build a Rule-6 honest NotImplementedError for ST1 skeleton stubs.

    Every stub method shares the same message format so operators can
    grep `kora.isokron.todo` in their logs to see which surfaces are
    still unimplemented.
    """
    msg = (
        f"[kora.isokron.todo] IsoKronMemoryProvider.{method} not yet wired — "
        f"KR-2 {sub_task} implements this. Rule-6: KR-2 ST1 shipped a "
        f"structural skeleton; do not rely on this surface yet."
    )
    logger.warning(msg)
    return NotImplementedError(msg)


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
        """Open the connection (IO loop in ST1; real handshake in ST2+)."""
        if self._connection is None:
            raise RuntimeError(
                "[kora.isokron] initialize called before construct — "
                "the plugin loader must instantiate with a valid config."
            )
        self._connection.start()
        self._session_id = session_id
        self._initialized = True
        logger.info(
            "[kora.isokron] initialize OK (Rule-6: KR-2 ST1 skeleton — "
            "real substrate handshake lands in ST2). session_id=%s "
            "actor_kind=%s",
            session_id,
            self._config.actor_kind if self._config else "<unset>",
        )

    def shutdown(self) -> None:
        """Tear down the connection (idempotent, safe on partial init)."""
        if self._connection is not None:
            self._connection.close()
        self._initialized = False
        logger.info("[kora.isokron] shutdown OK")

    # -- Static metadata --------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return tool schemas (ABC-required).

        ST1: return empty list — no tools surfaced until KR-3 wires the
        ``iso_node_*`` / ``iso_link_*`` family on top of this provider.
        """
        return []

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
        """Block on a parallel ``asyncio.gather`` of the three reads.

        Each result populates its TTL cache so the subsequent
        ``system_prompt_block`` call hits warm cache. Integrity errors
        (RoleCharterIntegrityError, NoActiveRoleCharterError) surface
        as exceptions per spec § "fail-closed"; the policy 31-row
        sanity warning is non-fatal.

        Called by ``on_turn_start`` (per spec acceptance) and as a
        cache-warm step from ``system_prompt_block`` when the cache
        is cold.
        """
        if self._connection is None:
            raise RuntimeError(
                "[kora.isokron] _prefetch_all called before construct"
            )
        pool = self._connection.get_pg_pool()

        async def _gather() -> tuple[
            RoleCharter, List[PolicyRegistryEntry], KoraCapabilityRow
        ]:
            return await asyncio.gather(
                read_active_role_charter(workspace_id, pool),
                read_kora_policy_registry(workspace_id, pool),
                read_kora_capability_row(pool),
            )

        charter, policies, caps = self._connection.submit_and_wait(
            _gather(), timeout=15.0
        )
        self._charter_cache.put(workspace_id, charter)
        self._policy_cache.put(workspace_id, policies)
        self._capability_cache.put(workspace_id, caps)

    def system_prompt_block(self) -> str:
        """Return the assembled identity prompt block.

        Sections (in order):
            §1 Identity — from ``content_md`` / sections.identity
            §2 CAN bullets — sections.authority_can_do
            §3 CANNOT bullets — sections.authority_cannot_do
            Active policy values — selected 5 load-bearing rows
            Capability matrix Kora-row summary — granted cap names
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
        ):
            self._prefetch_all(workspace_id)

        charter = self._charter_cache.get(workspace_id)
        policies = self._policy_cache.get(workspace_id)
        capabilities = self._capability_cache.get(workspace_id)
        # All three are populated post-_prefetch_all; the ``is None``
        # guards are defensive (e.g. zero-TTL test config).
        assert charter is not None, "charter cache miss after prefetch"
        assert policies is not None, "policy cache miss after prefetch"
        assert capabilities is not None, "capability cache miss after prefetch"

        return _assemble_system_prompt_block(charter, policies, capabilities)

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
        raise _not_yet_implemented("sync_turn", "ST3")

    def handle_tool_call(
        self,
        tool_name: str,
        args: Dict[str, Any],
        **kwargs: Any,
    ) -> str:
        raise _not_yet_implemented("handle_tool_call", "ST3 (writes) / KR-3 (iso_node_* tools)")

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
        raise _not_yet_implemented("on_session_end", "ST4")

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        **kwargs: Any,
    ) -> None:
        raise _not_yet_implemented("on_session_switch", "ST4")

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        raise _not_yet_implemented("on_pre_compress", "ST4")

    def on_delegation(
        self,
        task: str,
        result: str,
        *,
        child_session_id: str = "",
        **kwargs: Any,
    ) -> None:
        raise _not_yet_implemented("on_delegation", "ST4")

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        raise _not_yet_implemented("on_memory_write", "ST3")

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        raise _not_yet_implemented("save_config", "ST3 (`kora memory setup` walkthrough)")


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
) -> str:
    """Render the identity + policy + capability block for the system prompt.

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
        RULE_6_HONEST_LABEL,
    ]
    return "\n".join(blocks)
