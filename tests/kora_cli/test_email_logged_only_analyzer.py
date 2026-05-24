"""FE source-pin tests for KR-FE-EMAIL-LOGGED-ONLY-ANALYZER.

No new BE endpoint — the page reuses /api/email-intent/recent and
filters client-side. Pins keep the page wired correctly + verify
the future-affordance stub is rendered as a clearly-disabled CTA
(operator must see the surface coming, but it must NOT imply
functionality that doesn't exist).
"""

from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_TSX = _REPO_ROOT / "web" / "src" / "App.tsx"
_PAGE = (
    _REPO_ROOT
    / "web"
    / "src"
    / "pages"
    / "EmailLoggedOnlyAnalyzerPage.tsx"
)


def test_page_exists_and_uses_panel_view():
    assert _PAGE.is_file()
    src = _PAGE.read_text()
    assert 'usePanelView("EmailLoggedOnlyAnalyzerPage")' in src
    # AuditPanelKit reuse — formatters/EmptyFilteredMessage at minimum.
    assert "@/components/AuditPanelKit" in src


def test_filters_to_logged_only_action_client_side():
    src = _PAGE.read_text()
    # Client-side filter keeps the BE surface minimal — the page
    # is just a lens on the per-seam endpoint, not a new BE shape.
    assert 'e.action === "logged_only"' in src
    # Calls the per-seam endpoint via the existing api wrapper.
    assert "getEmailIntentEventsRecent" in src


def test_route_registered():
    src = _APP_TSX.read_text()
    assert "/email-intent-log/logged-only" in src
    assert "EmailLoggedOnlyAnalyzerPage" in src


def test_nav_entry_present():
    src = _APP_TSX.read_text()
    nav_block = re.search(
        r'path:\s*"/email-intent-log/logged-only"[^}]+labelKey:\s*"emailLoggedOnly"',
        src,
        re.DOTALL,
    )
    assert nav_block, "nav entry for /email-intent-log/logged-only missing"


def test_suggest_pattern_stub_is_disabled():
    """The 'Suggest pattern' CTA is a forward-looking stub for
    KR-FE-PATTERN-SUGGESTION. It MUST be rendered disabled so
    operator sees the surface coming WITHOUT implying functionality
    that doesn't exist yet. Pin both the disabled attribute and
    the tooltip text that explains why."""
    src = _PAGE.read_text()
    assert "Suggest pattern" in src
    assert "disabled" in src
    # Pin the explainer text so the affordance can't drift to
    # something that LOOKS interactive.
    assert "KR-FE-PATTERN-SUGGESTION" in src
    assert "coming soon" in src


def test_why_this_view_explainer_present():
    """The view's framing — triage + training-data — is the
    whole point. Pin so a refactor can't drop the explainer card
    and leave operator with no context for what the page is for."""
    src = _PAGE.read_text()
    assert "Why this view" in src
    assert "triage" in src.lower()
    assert "training data" in src.lower()
