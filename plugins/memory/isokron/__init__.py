"""IsoKron memory provider plugin entry point.

Discovery: ``plugins/memory/__init__.py`` scans this directory, sees
``MemoryProvider`` in this source file, and treats us as a bundled
memory provider plugin. The plugin loader calls our ``register(ctx)``;
``ctx.register_memory_provider`` stashes the instance.

Selected at runtime via::

    memory:
      provider: isokron
    plugins:
      enabled:
        - isokron
      entries:
        isokron:
          isokron_dsn: postgres://kora_runtime:${KORA_DB_PASSWORD}@db.isokron.local:5432/isokron
          mcp_endpoint: stdio://node ../isokron/packages/sea-mcp-server/dist/cli.js
          default_workspace_id: 00000000-0000-0000-0000-000000000001
          cache_ttl_seconds: 60

See ``README.md`` for the architecture (hybrid: PG reads, MCP writes)
and ``config.py`` for field-level documentation.
"""

from __future__ import annotations

import logging
import os
import re
import sys as _sys
from pathlib import Path as _Path
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# isokron-client sys.path bootstrap (mirrors the plugins/marvin/__init__.py
# pattern from #204). After ``pip install ./packages/isokron-client`` this
# block is a no-op — ``import isokron_client`` already resolves via site-
# packages and the duplicate sys.path entry is skipped by the membership
# check. Required only for in-tree dev where the package isn't installed.
# ---------------------------------------------------------------------------
_ISOKRON_CLIENT_SRC = (
    _Path(__file__).resolve().parents[2]
    / "packages"
    / "isokron-client"
    / "src"
)
if _ISOKRON_CLIENT_SRC.is_dir() and str(_ISOKRON_CLIENT_SRC) not in _sys.path:
    _sys.path.insert(0, str(_ISOKRON_CLIENT_SRC))

from agent.memory_provider import MemoryProvider  # noqa: F401,E402  — surfaces us to plugin discovery
from kora_cli.config import cfg_get  # noqa: E402

from .provider import IsoKronMemoryProvider  # noqa: E402

# ---------------------------------------------------------------------------
# Backward-compat re-exports — moved modules at packages/isokron-client/src/
# isokron_client/. After the KR-KORA-PIP-RESTRUCTURE-PHASE-1 extraction the
# substrate-functional code lives in the ``isokron_client`` package. Existing
# Kora-internal imports of the form ``from plugins.memory.isokron.<name>
# import X`` continue to work via the sys.modules aliasing below; the
# attribute-style ``from plugins.memory.isokron import <name>`` works via
# the explicit local bindings. New code SHOULD prefer ``from isokron_client.
# <name> import X``.
# ---------------------------------------------------------------------------
import isokron_client as _isokron_client  # noqa: E402

_MOVED_MODULES = (
    "assigned_sea_tickets",
    "cache",
    "capability_check",
    "capability_matrix_mirror",
    "claim_heartbeat",
    "config",
    "connection",
    "constitution",
    "cost_deferred_tickets",
    "dr_epoch",
    "events",
    "kora_control_reader",
    "kora_operation_ledger",
    "mcp_client",
    "models",
    "observed_kora_control",
    "reads",
    "relationlink",
    "scratchpad",
    "session_context",
)
for _name in _MOVED_MODULES:
    _mod = getattr(_isokron_client, _name)
    # Register under the old dotted name so ``from plugins.memory.isokron.<name>
    # import X`` keeps working without per-file shim modules. The same module
    # object is also bound as a package attribute so ``from plugins.memory.
    # isokron import <name>`` resolves.
    _sys.modules[f"{__name__}.{_name}"] = _mod
    globals()[_name] = _mod
del _name, _mod, _isokron_client

logger = logging.getLogger(__name__)


_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _expand_env_vars(value: Any) -> Any:
    """Expand ``${VAR}`` references in string config values.

    Mirrors the convention used by other Hermes-inherited plugins
    (Honcho, Mem0, etc.). Missing env vars are left as the literal
    ``${VAR}`` token so pydantic validation surfaces a clear error
    ("isokron_dsn must be a postgres:// URI; got '${KORA_DB_PASSWORD}'").
    """
    if isinstance(value, str):
        return _ENV_VAR_PATTERN.sub(
            lambda m: os.environ.get(m.group(1), m.group(0)),
            value,
        )
    if isinstance(value, dict):
        return {k: _expand_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env_vars(v) for v in value]
    return value


def _load_plugin_config() -> Optional[Dict[str, Any]]:
    """Read ``plugins.entries.isokron`` from config.yaml, env-expanded."""
    try:
        from kora_cli.config import load_config

        config = load_config()
    except Exception as exc:
        logger.warning(
            "[kora.isokron] could not load config.yaml (%s); the plugin "
            "is registered but unavailable until config is fixed.",
            exc,
        )
        return None

    raw = cfg_get(config, "plugins", "entries", "isokron")
    if not isinstance(raw, dict) or not raw:
        logger.debug(
            "[kora.isokron] no plugins.entries.isokron block in "
            "config.yaml — provider registered but is_available will "
            "return False."
        )
        return None

    return _expand_env_vars(raw)


_last_active_provider: Optional["IsoKronMemoryProvider"] = None
"""Process-level reference to the most recently registered IsoKron provider.

Mirrors the openviking plugin's same-named singleton (see
``plugins/memory/openviking/__init__.py:56``). Set in :func:`register`.
Read by out-of-band surfaces that want to inspect the live provider
without owning a reference — e.g. the KR-P2-CHARTER-PANEL
``/api/charter`` endpoint reaches in here to read
``IsoKronMemoryProvider.get_active_constitution_summary`` against the
already-primed cache.
"""


def get_last_active_provider() -> Optional["IsoKronMemoryProvider"]:
    """Return the most recently registered IsoKron provider, or ``None``.

    Returns ``None`` in environments where the plugin hasn't been
    registered (CI without substrate config, dev runs with a different
    memory provider selected, etc.). Callers MUST handle the None case
    gracefully — typically by surfacing "no Constitution state available"
    rather than failing the request.
    """
    return _last_active_provider


def register(ctx) -> None:
    """Register the IsoKron memory provider with the plugin system."""
    global _last_active_provider
    config = _load_plugin_config()
    provider = IsoKronMemoryProvider(config=config)
    ctx.register_memory_provider(provider)
    _last_active_provider = provider


__all__ = [
    "IsoKronMemoryProvider",
    "get_last_active_provider",
    "register",
]
