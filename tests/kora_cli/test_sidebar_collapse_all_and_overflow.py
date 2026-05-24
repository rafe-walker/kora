"""KR-FE-SIDEBAR-MOBILE-COLLAPSE-UX — collapse-all + badge-overflow
pins.

Tests cover:
  * Hook surface: useSidebarGroupCollapse exports allCollapsed +
    setAll (used by the Collapse-all/Expand-all shortcut)
  * App.tsx renders the SidebarCollapseAllButton above the groups
  * Badge overflow: formatBadgeCount caps display at "99+" (numeric
    aria-label still carries the exact count for screen readers)
"""

from __future__ import annotations

from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_TSX = _REPO_ROOT / "web" / "src" / "App.tsx"
_HOOK_TS = (
    _REPO_ROOT / "web" / "src" / "hooks" / "useSidebarGroupCollapse.ts"
)


def test_hook_exports_collapse_all_surface():
    """allCollapsed + setAll are the two new public surface methods
    the collapse-all shortcut depends on. Pin both so a refactor that
    removes either breaks at CI rather than silently disables the
    shortcut."""
    src = _HOOK_TS.read_text()
    assert "allCollapsed:" in src
    assert "setAll:" in src
    # The button reads allCollapsed to pick its label; the hook
    # must derive that from the override map + per-group default
    # (matches isCollapsed's resolution order).
    assert "if (override === \"collapsed\") return true" in src
    assert "if (override === \"expanded\") return false" in src


def test_app_renders_collapse_all_button_above_groups():
    """Button must be the FIRST child of the nav, above the groups,
    so operator can collapse without scrolling. Pin both the
    component reference + that it sits BEFORE the groups.map call."""
    src = _APP_TSX.read_text()
    assert "SidebarCollapseAllButton" in src
    btn_idx = src.find("<SidebarCollapseAllButton")
    groups_idx = src.find("sidebarGroups.map")
    assert btn_idx > 0
    assert groups_idx > 0
    assert btn_idx < groups_idx, (
        "SidebarCollapseAllButton must render BEFORE the groups "
        "map; operator should see the shortcut without scrolling"
    )


def test_button_label_flips_with_state():
    """The button's label must derive from ``allCollapsed`` — when
    every group is collapsed the button offers "Expand all"; when
    any group is expanded it offers "Collapse all". Pin both labels
    + the ternary on allCollapsed."""
    src = _APP_TSX.read_text()
    # Pin the inline label-flip ternary (component body).
    assert 'allCollapsed ? "Expand all" : "Collapse all"' in src


def test_collapse_all_button_tappable_on_mobile():
    """KR-FE-SIDEBAR-MOBILE-COLLAPSE-UX: the button must be inside
    the sidebar's nav region (which is itself the sliding overlay
    on mobile). Pin that the button is NOT wrapped in any
    desktop-only ``hidden lg:flex`` etc. class chain — the
    operator on a 375px viewport must see + tap it."""
    src = _APP_TSX.read_text()
    # The button is rendered inline in the nav; no responsive-hide
    # class should be in its component body.
    button_block = src[
        src.find("function SidebarCollapseAllButton") : src.find(
            "function SidebarSystemActions"
        )
    ]
    assert "hidden lg:flex" not in button_block
    assert "lg:hidden" not in button_block


def test_badge_overflow_caps_at_99_plus():
    """formatBadgeCount must cap displayed value at "99+" so a long
    backlog doesn't break the narrow mobile chip. Numeric aria-label
    stays exact (screen reader semantics)."""
    src = _APP_TSX.read_text()
    assert "function formatBadgeCount" in src
    # The cap logic.
    assert 'if (n > 99) return "99+"' in src
    # The accessibility contract: aria-label keeps the exact number.
    assert "aria-label={`${badgeCount} awaiting review`}" in src
    # And renders are routed through formatBadgeCount.
    assert "{formatBadgeCount(badgeCount)}" in src
    assert "{formatBadgeCount(badgeSum)}" in src
