"""Source-pin tests for KR-FRONTEND-CLEANUP Part A.

CC#1's KR-FEAT-HEARTBEAT ST2 (PR #118) added 4 fields to the
heartbeat surface that the FE consumers didn't handle:
  * HeartbeatStatus enum gained "unknown" (probe pending)
  * HeartbeatService.last_check_at became nullable
  * HeartbeatService.error: string | null
  * HeartbeatServicesResponse.cache_warming: boolean

This file pins that HeartbeatPanel.tsx + DashboardPage.tsx render
all four. Follows the CC#2 source-grep pattern (no FE test runner
in the repo) established by KR-MCP-CLIENTS-HEALTH-DISPLAY (#117).
"""

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_PANEL_PATH = _REPO_ROOT / "web" / "src" / "pages" / "HeartbeatPanel.tsx"
_DASHBOARD_PATH = _REPO_ROOT / "web" / "src" / "pages" / "DashboardPage.tsx"


from tests.kora_cli._panel_test_helpers import strip_ts_comments as _strip_ts_comments  # noqa: E402


# ---- 1. source files exist ----------------------------------------


def test_panel_source_exists():
    assert _PANEL_PATH.is_file(), f"missing: {_PANEL_PATH}"


def test_dashboard_source_exists():
    assert _DASHBOARD_PATH.is_file(), f"missing: {_DASHBOARD_PATH}"


# ---- 2. "unknown" status arm handled --------------------------------


def test_panel_status_tone_map_includes_unknown():
    """HeartbeatStatus enum gained "unknown" — the panel's STATUS_TONE
    map must include it so TypeScript exhaustiveness holds and the
    badge gets a tone. Catches a future enum addition that skips
    this consumer."""
    src = _PANEL_PATH.read_text()
    # The literal "unknown:" must appear as a STATUS_TONE map entry
    # (left-hand side of a key/value pair inside the Record literal).
    assert re.search(r"\bunknown\s*:", src), (
        "HeartbeatPanel.tsx STATUS_TONE / STATUS_LABEL maps must "
        'include the "unknown" arm'
    )


def test_panel_status_icon_handles_unknown():
    """StatusIcon switch must include a "unknown" case so TS
    exhaustiveness check passes and the icon renders."""
    src = _PANEL_PATH.read_text()
    assert re.search(r'case "unknown":', src), (
        'HeartbeatPanel.tsx StatusIcon must have a `case "unknown":` arm'
    )


def test_dashboard_heartbeat_counts_include_unknown():
    """The dashboard's HeartbeatCardBody aggregator must bucket
    "unknown" — otherwise the Record<HeartbeatStatus, number> literal
    is incomplete and tsc fails."""
    src = _DASHBOARD_PATH.read_text()
    # The counts object literal must initialize an `unknown:` field.
    assert re.search(
        r"counts:\s*Record<HeartbeatStatus,\s*number>\s*=\s*\{[^}]*\bunknown\s*:",
        src,
        re.DOTALL,
    ), (
        "DashboardPage HeartbeatCardBody counts object must include "
        'the "unknown" key (matches Record<HeartbeatStatus, number>)'
    )


# ---- 3. nullable last_check_at handled -----------------------------


def test_format_timestamp_accepts_null():
    """formatTimestamp + formatRelative must accept `string | null`
    so HeartbeatService.last_check_at: string | null type-checks.
    Catches a regression that drops the null arm from the helper
    signatures.

    Post KR-FE-PANEL-HELPERS-DRY: formatTimestamp + formatRelative
    moved into ``web/src/lib/panelHelpers.ts``. The pin now checks
    the shared module's signatures (the canonical source of truth)
    AND that HeartbeatPanel imports from it (so the assertion isn't
    just verifying an unused module).
    """
    helpers_path = _REPO_ROOT / "web" / "src" / "lib" / "panelHelpers.ts"
    helpers_src = helpers_path.read_text()
    # Both helpers must accept nullable + undefined (the superset
    # nullable signature so panels don't need wrapper logic).
    assert re.search(
        r"function formatTimestamp\(iso:\s*string\s*\|\s*null\s*\|\s*undefined\)",
        helpers_src,
    ), "panelHelpers.formatTimestamp must accept string | null | undefined"
    assert re.search(
        r"function formatRelative\(iso:\s*string\s*\|\s*null\s*\|\s*undefined\)",
        helpers_src,
    ), "panelHelpers.formatRelative must accept string | null | undefined"
    # HeartbeatPanel must import from the shared helpers (so the
    # pin above isn't checking an orphaned module).
    panel_src = _PANEL_PATH.read_text()
    assert re.search(
        r'from\s+"@/lib/panelHelpers"',
        panel_src,
    ), "HeartbeatPanel.tsx must import from @/lib/panelHelpers"


def test_format_relative_null_path_says_never_checked():
    """The null branch of formatRelative should produce a user-
    facing "never checked" label (consistent with the MCP-clients
    health-display pattern from #117), not an empty string."""
    src = _PANEL_PATH.read_text()
    assert '"never checked"' in src, (
        'formatRelative null path should return "never checked" '
        "(matches the MCP-clients health-display convention)"
    )


# ---- 4. error field rendered in expanded view ----------------------


def test_panel_renders_service_error_field():
    """KR-FEAT-HEARTBEAT ST2 error field rendered in the expanded
    detail view. Source-pin: branch on service.error !== null
    and render in a <pre> for multi-line legibility (mirrors
    MCP-clients last_error pattern from #117)."""
    src = _PANEL_PATH.read_text()
    assert "service.error" in src, (
        "HeartbeatPanel.tsx must reference service.error in render"
    )
    # Must appear inside a JSX expression (rendered as text node)
    assert re.search(r"\{[^{}]*service\.error[^{}]*\}", src), (
        "service.error should be rendered as a JSX child expression"
    )


def test_panel_error_rendering_uses_no_dangerously_set_inner_html():
    """The error field is operator-readable text from a probe; same
    untrusted-input contract as MCP-clients last_error. React's
    default escaping handles defanging; this guard catches a
    future edit that switches to dangerouslySetInnerHTML."""
    code = _strip_ts_comments(_PANEL_PATH.read_text())
    assert "dangerouslySetInnerHTML" not in code, (
        "HeartbeatPanel.tsx must not use dangerouslySetInnerHTML — "
        "error field is untrusted probe output"
    )


# ---- 5. cache_warming banner -------------------------------------


def test_panel_renders_cache_warming_banner():
    """KR-FEAT-HEARTBEAT ST2 cache_warming flag: render a "Probes
    warming up…" banner so the operator sees the warming context
    instead of misreading an empty/sparse list as an outage."""
    src = _PANEL_PATH.read_text()
    assert "data.cache_warming" in src, (
        "HeartbeatPanel.tsx must branch on data.cache_warming"
    )
    assert "warming" in src.lower(), (
        "HeartbeatPanel.tsx should render a warming-state affordance"
    )


def test_dashboard_card_suppresses_outage_tone_when_warming():
    """The dashboard card's headline tone must NOT go destructive
    when cache_warming is true — an empty heartbeat list during
    daemon cold-start isn't a real outage. Source-pin: the
    headlineClass derivation branches on data.cache_warming."""
    src = _DASHBOARD_PATH.read_text()
    # Crude but effective: the cache_warming branch must appear in
    # HeartbeatCardBody's logic, and it must influence the headline
    # class (text-muted-foreground), not just be a side affordance.
    hb_body_idx = src.find("function HeartbeatCardBody")
    next_fn_idx = src.find("\nfunction ", hb_body_idx + 1)
    body_slice = src[hb_body_idx:next_fn_idx]
    assert "data.cache_warming" in body_slice, (
        "HeartbeatCardBody must read data.cache_warming"
    )
    assert "text-muted-foreground" in body_slice, (
        "HeartbeatCardBody should mute the headline tone during "
        "warming (no false outage signal)"
    )
