"""Source-pin tests for KR-FE-DASHBOARD-SNAPSHOT-WIRE +
KR-FE-DASHBOARD-SNAPSHOT-FULLY-WIRED.

PR #162 (snapshot-wire) shipped 2-of-4 hero fields to snapshot +
PINNED the other 2 (cost + health) on fan-out because the snapshot
substrate lacked the necessary fields at the time. PR #169
(snapshot v3 cost_ladder.spent_to_date_usd + credit_pool_usd) +
PR #170 (snapshot v4 daemon_health) closed those data gaps. This
bucket shifts cost + health TO snapshot — so the anti-projection
pins from #162 (tests 4-a + 4-b) flip to assert the INVERSE
invariant: snapshot is now the source-of-truth for the hero
cards on the warm-cache path; fan-out is the per-field fallback.

Scenarios:
  1. api.getSnapshot wrapper exists + posts to /api/snapshot
  2. SnapshotResponse + SnapshotUnavailable TS types declared
     (incl. cost_ladder v3 + daemon_health v4 fields)
  3. FreshnessBadge component exists + supports the 3 modes
     (snapshot / live / unavailable) + mixed sub-mode
  4. DashboardPage projects ALL FOUR hero fields from snapshot:
     a. operational
     b. alerts
     c. cost  (FLIPPED — was anti-pinned in PR #162; now projected
        when snapshot.cost_ladder has populated USD fields)
     d. health (FLIPPED — was anti-pinned in PR #162; now projected
        when snapshot.daemon_health has populated overall_status)
  5. DashboardPage initial path tries snapshot first (loadInitial,
     not direct fan-out)
  6. forceFullLiveRefresh bypasses snapshot
  7. AlertsBanner renders from total_active (snapshot-compat)
     rather than alerts.length
  8. AlertsBanner dismissal hash gracefully degrades to aggregate
     shape when per-alert array is empty
  9. Anti-coercion preserved per-field: projection helpers return
     null when snapshot underlying is "unknown" → caller fans out
     for THAT field. Pins the null-return contract so a future
     refactor doesn't accidentally project misleading zeros.
 10. FreshnessBadge surfaces N-of-M hero-fields-from-snapshot
     count (KR-FE-DASHBOARD-SNAPSHOT-FULLY-WIRED). Operator can
     tell "all 4 from snapshot" from "2 of 4 from snapshot".
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
    # Must include the snapshot-covered field roots, incl. the v3
    # cost_ladder additions (PR #169) + v4 daemon_health section
    # (PR #170). The dashboard projection helpers depend on these
    # being typed — TS compile fails otherwise.
    for field in (
        "operational_state",
        "alerts",
        "cost_ladder",
        "service_health",
        "computed_at",
        "schema_version",
        # PR #169 cost_ladder v3 additions
        "spent_to_date_usd",
        "credit_pool_usd",
        # PR #170 daemon_health v4 section
        "daemon_health",
        "overall_status",
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


def test_freshness_badge_surfaces_hero_field_count():
    """KR-FE-DASHBOARD-SNAPSHOT-FULLY-WIRED — the badge must
    visually distinguish "all 4 hero fields fresh" from "2 of 4
    fresh" (= partial-snapshot path where cost or health fell
    back to fan-out per-field).

    Operator question being answered: "is the warm-cache path
    delivering everything, or did the cost holder not initialize
    yet?" Without this surface the operator can't tell, and the
    incremental value of #169 + #170 isn't visible.
    """
    src = _BADGE.read_text()
    assert "snapshotProjectedHeroCount" in src, (
        "badge must accept a snapshot-projected hero-field count"
    )
    assert "totalHeroFields" in src
    # The "all N from snapshot" copy when N >= total.
    assert "all" in src.lower() and "hero fields from snapshot" in src
    # The "K of N from snapshot" copy when K < total.
    assert "of " in src and "hero fields from snapshot" in src


def test_dashboard_passes_hero_count_to_freshness_badge():
    """Pin the wiring: DashboardPage must pass the projected-hero
    count to the badge. The literal `DASHBOARD_HERO_FIELD_COUNT`
    must equal 4 (operational + alerts + cost + health) so the
    badge's "all 4" copy matches the spec."""
    src = _DASHBOARD.read_text()
    assert "DASHBOARD_HERO_FIELD_COUNT" in src
    assert re.search(
        r"DASHBOARD_HERO_FIELD_COUNT\s*=\s*4",
        src,
    ), "literal hero count must equal 4 (the 4 spec'd hero fields)"
    assert "snapshotProjectedHeroCount=" in src
    assert "totalHeroFields={DASHBOARD_HERO_FIELD_COUNT}" in src
    # The hero-field keys must be exactly the 4 spec'd ones; this
    # prevents a future refactor from silently expanding the set
    # to e.g. "boot" + "dr" (which aren't snapshot-driven) and
    # making the "of 4" count meaningless.
    assert re.search(
        r'\["operational",\s*"alerts",\s*"cost",\s*"health"\]\s*as\s*const',
        src,
    ), "hero-field keys for the projection count must be the 4 spec'd fields"


# ---- 4. DashboardPage projection: all 4 hero fields ----------


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


def test_dashboard_projects_cost_from_snapshot():
    """KR-FE-DASHBOARD-SNAPSHOT-FULLY-WIRED — FLIPPED from
    test_dashboard_does_not_project_cost_from_snapshot (PR #162).

    PR #169 closed the spent_to_date_usd + credit_pool_usd gap
    that kept cost on fan-out. The helper now exists; it returns
    a CostStateResponse when snapshot's cost_ladder has populated
    USD fields (no $0 coercion), or null when fields are still
    "unknown" — caller fans out for cost in that case (per-field
    granularity preserves the original anti-coercion discipline).
    """
    src = _DASHBOARD.read_text()
    assert "projectCostFromSnapshot" in src, (
        "cost MUST now be projected from snapshot — PR #169 added "
        "the USD fields, this bucket flips the projection"
    )
    # The helper must read spent_to_date_usd + credit_pool_usd
    # from snap.cost_ladder.
    assert "spent_to_date_usd" in src
    assert "credit_pool_usd" in src
    # Null-return contract: when USD is "unknown", helper returns
    # null so caller fans out. Without this branch, the projection
    # would silently render misleading zeros — the very thing
    # PR #162 wisely refused to do.
    assert re.search(
        r'cl\.spent_to_date_usd\s*===\s*"unknown"',
        src,
    ), (
        "projectCostFromSnapshot must return null when USD is "
        "'unknown' — preserves anti-coercion discipline per-field"
    )
    # Caller branches on null to decide fan-out vs no-fan-out.
    # The conditional push into the remaining-fetches list pins
    # the call-site fallback path.
    assert "projectedCost === null" in src, (
        "loadInitial must fan out for cost only when projection "
        "returns null"
    )


def test_dashboard_projects_health_from_snapshot():
    """KR-FE-DASHBOARD-SNAPSHOT-FULLY-WIRED — FLIPPED from
    test_dashboard_does_not_project_health_from_snapshot (PR #162).

    PR #170 added the daemon_health section to the snapshot,
    closing the semantic-mismatch gap. The helper projects
    overall_status → HealthRollupResponse.overall (with the
    "unhealthy"→"outage" enum mapping; documented in the helper).
    Returns null when overall_status is "unknown" so the caller
    fans out per-field.

    Service_health (SaaS deps) is NOT used for HealthHero per the
    PR #162 reasoning — daemon_health is the correct snapshot
    section for Kora's own health.
    """
    src = _DASHBOARD.read_text()
    assert "projectHealthFromSnapshot" in src, (
        "health MUST now be projected from snapshot — PR #170 "
        "added daemon_health, this bucket flips the projection"
    )
    # Must read from snap.daemon_health, NOT snap.service_health
    # (still the wrong section per the original PR #162 analysis).
    assert "snap.daemon_health" in src
    # The helper must not project from service_health (the SaaS
    # deps section) — that would re-introduce the semantic
    # mismatch PR #162 correctly avoided.
    assert "projectHealthFromSnapshot" in src
    helper_body = src.split("projectHealthFromSnapshot")[1].split("\n\n")[0:3]
    helper_text = "".join(helper_body)
    assert "service_health" not in helper_text, (
        "projectHealthFromSnapshot must read daemon_health, NOT "
        "service_health — service_health is SaaS-deps, semantic "
        "mismatch with HealthRollupResponse.overall (Kora daemon)"
    )
    # Null-return contract for fan-out fallback per-field.
    assert re.search(
        r'dh\.overall_status\s*===\s*"unknown"',
        src,
    ), (
        "projectHealthFromSnapshot must return null when "
        "overall_status is 'unknown' — preserves anti-coercion "
        "per-field"
    )
    assert "projectedHealth === null" in src, (
        "loadInitial must fan out for health only when projection "
        "returns null"
    )


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
