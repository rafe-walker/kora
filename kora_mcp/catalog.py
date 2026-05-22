"""Default MCP catalog + health helpers (KR-MCP-1 ST2).

Ships the initial pair of endpoints Joshua will use:

  - **github** (stdio via npx) — the upstream
    ``@modelcontextprotocol/server-github`` package. Per PM ruling
    on KR-MCP-1 §4 Q1 (2026-05): no first-party hosted
    streamable_http endpoint is GA yet; defaulting to stdio.
  - **cloudflare** (streamable_http) — Cloudflare's hosted MCP
    gateway. Operators may override the URL per workspace via
    ``mcp_clients`` in ``~/.kora/config.yaml``.

Auth tokens come from Doppler-injected env vars
(``KORA_MCP_GITHUB_TOKEN`` / ``KORA_MCP_CLOUDFLARE_TOKEN``). The
catalog DOES NOT crash when these are unset — it marks the
endpoint unhealthy via :class:`EndpointHealth` so the operator
sees the gap in ``kora mcp clients list`` rather than getting a
crash when the daemon (or future agent loop) tries to use the
pool.

# Operator override

Anything in :data:`DEFAULT_CATALOG` is a SUGGESTED default. To
override (different endpoint URL, different auth env var, custom
allowlist regex), populate ``~/.kora/config.yaml`` with:

.. code-block:: yaml

    mcp_clients:
      endpoints:
        - name: github
          transport: stdio
          endpoint: "npx -y @modelcontextprotocol/server-github"
          auth_token_env: KORA_MCP_GITHUB_TOKEN
          allowed_tools_regex: "^(create_|list_).*$"
        - name: cloudflare
          transport: streamable_http
          endpoint: "https://mcp.cloudflare.com/sse"
          auth_token_env: KORA_MCP_CLOUDFLARE_TOKEN

The :func:`load_catalog` helper reads that key + merges with the
defaults below (operator entries take precedence by name).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from kora_mcp.registry import MCPEndpointConfig, MCPRegistryConfig


# ---------------------------------------------------------------------------
# Default catalog
# ---------------------------------------------------------------------------


GITHUB_AUTH_ENV = "KORA_MCP_GITHUB_TOKEN"
CLOUDFLARE_AUTH_ENV = "KORA_MCP_CLOUDFLARE_TOKEN"


DEFAULT_GITHUB_ENDPOINT = MCPEndpointConfig(
    name="github",
    transport="stdio",
    endpoint="npx -y @modelcontextprotocol/server-github",
    auth_token_env=GITHUB_AUTH_ENV,
    timeout_seconds=30.0,
    startup_timeout_seconds=60.0,
)


# Cloudflare's hosted MCP gateway. Operators with a workspace-
# specific gateway URL should override via config.yaml.
DEFAULT_CLOUDFLARE_ENDPOINT = MCPEndpointConfig(
    name="cloudflare",
    transport="streamable_http",
    endpoint="https://mcp.cloudflare.com/sse",
    auth_token_env=CLOUDFLARE_AUTH_ENV,
    timeout_seconds=30.0,
    startup_timeout_seconds=60.0,
)


DEFAULT_CATALOG: list[MCPEndpointConfig] = [
    DEFAULT_GITHUB_ENDPOINT,
    DEFAULT_CLOUDFLARE_ENDPOINT,
]


def default_registry() -> MCPRegistryConfig:
    """Return a fresh :class:`MCPRegistryConfig` with the default
    catalog. Useful for tests + the daemon's first-boot
    pre-config state."""
    return MCPRegistryConfig(endpoints=list(DEFAULT_CATALOG))


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EndpointHealth:
    """Per-endpoint health verdict, computed WITHOUT opening a
    connection.

    The pool's :meth:`MCPClientPool.has_open_connection` reports
    connection-level health; this dataclass reports
    CONFIG-level health (does the endpoint have an auth token in
    env, is its config sane, can it be expected to work).

    Attributes:
        name: Endpoint routing prefix.
        configured: ``True`` if the endpoint is present in the
            registry (always True when returned from
            :func:`check_endpoint_health` — the function takes the
            endpoint as input).
        auth_env_set: ``True`` if the ``auth_token_env`` env var
            is set to a non-empty value (when an auth env var is
            configured at all). ``True`` also when no auth env is
            configured (no token expected → no gap).
        reason: Operator-readable explanation of the health
            verdict. Empty string when ``healthy`` is True.
    """

    name: str
    configured: bool
    auth_env_set: bool
    reason: str

    @property
    def healthy(self) -> bool:
        return self.configured and self.auth_env_set


def check_endpoint_health(endpoint: MCPEndpointConfig) -> EndpointHealth:
    """Compute config-level health for one endpoint.

    Does NOT open a transport — purely reads the env to confirm
    the auth token is set when one is configured.
    """
    if endpoint.auth_token_env is None:
        return EndpointHealth(
            name=endpoint.name,
            configured=True,
            auth_env_set=True,
            reason="",
        )
    token = os.environ.get(endpoint.auth_token_env, "").strip()
    if not token:
        return EndpointHealth(
            name=endpoint.name,
            configured=True,
            auth_env_set=False,
            reason=(
                f"auth env var {endpoint.auth_token_env!r} is unset or "
                f"empty; endpoint will fail on first call"
            ),
        )
    return EndpointHealth(
        name=endpoint.name,
        configured=True,
        auth_env_set=True,
        reason="",
    )


def check_registry_health(
    registry: MCPRegistryConfig,
) -> list[EndpointHealth]:
    """Per-endpoint health for every entry in the registry."""
    return [check_endpoint_health(endpoint) for endpoint in registry.endpoints]


# ---------------------------------------------------------------------------
# Loading from config.yaml
# ---------------------------------------------------------------------------


def load_registry_from_config(
    config: dict,
    *,
    include_defaults: bool = True,
) -> MCPRegistryConfig:
    """Build a :class:`MCPRegistryConfig` from a parsed
    ``~/.kora/config.yaml`` dict.

    Args:
        config: The parsed YAML dict. Reads the ``mcp_clients`` key.
        include_defaults: When ``True`` (default), merges the
            :data:`DEFAULT_CATALOG` with operator overrides. An
            operator entry with the same ``name`` REPLACES the
            default (e.g. operator's ``github`` config wins). When
            ``False``, returns only operator entries (no defaults).

    Returns:
        A validated :class:`MCPRegistryConfig`. If ``config`` has
        no ``mcp_clients`` key or it's malformed, returns just the
        defaults (or empty if ``include_defaults=False``).

    Raises:
        pydantic.ValidationError: an operator entry has unknown
            keys or invalid values — surfaced at load time per
            K-DG drift discipline.
    """
    block = config.get("mcp_clients") if isinstance(config, dict) else None
    operator_endpoints: list[MCPEndpointConfig] = []
    if isinstance(block, dict):
        raw_endpoints = block.get("endpoints", [])
        if isinstance(raw_endpoints, list):
            for raw in raw_endpoints:
                if isinstance(raw, dict):
                    operator_endpoints.append(MCPEndpointConfig(**raw))

    if not include_defaults:
        return MCPRegistryConfig(endpoints=operator_endpoints)

    # Merge: operator entries by name take precedence over defaults
    operator_names = {endpoint.name for endpoint in operator_endpoints}
    merged: list[MCPEndpointConfig] = list(operator_endpoints)
    for default_endpoint in DEFAULT_CATALOG:
        if default_endpoint.name not in operator_names:
            merged.append(default_endpoint)
    return MCPRegistryConfig(endpoints=merged)


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def find_endpoint_by_name(
    registry: MCPRegistryConfig, name: str
) -> Optional[MCPEndpointConfig]:
    """Convenience wrapper for the CLI ``status <name>`` command."""
    return registry.get_endpoint(name)


def load_effective_catalog() -> MCPRegistryConfig:
    """One-call helper: read ``~/.kora/config.yaml`` + merge with defaults.

    Used by surfaces that need the live catalog without threading
    the config dict themselves (the ``/api/mcp/clients/list``
    endpoint in particular). Operator entries override defaults by
    name; new operator entries are appended. Pydantic
    ``extra="forbid"`` rejects unknown keys at load.

    Returns an empty :class:`MCPRegistryConfig` if the config file
    is unreadable — the endpoint surfaces "no clients configured"
    rather than crashing. Inner validation errors propagate per
    K-DG drift discipline (typos must surface, not silently load).
    """
    from kora_cli.config import load_config

    try:
        config = load_config() or {}
    except Exception:
        config = {}
    return load_registry_from_config(config, include_defaults=True)
