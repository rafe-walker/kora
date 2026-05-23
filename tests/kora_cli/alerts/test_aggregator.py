"""Tests for the KR-ALERTS-PANEL-FLIP aggregator.

Bucket §2(f) scenarios:

  Per-rule coverage (10 rules):
   1. cost_ladder_warned fires when rung == WARN_75
   2. cost_ladder_downshifted fires when rung == DOWNSHIFT_90
   3. cost_ladder_halted fires when rung == HARD_STOP_100
   4. cost ladder NORMAL → no alert
   5. operator_paused fires when state == PAUSED
   6. operator_stopped fires when state == STOPPED
   7. operational state ACTIVE → no alert
   8. webhook_dead_letters_24h fires when count > 5
   9. capability_denied_24h fires when count > 10
  10. reasoning_errors_24h fires when execution_error count > 5
  11. service_unhealthy fires per service in {degraded, unhealthy}
  12. service_unhealthy unhealthy → severity=critical
  13. service_unhealthy degraded → severity=warning
  14. slack_dm_reply_failed_24h fires when count > 3

  Sort + shape:
  15. Severity sort: critical → warning → info
  16. Empty state (no triggers) → empty list
  17. Alert.to_dict shape matches FE Alert TS interface

  Fail-soft:
  18. Cost holder raises → other rules still emit
  19. Operational holder None → no cost alerts but webhook alerts emit
  20. JSONL reader raises → other rules still emit
  21. Probe snapshots accessor raises → other rules still emit
  22. compute_active_alerts NEVER raises

  Endpoint integration:
  23. GET /api/alerts/current returns expected top-level shape
  24. stub: false always
  25. by_severity sums to total_active
  26. SECURITY: walk-payload sweep — no PII / token shapes
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from kora_cli.alerts.aggregator import (
    CAPABILITY_DENIED_24H_THRESHOLD,
    REASONING_ERRORS_24H_THRESHOLD,
    SLACK_DM_REPLY_FAILED_24H_THRESHOLD,
    WEBHOOK_DEAD_LETTER_24H_THRESHOLD,
    Alert,
    compute_active_alerts,
)


_EMAIL_ADDRESS = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)
_ANTHROPIC_KEY = re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}\b")
_HEX_SECRET_SHAPE = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_BEARER_TOKEN_SHAPE = re.compile(
    r"\b(?:Bearer|Authorization)\s*[: ]\s*[A-Za-z0-9+/_.-]{8,}",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers — synthesize source-state for mocks
# ---------------------------------------------------------------------------


def _make_cost_holder(rung_name: str, pct: float = 0.5):
    from agent.cost_state_holder import CostRung

    rung = getattr(CostRung, rung_name)
    holder = MagicMock()
    holder.active_rung.return_value = rung
    holder.current_pct_used.return_value = pct
    return holder


def _make_operational_holder(primary_state_name: str):
    from agent.operational_state import PrimaryState

    state = MagicMock()
    state.primary_state = getattr(PrimaryState, primary_state_name)
    holder = MagicMock()
    holder.current = state  # @property — set attribute on the mock
    return holder


def _make_audit_entry(seam: str, details: Optional[dict] = None) -> Any:
    """Build a mock that quacks like AuditEntry: ``.seam``,
    ``.emitted_at``, ``.details``."""
    entry = MagicMock()
    entry.seam = seam
    entry.emitted_at = datetime.now(timezone.utc)
    entry.details = details or {}
    return entry


def _make_snapshot(name: str, status: str):
    snap = MagicMock()
    snap.name = name
    snap.status = status
    return snap


# Reusable patches: each helper module imports its dependency lazily,
# so we patch where the helper looks for it (NOT where it's defined).


@pytest.fixture
def patch_sources():
    """Yield a context manager that patches all 5 sources to baseline
    (no-alert) shapes; individual tests override per-source."""
    from contextlib import ExitStack

    stack = ExitStack()

    cost_holder = _make_cost_holder("NORMAL")
    op_holder = _make_operational_holder("ACTIVE")

    stack.enter_context(
        patch(
            "agent.cost_state_holder.get_cost_holder",
            return_value=cost_holder,
        )
    )
    stack.enter_context(
        patch(
            "agent.operational_state_holder.get_holder",
            return_value=op_holder,
        )
    )
    stack.enter_context(
        patch(
            "kora_cli.audit.jsonl_reader.read_audit_entries",
            return_value=[],
        )
    )
    stack.enter_context(
        patch(
            "kora_cli.heartbeat_probes.runner.current_service_snapshots",
            return_value={},
        )
    )

    sources = {
        "cost_holder": cost_holder,
        "op_holder": op_holder,
    }
    try:
        yield sources, stack
    finally:
        stack.close()


# ===========================================================================
# Cost-ladder rules
# ===========================================================================


def test_cost_ladder_warned_fires(patch_sources):
    sources, _ = patch_sources
    sources["cost_holder"].active_rung.return_value = _enum("CostRung", "WARN_75")
    sources["cost_holder"].current_pct_used.return_value = 0.80
    alerts = compute_active_alerts()
    ids = [a.id for a in alerts]
    assert "cost_ladder_warned" in ids
    matching = next(a for a in alerts if a.id == "cost_ladder_warned")
    assert matching.severity == "warning"
    assert "80%" in matching.title


def test_cost_ladder_downshifted_fires(patch_sources):
    sources, _ = patch_sources
    sources["cost_holder"].active_rung.return_value = _enum(
        "CostRung", "DOWNSHIFT_90"
    )
    sources["cost_holder"].current_pct_used.return_value = 0.92
    alerts = compute_active_alerts()
    matching = [a for a in alerts if a.id == "cost_ladder_downshifted"]
    assert len(matching) == 1
    assert matching[0].severity == "warning"
    assert "92%" in matching[0].title


def test_cost_ladder_halted_fires(patch_sources):
    sources, _ = patch_sources
    sources["cost_holder"].active_rung.return_value = _enum(
        "CostRung", "HARD_STOP_100"
    )
    sources["cost_holder"].current_pct_used.return_value = 1.05
    alerts = compute_active_alerts()
    matching = [a for a in alerts if a.id == "cost_ladder_halted"]
    assert len(matching) == 1
    assert matching[0].severity == "critical"


def test_cost_ladder_normal_no_alert(patch_sources):
    alerts = compute_active_alerts()
    ids = [a.id for a in alerts]
    assert "cost_ladder_warned" not in ids
    assert "cost_ladder_downshifted" not in ids
    assert "cost_ladder_halted" not in ids


def _enum(enum_class_name: str, member_name: str):
    if enum_class_name == "CostRung":
        from agent.cost_state_holder import CostRung

        return getattr(CostRung, member_name)
    if enum_class_name == "PrimaryState":
        from agent.operational_state import PrimaryState

        return getattr(PrimaryState, member_name)
    raise ValueError(enum_class_name)


# ===========================================================================
# Operational-state rules
# ===========================================================================


def test_operator_paused_fires(patch_sources):
    sources, _ = patch_sources
    sources["op_holder"].current.primary_state = _enum(
        "PrimaryState", "PAUSED"
    )
    alerts = compute_active_alerts()
    matching = [a for a in alerts if a.id == "operator_paused"]
    assert len(matching) == 1
    assert matching[0].severity == "critical"


def test_operator_stopped_fires(patch_sources):
    sources, _ = patch_sources
    sources["op_holder"].current.primary_state = _enum(
        "PrimaryState", "STOPPED"
    )
    alerts = compute_active_alerts()
    matching = [a for a in alerts if a.id == "operator_stopped"]
    assert len(matching) == 1
    assert matching[0].severity == "critical"


def test_operational_active_no_alert(patch_sources):
    alerts = compute_active_alerts()
    ids = [a.id for a in alerts]
    assert "operator_paused" not in ids
    assert "operator_stopped" not in ids


# ===========================================================================
# Audit JSONL rules
# ===========================================================================


def test_webhook_dead_letters_fires_when_over_threshold(patch_sources):
    sources, _ = patch_sources
    entries = [
        _make_audit_entry("webhook.dead_letter")
        for _ in range(WEBHOOK_DEAD_LETTER_24H_THRESHOLD + 2)
    ]

    def fake_read(seam=None, since=None):
        return entries if seam == "webhook.dead_letter" else []

    with patch(
        "kora_cli.audit.jsonl_reader.read_audit_entries",
        side_effect=fake_read,
    ):
        alerts = compute_active_alerts()
    matching = [a for a in alerts if a.id == "webhook_dead_letters_24h"]
    assert len(matching) == 1
    assert str(WEBHOOK_DEAD_LETTER_24H_THRESHOLD + 2) in matching[0].title


def test_webhook_dead_letters_below_threshold_no_alert(patch_sources):
    sources, _ = patch_sources
    entries = [
        _make_audit_entry("webhook.dead_letter")
        for _ in range(WEBHOOK_DEAD_LETTER_24H_THRESHOLD)
    ]

    def fake_read(seam=None, since=None):
        return entries if seam == "webhook.dead_letter" else []

    with patch(
        "kora_cli.audit.jsonl_reader.read_audit_entries",
        side_effect=fake_read,
    ):
        alerts = compute_active_alerts()
    ids = [a.id for a in alerts]
    assert "webhook_dead_letters_24h" not in ids


def test_capability_denied_fires_when_over_threshold(patch_sources):
    sources, _ = patch_sources
    entries = [
        _make_audit_entry(
            "mcp.tool_called", details={"result": "capability_denied"}
        )
        for _ in range(CAPABILITY_DENIED_24H_THRESHOLD + 1)
    ] + [
        # Mixed-in OK entries — should NOT count.
        _make_audit_entry(
            "mcp.tool_called", details={"result": "ok"}
        )
        for _ in range(5)
    ]

    def fake_read(seam=None, since=None):
        return entries if seam == "mcp.tool_called" else []

    with patch(
        "kora_cli.audit.jsonl_reader.read_audit_entries",
        side_effect=fake_read,
    ):
        alerts = compute_active_alerts()
    matching = [a for a in alerts if a.id == "capability_denied_24h"]
    assert len(matching) == 1
    assert matching[0].severity == "info"
    # Title shows the matched-only count, not the total.
    assert str(CAPABILITY_DENIED_24H_THRESHOLD + 1) in matching[0].title


def test_capability_denied_today_emits_alert_when_threshold_exceeded(
    patch_sources,
):
    """KR-MCP-AUDIT-ON-DENIAL — the cap-gate's denial path now writes
    ``mcp.tool_called`` rows with ``details.result ==
    "capability_denied"``. With more than
    :data:`CAPABILITY_DENIED_24H_THRESHOLD` such rows in the
    trailing 24h, the alert rule fires exactly one warning so the
    panel can prompt the operator to review mcp_callers.yaml.

    Previously this test pinned the OPPOSITE behavior (forward-compat
    "no alert because audit doesn't emit denials yet") — that
    contract closed when audit-on-denial landed. The test is kept
    under a renamed identity to lock in the new behavior in the
    same slot so a future regression can't quietly drop the alert."""
    sources, _ = patch_sources
    # 11 denials + a mix of OK + actor_id_required entries that should
    # NOT count against the capability_denied threshold.
    entries = (
        [
            _make_audit_entry(
                "mcp.tool_called",
                details={"result": "capability_denied"},
            )
            for _ in range(CAPABILITY_DENIED_24H_THRESHOLD + 1)
        ]
        + [
            _make_audit_entry(
                "mcp.tool_called", details={"result": "ok"}
            )
            for _ in range(7)
        ]
        + [
            _make_audit_entry(
                "mcp.tool_called",
                details={"result": "actor_id_required"},
            )
            for _ in range(5)
        ]
    )

    def fake_read(seam=None, since=None):
        return entries if seam == "mcp.tool_called" else []

    with patch(
        "kora_cli.audit.jsonl_reader.read_audit_entries",
        side_effect=fake_read,
    ):
        alerts = compute_active_alerts()
    matching = [a for a in alerts if a.id == "capability_denied_24h"]
    assert len(matching) == 1, (
        "expected exactly one capability_denied_24h alert when "
        "11 denials exceed the threshold of 10"
    )
    assert matching[0].severity == "info"
    # Title surfaces the matched-only count (11 — not the mixed-bag
    # total of 11 + 7 + 5 = 23).
    assert str(CAPABILITY_DENIED_24H_THRESHOLD + 1) in matching[0].title


def test_reasoning_errors_fires_when_over_threshold(patch_sources):
    sources, _ = patch_sources
    entries = [
        _make_audit_entry(
            "reasoning.tool_called",
            details={"tool_status": "execution_error"},
        )
        for _ in range(REASONING_ERRORS_24H_THRESHOLD + 1)
    ] + [
        # Successful ones should not count.
        _make_audit_entry(
            "reasoning.tool_called", details={"tool_status": "ok"}
        )
        for _ in range(3)
    ]

    def fake_read(seam=None, since=None):
        return entries if seam == "reasoning.tool_called" else []

    with patch(
        "kora_cli.audit.jsonl_reader.read_audit_entries",
        side_effect=fake_read,
    ):
        alerts = compute_active_alerts()
    matching = [a for a in alerts if a.id == "reasoning_errors_24h"]
    assert len(matching) == 1
    assert matching[0].severity == "warning"


def test_slack_dm_reply_failed_fires_when_over_threshold(patch_sources):
    sources, _ = patch_sources
    entries = [
        _make_audit_entry("slack_dm.reply_failed")
        for _ in range(SLACK_DM_REPLY_FAILED_24H_THRESHOLD + 1)
    ]

    def fake_read(seam=None, since=None):
        return entries if seam == "slack_dm.reply_failed" else []

    with patch(
        "kora_cli.audit.jsonl_reader.read_audit_entries",
        side_effect=fake_read,
    ):
        alerts = compute_active_alerts()
    matching = [a for a in alerts if a.id == "slack_dm_reply_failed_24h"]
    assert len(matching) == 1


# ===========================================================================
# Service-snapshot rules
# ===========================================================================


def test_service_unhealthy_one_alert_per_affected_service(patch_sources):
    sources, _ = patch_sources
    snapshots = {
        "vercel": _make_snapshot("vercel", "unhealthy"),
        "sentry": _make_snapshot("sentry", "degraded"),
        "doppler": _make_snapshot("doppler", "healthy"),
        "supabase": _make_snapshot("supabase", "unknown"),
        "fly": _make_snapshot("fly", "unhealthy"),
    }
    with patch(
        "kora_cli.heartbeat_probes.runner.current_service_snapshots",
        return_value=snapshots,
    ):
        alerts = compute_active_alerts()
    service_alerts = [a for a in alerts if a.category == "service_unhealthy"]
    # vercel (unhealthy) + sentry (degraded) + fly (unhealthy) = 3
    # doppler (healthy) + supabase (unknown) excluded
    assert len(service_alerts) == 3
    ids = {a.id for a in service_alerts}
    assert "service_unhealthy:vercel" in ids
    assert "service_unhealthy:sentry" in ids
    assert "service_unhealthy:fly" in ids
    assert "service_unhealthy:doppler" not in ids
    assert "service_unhealthy:supabase" not in ids


def test_service_unhealthy_severity_mapping(patch_sources):
    sources, _ = patch_sources
    snapshots = {
        "vercel": _make_snapshot("vercel", "unhealthy"),
        "sentry": _make_snapshot("sentry", "degraded"),
    }
    with patch(
        "kora_cli.heartbeat_probes.runner.current_service_snapshots",
        return_value=snapshots,
    ):
        alerts = compute_active_alerts()
    vercel_alert = next(a for a in alerts if a.id == "service_unhealthy:vercel")
    sentry_alert = next(a for a in alerts if a.id == "service_unhealthy:sentry")
    assert vercel_alert.severity == "critical"
    assert sentry_alert.severity == "warning"


def test_service_snapshots_empty_no_alerts(patch_sources):
    alerts = compute_active_alerts()
    service_alerts = [a for a in alerts if a.category == "service_unhealthy"]
    assert service_alerts == []


# ===========================================================================
# Sort + shape
# ===========================================================================


def test_severity_sort_critical_first(patch_sources):
    sources, _ = patch_sources
    sources["cost_holder"].active_rung.return_value = _enum(
        "CostRung", "HARD_STOP_100"
    )
    sources["cost_holder"].current_pct_used.return_value = 1.0
    sources["op_holder"].current.primary_state = _enum(
        "PrimaryState", "ACTIVE"
    )
    snapshots = {
        "sentry": _make_snapshot("sentry", "degraded"),
    }
    with patch(
        "kora_cli.heartbeat_probes.runner.current_service_snapshots",
        return_value=snapshots,
    ):
        alerts = compute_active_alerts()
    # critical (cost_ladder_halted) must come before warning (sentry).
    severities = [a.severity for a in alerts]
    assert severities[0] == "critical"
    assert "warning" in severities


def test_empty_state_returns_empty_list(patch_sources):
    alerts = compute_active_alerts()
    assert alerts == []


def test_alert_to_dict_matches_fe_shape(patch_sources):
    """Alert.to_dict keys must match the FE EmailMessage TS interface
    exactly: id, severity, category, title, detail, source_panel,
    source_panel_route, first_seen_at."""
    sources, _ = patch_sources
    sources["op_holder"].current.primary_state = _enum(
        "PrimaryState", "PAUSED"
    )
    alerts = compute_active_alerts()
    assert len(alerts) >= 1
    d = alerts[0].to_dict()
    assert set(d.keys()) == {
        "id",
        "severity",
        "category",
        "title",
        "detail",
        "source_panel",
        "source_panel_route",
        "first_seen_at",
    }


# ===========================================================================
# Fail-soft
# ===========================================================================


def test_cost_holder_raises_other_rules_still_emit(patch_sources):
    sources, _ = patch_sources
    sources["cost_holder"].active_rung.side_effect = RuntimeError(
        "kaboom"
    )
    sources["op_holder"].current.primary_state = _enum(
        "PrimaryState", "PAUSED"
    )
    alerts = compute_active_alerts()
    ids = [a.id for a in alerts]
    # Cost rule shouldn't emit but operator_paused must still fire.
    assert "operator_paused" in ids
    # No cost alerts at all.
    assert not any(a.id.startswith("cost_ladder") for a in alerts)


def test_operational_holder_none_other_rules_still_emit(patch_sources):
    sources, _ = patch_sources
    with patch(
        "agent.operational_state_holder.get_holder", return_value=None
    ):
        sources["cost_holder"].active_rung.return_value = _enum(
            "CostRung", "WARN_75"
        )
        sources["cost_holder"].current_pct_used.return_value = 0.80
        alerts = compute_active_alerts()
    ids = [a.id for a in alerts]
    assert "cost_ladder_warned" in ids
    assert "operator_paused" not in ids
    assert "operator_stopped" not in ids


def test_audit_reader_raises_other_rules_still_emit(patch_sources):
    sources, _ = patch_sources
    sources["op_holder"].current.primary_state = _enum(
        "PrimaryState", "STOPPED"
    )
    with patch(
        "kora_cli.audit.jsonl_reader.read_audit_entries",
        side_effect=OSError("disk crash"),
    ):
        alerts = compute_active_alerts()
    ids = [a.id for a in alerts]
    assert "operator_stopped" in ids


def test_probe_snapshots_raises_other_rules_still_emit(patch_sources):
    sources, _ = patch_sources
    sources["op_holder"].current.primary_state = _enum(
        "PrimaryState", "PAUSED"
    )
    with patch(
        "kora_cli.heartbeat_probes.runner.current_service_snapshots",
        side_effect=RuntimeError("probe runner crashed"),
    ):
        alerts = compute_active_alerts()
    ids = [a.id for a in alerts]
    assert "operator_paused" in ids


def test_compute_active_alerts_never_raises(monkeypatch):
    """Even if EVERY source raises, the aggregator returns a list
    (possibly empty) rather than propagating."""
    monkeypatch.setattr(
        "agent.cost_state_holder.get_cost_holder",
        MagicMock(side_effect=RuntimeError("cost dead")),
    )
    monkeypatch.setattr(
        "agent.operational_state_holder.get_holder",
        MagicMock(side_effect=RuntimeError("op dead")),
    )
    monkeypatch.setattr(
        "kora_cli.audit.jsonl_reader.read_audit_entries",
        MagicMock(side_effect=RuntimeError("audit dead")),
    )
    monkeypatch.setattr(
        "kora_cli.heartbeat_probes.runner.current_service_snapshots",
        MagicMock(side_effect=RuntimeError("probe dead")),
    )
    alerts = compute_active_alerts()
    assert isinstance(alerts, list)


# ===========================================================================
# Endpoint integration
# ===========================================================================


@pytest.fixture
def _isolate_endpoint(tmp_path, monkeypatch):
    """Apply CC#2 #137 fixture-isolation discipline + reset all
    accessor sources to baseline no-alert state."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setattr("kora_constants.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr("kora_cli.config.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr(
        "kora_cli.web_server.get_kora_home", lambda: tmp_path
    )
    cost_holder = _make_cost_holder("NORMAL")
    op_holder = _make_operational_holder("ACTIVE")
    monkeypatch.setattr(
        "agent.cost_state_holder.get_cost_holder",
        lambda: cost_holder,
    )
    monkeypatch.setattr(
        "agent.operational_state_holder.get_holder",
        lambda: op_holder,
    )
    monkeypatch.setattr(
        "kora_cli.audit.jsonl_reader.read_audit_entries",
        lambda **kwargs: [],
    )
    monkeypatch.setattr(
        "kora_cli.heartbeat_probes.runner.current_service_snapshots",
        lambda: {},
    )
    return {"cost_holder": cost_holder, "op_holder": op_holder}


@pytest.mark.asyncio
async def test_endpoint_returns_expected_shape(_isolate_endpoint):
    from kora_cli import web_server

    result = await web_server.list_current_alerts()
    assert set(result.keys()) == {
        "alerts",
        "stub",
        "generated_at",
        "total_active",
        "by_severity",
    }
    assert result["stub"] is False
    assert isinstance(result["alerts"], list)
    assert isinstance(result["by_severity"], dict)


@pytest.mark.asyncio
async def test_endpoint_no_alerts_returns_empty_list(_isolate_endpoint):
    from kora_cli import web_server

    result = await web_server.list_current_alerts()
    assert result["alerts"] == []
    assert result["total_active"] == 0
    assert result["by_severity"] == {"critical": 0, "warning": 0, "info": 0}


@pytest.mark.asyncio
async def test_endpoint_by_severity_reconciles_total(_isolate_endpoint):
    _isolate_endpoint["cost_holder"].active_rung.return_value = _enum(
        "CostRung", "HARD_STOP_100"
    )
    _isolate_endpoint["cost_holder"].current_pct_used.return_value = 1.0
    _isolate_endpoint["op_holder"].current.primary_state = _enum(
        "PrimaryState", "PAUSED"
    )
    from kora_cli import web_server

    result = await web_server.list_current_alerts()
    by_sev_sum = sum(result["by_severity"].values())
    assert by_sev_sum == result["total_active"]
    assert result["total_active"] >= 2
    assert result["by_severity"]["critical"] >= 2


# ===========================================================================
# SECURITY — walk-payload sweep
# ===========================================================================


@pytest.mark.asyncio
async def test_no_pii_or_secret_shapes_in_payload(_isolate_endpoint):
    """Trigger many rules to exercise diverse alert text; sweep the
    whole serialized response for PII / secret shapes."""
    _isolate_endpoint["cost_holder"].active_rung.return_value = _enum(
        "CostRung", "HARD_STOP_100"
    )
    _isolate_endpoint["cost_holder"].current_pct_used.return_value = 1.0
    _isolate_endpoint["op_holder"].current.primary_state = _enum(
        "PrimaryState", "PAUSED"
    )

    def lots_of_entries(seam=None, since=None):
        if seam == "webhook.dead_letter":
            return [_make_audit_entry(seam) for _ in range(20)]
        if seam == "reasoning.tool_called":
            return [
                _make_audit_entry(
                    seam, details={"tool_status": "execution_error"}
                )
                for _ in range(10)
            ]
        if seam == "slack_dm.reply_failed":
            return [_make_audit_entry(seam) for _ in range(15)]
        return []

    with patch(
        "kora_cli.audit.jsonl_reader.read_audit_entries",
        side_effect=lots_of_entries,
    ), patch(
        "kora_cli.heartbeat_probes.runner.current_service_snapshots",
        return_value={"vercel": _make_snapshot("vercel", "unhealthy")},
    ):
        from kora_cli import web_server

        result = await web_server.list_current_alerts()

    blob = json.dumps(result)
    assert _EMAIL_ADDRESS.findall(blob) == []
    assert _ANTHROPIC_KEY.findall(blob) == []
    assert _HEX_SECRET_SHAPE.findall(blob) == []
    assert _BEARER_TOKEN_SHAPE.findall(blob) == []
