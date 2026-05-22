"""Daemon listeners — KR-D-DAEMON ST2.

Importing this package triggers import of each listener module, each
of which calls ``register_daemon_listener`` at module-import time so
the daemon coordinator finds them when it walks ``LISTENER_REGISTRY``.

Order matters for startup (FIFO) — heartbeat first (pure asyncio, no
external dependencies), then web (binds uvicorn on 9119 internal),
then mcp (routes already mounted on the web app by import time; the
listener startup just affirms the bearer token is configured).
"""

from __future__ import annotations

# Order-of-imports = order in LISTENER_REGISTRY.
from kora_cli.listeners import heartbeat  # noqa: F401
from kora_cli.listeners import web  # noqa: F401
from kora_cli.listeners import mcp  # noqa: F401
