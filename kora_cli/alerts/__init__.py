"""Alert aggregation + push-notification surface.

Aggregator (KR-ALERTS-PANEL-FLIP): operator-attention signals
from the 5 data sources (OperationalStateHolder + cost-ladder
holder + HealthRollup + audit JSONL + heartbeat probe snapshots)
projected to the FE's ``Alert`` shape from ``web/src/lib/api.ts``.

Notifier (KR-ALERT-NOTIFY): periodic task that diffs the active
alert set + pushes newly-firing alerts to Joshua via Slack DM
(critical / warning) or email (info).

Public surface:
  * :class:`Alert` — wire-shape dataclass mirroring the TS interface
  * :func:`compute_active_alerts` — single-call aggregator the
    endpoint consumes
  * :class:`AlertNotifier` + :class:`NotificationCycleResult` +
    :class:`DispatchOutcome` — push-notification surface
"""

from kora_cli.alerts.aggregator import Alert, compute_active_alerts
from kora_cli.alerts.notifier import (
    AlertNotifier,
    DigestFlushResult,
    DispatchOutcome,
    NotificationCycleResult,
)

__all__ = [
    "Alert",
    "AlertNotifier",
    "DigestFlushResult",
    "DispatchOutcome",
    "NotificationCycleResult",
    "compute_active_alerts",
]
