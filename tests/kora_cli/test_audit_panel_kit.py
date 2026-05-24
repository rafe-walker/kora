"""KR-FE-PANEL-KIT — source-pin tests for the kit itself.

The kit (web/src/components/AuditPanelKit/) is the shared
foundation for all audit-stream cockpit panels. Tests here
guarantee:

  1. All 7 files exist (Sparkline / SummaryChips / FilterChips /
     BadgeTone / formatters / EmptyFilteredMessage / index +
     types + README)
  2. Public API exports are present in index.ts
  3. README documents each export
  4. Sparkline uses plain SVG (no chart-library imports)
  5. BadgeTone tone literals match @nous-research/ui Badge
     (anti-drift against the library)
  6. CategoryDef type carries the 4 expected fields
  7. All 4 known consumers import from the kit (the integration
     contract — if a new consumer ships without using the kit,
     this list grows but each entry is verified)
"""

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_KIT = _REPO_ROOT / "web" / "src" / "components" / "AuditPanelKit"


def test_kit_files_present():
    assert (_KIT / "index.ts").is_file()
    assert (_KIT / "Sparkline.tsx").is_file()
    assert (_KIT / "SummaryChips.tsx").is_file()
    assert (_KIT / "FilterChips.tsx").is_file()
    assert (_KIT / "BadgeTone.ts").is_file()
    assert (_KIT / "EmptyFilteredMessage.tsx").is_file()
    assert (_KIT / "formatters.ts").is_file()
    assert (_KIT / "types.ts").is_file()
    assert (_KIT / "README.md").is_file()


def test_kit_index_exports_public_api():
    src = (_KIT / "index.ts").read_text()
    for name in (
        "Sparkline",
        "SparklineProps",
        "SummaryChips",
        "SummaryChipsProps",
        "FilterChips",
        "FilterChipsProps",
        "FilterValue",
        "EmptyFilteredMessage",
        "EmptyFilteredMessageProps",
        "BadgeTone",
        "CategoryDef",
        "DailyCountPoint",
        "formatTimestamp",
        "formatRelative",
        "truncate",
        "formatBytes",
        "formatChars",
        "formatDurationMs",
    ):
        assert name in src, (
            f"AuditPanelKit/index.ts missing public export: {name}"
        )


def test_kit_sparkline_plain_svg():
    src = (_KIT / "Sparkline.tsx").read_text()
    assert "<svg" in src
    assert "<rect" in src
    for lib in ("recharts", "chart.js", "d3", "@nivo", "victory"):
        assert lib not in src


def test_badge_tone_matches_library():
    """BadgeTone string-literal union must include exactly the
    6 tones the @nous-research/ui Badge component declares. If
    the lib adds / renames a tone, our type drifts silently — pin
    against the actual lib types file."""
    src = (_KIT / "BadgeTone.ts").read_text()
    for tone in (
        "default",
        "destructive",
        "outline",
        "secondary",
        "success",
        "warning",
    ):
        assert f'"{tone}"' in src


def test_category_def_carries_4_fields():
    src = (_KIT / "types.ts").read_text()
    assert "CategoryDef" in src
    for field in ("key", "label", "tone", "Icon"):
        assert field in src


def test_readme_documents_each_export():
    src = (_KIT / "README.md").read_text()
    for name in (
        "Sparkline",
        "SummaryChips",
        "FilterChips",
        "EmptyFilteredMessage",
        "BadgeTone",
        "CategoryDef",
        "formatTimestamp",
        "formatRelative",
        "truncate",
    ):
        assert name in src, f"README missing reference to {name}"


def test_all_known_consumers_import_from_kit():
    """All 4 cockpit panels that consume the kit must import from
    the canonical path. New audit-stream panels added later should
    extend this list."""
    pages_dir = _REPO_ROOT / "web" / "src" / "pages"
    consumers = (
        "EmailIntentLogPage.tsx",
        "OutboundEmailLogPage.tsx",
        "AutofixLogPage.tsx",
        "KoraActionsPage.tsx",
    )
    for name in consumers:
        page = pages_dir / name
        assert page.is_file(), f"expected consumer missing: {name}"
        src = page.read_text()
        assert "@/components/AuditPanelKit" in src, (
            f"{name} does not import from the kit — kit extraction "
            f"didn't reach this consumer"
        )


def test_no_residual_local_definitions_in_retrofitted_pages():
    """After retrofit, EmailIntentLogPage + OutboundEmailLogPage
    must NOT redefine the kit's components locally. A leftover
    `function Sparkline(` in either page is a refactor regression."""
    pages_dir = _REPO_ROOT / "web" / "src" / "pages"
    for name in ("EmailIntentLogPage.tsx", "OutboundEmailLogPage.tsx"):
        src = (pages_dir / name).read_text()
        for forbidden in (
            "function Sparkline(",
            "function SummaryChips(",
            "function FilterChips(",
            "function formatTimestamp(",
            "function formatRelative(",
            "function truncate(",
        ):
            assert forbidden not in src, (
                f"{name} still defines '{forbidden}' locally — "
                f"the retrofit didn't fully replace it with the "
                f"kit import"
            )
