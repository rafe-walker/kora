"""Probe wake-event listener — KR-PROBE-WAKE-CONSUMER.

Periodic task that tails ``${KORA_HOME}/kora_audit_log.jsonl``
for fresh ``probe.wake_requested`` rows (emitted by PR #163's
probe runner post-hook) and feeds each one to
:class:`ProbeWakeConsumer`.

# Tail strategy

Cron-driven, NOT inotify. Each tick:
  1. Read audit entries where ``seam == "probe.wake_requested"``
     and ``emitted_at > _last_seen_at``.
  2. For each fresh entry: hand its ``details`` dict to the
     consumer's ``consume_wake_event``.
  3. Update ``_last_seen_at`` to the latest ``emitted_at``
     processed so the next tick picks up cleanly without
     re-processing.

First tick after listener startup stamps ``_last_seen_at`` to NOW
(without firing any consumes) so historical wake events from
prior daemon runs don't all replay. The "consumer-side" semantic
is "wake events fired AFTER the listener came up." This matches
AlertNotifier's Q3 ruling (fire-on-first-cycle, no persistence).

# Why poll vs file-watcher

Audit JSONL is append-only via :func:`utils.atomic_replace`-
equivalent (the sink uses ``open(..., "a")`` which is atomic for
small payloads on POSIX). A polling reader is simpler than an
inotify watcher and matches every other Kora periodic-task
pattern. Polling cadence is ``KORA_PROBE_WAKE_POLL_SEC`` (default
30s; sub-minute latency for operator DMs without burning
read-cycle CPU).

# Fail-soft

The periodic task is wrapped in try/except (heartbeat scheduler
patterns from MCP-CONSUMPTION + alert_notifier_listener + email
IMAP poll). Single-event failures inside ``consume_wake_event``
are caught by the consumer itself per its own contract; the
listener-side wrapper catches anything that escapes.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from kora_cli.daemon import DEFAULT_SHUTDOWN_TIMEOUT, register_daemon_listener
from kora_cli.listeners.heartbeat import register_periodic_task
from kora_cli.probes.wake_consumer import ProbeWakeConsumer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cadence config
# ---------------------------------------------------------------------------


DEFAULT_POLL_SEC: float = 30.0  # sub-minute latency, light CPU
POLL_SEC_ENV: str = "KORA_PROBE_WAKE_POLL_SEC"


def _read_poll_sec() -> float:
    raw = os.environ.get(POLL_SEC_ENV, "").strip()
    if not raw:
        return DEFAULT_POLL_SEC
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "[kora.probe_wake_listener] %s=%r is not numeric; using "
            "default %ss",
            POLL_SEC_ENV,
            raw,
            DEFAULT_POLL_SEC,
        )
        return DEFAULT_POLL_SEC
    if value <= 0:
        logger.warning(
            "[kora.probe_wake_listener] %s=%s must be > 0; using "
            "default %ss",
            POLL_SEC_ENV,
            value,
            DEFAULT_POLL_SEC,
        )
        return DEFAULT_POLL_SEC
    return value


# ---------------------------------------------------------------------------
# Module-level singleton + tail-position state
# ---------------------------------------------------------------------------


_consumer_singleton: Optional[ProbeWakeConsumer] = None
_last_seen_at: Optional[datetime] = None


def current_probe_wake_consumer() -> Optional[ProbeWakeConsumer]:
    """Read-only accessor for the live :class:`ProbeWakeConsumer`,
    or ``None`` if the listener isn't running."""
    return _consumer_singleton


def _set_consumer(consumer: ProbeWakeConsumer) -> None:
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
    """Lazy resolver for the live reasoning engine. Falls through to
    ``None`` when the listener stack isn't initialized (test paths)."""
    try:
        from kora_cli.listeners.reasoning_engine_listener import (
            current_reasoning_engine,
        )
    except Exception:
        return None
    return current_reasoning_engine()


def _slack_client_factory() -> Optional[Any]:
    """Lazy resolver for the live SlackClient. Same fall-through
    posture as the AlertNotifier's slack factory (PR #149)."""
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
    """One tail tick: pick up fresh probe.wake_requested rows + hand
    them to the consumer.

    First tick after listener startup stamps ``_last_seen_at`` to
    NOW without firing any consumes — historical wake events from
    prior daemon runs don't replay.

    Fail-soft: any exception inside (audit read, consumer raise,
    etc.) is caught + logged so the heartbeat scheduler keeps
    ticking.
    """
    global _last_seen_at
    consumer = current_probe_wake_consumer()
    if consumer is None:
        logger.debug(
            "[kora.probe_wake_listener] tick skipped: no active consumer"
        )
        return

    # Stamp tail-position on first tick (mirrors AlertNotifier's
    # "first cycle treats all current as new" Q3 default, inverted
    # — for probe wakes we DON'T want to replay history at boot).
    now = datetime.now(timezone.utc)
    if _last_seen_at is None:
        _last_seen_at = now
        logger.info(
            "[kora.probe_wake_listener] tail-position stamped at boot "
            "(no replay of prior probe wakes)"
        )
        return

    try:
        from kora_cli.audit.jsonl_reader import read_audit_entries
    except Exception as exc:
        logger.warning(
            "[kora.probe_wake_listener] audit reader import failed: %r",
            exc,
        )
        return

    try:
        rows = read_audit_entries(
            seam="probe.wake_requested",
            since=_last_seen_at,
        )
    except Exception as exc:
        logger.warning(
            "[kora.probe_wake_listener] read_audit_entries raised %r",
            exc,
        )
        return

    if not rows:
        return

    # Reader returns newest-first; reverse to oldest-first so
    # consume + last_seen_at advance monotonically.
    rows_chronological = list(reversed(rows))

    new_max_ts = _last_seen_at
    for row in rows_chronological:
        try:
            await consumer.consume_wake_event(
                getattr(row, "details", {}) or {}
            )
        except Exception as exc:
            logger.warning(
                "[kora.probe_wake_listener] consume_wake_event raised "
                "%r — continuing past this event",
                exc,
            )
        # Advance the tail regardless of consume success — failures
        # are recorded in the consumer's outcome; we don't replay.
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


class ProbeWakeListener:
    """Holds the live :class:`ProbeWakeConsumer` for the daemon
    lifetime. The periodic task picks up fresh wake events on each
    tick and hands them to the consumer.
    """

    async def startup(self) -> None:
        """Construct the consumer bound to lazy reasoning + Slack
        factories. Fail-soft per the AlertNotifier listener pattern
        (PR #149) — construction errors leave the singleton None +
        daemon boots regardless."""
        try:
            consumer = ProbeWakeConsumer(
                reasoning_engine_factory=_reasoning_engine_factory,
                slack_client_factory=_slack_client_factory,
            )
        except Exception as exc:
            logger.warning(
                "[kora.probe_wake_listener] startup raised %r — consumer "
                "disabled this run",
                exc,
            )
            _clear_consumer()
            return

        _set_consumer(consumer)
        logger.info(
            "[kora.probe_wake_listener] ProbeWakeConsumer constructed; "
            "poll cadence=%ss",
            _read_poll_sec(),
        )

    async def shutdown(self) -> None:
        """Drop the singleton + reset debounce state + tail position
        so a subsequent listener start sees a clean slate."""
        consumer = _consumer_singleton
        if consumer is not None:
            consumer.reset_debounce_state()
        _clear_consumer()
        logger.info(
            "[kora.probe_wake_listener] ProbeWakeConsumer cleared"
        )


# ---------------------------------------------------------------------------
# Factory + registration (import-time side effect)
# ---------------------------------------------------------------------------


def _factory():
    listener = ProbeWakeListener()
    return (listener.startup, listener.shutdown, DEFAULT_SHUTDOWN_TIMEOUT)


register_daemon_listener("probe_wake", _factory)


register_periodic_task(
    "probe_wake.tail",
    interval_seconds=_read_poll_sec(),
    callable=run_tail_cycle,
)


def _reset_tail_position_for_tests() -> None:
    """Test-only: clear the tail-position state. Production code
    MUST NOT call this — it would cause replay of all historical
    probe wakes on the next tick."""
    global _last_seen_at
    _last_seen_at = None
