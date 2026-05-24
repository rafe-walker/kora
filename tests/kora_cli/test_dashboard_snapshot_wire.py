"""Source-pin tests for KR-FE-DASHBOARD-SNAPSHOT-WIRE.

Backend /api/snapshot endpoint was already covered by the
snapshot-infrastructure bucket (PR #157). This file pins the FE
wiring + the explicit choice of which snapshot fields project
into which dashboard cards.

Scenarios:
  1. api.getSnapshot wrapper exists + posts to /api/snapshot
  2. SnapshotResponse + SnapshotUnavailable TS types declared
  3. FreshnessBadge component exists + supports the 3 modes
     (snapshot / live / unavailable) + mixed sub-mode
  4. DashboardPage projects ONLY operational + alerts from snapshot;
     cost + health stay on fan-out (anti-coercion discipline)
  5. DashboardPage initial path tries snapshot first (loadInitial,
     not direct fan-out)
  6. forceFullLiveRefresh bypasses snapshot
  7. AlertsBanner renders from total_active (snapshot-compat)
     rather than alerts.length
  8. AlertsBanner dismissal hash gracefully degrades to aggregate
     shape when per-alert array is empty
"""

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_API_TS = _REPO_ROOT / "web" / "src" / "lib" / "api.ts"
_DASHBOARD = _REPO_ROOT / "web" / "src" / "pages" / "DashboardPage.tsx"
_BADGE = _REPO_ROOT / "web" / "src" / "components" / "FreshnessBadge.tsx"
_ALERTS_BANNER = _REPO_ROOT / "web" / "src" / "components" / "AlertsBanner.tsx"


# ---- 1-2. api wrapper + types ----------------------------------


def test_api_get_snapshot_wrapper_exists():
    src = _API_TS.read_text()
    assert re.search(
        r"getSnapshot:\s*\(\)\s*=>\s*fetchJSON<\s*SnapshotResponse\s*\|\s*SnapshotUnavailable\s*>\(\"/api/snapshot\"\)",
        src,
    ), "api.getSnapshot must wrap GET /api/snapshot with the union type"


def test_snapshot_response_type_declared():
    src = _API_TS.read_text()
    assert "export interface SnapshotResponse" in src
    # Must include the 4 snapshot-covered field roots
    for field in (
        "operational_state",
        "alerts",
        "cost_ladder",
        "service_health",
        "computed_at",
        "schema_version",
    ):
        assert field in src, (
            f"SnapshotResponse must declare the `{field}` field"
        )


def test_snapshot_unavailable_type_declared():
    src = _API_TS.read_text()
    assert "export interface SnapshotUnavailable" in src
    assert 'error: "no_snapshot"' in src


# ---- 3. FreshnessBadge ----------------------------------------


def test_freshness_badge_component_exists():
    assert _BADGE.is_file(), f"missing: {_BADGE}"


def test_freshness_badge_supports_three_modes():
    src = _BADGE.read_text()
    # Type union for the mode prop
    assert re.search(
        r'FreshnessMode\s*=\s*"snapshot"\s*\|\s*"live"\s*\|\s*"unavailable"',
        src,
    ), "FreshnessBadge must declare a 3-mode union"


def test_freshness_badge_force_refresh_button_present():
    src = _BADGE.read_text()
    assert "onForceRefresh" in src
    assert "Force live refresh" in src, (
        "Badge must render a labeled Force-refresh button"
    )


def test_freshness_badge_zero_cost_hint_visible():
    """Cost-economy thesis made visible: $0 hint must literally
    appear in the snapshot-mode render path."""
    src = _BADGE.read_text()
    assert "$0 cost view" in src, (
        "Badge must surface '$0 cost view' for the snapshot path "
        "— operator must see the cost-economy difference"
    )


def test_freshness_badge_mixed_mode_indicates_overrides():
    """When per-card refresh overrides snapshot-projected fields,
    badge must visually flag the mixed state."""
    src = _BADGE.read_text()
    assert "liveOverrideCount" in src
    assert "force-refreshed" in src


# ---- 4. DashboardPage projection: ops + alerts only ----------


def test_dashboard_projects_operational_from_snapshot():
    src = _DASHBOARD.read_text()
    assert "projectOperationalFromSnapshot" in src
    # Must populate primary_state from snapshot.operational_state.primary
    assert re.search(
        r"primary_state:\s*ops\.primary",
        src,
    ), "operational projection must map snapshot.operational_state.primary"


def test_dashboard_projects_alerts_from_snapshot():
    src = _DASHBOARD.read_text()
    assert "projectAlertsFromSnapshot" in src
    # total_active must come from snap.alerts.active_count
    assert re.search(
        r"total_active:\s*snap\.alerts\.active_count",
        src,
    )


def test_dashboard_does_not_project_cost_from_snapshot():
    """SPEC §2(b): leave fan-out rather than coerce. Snapshot's
    cost_ladder lacks spent_to_date_usd / credit_pool_usd which
    CostCardBody renders prominently — coercion would display
    misleading $0 figures.

    Pin: no projectCostFromSnapshot helper; cost still fans out
    via api.getCostState in the snapshot-success path."""
    src = _DASHBOARD.read_text()
    assert "projectCostFromSnapshot" not in src, (
        "cost must NOT be projected from snapshot — snapshot lacks "
        "USD values, would mislead operator with $0 figures"
    )


def test_dashboard_does_not_project_health_from_snapshot():
    """SPEC §2(b): snapshot's service_health is SaaS-dependency
    health (vercel/sentry/etc), NOT Kora's own control_plane /
    worker daemons. Semantic mismatch — would conflate dep health
    with daemon health in HealthHero."""
    src = _DASHBOARD.read_text()
    assert "projectHealthFromSnapshot" not in src
    assert "projectServiceHealthFromSnapshot" not in src


# ---- 5. Snapshot-first initial path --------------------------


def test_dashboard_initial_path_tries_snapshot_first():
    src = _DASHBOARD.read_text()
    # loadInitial calls api.getSnapshot BEFORE any fan-out
    assert "loadInitial" in src
    assert re.search(
        r"loadInitial[^{]*\{[^}]*api\.getSnapshot\(\)",
        src,
        re.DOTALL,
    ), "loadInitial must call api.getSnapshot first"


def test_dashboard_falls_back_to_fan_out_on_snapshot_unavailable():
    src = _DASHBOARD.read_text()
    # Branch on `"error" in snap` to enter the fan-out fallback
    assert '"error" in snap' in src
    assert "fanOutAll" in src
    assert "setSnapshotMode(\"unavailable\")" in src


# ---- 6. Force-refresh bypasses snapshot --------------------


def test_dashboard_force_full_live_refresh_bypasses_snapshot():
    src = _DASHBOARD.read_text()
    assert "forceFullLiveRefresh" in src
    # The force path sets mode="live" and runs fanOutAll directly
    # (not via loadInitial which would try the snapshot first)
    assert re.search(
        r"forceFullLiveRefresh[^{]*\{[^}]*setSnapshotMode\(\"live\"\)",
        src,
        re.DOTALL,
    )


# ---- 7-8. AlertsBanner snapshot-compat -------------------


def test_alerts_banner_uses_total_active_for_visibility():
    """Per snapshot projection, alerts.length may be 0 when
    total_active > 0 (snapshot carries only aggregate counts).
    Banner must hide based on total_active, NOT alerts.length —
    otherwise the snapshot path silently suppresses the banner."""
    src = _ALERTS_BANNER.read_text()
    assert re.search(
        r"data\.total_active\s*===\s*0",
        src,
    ), "AlertsBanner must use total_active for the empty-check"


def test_alerts_banner_dismissal_hash_handles_empty_per_alert_array():
    """When alerts: [] (snapshot path), the dismissal hash must
    fall through to an aggregate hash so a future alert addition
    still triggers re-dismissal — operator doesn't get silenced
    across new alert shapes just because we're on the $0 path."""
    src = _ALERTS_BANNER.read_text()
    # The fall-through hash uses an "agg:" prefix per the
    # KR-FE-DASHBOARD-SNAPSHOT-WIRE patch
    assert "agg:" in src, (
        "AlertsBanner dismissal hash must have an aggregate-shape "
        "fall-through for the snapshot-projection path"
    )
