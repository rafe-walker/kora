"""Tests for KR-FE-PROMOTION-PREVIEW backend endpoint.

POST /api/phrasebook/slack_dm/preview-template — renders an
arbitrary reply_template against the live snapshot. Used by the
PromotionReviewPage's SnapshotPreview component so the operator
sees the actual reply text Kora would send if the entry were live.

Behaviour matrix:
  1. Snapshot present + every field interpolates → rendered_reply
     populated, would_fall_through=false, missing=[]
  2. Snapshot present + at least one field missing → rendered_reply
     None, would_fall_through=true, missing lists the exact paths
  3. Snapshot present + at least one field == "unknown" → same as #2
  4. Snapshot absent → rendered_reply None, would_fall_through=true,
     missing=referenced, snapshot_present=false
  5. Empty template → rendered_reply ""? — actually returns ""
     (no placeholders means no fall-through)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_API_TS = _REPO_ROOT / "web" / "src" / "lib" / "api.ts"
_PAGE = _REPO_ROOT / "web" / "src" / "pages" / "PromotionReviewPage.tsx"
_WEB_SERVER = _REPO_ROOT / "kora_cli" / "web_server.py"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setattr("kora_constants.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr("kora_cli.config.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr("kora_cli.web_server.get_kora_home", lambda: tmp_path)
    return tmp_path


def _write_snapshot(env_dir: Path, payload: dict) -> None:
    if "computed_at" not in payload:
        payload = {
            **payload,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }
    snap_dir = env_dir / "cache"
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "daemon_snapshot.json").write_text(
        json.dumps(payload, default=str)
    )


async def _call(payload: dict) -> dict:
    from kora_cli import web_server

    return await web_server.preview_phrasebook_template(payload)


@pytest.mark.asyncio
async def test_renders_clean_when_fields_present(env):
    _write_snapshot(
        env,
        {
            "schema_version": 5,
            "cost_ladder": {
                "spent_to_date_usd": 42.13,
                "current_tier": "default",
            },
        },
    )
    body = await _call(
        {
            "reply_template": (
                "Burn is ${snapshot.cost_ladder.spent_to_date_usd}; "
                "tier {snapshot.cost_ladder.current_tier}."
            )
        }
    )
    assert body["snapshot_present"] is True
    assert body["would_fall_through_to_reasoning_engine"] is False
    assert body["missing_or_degraded_fields"] == []
    assert body["rendered_reply"] is not None
    assert "42.13" in body["rendered_reply"]
    assert "default" in body["rendered_reply"]


@pytest.mark.asyncio
async def test_falls_through_on_missing_field(env):
    _write_snapshot(env, {"cost_ladder": {}})
    body = await _call(
        {
            "reply_template": (
                "Burn is {snapshot.cost_ladder.spent_to_date_usd}."
            )
        }
    )
    assert body["snapshot_present"] is True
    assert body["would_fall_through_to_reasoning_engine"] is True
    assert "cost_ladder.spent_to_date_usd" in body["missing_or_degraded_fields"]
    assert body["rendered_reply"] is None
    assert "{missing:cost_ladder.spent_to_date_usd}" in body[
        "rendered_with_missing_markers"
    ]


@pytest.mark.asyncio
async def test_falls_through_on_unknown_sentinel(env):
    _write_snapshot(
        env,
        {"cost_ladder": {"spent_to_date_usd": "unknown"}},
    )
    body = await _call(
        {
            "reply_template": (
                "Burn is {snapshot.cost_ladder.spent_to_date_usd}."
            )
        }
    )
    assert body["would_fall_through_to_reasoning_engine"] is True
    assert "cost_ladder.spent_to_date_usd" in body["missing_or_degraded_fields"]


@pytest.mark.asyncio
async def test_no_snapshot_treats_all_fields_as_missing(env):
    body = await _call(
        {
            "reply_template": "Burn is {snapshot.cost_ladder.spent_to_date_usd}.",
        }
    )
    assert body["snapshot_present"] is False
    assert body["would_fall_through_to_reasoning_engine"] is True
    assert body["referenced_fields"] == ["cost_ladder.spent_to_date_usd"]
    assert body["missing_or_degraded_fields"] == [
        "cost_ladder.spent_to_date_usd"
    ]
    assert body["snapshot_computed_at"] is None


@pytest.mark.asyncio
async def test_static_template_renders_cleanly(env):
    _write_snapshot(env, {"cost_ladder": {}})
    body = await _call({"reply_template": "All good."})
    assert body["snapshot_present"] is True
    assert body["referenced_fields"] == []
    assert body["missing_or_degraded_fields"] == []
    assert body["would_fall_through_to_reasoning_engine"] is False
    assert body["rendered_reply"] == "All good."


@pytest.mark.asyncio
async def test_snapshot_computed_at_echoed(env):
    # Snapshot must be fresh (<10 min) or read_snapshot() returns
    # None and the endpoint shortcuts to "snapshot_present: False".
    # Use ``now()`` so the value is whatever-fresh and assert the
    # round-trip preserves it.
    iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    _write_snapshot(env, {"computed_at": iso, "cost_ladder": {}})
    body = await _call({"reply_template": "static"})
    # snapshot_computed_at echoes whatever was on disk — used by FE
    # for the "snapshot N min ago" freshness chip in the preview.
    assert body["snapshot_present"] is True
    assert body["snapshot_computed_at"] == iso


# ---------------------------------------------------------------------------
# FE source-pins
# ---------------------------------------------------------------------------


def test_api_wrapper_exists():
    src = _API_TS.read_text()
    assert "previewSlackDmPhrasebookTemplate" in src
    assert "/api/phrasebook/slack_dm/preview-template" in src


def test_preview_response_type_declared():
    src = _API_TS.read_text()
    assert "PhrasebookPreviewTemplateResponse" in src
    for f in (
        "rendered_reply",
        "rendered_with_missing_markers",
        "referenced_fields",
        "missing_or_degraded_fields",
        "would_fall_through_to_reasoning_engine",
        "snapshot_present",
        "snapshot_computed_at",
    ):
        assert f in src, f"missing FE field: {f}"


def test_snapshot_preview_component_wired_in_promotion_review_page():
    src = _PAGE.read_text()
    # SnapshotPreview is the FE component that calls the new endpoint.
    assert "function SnapshotPreview" in src
    # Wired into BOTH view + edit modes — view shows proposal text,
    # edit shows the live-edit textarea text. Both must re-render on
    # template change, which is what makes edit-before-approve useful.
    # Pin both call sites so a refactor that drops one is caught.
    assert "<SnapshotPreview template={proposal.proposed_reply_template}" in src
    assert "<SnapshotPreview template={replyTemplate}" in src
    # Drift-guard: the API wrapper name is the integration point.
    assert "previewSlackDmPhrasebookTemplate" in src
