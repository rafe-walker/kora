"""Alert aggregation — KR-ALERTS-PANEL-FLIP.

Aggregates operator-attention signals from the 5 existing data
sources (OperationalStateHolder + cost-ladder holder + HealthRollup
+ audit JSONL + heartbeat probe snapshots) into the FE's
``Alert`` shape from ``web/src/lib/api.ts``.

Public surface:
  * :class:`Alert` — wire-shape dataclass mirroring the TS interface
  * :func:`compute_active_alerts` — single-call aggregator the
    endpoint consumes
"""

from kora_cli.alerts.aggregator import Alert, compute_active_alerts

__all__ = ["Alert", "compute_active_alerts"]
