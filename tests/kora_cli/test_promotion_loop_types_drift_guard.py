"""KR-FE-PROMOTION-REVIEW-MULTI-LOOP-EXTEND — drift-guard +
endpoint behaviour tests for the multi-loop PromotionReviewPage.

Pins:
  * _PROMOTION_LOOP_TYPES (BE) ↔ PROMOTION_LOOP_NAMES (FE)
  * Per-loop /pending response shape carries ``loop_name`` for FE
    discrimination
  * /api/promotions/counts aggregate endpoint shape + behaviour
    when each loop directory is empty / present / missing
  * /api/promotions/snapshot-expand/recent reads
    promotion.snapshot_field_added audit + echoes auto_apply flag
  * Phrasebook /pending response gains ``loop_name`` for parity
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_API_TS = _REPO_ROOT / "web" / "src" / "lib" / "api.ts"
_APP_TSX = _REPO_ROOT / "web" / "src" / "App.tsx"
_PAGE = _REPO_ROOT / "web" / "src" / "pages" / "PromotionReviewPage.tsx"
_WEB_SERVER = _REPO_ROOT / "kora_cli" / "web_server.py"


# ---------------------------------------------------------------------------
# Drift guard
# ---------------------------------------------------------------------------


def test_promotion_loop_types_drift_guard():
    """_PROMOTION_LOOP_TYPES (web_server.py) ↔ PROMOTION_LOOP_NAMES
    (api.ts) must agree.

    Order matters: the FE iterates the constant for tab order, so
    a re-arrangement on either side must be mirrored or the tabs
    silently re-order without the FE realizing.
    """
    expected = [
        "phrasebook",
        "router_tuning",
        "tool_trimming",
        "probe_fix_envelopes",
        "snapshot_expand",
        "email_intent",
    ]

    ws_src = _WEB_SERVER.read_text()
    m = re.search(
        r"_PROMOTION_LOOP_TYPES[^=]*=\s*\(([^)]+)\)",
        ws_src,
        re.DOTALL,
    )
    assert m is not None, "_PROMOTION_LOOP_TYPES tuple not found"
    be_values = re.findall(r'"([^"]+)"', m.group(1))
    assert be_values == expected, f"BE order drift: {be_values}"

    fe_src = _API_TS.read_text()
    m = re.search(
        r"PROMOTION_LOOP_NAMES[^=]*=\s*\[([^\]]+)\]",
        fe_src,
    )
    assert m is not None, "PROMOTION_LOOP_NAMES constant not found"
    fe_values = re.findall(r'"([^"]+)"', m.group(1))
    assert fe_values == expected, f"FE order drift: {fe_values}"


def test_promotion_loop_slugs_match_loop_dirs():
    """The FE slug map (URL paths) must map every loop_name to a
    BE endpoint path that actually exists. Catches typos in either
    direction — a slug rename without a BE endpoint rename would
    404 every approve/reject call for that loop."""
    fe_src = _API_TS.read_text()
    ws_src = _WEB_SERVER.read_text()

    # Each loop the BE has a /pending endpoint for must appear in
    # the FE PROMOTION_LOOP_SLUGS map as a slug value.
    # phrasebook is the typed-wrapper path, not slug-routed.
    expected_slugs = {
        "phrasebook",
        "router-tuning",
        "tool-trimming",
        "probe-envelopes",
        # snapshot-expand uses a non-standard /recent endpoint;
        # email-intent (forward-compat) follows the slug-routed
        # pattern once #420 lands.
    }
    m = re.search(
        r"PROMOTION_LOOP_SLUGS:\s*Record<[^>]+>\s*=\s*\{([^}]+)\}",
        fe_src,
        re.DOTALL,
    )
    assert m is not None, "PROMOTION_LOOP_SLUGS map not found"
    slug_values = set(re.findall(r':\s*"([^"]+)"', m.group(1)))
    for slug in expected_slugs:
        assert slug in slug_values, (
            f"FE slug map missing {slug!r}"
        )
        assert (
            f'/api/promotions/{slug}/pending' in ws_src
        ), f"BE has no /api/promotions/{slug}/pending endpoint"


# ---------------------------------------------------------------------------
# /api/promotions/counts behaviour
# ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setattr("kora_constants.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr("kora_cli.config.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr("kora_cli.web_server.get_kora_home", lambda: tmp_path)
    monkeypatch.setenv(
        "KORA_PROMOTIONS_DIR", str(tmp_path / "promotions")
    )
    return tmp_path


def _write_pending_proposal_file(
    env_dir: Path, loop_name: str, proposal_id: str
) -> None:
    """Create a minimal proposal file in the loop's pending/ dir."""
    p = env_dir / "promotions" / loop_name / "pending"
    p.mkdir(parents=True, exist_ok=True)
    (p / f"{proposal_id}.json").write_text(
        json.dumps({"proposal_id": proposal_id, "status": "pending"})
    )


def _write_audit_jsonl(env_dir: Path, entries: list[dict]) -> None:
    log_path = env_dir / "kora_audit_log.jsonl"
    with log_path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, default=str) + "\n")


async def _call_counts() -> dict:
    from kora_cli import web_server

    return await web_server.get_promotion_counts()


@pytest.mark.asyncio
async def test_counts_empty_when_no_promotions(env):
    body = await _call_counts()
    assert set(body["counts"].keys()) == {
        "phrasebook",
        "router_tuning",
        "tool_trimming",
        "probe_fix_envelopes",
        "snapshot_expand",
        "email_intent",
    }
    for v in body["counts"].values():
        assert v == 0
    assert body["total_pending"] == 0


@pytest.mark.asyncio
async def test_counts_aggregates_actionable_only(env):
    # Drop a pending proposal in each of 3 actionable loops + an
    # audit row for snapshot_expand. total_pending must exclude
    # snapshot_expand.
    _write_pending_proposal_file(env, "router_tuning", "r1")
    _write_pending_proposal_file(env, "tool_trimming", "t1")
    _write_pending_proposal_file(env, "tool_trimming", "t2")
    _write_pending_proposal_file(env, "probe_fix_envelopes", "p1")

    _write_audit_jsonl(
        env,
        [
            {
                "emitted_at": datetime.now(timezone.utc).isoformat(),
                "seam": "promotion.snapshot_field_added",
                "details": {
                    "proposal_id": "s1",
                    "action": "proposed",
                    "proposed_field_path": "x.y",
                },
                "source": "reasoning",
                "caller_session_id": "promotion:snapshot_expand:s1",
            },
        ],
    )

    body = await _call_counts()
    assert body["counts"]["router_tuning"] == 1
    assert body["counts"]["tool_trimming"] == 2
    assert body["counts"]["probe_fix_envelopes"] == 1
    assert body["counts"]["snapshot_expand"] == 1
    # Actionable total excludes snapshot_expand by definition.
    assert body["total_pending"] == 4


# ---------------------------------------------------------------------------
# /api/promotions/snapshot-expand/recent behaviour
# ---------------------------------------------------------------------------


async def _call_snapshot_expand_recent() -> dict:
    from kora_cli import web_server

    return await web_server.list_recent_snapshot_expand_proposals()


@pytest.mark.asyncio
async def test_snapshot_expand_recent_projects_audit_rows(env):
    _write_audit_jsonl(
        env,
        [
            {
                "emitted_at": datetime.now(timezone.utc).isoformat(),
                "seam": "promotion.snapshot_field_added",
                "details": {
                    "proposal_id": "s1",
                    "action": "proposed",
                    "cluster_size": 6,
                    "proposed_field_path": "tickets.open_count",
                    "proposed_collector_summary": "count of open IsoKron tickets",
                    "source_tool_name": "kora__open_tickets",
                    "sample_caller_session_ids": [
                        "slack_dm:U01:T01",
                        "slack_dm:U01:T02",
                    ],
                    "confidence": 0.93,
                    "created_at": "2026-05-24T10:00:00Z",
                },
                "source": "reasoning",
                "caller_session_id": "promotion:snapshot_expand:s1",
            },
        ],
    )
    body = await _call_snapshot_expand_recent()
    assert body["loop_name"] == "snapshot_expand"
    assert len(body["proposals"]) == 1
    p = body["proposals"][0]
    assert p["proposal_id"] == "s1"
    assert p["action"] == "proposed"
    assert p["proposed_field_path"] == "tickets.open_count"
    assert p["source_tool_name"] == "kora__open_tickets"
    assert "auto_apply_enabled" in body


@pytest.mark.asyncio
async def test_snapshot_expand_auto_apply_flag_echoed(env, monkeypatch):
    monkeypatch.setenv("KORA_PROMOTE_SNAPSHOT_EXPAND_AUTO_APPLY", "true")
    body = await _call_snapshot_expand_recent()
    assert body["auto_apply_enabled"] is True


# ---------------------------------------------------------------------------
# Phrasebook /pending parity — must now include ``loop_name``
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phrasebook_pending_response_includes_loop_name(env):
    """The multi-loop refactor relies on EVERY /pending endpoint
    echoing ``loop_name`` for FE discrimination. Phrasebook was
    pre-existing — this test pins the addition."""
    from kora_cli import web_server

    body = await web_server.list_pending_phrasebook_proposals()
    assert body["loop_name"] == "phrasebook"
    assert "proposals" in body
    assert "status_values" in body


# ---------------------------------------------------------------------------
# FE page pins
# ---------------------------------------------------------------------------


def test_promotion_review_page_renders_all_card_variants():
    """The 6 per-loop card variants must all be declared in
    PromotionReviewPage.tsx — a refactor that drops one would
    silently break that tab. Pin by component-name search since
    each variant is a top-level function in the page module."""
    src = _PAGE.read_text()
    for component in (
        "function PhrasebookCard",
        "function RouterTuningCard",
        "function ToolTrimmingCard",
        "function ProbeEnvelopeCard",
        "function SnapshotExpandCard",
        "function EmailIntentCard",
    ):
        assert component in src, f"missing card variant: {component}"


def test_loop_tabs_render_all_loop_types():
    """LoopTypeTabs iterates the LOOP_TABS array, which must cover
    every PROMOTION_LOOP_NAMES entry. The page-level test ensures
    each loop has a visible tab; the drift-guard above ensures
    the loop names themselves stay in lockstep BE ↔ FE."""
    src = _PAGE.read_text()
    for loop in (
        "phrasebook",
        "router_tuning",
        "tool_trimming",
        "probe_fix_envelopes",
        "snapshot_expand",
        "email_intent",
    ):
        assert f'loop: "{loop}"' in src, (
            f"LOOP_TABS missing entry for {loop}"
        )


def test_high_risk_probe_envelope_visual_treatment():
    """Spec calls for HIGH-RISK visual treatment on the
    ProbeEnvelopeCard: red border accent + manual-scaffold
    disclaimer. Pin both."""
    src = _PAGE.read_text()
    # ``highRisk`` prop flips the CardChrome border to destructive.
    assert "highRisk\n          status" in src or "highRisk" in src
    # The blast-radius treatment + manual-scaffold disclaimer.
    assert "HIGH RISK" in src
    assert "fix_envelopes.py" in src
    assert "blast" in src.lower()


def test_snapshot_expand_card_warns_on_auto_apply():
    """When KORA_PROMOTE_SNAPSHOT_EXPAND_AUTO_APPLY=true the card
    must visually flag that the proposal may already be in the
    snapshot schema next cycle. Pin the env var name + the
    warning copy."""
    src = _PAGE.read_text()
    assert "KORA_PROMOTE_SNAPSHOT_EXPAND_AUTO_APPLY" in src
    assert "AUTO-APPLY" in src


def test_pending_count_hook_uses_counts_endpoint():
    """usePromotionPendingCount must call the new aggregate
    endpoint (not the phrasebook-only /pending) so the badge
    reflects all actionable loops."""
    hook_src = (
        _REPO_ROOT / "web" / "src" / "hooks" / "usePromotionPendingCount.ts"
    ).read_text()
    assert "getPromotionCounts" in hook_src
    assert "total_pending" in hook_src


def test_api_wrappers_for_generic_loop_endpoints():
    """The generic getPromotionProposals/approve/reject wrappers
    must exist alongside the typed phrasebook ones (which keep
    the typed override allowlist)."""
    src = _API_TS.read_text()
    for fn in (
        "getPromotionProposals",
        "approvePromotion",
        "rejectPromotion",
        "getSnapshotExpandPromotions",
        "getPromotionCounts",
    ):
        assert fn in src, f"missing api wrapper: {fn}"
