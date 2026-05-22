"""KR-MCP-1 — multi-MCP registry + client manager (Phase 2, Feature 1).

Library-level package: Pydantic config models for N outbound MCP
endpoints + a lazy-connecting :class:`MCPClientPool` that routes
tool calls by prefix.

Public surface:
  - :class:`kora_mcp.registry.MCPEndpointConfig` — one endpoint
  - :class:`kora_mcp.registry.MCPRegistryConfig` — collection
  - :class:`kora_mcp.pool.MCPClientPool` — runtime
  - :class:`kora_mcp.pool.ToolDescriptor` — projected tool shape
  - :exc:`kora_mcp.pool.MCPToolNotAllowed` — allowlist rejection
  - :exc:`kora_mcp.pool.MCPCallFailed` — transport error mid-call
  - :func:`kora_mcp.routing.parse_qualified_tool_name` — splits
    ``"github__create_issue"`` → ``("github", "create_issue")``

# Package name (vs. bucket spec)

The KR-MCP-1 bucket spec proposed ``kora/mcp/registry.py``. The
repo root already has an executable script named ``kora`` (the CLI
launcher wrapping ``kora_cli.main``), and a directory at the same
path can't co-exist with the file on POSIX. The CLI launcher is a
Class B identifier per the K-DG taxonomy
(``kora_docs/00_canonical_current_state/identity_literal_taxonomy.md``)
— preserved permanently for operator muscle memory. This package
sits at ``kora_mcp/`` to mirror the existing ``kora_cli/`` naming
convention and avoid the collision.
"""
