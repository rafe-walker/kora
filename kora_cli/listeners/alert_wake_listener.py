"""Alert wake-event listener — KR-ALERT-INVESTIGATION-WAKE-CONSUMER.

Periodic task that tails ``${KORA_HOME}/kora_audit_log.jsonl`` for
fresh ``notification.dispatched`` rows (emitted by AlertNotifier
#149) and feeds each one to :class:`AlertWakeConsumer`.

Tail strategy mirrors :mod:`kora_cli.listeners.probe_wake_listener`
(#166): cron-driven, NOT inotify; first-tick-after-startup stamps
``_last_seen_at`` to NOW so historical dispatches don't replay.

# Tail filter

The consumer itself filters aggregate rows (burst_summary /
digest_email) + non-ok status rows — this listener pulls every
``notification.dispatched`` entry and the consumer's
``consume_alert_event`` short-circuits the ones it shouldn't act
on. Keeps the tail logic simple + lets the consumer-side filter
remain a single source of truth for "what counts as an
investigatable alert."

# Cadence

``KORA_ALERT_WAKE_POLL_SEC`` (default 30s; same as probe wake
listener for operator-grep parity in the boot logs).

# Fail-soft

Wraps the consumer call in try/except; single-event failures
inside ``consume_alert_event`` are caught by the consumer itself
per its own contract, and the listener wrapper guards against
anything that escapes (e.g. an unexpected import-time failure
inside a lazily-loaded helper).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from kora_cli.alerts.wake_consumer import AlertWakeConsumer
from kora_cli.daemon import DEFAULT_SHUTDOWN_TIMEOUT, register_daemon_listener
from kora_cli.listeners.heartbeat import register_periodic_task

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cadence config
# ---------------------------------------------------------------------------


DEFAULT_POLL_SEC: float = 30.0
POLL_SEC_ENV: str = "KORA_ALERT_WAKE_POLL_SEC"


def _read_poll_sec() -> float:
    raw = os.environ.get(POLL_SEC_ENV, "").strip()
    if not raw:
        return DEFAULT_POLL_SEC
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "[kora.alert_wake_listener] %s=%r is not numeric; using "
            "default %ss",
            POLL_SEC_ENV,
            raw,
            DEFAULT_POLL_SEC,
        )
        return DEFAULT_POLL_SEC
    if value <= 0:
        return DEFAULT_POLL_SEC
    return value


# ---------------------------------------------------------------------------
# Singleton + tail-position state (mirrors probe_wake_listener)
# ---------------------------------------------------------------------------


_consumer_singleton: Optional[AlertWakeConsumer] = None
_last_seen_at: Optional[datetime] = None


def current_alert_wake_consumer() -> Optional[AlertWakeConsumer]:
    """Read-only accessor for tests + introspection."""
    return _consumer_singleton


def _set_consumer(consumer: AlertWakeConsumer) -> None:
    global _consumer_singleton
    _consumer_singleton = consumer


def _clear_consumer() -> None:
    global _consumer_singleton, _last_seen_at
    _consumer_singleton = None
    _last_seen_at = None


# ---------------------------------------------------------------------------
# Lazy factories for reasoning engine + Slack client
# ---------------------------------------------------------------------------


def _reasoning_engine_factory() -> Optional[Any]:
    try:
        from kora_cli.listeners.reasoning_engine_listener import (
            current_reasoning_engine,
        )
    except Exception:
        return None
    return current_reasoning_engine()


def _slack_client_factory() -> Optional[Any]:
    try:
        from kora_cli.listeners.slack_client_listener import (
            current_slack_client,
        )
    except Exception:
        return None
    return current_slack_client()


# ---------------------------------------------------------------------------
# Periodic-task entry
# ---------------------------------------------------------------------------


async def run_tail_cycle() -> None:
    """One tail tick: pick up fresh notification.dispatched rows +
    hand them to the consumer.

    Fail-soft: any exception inside (audit read, consumer raise,
    etc.) is caught + logged so the heartbeat scheduler keeps
    ticking.
    """
    global _last_seen_at
    consumer = current_alert_wake_consumer()
    if consumer is None:
        logger.debug(
            "[kora.alert_wake_listener] tick skipped: no active consumer"
        )
        return

    now = datetime.now(timezone.utc)
    if _last_seen_at is None:
        _last_seen_at = now
        logger.info(
            "[kora.alert_wake_listener] tail-position stamped at boot "
            "(no replay of prior notification dispatches)"
        )
        return

    try:
        from kora_cli.audit.jsonl_reader import read_audit_entries
    except Exception as exc:
        logger.warning(
            "[kora.alert_wake_listener] audit reader import failed: %r",
            exc,
        )
        return

    try:
        rows = read_audit_entries(
            seam="notification.dispatched",
            since=_last_seen_at,
        )
    except Exception as exc:
        logger.warning(
            "[kora.alert_wake_listener] read_audit_entries raised %r",
            exc,
        )
        return

    if not rows:
        return

    # Reader returns newest-first; reverse so consume + last_seen_at
    # advance monotonically (probe wake listener pattern).
    rows_chronological = list(reversed(rows))

    new_max_ts = _last_seen_at
    for row in rows_chronological:
        try:
            await consumer.consume_alert_event(
                getattr(row, "details", {}) or {}
            )
        except Exception as exc:
            logger.warning(
                "[kora.alert_wake_listener] consume_alert_event raised "
                "%r — continuing past this event",
                exc,
            )
        emitted_at = getattr(row, "emitted_at", None)
        if isinstance(emitted_at, datetime):
            if emitted_at.tzinfo is None:
                emitted_at = emitted_at.replace(tzinfo=timezone.utc)
            if emitted_at > new_max_ts:
                new_max_ts = emitted_at

    _last_seen_at = new_max_ts


# ---------------------------------------------------------------------------
# Listener lifecycle
# ---------------------------------------------------------------------------


class AlertWakeListener:
    """Holds the live :class:`AlertWakeConsumer` for the daemon
    lifetime. Periodic task picks up fresh notification rows on
    each tick + hands them to the consumer.
    """

    async def startup(self) -> None:
        try:
            consumer = AlertWakeConsumer(
                reasoning_engine_factory=_reasoning_engine_factory,
                slack_client_factory=_slack_client_factory,
            )
        except Exception as exc:
            logger.warning(
                "[kora.alert_wake_listener] startup raised %r — "
                "consumer disabled this run",
                exc,
            )
            _clear_consumer()
            return

        _set_consumer(consumer)
        logger.info(
            "[kora.alert_wake_listener] AlertWakeConsumer constructed; "
            "poll cadence=%ss",
            _read_poll_sec(),
        )

    async def shutdown(self) -> None:
        consumer = _consumer_singleton
        if consumer is not None:
            consumer.reset_debounce_state()
        _clear_consumer()
        logger.info(
            "[kora.alert_wake_listener] AlertWakeConsumer cleared"
        )


# ---------------------------------------------------------------------------
# Factory + registration (import-time side effect)
# ---------------------------------------------------------------------------


def _factory():
    listener = AlertWakeListener()
    return (listener.startup, listener.shutdown, DEFAULT_SHUTDOWN_TIMEOUT)


register_daemon_listener("alert_wake", _factory)


register_periodic_task(
    "alert_wake.tail",
    interval_seconds=_read_poll_sec(),
    callable=run_tail_cycle,
)


def _reset_tail_position_for_tests() -> None:
    """Test-only: clear the tail-position state. Production code
    MUST NOT call this — it would cause replay of all historical
    notifications on the next tick."""
    global _last_seen_at
    _last_seen_at = None
