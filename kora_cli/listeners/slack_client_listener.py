"""SlackClient daemon listener (KR-MCP-SEND-TOOLS).

Promotes :class:`kora_cli.clients.slack_client.SlackClient` from
per-handler lazy construction to a daemon-coordinator-managed
singleton. Both the Slack DM handler's reply path AND the
``kora__send_slack_dm`` MCP tool consume the same instance via
:func:`current_slack_client`.

# Why promote

Pre-KR-MCP-SEND-TOOLS, the Slack DM handler constructed its own
SlackClient lazily on the first reply. That worked when the handler
was the sole outbound caller. KR-MCP-SEND-TOOLS adds an MCP tool
that also needs to call ``post_dm``; rather than re-implementing
construction in the MCP layer, we centralize on a singleton.

# Fail-soft startup

If ``KORA_SLACK_BOT_TOKEN`` is unset (Slack outbound not yet
configured), :meth:`SlackClient.__init__` raises
``SlackClientNotConfigured``. The listener catches that + leaves
the singleton as ``None`` — daemon boot doesn't fail. Outbound
callers see ``current_slack_client() is None`` and act
accordingly (handler logs WARN + records failed-outbound entry;
MCP tool returns -32001 ``slack_client_unavailable``).

# Backwards compat

The Slack DM handler's :func:`_get_or_create_slack_client` checks
the listener accessor FIRST + falls back to lazy construct if the
listener isn't running. Standalone-handler test fixtures keep
working without daemon listeners. Production daemon paths get the
shared instance.
"""

from __future__ import annotations

import logging
from typing import Optional

from kora_cli.daemon import DEFAULT_SHUTDOWN_TIMEOUT, register_daemon_listener

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level singleton + accessor
# ---------------------------------------------------------------------------


_slack_client_singleton: Optional["object"] = None


def _set_singleton(client: object) -> None:
    global _slack_client_singleton
    _slack_client_singleton = client


def _clear_singleton() -> None:
    global _slack_client_singleton
    _slack_client_singleton = None


def current_slack_client() -> Optional["object"]:
    """Return the live :class:`SlackClient` instance, or ``None``.

    ``None`` cases:
      - Daemon not running
      - Listener registered but not yet started
      - Listener started but ``KORA_SLACK_BOT_TOKEN`` unset →
        construction skipped + singleton stays ``None`` (fail-soft)
      - Listener stopped (post-shutdown)
    """
    return _slack_client_singleton


# ---------------------------------------------------------------------------
# Listener
# ---------------------------------------------------------------------------


class SlackClientListener:
    """Holds the live :class:`SlackClient` for the daemon's lifetime."""

    async def startup(self) -> None:
        """Try to construct a SlackClient; fail-soft if env unset.

        The daemon boots regardless — Slack outbound is a capability,
        not a gate. Operators can enable Slack later by setting
        ``KORA_SLACK_BOT_TOKEN`` + restarting.
        """
        try:
            from kora_cli.clients.slack_client import (
                SlackClient,
                SlackClientNotConfigured,
            )

            try:
                client = SlackClient()
            except SlackClientNotConfigured as exc:
                logger.info(
                    "[kora.slack_client_listener] KORA_SLACK_BOT_TOKEN "
                    "unset — Slack outbound disabled (%s)",
                    exc,
                )
                _clear_singleton()
                return
        except Exception as exc:
            # Import-side error — log + leave singleton None.
            logger.warning(
                "[kora.slack_client_listener] startup raised %r — "
                "Slack outbound disabled",
                exc,
            )
            _clear_singleton()
            return

        _set_singleton(client)
        logger.info(
            "[kora.slack_client_listener] SlackClient constructed; "
            "ready for outbound sends"
        )

    async def shutdown(self) -> None:
        """Clear the singleton. SlackClient has no transport state
        to close (httpx clients are created per-call inside
        ``post_dm``)."""
        _clear_singleton()
        logger.info("[kora.slack_client_listener] SlackClient cleared")


# ---------------------------------------------------------------------------
# Factory + registration (import-time side effect)
# ---------------------------------------------------------------------------


def _factory():
    listener = SlackClientListener()
    return (listener.startup, listener.shutdown, DEFAULT_SHUTDOWN_TIMEOUT)


register_daemon_listener("slack_client", _factory)
