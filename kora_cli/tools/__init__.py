"""Kora reasoning-callable tools — KR-EMAIL-OUTBOUND-COMPOSE-TOOL et al.

Modules in this package host the orchestration backing tools that
Kora invokes from her own reasoning loop (via the dispatcher in
``kora_cli/reasoning/tool_registry.py``). The MCP tool descriptors
+ JSON-RPC dispatchers live in ``kora_cli/listeners/mcp_tools.py``
— this package owns the actual behavior (file IO, rate limits,
audit emission) so the MCP wrapper stays a thin JSON-shape
adapter.
"""
