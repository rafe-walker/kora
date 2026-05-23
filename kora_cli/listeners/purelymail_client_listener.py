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

    async def startup(self) -> None:
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


def _factory():
    listener = PurelymailClientListener()
    return (listener.startup, listener.shutdown, DEFAULT_SHUTDOWN_TIMEOUT)


register_daemon_listener("purelymail_client", _factory)
