"""IsoKronMemoryProvider — Kora's substrate-backed MemoryProvider.

KR-2 ST1 ships this as a structural skeleton: all 15 hooks from the
``MemoryProvider`` ABC are overridden, but every non-trivial method
raises ``NotImplementedError`` with a Rule-6 honest log message.
Subsequent sub-tasks fill in:

* **ST2** — read paths: Role Charter, capability matrix Kora row,
  policy registry. Wires ``initialize`` / ``on_turn_start`` /
  ``system_prompt_block``.
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

import logging
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

from .config import ISOKRON_CONFIG_SCHEMA, IsoKronProviderConfig
from .connection import IsoKronConnection

logger = logging.getLogger(__name__)


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

    # -- Stubs (KR-2 ST2-ST4 fill these) -------------------------------------

    def system_prompt_block(self) -> str:
        raise _not_yet_implemented("system_prompt_block", "ST2")

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        raise _not_yet_implemented("prefetch", "ST2")

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        raise _not_yet_implemented("queue_prefetch", "ST2")

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
        raise _not_yet_implemented("on_turn_start", "ST2")

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
        raise _not_yet_implemented("save_config", "ST2")
