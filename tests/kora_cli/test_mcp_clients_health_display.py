"""Source-pin tests for KR-MCP-CLIENTS-HEALTH-DISPLAY.

The repo has no FE component-test runner (no vitest/jest/jsdom in
``web/package.json``). The CC#2 pattern for FE work is:

  1. tsc + vite build for type / compile correctness
  2. Python source-pin tests for invariants that would silently
     drift if the .tsx file were restyled

This file covers the bucket §4 ship-checklist items + the PM's
explicit security ask ("test asserting that <script> tags in
last_error render as text, not executed") as a source-pin against
``dangerouslySetInnerHTML``. React's default child escaping
guarantees text rendering by default; the *real* risk is a future
edit that switches to dangerouslySetInnerHTML for "richer error
rendering" — that's what this test catches.

Bucket spec hints:
  * stale threshold = 2x default cadence = 10 min
  * error truncation cap = ~80 chars
  * relative-time labels next to status pill
  * plain-text rendering of last_error (no HTML/markdown interp)
"""

import re
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_PANEL_PATH = _REPO_ROOT / "web" / "src" / "pages" / "MCPClientsPanel.tsx"
_HELPERS_PATH = _REPO_ROOT / "web" / "src" / "lib" / "mcpHealth.ts"


from tests.kora_cli._panel_test_helpers import strip_ts_comments as _strip_ts_comments  # noqa: E402


# ---- 0. Source files exist (fast-fail if rename) -------------------


def test_panel_source_file_exists():
    assert _PANEL_PATH.is_file(), f"missing: {_PANEL_PATH}"


def test_helpers_source_file_exists():
    assert _HELPERS_PATH.is_file(), f"missing: {_HELPERS_PATH}"


# ---- 1. SECURITY: no dangerouslySetInnerHTML in panel -------------


def test_panel_uses_no_dangerously_set_inner_html():
    """HARD CONSTRAINT (bucket §2(a) SECURITY): last_error is rendered
    as plain text. React's default child escaping defangs HTML/script
    content automatically — but this guard catches a future edit
    that flips to dangerouslySetInnerHTML for "richer error
    formatting". An MCP catalog probe's stderr / HTTP body is
    untrusted input, so an HTML-injection vector here would let
    a misbehaving MCP server hand the operator a malicious popup.
    """
    # Strip comments first — a 'NEVER use dangerouslySetInnerHTML'
    # warning comment in the source is legitimate documentation, not
    # a contract violation. We're banning the *code* usage.
    code = _strip_ts_comments(_PANEL_PATH.read_text())
    assert "dangerouslySetInnerHTML" not in code, (
        "MCPClientsPanel.tsx must not use dangerouslySetInnerHTML — "
        "last_error is operator-readable text from an untrusted "
        "subprocess/HTTP probe and must render as plain text only"
    )


def test_panel_renders_last_error_as_child_text_node():
    """Belt+braces complement to the dangerouslySetInnerHTML pin:
    confirm last_error is rendered as a JSX child (which React
    escapes) inside the expandable section's <pre>, not assigned
    to an `innerHTML` attribute or piped through a markdown lib.
    """
    src = _PANEL_PATH.read_text()
    # Must render as a child expression — i.e. {client.last_error}
    # appears between JSX tags, not in an attribute position.
    assert re.search(r">\s*\{client\.last_error\}\s*</", src), (
        "MCPClientsPanel.tsx should render client.last_error as a "
        "JSX child expression (text node), not as an attribute or "
        "via a markdown/HTML formatter"
    )


# ---- 2. Stale-snapshot threshold = 10 min --------------------------


def test_stale_threshold_is_ten_minutes():
    """Spec §2(a): "stale" chip when last_check_at older than 10
    minutes (2x the 5-min default cadence). Pin the constant value
    so a future "let's make it 5 min" tweak doesn't accidentally
    fire the chip on every freshly-checked endpoint."""
    src = _HELPERS_PATH.read_text()
    # Match the constant declaration as 10 * 60 * 1000 in ms, in any
    # equivalent arithmetic form. Anchor on the symbol so renames
    # are caught here rather than at runtime.
    match = re.search(
        r"STALE_CHECK_THRESHOLD_MS\s*=\s*([^;]+);",
        src,
    )
    assert match, "STALE_CHECK_THRESHOLD_MS not exported from mcpHealth.ts"
    expr = match.group(1).strip()
    # Evaluate the arithmetic to a number to allow either form
    # (10 * 60 * 1000  or  600_000  or  600000).
    normalized = expr.replace("_", "")
    value = eval(normalized, {"__builtins__": {}}, {})  # noqa: S307
    assert value == 10 * 60 * 1000, (
        f"STALE_CHECK_THRESHOLD_MS = {value} (expected 600000 = 10min)"
    )


# ---- 3. Error truncation cap matches spec --------------------------


def test_error_truncate_len_is_around_eighty():
    """Spec §2(a): collapsed-view error truncated to ~80 chars. Pin
    the constant — short enough to fit on a single panel row, long
    enough to convey "401 Unauthorized: invalid bearer token" etc."""
    src = _HELPERS_PATH.read_text()
    match = re.search(r"ERROR_TRUNCATE_LEN\s*=\s*(\d+)\s*;", src)
    assert match, "ERROR_TRUNCATE_LEN not exported from mcpHealth.ts"
    value = int(match.group(1))
    # ~80 ± 20 is the spec-acceptable range; pin the exact value too
    # so a drift produces a clear failure rather than a soft warning.
    assert 60 <= value <= 100, (
        f"ERROR_TRUNCATE_LEN = {value} drifted outside the spec's "
        f"~80-char window (60..100 acceptable)"
    )
    assert value == 80, (
        f"ERROR_TRUNCATE_LEN = {value} (expected 80 per bucket spec)"
    )


# ---- 4. Helpers export the spec-required functions -----------------


def test_helpers_export_relative_check_formatter():
    src = _HELPERS_PATH.read_text()
    assert re.search(r"export function formatRelativeCheck\b", src), (
        "mcpHealth.ts must export formatRelativeCheck for the panel"
    )


def test_helpers_export_stale_check_predicate():
    src = _HELPERS_PATH.read_text()
    assert re.search(r"export function isStaleCheck\b", src), (
        "mcpHealth.ts must export isStaleCheck for the panel"
    )


def test_helpers_export_error_truncator():
    src = _HELPERS_PATH.read_text()
    assert re.search(r"export function truncateError\b", src), (
        "mcpHealth.ts must export truncateError for the panel"
    )


def test_panel_imports_helpers_from_mcp_health_module():
    """Helpers must come from the dedicated mcpHealth.ts module so
    the source-pin tests above stay load-bearing. Inline duplicate
    implementations would silently bypass the threshold pin."""
    src = _PANEL_PATH.read_text()
    assert re.search(
        r'from\s+"@/lib/mcpHealth"',
        src,
    ), (
        "MCPClientsPanel.tsx must import its health helpers from "
        "@/lib/mcpHealth — inline copies would bypass the pinned "
        "stale-threshold constant"
    )


# ---- 5. Panel renders the required visual elements ----------------


def test_panel_renders_relative_check_next_to_status():
    """Spec §2(a): "checked Xm ago" relative-time label next to the
    status pill. Source-pin: the panel imports and *uses*
    formatRelativeCheck."""
    src = _PANEL_PATH.read_text()
    assert "formatRelativeCheck(" in src, (
        "MCPClientsPanel.tsx should invoke formatRelativeCheck() to "
        "render the relative check timestamp"
    )


def test_panel_renders_stale_chip():
    """Spec §2(a): stale chip when last_check_at > 10 min. Source-pin:
    the panel branches on isStaleCheck() and renders a "stale"
    Badge in the affected path."""
    src = _PANEL_PATH.read_text()
    assert "isStaleCheck(" in src, (
        "MCPClientsPanel.tsx should call isStaleCheck() to decide "
        "whether to render the stale-snapshot chip"
    )
    # The visible chip text "stale" must appear adjacent to a
    # Badge — pin the affordance so a refactor doesn't silently
    # drop the user-visible signal.
    assert re.search(r'>stale\b', src) or "stale</span>" in src or "stale (&gt;10m)" in src, (
        "Stale-snapshot chip text not found in MCPClientsPanel.tsx"
    )


def test_panel_renders_truncated_error_in_collapsed_view():
    """Spec §2(a): collapsed-view error message uses truncateError()."""
    src = _PANEL_PATH.read_text()
    assert "truncateError(" in src, (
        "MCPClientsPanel.tsx should call truncateError() for the "
        "collapsed-view error message"
    )


def test_panel_extends_expanded_view_with_last_check_section():
    """Spec §2(b): "Last Check" section in the expandable detail view
    surfaces the absolute last_check_at timestamp + the full
    last_error in a monospace block. Source-pin: both field names
    appear in the source (the expanded section labels them)."""
    src = _PANEL_PATH.read_text()
    assert "last_check_at" in src, (
        "Expanded view should label the last_check_at field"
    )
    assert "last_error" in src, (
        "Expanded view should label the last_error field"
    )
    # Monospace rendering for multi-line error readability
    assert "<pre " in src, (
        "Full last_error should render in a <pre> for multi-line "
        "error legibility per spec §2(b)"
    )


# ---- 6. Aggregate strip updated to reflect last_error ---------------


def test_aggregate_errors_count_includes_last_error_clients():
    """Spec §2(c): aggregate "errors" count should now include
    clients with last_error !== null, not just status=error/unhealthy.
    Catches the case where status=connected but the last probe
    surfaced an error message (e.g., probe ran in fallback mode)."""
    src = _PANEL_PATH.read_text()
    # The errors filter expression must include a check on last_error
    assert re.search(
        r"errors:\s*data\.clients\.filter\(.*last_error\s*!==\s*null",
        src,
        re.DOTALL,
    ), (
        "Aggregate errors filter should include c.last_error !== null "
        "per bucket spec §2(c)"
    )


# ---- 7. Cron-regression sanity -------------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_health_display(tmp_path, monkeypatch):
    """Smoke-pin: rendering changes shouldn't have touched the cron
    endpoint, but assert it still imports and returns a list."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("kora_cli.config.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr(
        "kora_cli.config.get_config_path", lambda: tmp_path / "config.yaml"
    )
    monkeypatch.setattr(
        "kora_cli.config.get_env_path", lambda: tmp_path / ".env"
    )

    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
