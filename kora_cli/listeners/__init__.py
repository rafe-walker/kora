"""Daemon listeners — KR-D-DAEMON ST2 + ST3.

Importing this package triggers import of each listener module, each
of which calls ``register_daemon_listener`` at module-import time so
the daemon coordinator finds them when it walks ``LISTENER_REGISTRY``.

Order matters for startup (FIFO) — heartbeat first (pure asyncio, no
external dependencies), then web (binds uvicorn on 9119 internal),
then mcp (routes mounted on the web app by import time; the
listener startup just affirms the bearer token is configured), then
webhooks (second uvicorn on 9118 PUBLIC — KR-D-DAEMON ST3, per the
R2 §5 amendment).
"""

from __future__ import annotations

# Order-of-imports = order in LISTENER_REGISTRY.
from kora_cli.listeners import heartbeat  # noqa: F401
from kora_cli.listeners import web  # noqa: F401
from kora_cli.listeners import mcp  # noqa: F401
from kora_cli.listeners import webhooks  # noqa: F401
# KR-MCP-CONSUMPTION ST1 — registers AFTER web/mcp so the pool
# accessor is available once the MCP-server routes go live. Lazy
# startup (no transport opens) means listener insertion here is
# fast + can't fail on remote-MCP availability.
from kora_cli.listeners import mcp_consumption  # noqa: F401
# KR-FEAT-HEARTBEAT ST1 — service-probe listener. Registers
# a heartbeat-scheduler task at module-import time. Probe-instance
# construction happens per cycle (stateless across cycles), so
# startup is a clean no-op + LOG line.
from kora_cli.listeners import heartbeat_probes_listener  # noqa: F401
# KR-FEAT-AI-RESPONSE-LOOP ST2 — reasoning engine listener.
# Constructs the AnthropicReasoningEngine at daemon startup;
# fail-CLOSED on missing creds / missing system prompt (coordinator
# aborts boot). Module-level `current_reasoning_engine()` accessor
# mirrors `current_pool()` so SlackDMHandler reads cross-cuttingly.
from kora_cli.listeners import reasoning_engine_listener  # noqa: F401
# KR-MCP-SEND-TOOLS — promote SlackClient + PurelymailClient from
# per-handler lazy construction to daemon-coordinator-managed
# singletons. Both fail-soft on missing auth env (Slack outbound /
# email outbound are capabilities, not gates — daemon boots
# without them). Imported AFTER mcp_consumption so the same lazy-
# constructed-fallback pattern is established.
from kora_cli.listeners import slack_client_listener  # noqa: F401
from kora_cli.listeners import purelymail_client_listener  # noqa: F401
