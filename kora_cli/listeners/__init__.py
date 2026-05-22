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
