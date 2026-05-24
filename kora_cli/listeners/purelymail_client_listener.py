"""PurelymailClient daemon listener (KR-MCP-SEND-TOOLS).

Promotes :class:`kora_cli.clients.purelymail_client.PurelymailClient`
from per-call construction (via the ``send_email_internal`` helper)
to a daemon-coordinator-managed singleton. The
``kora__send_email`` MCP tool consumes the singleton via
:func:`current_purelymail_client`.

# Fail-soft startup

If ``KORA_PUREMAIL_SMTP_USERNAME`` or
``KORA_PUREMAIL_SMTP_APP_PASSWORD`` is unset / empty,
:class:`PurelymailClient` raises :class:`PurelymailConfigError`.
The listener catches that + leaves the singleton as ``None`` —
daemon boot doesn't fail. The ``kora__send_email`` MCP tool
returns -32001 ``purelymail_client_unavailable`` when the
singleton is missing.

# Why this listener doesn't validate the from-domain allowlist at startup

``KORA_EMAIL_KORA_ALLOWED_FROM_DOMAINS`` is consulted PER SEND
inside :meth:`PurelymailClient.send_email` (not at construction).
Listener startup intentionally doesn't check it — operators can
set the allowlist after the daemon starts; the next send will
either succeed or raise :class:`PurelymailConfigError` with a
clear message.
"""

from __future__ import annotations

import logging
from typing import Optional

from agent.background_daemon_registry import (
    BackgroundDaemonEntry,
    background_daemon_registry,
)
from kora_cli.daemon import DEFAULT_SHUTDOWN_TIMEOUT, register_daemon_listener

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level singleton + accessor
# ---------------------------------------------------------------------------


_purelymail_client_singleton: Optional["object"] = None


def _set_singleton(client: object) -> None:
    global _purelymail_client_singleton
    _purelymail_client_singleton = client


def _clear_singleton() -> None:
    global _purelymail_client_singleton
    _purelymail_client_singleton = None


def current_purelymail_client() -> Optional["object"]:
    """Return the live :class:`PurelymailClient`, or ``None``.

    ``None`` cases:
      - Daemon not running
      - Listener registered but not yet started
      - Listener started but SMTP auth env unset → construction
        skipped + singleton stays ``None`` (fail-soft)
      - Listener stopped (post-shutdown)
    """
    return _purelymail_client_singleton


# ---------------------------------------------------------------------------
# Listener
# ---------------------------------------------------------------------------


class PurelymailClientListener:
    """Holds the live :class:`PurelymailClient`."""

    async def startup(self, coordinator=None) -> None:
        """Try to construct a PurelymailClient; fail-soft if env unset.

        The daemon boots regardless — outbound email is a capability,
        not a gate. Operators can enable email later by setting the
        SMTP auth envs + restarting.
        """
        try:
            from kora_cli.clients.purelymail_client import (
                PurelymailClient,
                PurelymailConfigError,
            )

            try:
                client = PurelymailClient()
            except PurelymailConfigError as exc:
                logger.info(
                    "[kora.purelymail_client_listener] SMTP auth env "
                    "unset — Purelymail outbound disabled (%s)",
                    exc,
                )
                _clear_singleton()
                return
        except Exception as exc:
            logger.warning(
                "[kora.purelymail_client_listener] startup raised %r — "
                "Purelymail outbound disabled",
                exc,
            )
            _clear_singleton()
            return

        _set_singleton(client)
        logger.info(
            "[kora.purelymail_client_listener] PurelymailClient "
            "constructed (host=%s port=%s); ready for outbound sends",
            getattr(client, "_host", "<unknown>"),
            getattr(client, "_port", "<unknown>"),
        )

    async def shutdown(self) -> None:
        """Clear the singleton. PurelymailClient has no persistent
        transport — each ``send_email`` opens a fresh SMTP
        connection per the ST1 design."""
        _clear_singleton()
        logger.info(
            "[kora.purelymail_client_listener] PurelymailClient cleared"
        )


# ---------------------------------------------------------------------------
# Factory + registration (import-time side effect)
# ---------------------------------------------------------------------------


# Process-wide singleton — KR-DAEMON-LISTENERS-VIA-GATEWAY Phase 3.
# Singleton-holder shape: both registries point at the same instance
# so the cross-cutting current_purelymail_client() accessor returns
# the same client object regardless of which consumer ran startup.
# Note: this listener is for OUTBOUND SMTP (not the IMAP poller —
# that's email_inbound_imap_listener.py per CC#3's #199 clarification).
_listener_singleton = PurelymailClientListener()


def _factory():
    return (
        _listener_singleton.startup,
        _listener_singleton.shutdown,
        DEFAULT_SHUTDOWN_TIMEOUT,
    )


register_daemon_listener("purelymail_client", _factory)


# ---------------------------------------------------------------------------
# Hermes-side registration (Phase 3; Path B thin-shim same as snapshot #196)
# ---------------------------------------------------------------------------
# No periodic_task — outbound SMTP is event-driven (other code paths
# call current_purelymail_client() to send; no scheduled work owned
# by this listener). The Hermes entry carries only the lifecycle hooks.

_hermes_entry = BackgroundDaemonEntry(
    name="purelymail_client",
    startup=_listener_singleton.startup,
    shutdown=_listener_singleton.shutdown,
    periodic_task=None,
    shutdown_timeout=DEFAULT_SHUTDOWN_TIMEOUT,
    plugin_name="kora",
)

try:
    background_daemon_registry().register(_hermes_entry)
except ValueError as _exc:
    logger.debug(
        "[kora.purelymail_client_listener] hermes registry already had "
        "'purelymail_client' entry: %s — skipping duplicate registration",
        _exc,
    )
