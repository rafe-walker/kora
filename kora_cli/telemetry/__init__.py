"""Telemetry — KR-CHEAP-COST-TELEMETRY.

Per-route cost counters tagged on every ``record_inference``
call. See :mod:`kora_cli.telemetry.cost_telemetry` for the full
contract.

Public surface:
  - :class:`CostRouteTelemetry` — singleton accumulator
  - :func:`get_telemetry` — process-global accessor
  - ``ROUTE_*`` literal constants — canonical route taxonomy
  - ``WINDOW_*`` literal constants — three counter windows
"""

from kora_cli.telemetry.cost_telemetry import (
    KNOWN_ROUTES,
    KNOWN_WINDOWS,
    ROUTE_ALERT_INVESTIGATION,
    ROUTE_EMAIL_INBOUND,
    ROUTE_EMAIL_OUTBOUND_COMPOSE,
    ROUTE_MCP_TOOL,
    ROUTE_PROBE_INVESTIGATION,
    ROUTE_SCHEDULED_TASK,
    ROUTE_SLACK_DM,
    ROUTE_TOOL_LOOP_ITERATION,
    ROUTE_UNKNOWN,
    WINDOW_MONTHLY,
    WINDOW_PROCESS_LIFETIME,
    WINDOW_ROLLING_24H,
    CostRouteTelemetry,
    get_telemetry,
)

__all__ = [
    "CostRouteTelemetry",
    "KNOWN_ROUTES",
    "KNOWN_WINDOWS",
    "ROUTE_ALERT_INVESTIGATION",
    "ROUTE_EMAIL_INBOUND",
    "ROUTE_EMAIL_OUTBOUND_COMPOSE",
    "ROUTE_MCP_TOOL",
    "ROUTE_PROBE_INVESTIGATION",
    "ROUTE_SCHEDULED_TASK",
    "ROUTE_SLACK_DM",
    "ROUTE_TOOL_LOOP_ITERATION",
    "ROUTE_UNKNOWN",
    "WINDOW_MONTHLY",
    "WINDOW_PROCESS_LIFETIME",
    "WINDOW_ROLLING_24H",
    "get_telemetry",
]
