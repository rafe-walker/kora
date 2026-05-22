"""Pydantic config models for the multi-MCP registry (KR-MCP-1 ST1).

Loads from ``~/.kora/config.yaml`` under the top-level ``mcp_clients``
key. Distinct from ``mcp_servers`` (Kora's outward-facing MCP
server — see ``agent/transports/kora_tools_mcp_server.py``); this
key represents Kora-AS-CLIENT outbound to external MCP endpoints
(github, cloudflare, etc.).

# K-DG drift discipline

Both models set ``extra="forbid"`` — unknown YAML keys raise rather
than being silently dropped. Catches config typos at load time.
Matches the convention IsoKron uses
(``plugins/memory/isokron/config.py:54``).
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class MCPEndpointConfig(BaseModel):
    """One outbound MCP endpoint.

    Attributes:
        name: Routing prefix — used by callers to address tools at
            this endpoint as ``"<name>__<tool>"`` (e.g.
            ``"github__create_issue"``). Must be unique within a
            registry.
        transport: ``"stdio"`` (subprocess) or ``"streamable_http"``
            (HTTP endpoint).
        endpoint: For ``stdio``: command line, parsed via
            :func:`shlex.split` (e.g.
            ``"npx -y @modelcontextprotocol/server-github"``).
            For ``streamable_http``: full URL.
        auth_token_env: Optional env var name whose value is the
            bearer token. For HTTP transports, injected as
            ``Authorization: Bearer <token>`` header. For stdio,
            injected into the subprocess env. ``None`` = no auth.
        allowed_tools_regex: Optional regex (case-insensitive by
            default per §4 Q2 ruling). When set, :meth:`call_tool`
            rejects tool names that don't match. ``None`` = all
            tools allowed. Match is against the bare ``tool_name``
            (without prefix), per :func:`re.search` semantics.
        timeout_seconds: Per-call timeout for ``call_tool`` /
            ``list_tools`` invocations.
        startup_timeout_seconds: Timeout for the initial
            connection open (transport handshake + session init).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    transport: Literal["stdio", "streamable_http"]
    endpoint: str = Field(min_length=1)
    auth_token_env: Optional[str] = None
    allowed_tools_regex: Optional[str] = None
    timeout_seconds: float = Field(default=30.0, gt=0)
    startup_timeout_seconds: float = Field(default=60.0, gt=0)


class MCPRegistryConfig(BaseModel):
    """Collection of :class:`MCPEndpointConfig` entries.

    Loaded from the ``mcp_clients`` key of ``~/.kora/config.yaml``:

    .. code-block:: yaml

        mcp_clients:
          endpoints:
            - name: github
              transport: stdio
              endpoint: "npx -y @modelcontextprotocol/server-github"
              auth_token_env: KORA_MCP_GITHUB_TOKEN
            - name: cloudflare
              transport: streamable_http
              endpoint: "https://mcp.cloudflare.com/..."
              auth_token_env: KORA_MCP_CLOUDFLARE_TOKEN

    Empty default — a registry with zero endpoints is valid (an
    operator who hasn't configured any external MCPs yet).
    """

    model_config = ConfigDict(extra="forbid")

    endpoints: list[MCPEndpointConfig] = Field(default_factory=list)

    def get_endpoint(self, name: str) -> Optional[MCPEndpointConfig]:
        """Look up an endpoint by ``name`` (routing prefix)."""
        for endpoint in self.endpoints:
            if endpoint.name == name:
                return endpoint
        return None
