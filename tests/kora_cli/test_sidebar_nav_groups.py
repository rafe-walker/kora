"""KR-FE-COCKPIT-NAV-RESTRUCTURE — drift-guard + orphan-page pins
for the grouped sidebar nav.

The sidebar is grouped into 9 operator-friendly buckets (per
CC#2's #198 recommendation: past 25 entries, scan time was too
high). Each path declared in BUILTIN_NAV_GROUPS must belong to
exactly ONE group; any new page added without a group placement
gets caught by the orphan-page test.

Drift-guards:
  1. Canonical group order matches what the FE renders
  2. Every nav path belongs to exactly one group (orphan detection)
  3. ``SIDEBAR_GROUP_KEYS_IN_ORDER`` + ``SIDEBAR_PATH_TO_GROUP``
     are exported as the test-pin surface
  4. Per-group collapse state hook + storage key are pinned
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_TSX = _REPO_ROOT / "web" / "src" / "App.tsx"
_HOOK_TSX = (
    _REPO_ROOT / "web" / "src" / "hooks" / "useSidebarGroupCollapse.ts"
)


def _parse_sidebar_groups(src: str) -> dict[str, list[str]]:
    """Walk the BUILTIN_NAV_GROUPS array literal in App.tsx and
    return {group_key: [path, ...]}.

    The source is structured TS — naive regex parsing is enough
    because each group is one literal block with ``key:`` /
    ``items:`` keys and item paths inside ``{ path: "X" ... }``
    objects. A more robust parser would be overkill for a single
    config block.
    """
    # Find the BUILTIN_NAV_GROUPS array body.
    m = re.search(
        r"const BUILTIN_NAV_GROUPS:\s*readonly SidebarNavGroupDef\[\]\s*=\s*\[",
        src,
    )
    assert m is not None, "BUILTIN_NAV_GROUPS not found in App.tsx"
    start = m.end()

    # Walk forward, tracking brace depth to find the matching ``];``.
    depth = 1
    i = start
    while i < len(src) and depth > 0:
        ch = src[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        i += 1
    body = src[start : i - 1]

    # Split into per-group blocks. Each group is `{ key: "X", ... items: [...] }`.
    # Find each ``key: "..."`` followed (eventually) by ``items: [...]``.
    groups: dict[str, list[str]] = {}
    for group_match in re.finditer(
        r'key:\s*"([^"]+)",[^}]*?items:\s*\[([\s\S]*?)\],?\s*\}',
        body,
    ):
        key = group_match.group(1)
        items_body = group_match.group(2)
        paths = re.findall(r'path:\s*"([^"]+)"', items_body)
        groups[key] = paths
    return groups


# ---------------------------------------------------------------------------
# Group structure pins
# ---------------------------------------------------------------------------


def test_canonical_group_order_pinned():
    """Pin the operator-facing group order. Re-arrangement in App.tsx
    is fine — bump this list to match if you intend the change."""
    src = _APP_TSX.read_text()
    groups = _parse_sidebar_groups(src)
    expected_order = [
        "overview",
        "watch",
        "email",
        "promotion",
        "activity",
        "control",
        "daemon",
        "settings",
        "diagnostic",
    ]
    assert list(groups.keys()) == expected_order, (
        f"sidebar group order changed: {list(groups.keys())}"
    )


def test_no_orphan_paths():
    """Every path in the merged group items list must be declared in
    exactly one group. An orphan path = a new page added to
    App.tsx without a group placement → it would render somewhere
    arbitrary in the sidebar (whichever group its index falls into
    via the plugin-position fallback). Catch at CI."""
    src = _APP_TSX.read_text()
    groups = _parse_sidebar_groups(src)
    seen: dict[str, str] = {}
    for group_key, paths in groups.items():
        for p in paths:
            assert p not in seen, (
                f"path {p!r} declared in both {seen[p]!r} and "
                f"{group_key!r} — sidebar orphan/duplicate"
            )
            seen[p] = group_key
    # The flat BUILTIN_NAV_REST derived list must equal the union
    # of group items (no flat-list-only entries).
    flat_match = re.search(
        r"const BUILTIN_NAV_REST:\s*NavItem\[\]\s*=\s*BUILTIN_NAV_GROUPS\.flatMap",
        src,
    )
    assert flat_match, (
        "BUILTIN_NAV_REST must derive from BUILTIN_NAV_GROUPS via flatMap "
        "(can't be hand-maintained alongside groups — invites drift)"
    )


def test_expected_paths_appear_in_expected_groups():
    """Spot-check that the operator-critical pages land in the
    operator-expected groups. (Not exhaustive — orphan test above
    covers the placement-existence side.)"""
    src = _APP_TSX.read_text()
    groups = _parse_sidebar_groups(src)

    expected_placements = {
        "/alerts": "overview",
        "/": "overview",
        "/kora-actions": "overview",
        "/health-rollup": "watch",
        "/probe-investigations": "watch",
        "/alert-investigations": "watch",
        "/cost-state": "watch",
        "/cost-telemetry": "watch",
        "/email": "email",
        "/email-intent-log": "email",
        "/email-intent-log/logged-only": "email",
        "/outbound-email-log": "email",
        "/promotions/phrasebook": "promotion",
        "/phrasebook": "promotion",
        "/reasoning": "activity",
        "/slack-dm": "activity",
        "/probe-autofix-log": "activity",
        "/sea-tickets": "control",
        "/kora-control": "control",
        "/boot-status": "daemon",
        "/cron": "daemon",
        "/plugins": "daemon",
        "/config": "settings",
        "/env": "settings",
        "/models": "settings",
        "/logs": "diagnostic",
        "/docs": "diagnostic",
        "/runbooks": "diagnostic",
    }
    for path, group in expected_placements.items():
        found_in = [g for g, paths in groups.items() if path in paths]
        assert found_in == [group], (
            f"{path!r} expected in {group!r} but found in {found_in}"
        )


def test_default_collapsed_groups_are_the_less_used():
    """Default-collapsed groups should be the less-used ones —
    keep the top-of-sidebar focused on operator-priority surfaces.
    Pin the default-collapse set so a refactor doesn't accidentally
    collapse a critical group."""
    src = _APP_TSX.read_text()
    # Find each group's defaultCollapsed flag.
    collapsed_default: set[str] = set()
    for m in re.finditer(
        r'key:\s*"([^"]+)",[^}]*?defaultCollapsed:\s*(true|false)',
        src,
    ):
        if m.group(2) == "true":
            collapsed_default.add(m.group(1))
    assert collapsed_default == {"daemon", "settings", "diagnostic"}, (
        f"default-collapse set drift: {collapsed_default}. The "
        f"operator-priority groups (overview/watch/email/promotion/"
        f"activity/control) must stay expanded by default."
    )


def test_exported_drift_guard_constants_present():
    """SIDEBAR_GROUP_KEYS_IN_ORDER + SIDEBAR_PATH_TO_GROUP are
    exported as the test-pin surface — keep them addressable so
    future drift-guard tests can grep against the canonical
    source rather than re-parsing the App.tsx literal."""
    src = _APP_TSX.read_text()
    assert "export const SIDEBAR_GROUP_KEYS_IN_ORDER" in src
    assert "export const SIDEBAR_PATH_TO_GROUP" in src


# ---------------------------------------------------------------------------
# Collapse hook pins
# ---------------------------------------------------------------------------


def test_collapse_hook_persists_to_localstorage():
    """The hook must persist collapse state to localStorage so
    operator preference survives a refresh. Pin both the storage
    key (versioned for future schema changes) + the read/write
    contract."""
    assert _HOOK_TSX.is_file()
    src = _HOOK_TSX.read_text()
    # Versioned key — bump if the stored shape ever changes.
    assert 'STORAGE_KEY = "kora.sidebar.groupCollapse.v1"' in src
    # Best-effort read + write semantics.
    assert "function readStored" in src
    assert "function writeStored" in src
    # Public API surface.
    assert "isCollapsed" in src
    assert "toggle" in src


def test_collapse_hook_imported_in_app():
    src = _APP_TSX.read_text()
    assert "useSidebarGroupCollapse" in src
    assert "from \"@/hooks/useSidebarGroupCollapse\"" in src


# ---------------------------------------------------------------------------
# SidebarNavGroup render pin
# ---------------------------------------------------------------------------


def test_sidebar_renders_grouped_not_flat():
    """The render call site must iterate ``sidebarGroups`` (the
    grouped shape) rather than the flat ``coreItems``. Pin the
    grouped iteration so a regression to the flat render is
    caught — that would silently undo the operator-attention
    reordering."""
    src = _APP_TSX.read_text()
    assert "sidebarGroups.map" in src
    assert "<SidebarNavGroup" in src
    # The flat coreItems.map call MUST be gone — keeping it
    # alongside the grouped render would double-render every link.
    assert "sidebarNav.coreItems.map" not in src
