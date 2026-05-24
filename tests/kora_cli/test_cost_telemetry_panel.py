"""Source-pin tests for KR-FE-COST-TELEMETRY-PANEL.

Per the CC#2 source-pin discipline (no FE component test runner
in the repo) — verifies the new page wires correctly without
requiring a runtime browser.

Scenarios:
  1. api.getCostTelemetry wrapper exists + posts to /api/cost_telemetry
  2. RouteCounters + CostTelemetryResponse TS types declared
  3. SnapshotResponse.cost_telemetry typed (not opaque `unknown`)
  4. CostTelemetryPage.tsx exists + uses usePanelView
  5. Three windows declared (process_lifetime / rolling_24h / monthly)
  6. Page tries snapshot first; falls back to /api/cost_telemetry
  7. Force-refresh button bypasses snapshot
  8. KNOWN_ROUTES literals match the backend writer at
     cost_telemetry.py:73-81 exactly
  9. ROUTE_BUCKET_REF cites the queued-consumer bucket per
     reserved-no-consumer route
 10. Escalation rate color bands per feedback-opus-escalation-
     must-be-earned (5-15% target green)
 11. Pre-router state surfaces gracefully (no escalation rate
     until KR-HAIKU-ROUTER ships)
 12. Cache effectiveness math handles zero-denom edge case
 13. Route nav entry + path registered in App.tsx
 14. Chart implementation uses plain SVG / divs (no new dep)
"""

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_API_TS = _REPO_ROOT / "web" / "src" / "lib" / "api.ts"
_APP_TSX = _REPO_ROOT / "web" / "src" / "App.tsx"
_PAGE = _REPO_ROOT / "web" / "src" / "pages" / "CostTelemetryPage.tsx"
_WEB_PKG = _REPO_ROOT / "web" / "package.json"
_TELEMETRY_PY = _REPO_ROOT / "kora_cli" / "telemetry" / "cost_telemetry.py"


# ---- 1-3. api wrapper + types --------------------------------


def test_api_get_cost_telemetry_wrapper_exists():
    src = _API_TS.read_text()
    assert re.search(
        r"getCostTelemetry:\s*\(\)\s*=>\s*fetchJSON<CostTelemetryResponse>\(\"/api/cost_telemetry\"\)",
        src,
    )


def test_route_counters_type_declared():
    src = _API_TS.read_text()
    assert "export interface RouteCounters" in src
    # Match the backend writer's _RouteCounters fields verbatim
    for field in (
        "calls_count",
        "input_tokens_total",
        "output_tokens_total",
        "cache_read_tokens_total",
        "cache_creation_tokens_total",
        "cost_estimate_usd_total",
        "escalation_count",
        "model_breakdown",
    ):
        assert field in src


def test_cost_telemetry_response_type_declared():
    src = _API_TS.read_text()
    assert "export interface CostTelemetryResponse" in src
    for window in ("process_lifetime", "rolling_24h", "monthly"):
        assert window in src


def test_snapshot_cost_telemetry_typed_not_unknown():
    """SnapshotResponse.cost_telemetry was opaque `unknown` per PR
    #162's wrapper; this bucket refines it to the typed
    {rolling_24h, monthly} dict. Pin keeps the type sharp so
    consumers don't fall back to unsafe casts."""
    src = _API_TS.read_text()
    assert re.search(
        r"cost_telemetry\?:\s*\{[^}]*rolling_24h:\s*Record<string,\s*RouteCounters>",
        src,
        re.DOTALL,
    )


# ---- 4-5. Page exists + windows ---------------------------


def test_cost_telemetry_page_exists():
    assert _PAGE.is_file()


def test_page_uses_panel_view_hook():
    src = _PAGE.read_text()
    assert 'usePanelView("CostTelemetryPage")' in src


def test_three_windows_declared():
    src = _PAGE.read_text()
    for w in ("process_lifetime", "rolling_24h", "monthly"):
        assert w in src
    # Tab labels readable
    for label in ("Lifetime", "Rolling 24h", "Monthly"):
        assert label in src


# ---- 6-7. Snapshot-first + force-refresh -----------------


def test_loads_from_snapshot_first():
    """Snapshot-first path mirrors DashboardPage (PR #162):
    api.getSnapshot before any fan-out to /api/cost_telemetry."""
    src = _PAGE.read_text()
    assert "loadFromSnapshot" in src
    assert re.search(
        r"loadFromSnapshot[^{]*\{[^}]*api\.getSnapshot\(\)",
        src,
        re.DOTALL,
    )


def test_force_refresh_bypasses_snapshot():
    src = _PAGE.read_text()
    assert "forceLiveRefresh" in src
    # The force path calls the endpoint directly (loadFromEndpoint),
    # not the snapshot-first loadInitial.
    assert re.search(
        r"forceLiveRefresh[^{]*\{[^}]*loadFromEndpoint\(\)",
        src,
        re.DOTALL,
    )


def test_lifetime_tab_falls_through_to_endpoint():
    """process_lifetime is endpoint-only by design (the snapshot
    doesn't carry it — keeps on-disk size bounded). Picking the
    Lifetime tab when current data lacks process_lifetime should
    auto-fetch the endpoint."""
    src = _PAGE.read_text()
    assert re.search(
        r"process_lifetime[^=]*===\s*0[^}]*loadFromEndpoint\(\)",
        src,
        re.DOTALL,
    ), (
        "Lifetime tab must auto-fetch /api/cost_telemetry when the "
        "snapshot path left process_lifetime empty"
    )


# ---- 8. KNOWN_ROUTES literal match ------------------------


def test_known_routes_match_backend_writer():
    """The FE's KNOWN_ROUTES array must match the route literals
    declared in kora_cli/telemetry/cost_telemetry.py:73-81
    EXACTLY. Drift between the two = silent missing rows on the
    panel for a new route."""
    py_src = _TELEMETRY_PY.read_text()
    py_routes = set()
    for match in re.finditer(r'^ROUTE_[A-Z_]+ = "([a-z_]+)"', py_src, re.MULTILINE):
        py_routes.add(match.group(1))
    ts_src = _PAGE.read_text()
    ts_match = re.search(
        r"const KNOWN_ROUTES = \[([^\]]+)\]",
        ts_src,
        re.DOTALL,
    )
    assert ts_match, "CostTelemetryPage must declare KNOWN_ROUTES"
    ts_routes = set()
    for literal in re.findall(r'"([a-z_]+)"', ts_match.group(1)):
        ts_routes.add(literal)
    assert py_routes == ts_routes, (
        f"KNOWN_ROUTES drift: backend has {py_routes - ts_routes} "
        f"that FE lacks; FE has {ts_routes - py_routes} that "
        f"backend lacks"
    )


# ---- 9. Reserved-no-consumer bucket references ----------


def test_reserved_routes_have_bucket_references():
    """Per spec: reserved-no-consumer routes show '[Awaiting
    consumer]' with the queued bucket name. Pin that the
    ROUTE_BUCKET_REF map covers every non-active known route
    so the panel never says '[Awaiting consumer]' without
    naming which bucket."""
    src = _PAGE.read_text()
    # The map literal must reference each of the 7 reserved routes.
    # Skip slack_dm (active) + unknown (fallback).
    for route in (
        "email_inbound",
        "email_outbound_compose",
        "mcp_tool",
        "alert_investigation",
        "probe_investigation",
        "tool_loop_iteration",
        "scheduled_task",
    ):
        assert re.search(
            rf"{route}:\s*\"KR-[A-Z-]+\"",
            src,
        ), (
            f"ROUTE_BUCKET_REF must cite the queued bucket for "
            f"reserved route '{route}'"
        )


def test_awaiting_consumer_marker_renders():
    src = _PAGE.read_text()
    assert "[Awaiting consumer]" in src


# ---- 10-11. Escalation rate color bands -------------------


def test_escalation_color_bands_match_feedback_thresholds():
    """Per feedback-opus-escalation-must-be-earned: 5-15% is the
    target band (Haiku-router earning Opus at the right rate).
    Below 5% means classifier too tight (cheap-substrate not
    landing); above 30% means classifier too loose."""
    src = _PAGE.read_text()
    assert re.search(
        r"ratePct\s*>=\s*5\s*&&\s*ratePct\s*<=\s*15",
        src,
    ), "green band must be 5-15%"
    assert re.search(
        r"ratePct\s*>\s*30",
        src,
    ), "red band threshold must be >30%"


def test_pre_router_state_renders_gracefully():
    """Pre-KR-HAIKU-ROUTER: escalation_count is always 0 because
    no router exists. Display 'pre-router' instead of '0.0%'
    which would imply a (good!) 0% escalation rate from a router
    that doesn't exist yet."""
    src = _PAGE.read_text()
    assert '"pre-router"' in src
    assert re.search(
        r"escalation_count\s*===\s*0[^}]*pre-router",
        src,
        re.DOTALL,
    )


# ---- 12. Cache effectiveness math ------------------------


def test_cache_effectiveness_handles_zero_denom():
    """When no input tokens recorded yet (fresh telemetry), the
    bar must render a 'no data' tile instead of dividing by zero."""
    src = _PAGE.read_text()
    assert "No input tokens recorded" in src


def test_uncached_input_floored_at_zero():
    """Defensive: counter accounting could drift (cache_read +
    cache_creation > total_input is impossible per the writer,
    but FE should not crash if it ever happens). uncached
    derivation floors at 0."""
    src = _PAGE.read_text()
    assert re.search(
        r"Math\.max\(\s*0\s*,[^)]*total_input[^)]*cache_read[^)]*cache_creation",
        src,
        re.DOTALL,
    )


# ---- 13. Route registration -------------------------------


def test_route_registered_in_app_tsx():
    src = _APP_TSX.read_text()
    assert '"/cost-telemetry": CostTelemetryPage' in src
    # Nav entry — label visible + path matches
    assert re.search(
        r'path:\s*"/cost-telemetry"[^}]+labelKey:\s*"costTelemetry"',
        src,
        re.DOTALL,
    )


# ---- 14. Chart-library choice: plain SVG / divs only ---


def test_no_chart_library_added():
    """Per spec §4 STOP-ASK preference: no new dep for charts.
    The bars are CSS-width divs inside a flex container — pin
    that nothing like recharts / chart.js / d3 / victory was
    added to package.json."""
    pkg = _WEB_PKG.read_text()
    banned = ("recharts", "chart.js", "victory", "d3", "echarts", "plotly")
    for lib in banned:
        assert lib not in pkg, (
            f"Chart library '{lib}' must NOT be added per spec § "
            f"plain-SVG/divs preference"
        )


def test_chart_implementation_uses_css_widths():
    """Cache effectiveness + model breakdown bars are flex
    containers with per-segment divs whose widths come from
    inline style={{ width: `${pct}%` }}."""
    src = _PAGE.read_text()
    assert "CacheEffectivenessBar" in src
    assert "ModelBreakdownBar" in src
    # Both use the same inline-style-percentage pattern
    assert re.search(
        r"style=\{\{\s*width:\s*`\$\{[a-zA-Z]+Pct\}%`\s*\}\}",
        src,
    )
