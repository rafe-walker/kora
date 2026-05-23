"""Alert push-notifier daemon listener — KR-ALERT-NOTIFY ST1.

Registers a periodic task with the heartbeat scheduler that runs
:meth:`AlertNotifier.run_notification_cycle` every
``KORA_ALERT_NOTIFY_INTERVAL_SEC`` (default 180s / 3 min) seconds.

# Wiring

  - Daemon startup: construct one :class:`AlertNotifier` bound to
    the live SlackClient + PurelymailClient lazy factories
    (:func:`current_slack_client` /
    :func:`current_purelymail_client`). Expose via the module
    singleton + accessor pattern.
  - Daemon shutdown: clear the notifier's in-memory dedup state
    so a subsequent listener start sees a clean slate. The
    notifier itself doesn't hold a long-lived transport.

# Cadence rationale (PM Q1 default)

3 minutes balances responsiveness (operator gets pinged within
3 min of an alert firing) against burn (slack/email API budget).
``KORA_ALERT_NOTIFY_INTERVAL_SEC`` env override available; values
≤ 0 or non-numeric fall back to the default.

# Why fail-soft on client unavailability

The notifier resolves SlackClient / PurelymailClient per cycle
(via lazy factories). If either is unavailable the dispatch
fails, the audit log records the failure, and the alert ID still
enters ``last_alert_ids`` so the next cycle doesn't re-spam. The
alert remains visible in the cockpit (the panel reads the
aggregator directly; notifications are a convenience layer).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from kora_cli.alerts.notifier import AlertNotifier
from kora_cli.daemon import DEFAULT_SHUTDOWN_TIMEOUT, register_daemon_listener
from kora_cli.listeners.heartbeat import register_periodic_task

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cadence config
# ---------------------------------------------------------------------------


DEFAULT_INTERVAL_SEC: float = 180.0  # 3 min per §4 Q1 default
INTERVAL_ENV: str = "KORA_ALERT_NOTIFY_INTERVAL_SEC"

# ST2 digest-flush cadence — daily by default. The digest-flush task
# is registered unconditionally; in immediate mode the notifier's
# `flush_digest` is a no-op (queue is always empty in immediate mode).
DEFAULT_DIGEST_INTERVAL_SEC: float = 86400.0  # 24 h
DIGEST_INTERVAL_ENV: str = "KORA_ALERT_NOTIFY_DIGEST_INTERVAL_SEC"


def _read_positive_interval(
    env_name: str, default_value: float
) -> float:
    """Generic env-or-default reader for cadence values. Mirrors the
    pattern in mcp_consumption / email_inbound_imap_listener."""
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default_value
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "[kora.alert_notifier_listener] %s=%r is not numeric; using "
            "default %ss",
            env_name,
            raw,
            default_value,
        )
        return default_value
    if value <= 0:
        logger.warning(
            "[kora.alert_notifier_listener] %s=%s must be > 0; using "
            "default %ss",
            env_name,
            value,
            default_value,
        )
        return default_value
    return value


def _read_interval() -> float:
    """Resolve cycle cadence from env with sane fallback."""
    return _read_positive_interval(INTERVAL_ENV, DEFAULT_INTERVAL_SEC)


def _read_digest_interval() -> float:
    """Resolve digest-flush cadence from env with sane fallback."""
    return _read_positive_interval(
        DIGEST_INTERVAL_ENV, DEFAULT_DIGEST_INTERVAL_SEC
    )


# ---------------------------------------------------------------------------
# Module-level singleton + accessor
# ---------------------------------------------------------------------------


_notifier_singleton: Optional[AlertNotifier] = None


def _set_singleton(notifier: AlertNotifier) -> None:
    global _notifier_singleton
    _notifier_singleton = notifier


def _clear_singleton() -> None:
    global _notifier_singleton
    _notifier_singleton = None


def current_alert_notifier() -> Optional[AlertNotifier]:
    """Return the live :class:`AlertNotifier`, or ``None``.

    ``None`` when the daemon isn't running with this listener
    registered + started.
    """
    return _notifier_singleton


# ---------------------------------------------------------------------------
# Periodic task — runs per heartbeat scheduler tick
# ---------------------------------------------------------------------------


async def run_notification_cycle() -> None:
    """One scheduler tick. Short-circuits cleanly when the notifier
    singleton is absent (daemon shutdown in progress, fail-soft boot,
    etc.). The notifier's own ``run_notification_cycle`` is fail-soft
    too — exceptions inside don't crash the scheduler.
    """
    notifier = current_alert_notifier()
    if notifier is None:
        logger.debug(
            "[kora.alert_notifier_listener] tick skipped: no active notifier"
        )
        return
    try:
        result = await notifier.run_notification_cycle()
    except Exception as exc:
        # Defense in depth — AlertNotifier.run_notification_cycle
        # already catches inside; this is the outermost guard so the
        # scheduler keeps ticking.
        logger.warning(
            "[kora.alert_notifier_listener] cycle raised past inner "
            "catch: %r",
            exc,
        )
        return

    if (
        result.newly_firing_count > 0
        or result.dispatch_errors > 0
        or result.cooldown_suppressed > 0
        or result.burst_summarized > 0
        or result.digest_queued > 0
    ):
        logger.info(
            "[kora.alert_notifier_listener] cycle: active=%d new=%d "
            "resolved=%d slack=%d email=%d errors=%d "
            "cooldown_suppressed=%d burst_summarized=%d digest_queued=%d",
            result.active_count,
            result.newly_firing_count,
            result.newly_resolved_count,
            result.slack_dispatched,
            result.email_dispatched,
            result.dispatch_errors,
            result.cooldown_suppressed,
            result.burst_summarized,
            result.digest_queued,
        )


async def run_digest_flush() -> None:
    """ST2 digest-flush scheduler tick. No-op in immediate mode (the
    notifier's :meth:`flush_digest` checks mode internally + returns
    a zero result without sending). Defense-in-depth outer catch."""
    notifier = current_alert_notifier()
    if notifier is None:
        logger.debug(
            "[kora.alert_notifier_listener] digest tick skipped: no "
            "active notifier"
        )
        return
    try:
        result = await notifier.flush_digest()
    except Exception as exc:
        logger.warning(
            "[kora.alert_notifier_listener] digest flush raised past "
            "inner catch: %r",
            exc,
        )
        return
    if result.flushed_count > 0 or result.success is False:
        logger.info(
            "[kora.alert_notifier_listener] digest flush: count=%d "
            "success=%s error=%s",
            result.flushed_count,
            result.success,
            result.error,
        )


# ---------------------------------------------------------------------------
# Listener
# ---------------------------------------------------------------------------


class AlertNotifierListener:
    """Holds the live :class:`AlertNotifier` for the daemon's lifetime."""

    async def startup(self) -> None:
        """Construct the notifier bound to live client lazy factories.

        Fail-soft on construction errors so daemon boot doesn't
        crash even in unusual environments where the alert module
        imports fail.
        """
        try:
            slack_factory = _slack_client_factory
            purelymail_factory = _purelymail_client_factory
            notifier = AlertNotifier(
                slack_client_factory=slack_factory,
                purelymail_client_factory=purelymail_factory,
            )
        except Exception as exc:
            logger.warning(
                "[kora.alert_notifier_listener] startup raised %r — "
                "notifier disabled this run",
                exc,
            )
            _clear_singleton()
            return

        _set_singleton(notifier)
        logger.info(
            "[kora.alert_notifier_listener] AlertNotifier constructed; "
            "cycle cadence=%ss",
            _read_interval(),
        )

    async def shutdown(self) -> None:
        """Reset dedup state + clear the singleton. The notifier has
        no transport state to release."""
        notifier = _notifier_singleton
        if notifier is not None:
            notifier.reset_dedup_state()
        _clear_singleton()
        logger.info("[kora.alert_notifier_listener] AlertNotifier cleared")


def _slack_client_factory() -> Optional[Any]:
    """Lazy resolver for the live SlackClient. Falls through to
    ``None`` when the listener stack isn't initialized (test paths)."""
    try:
        from kora_cli.listeners.slack_client_listener import (
            current_slack_client,
        )
    except Exception:
        return None
    return current_slack_client()


def _purelymail_client_factory() -> Optional[Any]:
    """Lazy resolver for the live PurelymailClient. Same fall-through
    posture as the Slack factory."""
    try:
        from kora_cli.listeners.purelymail_client_listener import (
            current_purelymail_client,
        )
    except Exception:
        return None
    return current_purelymail_client()


# ---------------------------------------------------------------------------
# Factory + registration (import-time side effect)
# ---------------------------------------------------------------------------


def _factory():
    listener = AlertNotifierListener()
    return (listener.startup, listener.shutdown, DEFAULT_SHUTDOWN_TIMEOUT)


register_daemon_listener("alert_notifier", _factory)


# ---------------------------------------------------------------------------
# Periodic-task registration (import-time side effect)
# ---------------------------------------------------------------------------


register_periodic_task(
    "alerts.notify",
    interval_seconds=_read_interval(),
    callable=run_notification_cycle,
)

# ST2 digest-flush task. Registered unconditionally — the notifier's
# flush_digest() checks mode internally and no-ops in immediate mode,
# so the env can flip between modes across daemon restarts without
# changing the registered task list.
register_periodic_task(
    "alerts.digest_flush",
    interval_seconds=_read_digest_interval(),
    callable=run_digest_flush,
)
