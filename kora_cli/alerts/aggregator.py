"""Active-alert aggregator — KR-ALERTS-PANEL-FLIP.

:func:`compute_active_alerts` calls 5 per-source helpers, each
isolated by try/except so a single source failure (uninitialized
holder, unreadable JSONL, transient probe glitch) NEVER bubbles a
500 to the operator. Degraded data is better than no panel.

# Rule taxonomy (10 rules)

| Rule | Source | Severity | Trigger |
|---|---|---|---|
| ``cost_ladder_warned`` | cost holder | warning | active_rung == WARN_75 |
| ``cost_ladder_downshifted`` | cost holder | warning | active_rung == DOWNSHIFT_90 |
| ``cost_ladder_halted`` | cost holder | critical | active_rung == HARD_STOP_100 |
| ``operator_paused`` | operational state | critical | primary_state == PAUSED |
| ``operator_stopped`` | operational state | critical | primary_state == STOPPED |
| ``webhook_dead_letters_24h`` | audit JSONL | warning | count > 5 |
| ``capability_denied_24h`` | audit JSONL | info | count > 10 |
| ``reasoning_errors_24h`` | audit JSONL | warning | count > 5 |
| ``service_unhealthy`` | probe snapshots | warning | per service in {degraded, unhealthy} |
| ``slack_dm_reply_failed_24h`` | audit JSONL | warning | count > 3 |

Thresholds are PROPOSED (per bucket §2(b)) and tunable per
operator feedback; they're module-level constants below for ease
of edits.

# K-DG K-DG (yes, twice — paranoid by design)

Per §1 of the bucket spec + the live grep at HEAD ``054f4086``:

  * Cost holder accessor: ``agent.cost_state_holder.get_cost_holder()``.
    Rung resolution: ``.active_rung()`` — METHOD, not property
    (PM-locked #126 catch).
  * Operational state accessor: ``agent.operational_state_holder.get_holder()``
    (NOT ``get_operational_state_holder()`` — bucket spec drift
    versus actual symbol). ``holder.current`` is @PROPERTY
    (#112 catch); ``.primary_state`` is a bare enum field on the
    inner ``OperationalState`` dataclass.
  * Health rollup accessor:
    ``agent.health_rollup_holder.get_health_rollup_holder()``
    (NOT ``get_health_holder()`` — bucket spec drift). ``.current()``
    is METHOD; returns a frozen dataclass with BARE field names
    ``overall`` / ``control_plane`` / ``worker`` (no @property
    wrapper per #112 catch).
  * Audit reader: ``kora_cli.audit.jsonl_reader.read_audit_entries``
    accepts ``seam=`` and ``since=`` kwargs (jsonl_reader.py:58).
  * Heartbeat snapshots: ``kora_cli.heartbeat_probes.runner.current_service_snapshots()``
    returns ``dict[str, ServiceHealthSnapshot]``; status enum
    drawn from ``ServiceStatus`` Literal in
    ``heartbeat_probes/types.py:28``: {healthy, degraded,
    unhealthy, unknown}.

# Forward-compat note: capability_denied

The ``capability_denied_24h`` rule is forward-looking: today the
``mcp.tool_called`` audit emit at
``kora_cli/listeners/mcp_tools.py:714`` is reached AFTER the
capability gate (``listeners/mcp.py:181``), so denial responses
are NOT currently audit-logged. This rule's matcher uses
``details.result == "capability_denied"`` so when a follow-on
bucket adds audit-on-denial the rule activates automatically; in
the meantime it emits zero alerts (no false negatives — the data
genuinely isn't there).
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Thresholds — proposed per bucket §2(b); tune from operator feedback
# ---------------------------------------------------------------------------


WEBHOOK_DEAD_LETTER_24H_THRESHOLD = 5
CAPABILITY_DENIED_24H_THRESHOLD = 10
REASONING_ERRORS_24H_THRESHOLD = 5
SLACK_DM_REPLY_FAILED_24H_THRESHOLD = 3


# Severity rank for ordering (lower index = higher priority).
_SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}


@dataclass(frozen=True, slots=True)
class Alert:
    """One operator-attention alert.

    Wire-stable shape — keys match the FE's ``Alert`` interface in
    ``web/src/lib/api.ts`` verbatim. ``to_dict()`` returns a plain
    dict the endpoint serializes through FastAPI's JSON encoder.
    """

    id: str
    severity: str  # "critical" | "warning" | "info"
    category: str  # e.g. "cost_ladder" / "operational_state" / "service_unhealthy"
    title: str
    detail: str
    source_panel: str
    source_panel_route: str
    first_seen_at: str  # ISO-8601 UTC with Z suffix

    def to_dict(self) -> dict:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Per-source rule helpers
# ---------------------------------------------------------------------------


def _rules_from_cost_holder() -> List[Alert]:
    """Cost-ladder rules (3 — one per non-NORMAL rung)."""
    try:
        from agent.cost_state_holder import CostRung, get_cost_holder
    except Exception as exc:
        logger.warning(
            "[kora.alerts] cost_state_holder import failed: %r — skipping "
            "cost-ladder rules",
            exc,
        )
        return []

    holder = get_cost_holder()
    if holder is None:
        return []

    try:
        rung = holder.active_rung()
    except Exception as exc:
        logger.warning(
            "[kora.alerts] cost holder.active_rung() raised %r — skipping",
            exc,
        )
        return []

    try:
        pct = holder.current_pct_used()
    except Exception:
        pct = None

    now = _now_iso()
    pct_str = f"{int(pct * 100)}%" if isinstance(pct, (int, float)) else "?%"

    if rung is CostRung.WARN_75:
        return [
            Alert(
                id="cost_ladder_warned",
                severity="warning",
                category="cost_ladder",
                title=f"Budget at {pct_str} of monthly cap",
                detail=(
                    "Cost ladder at warn_75 — reasoning still runs on the "
                    "selected model; reply-failure rates may climb if the "
                    "rung escalates."
                ),
                source_panel="cost",
                source_panel_route="/cost-state",
                first_seen_at=now,
            )
        ]
    if rung is CostRung.DOWNSHIFT_90:
        return [
            Alert(
                id="cost_ladder_downshifted",
                severity="warning",
                category="cost_ladder",
                title=f"Reasoning downshifted at {pct_str}",
                detail=(
                    "Cost ladder at downshift_90 — calls that requested "
                    "opus are being routed to sonnet (and sonnet → haiku) "
                    "per agent/cost_downshift.py."
                ),
                source_panel="cost",
                source_panel_route="/cost-state",
                first_seen_at=now,
            )
        ]
    if rung is CostRung.HARD_STOP_100:
        return [
            Alert(
                id="cost_ladder_halted",
                severity="critical",
                category="cost_ladder",
                title=f"Reasoning halted at {pct_str} of budget",
                detail=(
                    "Cost ladder at hard_stop_100 — non-critical reasoning "
                    "calls refuse with cost_ladder_halted; AUTO_REPLY paths "
                    "fall back to canned text."
                ),
                source_panel="cost",
                source_panel_route="/cost-state",
                first_seen_at=now,
            )
        ]
    # CostRung.NORMAL → no alert
    return []


def _rules_from_operational_state() -> List[Alert]:
    """Operational state rules (PAUSED + STOPPED)."""
    try:
        from agent.operational_state import PrimaryState
        from agent.operational_state_holder import get_holder
    except Exception as exc:
        logger.warning(
            "[kora.alerts] operational_state_holder import failed: %r — "
            "skipping",
            exc,
        )
        return []

    holder = get_holder()
    if holder is None:
        return []

    try:
        state = holder.current  # @property — value snapshot
        ps = state.primary_state
    except Exception as exc:
        logger.warning(
            "[kora.alerts] operational holder.current raised %r — skipping",
            exc,
        )
        return []

    now = _now_iso()

    if ps is PrimaryState.PAUSED:
        return [
            Alert(
                id="operator_paused",
                severity="critical",
                category="operational_state",
                title="Kora paused",
                detail=(
                    "Slack DM + email handlers drop inbound traffic at "
                    "the state gate; reasoning engine refuses calls. "
                    "Resume from the operational-state panel."
                ),
                source_panel="ops",
                source_panel_route="/operational-state",
                first_seen_at=now,
            )
        ]
    if ps is PrimaryState.STOPPED:
        return [
            Alert(
                id="operator_stopped",
                severity="critical",
                category="operational_state",
                title="Kora STOPPED",
                detail=(
                    "Terminal state — all surfaces refuse traffic. "
                    "STOPPED requires manual operator action to clear "
                    "(typically a daemon restart)."
                ),
                source_panel="ops",
                source_panel_route="/operational-state",
                first_seen_at=now,
            )
        ]
    return []


def _count_audit_entries_in_window(
    seam: str,
    *,
    since: datetime,
    detail_match: Optional[dict] = None,
) -> int:
    """Helper: read audit entries for ``seam`` since ``since`` + return
    the count, optionally filtered to entries whose ``details`` dict
    contains every key/value pair in ``detail_match``.

    Returns 0 on any read failure — caller's fail-soft posture.
    """
    try:
        from kora_cli.audit.jsonl_reader import read_audit_entries
    except Exception as exc:
        logger.warning(
            "[kora.alerts] audit jsonl_reader import failed: %r", exc
        )
        return 0
    try:
        entries = read_audit_entries(seam=seam, since=since)
    except Exception as exc:
        logger.warning(
            "[kora.alerts] read_audit_entries(seam=%s) raised %r",
            seam,
            exc,
        )
        return 0
    if detail_match is None:
        return len(entries)
    matched = 0
    for entry in entries:
        details = entry.details or {}
        if all(details.get(k) == v for k, v in detail_match.items()):
            matched += 1
    return matched


def _rules_from_webhook_audit() -> List[Alert]:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    count = _count_audit_entries_in_window(
        seam="webhook.dead_letter", since=since
    )
    if count <= WEBHOOK_DEAD_LETTER_24H_THRESHOLD:
        return []
    return [
        Alert(
            id="webhook_dead_letters_24h",
            severity="warning",
            category="webhook_dead_letter",
            title=f"{count} webhook dead-letters in last 24h",
            detail=(
                f"Threshold {WEBHOOK_DEAD_LETTER_24H_THRESHOLD} exceeded. "
                "Common causes: signing-secret mismatch, malformed payloads "
                "from upstream, or webhook listener panic. Check the "
                "webhook-events panel for per-event diagnostic."
            ),
            source_panel="webhook_events",
            source_panel_route="/webhook-events",
            first_seen_at=_now_iso(),
        )
    ]


def _rules_from_capability_denied_audit() -> List[Alert]:
    """Forward-looking — see module docstring's forward-compat note.

    Today the audit log at ``mcp.tool_called`` only records successful
    invocations (capability gate at ``listeners/mcp.py:181`` returns
    BEFORE the audit emit at ``listeners/mcp_tools.py:714``). This
    rule's matcher is forward-compatible: when a follow-on bucket
    adds audit-on-denial it'll start firing without an aggregator
    edit.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    count = _count_audit_entries_in_window(
        seam="mcp.tool_called",
        since=since,
        detail_match={"result": "capability_denied"},
    )
    if count <= CAPABILITY_DENIED_24H_THRESHOLD:
        return []
    return [
        Alert(
            id="capability_denied_24h",
            severity="info",
            category="agent_capability_denied",
            title=f"{count} capability_denied responses in 24h",
            detail=(
                f"Threshold {CAPABILITY_DENIED_24H_THRESHOLD} exceeded. "
                "Unconfigured caller actor_kinds may indicate misconfigured "
                "third-party agents. Review mcp_callers.yaml + the "
                "agent-activity panel for the denied caller distribution."
            ),
            source_panel="agent_activity",
            source_panel_route="/agent-activity",
            first_seen_at=_now_iso(),
        )
    ]


def _rules_from_reasoning_audit() -> List[Alert]:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    count = _count_audit_entries_in_window(
        seam="reasoning.tool_called",
        since=since,
        detail_match={"tool_status": "execution_error"},
    )
    if count <= REASONING_ERRORS_24H_THRESHOLD:
        return []
    return [
        Alert(
            id="reasoning_errors_24h",
            severity="warning",
            category="reasoning_halted",
            title=f"{count} reasoning failures in 24h",
            detail=(
                f"Threshold {REASONING_ERRORS_24H_THRESHOLD} exceeded "
                "(tool_status=execution_error). Common causes: 3P tool "
                "transport failure, malformed Pydantic args from the "
                "model, or transient SDK 5xx. Check the reasoning panel "
                "for per-call diagnostic."
            ),
            source_panel="reasoning",
            source_panel_route="/reasoning",
            first_seen_at=_now_iso(),
        )
    ]


def _rules_from_slack_dm_audit() -> List[Alert]:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    count = _count_audit_entries_in_window(
        seam="slack_dm.reply_failed", since=since
    )
    if count <= SLACK_DM_REPLY_FAILED_24H_THRESHOLD:
        return []
    return [
        Alert(
            id="slack_dm_reply_failed_24h",
            severity="warning",
            category="reasoning_halted",
            title=f"{count} Slack DM reply failures in 24h",
            detail=(
                f"Threshold {SLACK_DM_REPLY_FAILED_24H_THRESHOLD} "
                "exceeded. Causes typically split between Slack-API "
                "transport failures and reasoning-engine errors. Check "
                "the slack-dm panel for the failure-reason taxonomy."
            ),
            source_panel="slack_dm",
            source_panel_route="/slack-dm",
            first_seen_at=_now_iso(),
        )
    ]


def _rules_from_probe_snapshots() -> List[Alert]:
    """One alert per service in ``degraded`` or ``unhealthy``.

    Per bucket §2(b): emits N alerts (not 1) so the operator sees
    each affected service's name in the alerts list — clicking the
    alert routes to the heartbeat panel where the per-service
    diagnostic lives.
    """
    try:
        from kora_cli.heartbeat_probes.runner import current_service_snapshots
    except Exception as exc:
        logger.warning(
            "[kora.alerts] heartbeat snapshots import failed: %r", exc
        )
        return []

    try:
        snapshots = current_service_snapshots() or {}
    except Exception as exc:
        logger.warning(
            "[kora.alerts] current_service_snapshots raised %r", exc
        )
        return []

    out: List[Alert] = []
    for service_name, snapshot in snapshots.items():
        try:
            status = snapshot.status
        except Exception:
            continue
        if status not in {"degraded", "unhealthy"}:
            continue
        severity = "critical" if status == "unhealthy" else "warning"
        out.append(
            Alert(
                id=f"service_unhealthy:{service_name}",
                severity=severity,
                category="service_unhealthy",
                title=f"{service_name} probe: {status}",
                detail=(
                    f"Heartbeat probe for {service_name} returned "
                    f"status={status}. See the heartbeat panel for the "
                    "last_check_at + sanitized error diagnostic."
                ),
                source_panel="heartbeat",
                source_panel_route="/heartbeat",
                first_seen_at=_now_iso(),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


_RULE_HELPERS = (
    _rules_from_cost_holder,
    _rules_from_operational_state,
    _rules_from_webhook_audit,
    _rules_from_capability_denied_audit,
    _rules_from_reasoning_audit,
    _rules_from_slack_dm_audit,
    _rules_from_probe_snapshots,
)


def compute_active_alerts() -> List[Alert]:
    """Run every rule helper + return the merged alert list.

    Sort: severity rank (critical → warning → info), then by ``id``
    for stable ordering within a severity tier so the FE list
    doesn't reshuffle between polls.

    Fail-soft: each helper already catches per-source exceptions
    + returns ``[]`` on any failure; an unexpected error past that
    layer is caught here too (defense in depth) so the endpoint
    NEVER 500s.
    """
    alerts: List[Alert] = []
    for helper in _RULE_HELPERS:
        try:
            alerts.extend(helper())
        except Exception as exc:
            # Reachable only if a helper bypassed its own try/except —
            # which shouldn't happen but the contract is the endpoint
            # never crashes the panel.
            logger.warning(
                "[kora.alerts] helper %s raised past inner catch: %r",
                helper.__name__,
                exc,
            )
    alerts.sort(key=lambda a: (_SEVERITY_RANK.get(a.severity, 99), a.id))
    return alerts
