"""IMAP poll listener (KR-FEAT-EMAIL-INBOUND-IMAP ST1).

Registers a periodic task with the heartbeat scheduler that
runs once per ``KORA_EMAIL_IMAP_POLL_INTERVAL_SEC`` (default
300s) seconds:

  1. Open IMAP connection (or skip cleanly if client is unavailable)
  2. SEARCH UNSEEN → FETCH each → parse → emit to downstream handler
  3. Close connection

ST2 wires :class:`EmailInboundHandler` in place of ST1's stub.
The handler returns a :class:`HandlerResult`; this listener calls
:meth:`PurelymailIMAPClient.mark_seen` for each UID whose result
sets ``should_mark_seen=True`` — handler errors keep the UID
UNSEEN for next-poll retry.

# Fail-soft startup

Same pattern as :mod:`purelymail_client_listener`: if the IMAP
client can't be constructed (missing env), the listener leaves
the singleton ``None`` + daemon boots regardless. The periodic
task short-circuits cleanly each cycle until the operator sets
the envs + restarts.

# Module-level singleton + accessor

:func:`current_imap_client` mirrors :func:`current_purelymail_client`
so ST2's handler / future MCP tools can resolve the live client
without import-time coupling to this listener.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from kora_cli.daemon import DEFAULT_SHUTDOWN_TIMEOUT, register_daemon_listener
from kora_cli.listeners.heartbeat import register_periodic_task

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cadence config
# ---------------------------------------------------------------------------


DEFAULT_POLL_INTERVAL_SEC: float = 300.0  # 5 min per §4 Q3 default
POLL_INTERVAL_ENV: str = "KORA_EMAIL_IMAP_POLL_INTERVAL_SEC"


def _read_poll_interval() -> float:
    """Resolve poll cadence from env with sane fallback.

    Invalid values (non-numeric, <=0) log WARN + fall back to
    the 300s default — same shape as
    :func:`kora_cli.listeners.mcp_consumption._read_health_check_interval`.
    """
    raw = os.environ.get(POLL_INTERVAL_ENV, "").strip()
    if not raw:
        return DEFAULT_POLL_INTERVAL_SEC
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "[kora.email_inbound_imap] %s=%r is not numeric; using "
            "default %ss",
            POLL_INTERVAL_ENV,
            raw,
            DEFAULT_POLL_INTERVAL_SEC,
        )
        return DEFAULT_POLL_INTERVAL_SEC
    if value <= 0:
        logger.warning(
            "[kora.email_inbound_imap] %s=%s must be > 0; using "
            "default %ss",
            POLL_INTERVAL_ENV,
            value,
            DEFAULT_POLL_INTERVAL_SEC,
        )
        return DEFAULT_POLL_INTERVAL_SEC
    return value


# ---------------------------------------------------------------------------
# Module-level singleton + accessor
# ---------------------------------------------------------------------------


_imap_client_singleton: Optional["object"] = None


def _set_singleton(client: object) -> None:
    global _imap_client_singleton
    _imap_client_singleton = client


def _clear_singleton() -> None:
    global _imap_client_singleton
    _imap_client_singleton = None


def current_imap_client() -> Optional["object"]:
    """Return the live :class:`PurelymailIMAPClient`, or ``None``.

    ``None`` cases mirror :func:`current_purelymail_client`:
      - Daemon not running
      - Listener registered but not yet started
      - Listener started but IMAP auth env unset → construction
        skipped + singleton stays ``None`` (fail-soft)
      - Listener stopped (post-shutdown)
    """
    return _imap_client_singleton


# ---------------------------------------------------------------------------
# Periodic poll cycle
# ---------------------------------------------------------------------------


async def run_poll_cycle() -> None:
    """One poll: connect → fetch unseen → handle per-message → close.

    Per-cycle failure (transport, parse) is captured + logged —
    never crashes the heartbeat scheduler. The connection is
    rebuilt each cycle so a transient failure resets cleanly
    without long-lived stale-connection retry pathology.

    Per-message: invokes :meth:`EmailInboundHandler.handle_event`.
    When the returned :class:`HandlerResult` sets
    ``should_mark_seen=True`` we call ``client.mark_seen(uid)``;
    handler errors leave the UID UNSEEN for next-poll retry.
    """
    client = current_imap_client()
    if client is None:
        logger.debug(
            "[kora.email_inbound_imap] poll skipped: no active IMAP client"
        )
        return

    from kora_cli.clients.purelymail_imap_client import PurelymailIMAPError
    from kora_cli.handlers.email_inbound_handler import EmailInboundHandler

    try:
        await client.connect()
    except PurelymailIMAPError as exc:
        logger.warning(
            "[kora.email_inbound_imap] connect failed; skipping cycle: %s",
            exc,
        )
        return
    except Exception as exc:
        logger.warning(
            "[kora.email_inbound_imap] unexpected connect failure %r — "
            "skipping cycle",
            exc,
        )
        return

    handler = EmailInboundHandler()

    try:
        try:
            unseen = await client.fetch_unseen()
        except PurelymailIMAPError as exc:
            logger.warning(
                "[kora.email_inbound_imap] fetch_unseen failed; skipping "
                "cycle: %s",
                exc,
            )
            return

        if not unseen:
            logger.debug("[kora.email_inbound_imap] poll: no unseen messages")
            return

        logger.info(
            "[kora.email_inbound_imap] poll found %d unseen message(s)",
            len(unseen),
        )

        for parsed in unseen:
            try:
                result = await handler.handle_event(parsed)
            except Exception as exc:
                logger.warning(
                    "[kora.email_inbound_imap] handler raised %r for "
                    "uid=%d — keeping UNSEEN for next-poll retry",
                    exc,
                    parsed.imap_uid,
                )
                continue

            if result.should_mark_seen:
                try:
                    await client.mark_seen(parsed.imap_uid)
                except Exception as exc:
                    logger.warning(
                        "[kora.email_inbound_imap] mark_seen uid=%d "
                        "failed: %r — message stays UNSEEN, handler "
                        "result already logged",
                        parsed.imap_uid,
                        exc,
                    )
    finally:
        try:
            await client.close()
        except Exception as exc:
            logger.warning(
                "[kora.email_inbound_imap] close raised %r — proceeding",
                exc,
            )


# ---------------------------------------------------------------------------
# Listener
# ---------------------------------------------------------------------------


class EmailInboundIMAPListener:
    """Holds the live :class:`PurelymailIMAPClient`.

    Same fail-soft pattern as :class:`PurelymailClientListener`:
    construction errors on missing auth env leave the singleton
    ``None`` rather than aborting daemon boot.
    """

    async def startup(self) -> None:
        try:
            from kora_cli.clients.purelymail_imap_client import (
                PurelymailIMAPClient,
                PurelymailIMAPConfigError,
            )

            try:
                client = PurelymailIMAPClient()
            except PurelymailIMAPConfigError as exc:
                logger.info(
                    "[kora.email_inbound_imap_listener] IMAP auth env "
                    "unset — Purelymail inbound disabled (%s)",
                    exc,
                )
                _clear_singleton()
                return
        except Exception as exc:
            logger.warning(
                "[kora.email_inbound_imap_listener] startup raised %r — "
                "Purelymail inbound disabled",
                exc,
            )
            _clear_singleton()
            return

        _set_singleton(client)
        logger.info(
            "[kora.email_inbound_imap_listener] PurelymailIMAPClient "
            "constructed (host=%s port=%s); poll cadence=%ss",
            getattr(client, "_host", "<unknown>"),
            getattr(client, "_port", "<unknown>"),
            _read_poll_interval(),
        )

    async def shutdown(self) -> None:
        """Drop the singleton; best-effort close of any open IMAP handle."""
        client = _imap_client_singleton
        _clear_singleton()
        if client is not None:
            try:
                await client.close()
            except Exception as exc:
                logger.warning(
                    "[kora.email_inbound_imap_listener] close raised %r "
                    "during shutdown — proceeding",
                    exc,
                )
        logger.info(
            "[kora.email_inbound_imap_listener] PurelymailIMAPClient cleared"
        )


# ---------------------------------------------------------------------------
# Factory + registration (import-time side effect)
# ---------------------------------------------------------------------------


def _factory():
    listener = EmailInboundIMAPListener()
    return (listener.startup, listener.shutdown, DEFAULT_SHUTDOWN_TIMEOUT)


register_daemon_listener("email_inbound_imap", _factory)


# ---------------------------------------------------------------------------
# Periodic poll registration (import-time side effect)
# ---------------------------------------------------------------------------
#
# Mirrors mcp_consumption's health-check registration — the
# heartbeat scheduler owns the asyncio.Task; we just register the
# callable + cadence. Cadence is read once at module-import time
# per the §4 Q3 default (300s; KORA_EMAIL_IMAP_POLL_INTERVAL_SEC
# override). Restart-driven cadence-config refresh.

register_periodic_task(
    "email.imap_poll",
    interval_seconds=_read_poll_interval(),
    callable=run_poll_cycle,
)
