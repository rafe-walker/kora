"""Source-pin tests for KR-FE-PROMOTION-REVIEW-PANEL.

Wire-shape contract — both sides agree on:

  * BE _PROMOTION_STATUS_VALUES tuple in web_server.py
  * FE PROMOTION_STATUS_VALUES constant in api.ts

Plus FE-side pins for the page existence + route registration +
sidebar nav entry + drift-guarded constant import.
"""

from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_API_TS = _REPO_ROOT / "web" / "src" / "lib" / "api.ts"
_APP_TSX = _REPO_ROOT / "web" / "src" / "App.tsx"
_PAGE = _REPO_ROOT / "web" / "src" / "pages" / "PromotionReviewPage.tsx"
_WEB_SERVER = _REPO_ROOT / "kora_cli" / "web_server.py"
_PROPOSER = _REPO_ROOT / "kora_cli" / "promote" / "phrasebook" / "proposer.py"


# ---------------------------------------------------------------
# Drift guards (wire-stable)
# ---------------------------------------------------------------


def test_promotion_status_drift_guard():
    """status values must match across 3 source-of-truth points:

      * proposer.py PROPOSAL_STATUS_VALUES (the canonical list)
      * web_server.py _PROMOTION_STATUS_VALUES (echoed in the
        /api/promotions/phrasebook/pending response)
      * api.ts PROMOTION_STATUS_VALUES (FE filter chips)
    """
    expected = {"pending", "approved", "rejected", "expired"}

    proposer_src = _PROPOSER.read_text()
    m = re.search(
        r"PROPOSAL_STATUS_VALUES[^=]*=\s*\(([^)]+)\)",
        proposer_src,
    )
    assert m is not None, "PROPOSAL_STATUS_VALUES tuple not found"
    proposer_values = set(re.findall(r'"(\w+)"', m.group(1)))
    assert proposer_values == expected, f"proposer drift: {proposer_values}"

    ws_src = _WEB_SERVER.read_text()
    m = re.search(
        r"_PROMOTION_STATUS_VALUES[^=]*=\s*\(([^)]+)\)",
        ws_src,
    )
    assert m is not None, "_PROMOTION_STATUS_VALUES tuple not found"
    be_values = set(re.findall(r'"(\w+)"', m.group(1)))
    assert be_values == expected, f"BE drift: {be_values}"

    fe_src = _API_TS.read_text()
    m = re.search(
        r"PROMOTION_STATUS_VALUES[^=]*=\s*\[([^\]]+)\]",
        fe_src,
    )
    assert m is not None, "FE PROMOTION_STATUS_VALUES not found"
    fe_values = set(re.findall(r'"(\w+)"', m.group(1)))
    assert fe_values == expected, f"FE drift: {fe_values}"


# ---------------------------------------------------------------
# FE source pins
# ---------------------------------------------------------------


def test_api_wrappers_exist():
    src = _API_TS.read_text()
    assert "getPhrasebookPromotionProposals" in src
    assert "/api/promotions/phrasebook/pending" in src
    assert "approvePhrasebookPromotion" in src
    assert "/approve" in src
    assert "rejectPhrasebookPromotion" in src
    assert "/reject" in src


def test_response_types_declared():
    src = _API_TS.read_text()
    for ts_type in (
        "PromotionProposal",
        "PromotionProposalsResponse",
        "PromotionApproveOverrides",
        "PromotionApproveResponse",
        "PromotionRejectResponse",
        "PromotionStatus",
    ):
        assert ts_type in src, f"missing TS type: {ts_type}"


def test_fe_status_constant_exported():
    src = _API_TS.read_text()
    assert "export const PROMOTION_STATUS_VALUES" in src


def test_proposal_fields_declared():
    src = _API_TS.read_text()
    # Fields the page renders. Renames on the BE side must be
    # mirrored here or render breaks silently.
    for field in (
        "proposal_id",
        "cluster_size",
        "sample_questions",
        "proposed_pattern",
        "proposed_reply_template",
        "proposed_category",
        "confidence",
        "haiku_synthesized",
        "review_notes",
    ):
        assert field in src, f"missing field in TS type: {field}"


def test_page_exists_and_uses_panel_view():
    assert _PAGE.is_file()
    src = _PAGE.read_text()
    assert 'usePanelView("PromotionReviewPage")' in src
    # 5th consumer of AuditPanelKit — pinned by reading from the
    # kit's public surface.
    assert "@/components/AuditPanelKit" in src


def test_route_registered():
    src = _APP_TSX.read_text()
    assert "/promotions/phrasebook" in src
    assert "PromotionReviewPage" in src


def test_nav_entry_present():
    src = _APP_TSX.read_text()
    nav_block = re.search(
        r'path:\s*"/promotions/phrasebook"[^}]+labelKey:\s*"promotionReview"',
        src,
        re.DOTALL,
    )
    assert nav_block, "nav entry for /promotions/phrasebook missing"


def test_pending_badge_wired():
    """Sidebar nav PendingBadge — the spec calls this an
    operator-attention surface, so the count must come from the
    same endpoint the page reads from (single source of truth) and
    must hydrate the NavItem.badgeCount field that SidebarNavLink
    renders."""
    src = _APP_TSX.read_text()
    assert "usePromotionPendingCount" in src
    assert "badgeCount" in src
    # The chip MUST be rendered conditionally on a numeric count
    # (not "0 hidden" / "always shown"). The spec wants a glanceable
    # "N pending" surface; zero-count hidden keeps the chip from
    # crowding the sidebar when nothing's pending.
    assert "badgeCount > 0" in src


def test_drift_guard_constant_imported_in_page():
    """The FE constant must remain imported (not just typed-only)
    so the drift-guard test_promotion_status_drift_guard regex match
    above corresponds to a live binding rather than an orphan const."""
    src = _PAGE.read_text()
    assert "PROMOTION_STATUS_VALUES" in src


def test_kora_actions_categories_extended():
    """Deliverable C — KoraActionsPage extended seams. The 3
    promotion categories + investigation_completed must be visually
    defined in the timeline page so promotion-loop events render
    correctly in the apex view."""
    src = (
        _REPO_ROOT / "web" / "src" / "pages" / "KoraActionsPage.tsx"
    ).read_text()
    for cat in (
        "promotion_proposed",
        "promotion_approved",
        "promotion_rejected",
        "investigation_completed",
    ):
        assert cat in src, f"KoraActionsPage missing category: {cat}"
