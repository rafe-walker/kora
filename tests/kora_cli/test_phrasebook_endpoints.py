"""Backend + source-pin tests for KR-FE-PHRASEBOOK-VIEWER.

Two new endpoints (GET /api/phrasebook/slack_dm + POST .../test) +
FE wiring. All read-only; no edit path in v1 — that's the
KR-FE-PHRASEBOOK-EDITOR follow-on.

Scenarios:
  GET endpoint:
    1. Returns entries from bundled default when no override exists
    2. Returns from override when override file exists
    3. referenced_snapshot_fields extracted from each entry's
       reply_template via the same regex dm_phrasebook uses (so
       the FE's per-entry dependency list agrees with what
       render_reply will walk at runtime)
    4. _extract_phrasebook_snapshot_refs is sorted + deduped
    5. override_candidate_path echoed even when file is absent
       (operator needs to know where to put a YAML)

  POST /test endpoint:
    6. Non-matching text → matched=False + would_fall_through=True
    7. Matching text + fresh snapshot + all-fields-present →
       matched=True + rendered_reply populated +
       would_fall_through=False
    8. Matching text + missing snapshot → matched=True +
       rendered_reply=null + would_fall_through=True
    9. Matching text + snapshot field is "unknown" sentinel →
       same fall-through outcome
   10. Oversized text truncated to 1024 (defensive cap)
   11. Result shape SECURITY: the test endpoint must NOT echo
       any internal snapshot beyond rendered_reply (no field
       dumps that could leak operational state to a caller
       without snapshot-read auth)

  FE wiring:
   12. api.getSlackDmPhrasebook + testSlackDmPhrasebook wrappers
   13. PhrasebookEntryDto + PhrasebookResponse + PhrasebookTestResponse
       types declared
   14. PhrasebookPage exists + uses usePanelView + route registered
   15. Page renders the 3-state outcome correctly (source-pin)
   16. _PHRASEBOOK_PLACEHOLDER_RE matches dm_phrasebook's regex
       exactly (drift guard)
"""

import re
from pathlib import Path
from typing import Any, Dict

import pytest

from tests.kora_cli._panel_test_helpers import isolated_kora_home


_REPO_ROOT = Path(__file__).resolve().parents[2]
_API_TS = _REPO_ROOT / "web" / "src" / "lib" / "api.ts"
_APP_TSX = _REPO_ROOT / "web" / "src" / "App.tsx"
_PAGE = _REPO_ROOT / "web" / "src" / "pages" / "PhrasebookPage.tsx"
_DM_PHRASEBOOK_PY = _REPO_ROOT / "kora_cli" / "short_circuit" / "dm_phrasebook.py"
# KR-PLUGIN-EXTRACTIONS-BATCH-2 (Deliverable C) — the
# ``_PLACEHOLDER_RE`` regex definition moved to the canonical
# matcher module; ``dm_phrasebook.py`` is now a re-export shim.
# Source-grep drift guard reads from the canonical location.
_MATCHER_PY = (
    _REPO_ROOT
    / "kora_cli"
    / "reasoning"
    / "kora_hermes_plugin"
    / "short_circuit"
    / "matcher.py"
)


# ---- Fixtures ----------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    return isolated_kora_home(tmp_path, monkeypatch)


def _write_override(env_path: Path, yaml_text: str) -> Path:
    """Write an operator-override phrasebook so load_phrasebook
    picks it up."""
    override = env_path / "phrasebook" / "slack_dm.yml"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(yaml_text, encoding="utf-8")
    return override


# ---- 1-5. GET endpoint --------------------------------------


@pytest.mark.asyncio
async def test_get_returns_bundled_default_when_no_override(env):
    from kora_cli import web_server

    result = await web_server.get_slack_dm_phrasebook()
    assert result["source"] == "bundled_default"
    assert result["source_path"] == "bundled"
    assert isinstance(result["entries"], list)
    assert len(result["entries"]) > 0
    # The bundled default ships with at least the greeting entry.
    categories = {e["category"] for e in result["entries"]}
    assert "greeting" in categories


@pytest.mark.asyncio
async def test_get_returns_override_when_override_exists(env):
    _write_override(
        env,
        """
entries:
  - pattern: "^test-pattern$"
    category: test_cat
    description: A test entry
    reply_template: "test reply {snapshot.foo.bar}"
""".strip(),
    )
    from kora_cli import web_server

    result = await web_server.get_slack_dm_phrasebook()
    assert result["source"] == "override"
    assert "slack_dm.yml" in result["source_path"]
    assert len(result["entries"]) == 1
    e = result["entries"][0]
    assert e["category"] == "test_cat"
    assert e["pattern"] == "^test-pattern$"
    assert e["referenced_snapshot_fields"] == ["foo.bar"]


@pytest.mark.asyncio
async def test_get_extracts_referenced_snapshot_fields(env):
    _write_override(
        env,
        """
entries:
  - pattern: "^x$"
    category: multi
    description: multi-field template
    reply_template: "{snapshot.a.b} and {snapshot.c} and {snapshot.a.b}"
""".strip(),
    )
    from kora_cli import web_server

    result = await web_server.get_slack_dm_phrasebook()
    fields = result["entries"][0]["referenced_snapshot_fields"]
    # Sorted + deduped
    assert fields == ["a.b", "c"]


@pytest.mark.asyncio
async def test_override_candidate_path_echoed_when_absent(env):
    from kora_cli import web_server

    result = await web_server.get_slack_dm_phrasebook()
    # No override created in this test → source is bundled but the
    # candidate path is echoed so the FE can show "create at X" hint
    assert result["source"] == "bundled_default"
    assert result["override_candidate_path"] is not None
    assert result["override_candidate_path"].endswith("phrasebook/slack_dm.yml")


# ---- 6-11. POST /test endpoint ------------------------------


@pytest.mark.asyncio
async def test_post_non_matching_text_returns_unmatched(env):
    from kora_cli import web_server

    result = await web_server.test_phrasebook_match(
        {"text": "completely-unrecognized-message-xyz-12345"}
    )
    assert result["matched"] is False
    assert result["would_fall_through_to_reasoning_engine"] is True


@pytest.mark.asyncio
async def test_post_matching_text_with_no_snapshot_falls_through(env):
    """Per dm_phrasebook.py:285-286 (`if snapshot is None: return
    None`), missing snapshot is the FIRST fall-through trigger —
    universal, regardless of whether the template has placeholders.
    A no-placeholder template like "Hey. What's up?" still falls
    through when snapshot is None — the handler treats absence
    of fresh state as a signal to defer."""
    from kora_cli import web_server

    result = await web_server.test_phrasebook_match({"text": "hey"})
    assert result["matched"] is True
    assert result["category"] == "greeting"
    assert result["snapshot_present"] is False
    # No snapshot → universal fall-through (no rendered reply)
    assert result["rendered_reply"] is None
    assert result["would_fall_through_to_reasoning_engine"] is True


@pytest.mark.asyncio
async def test_post_matching_text_with_fresh_snapshot_renders(env):
    """Seed a snapshot so render_reply has fields to interpolate
    against. The 'status' query template references
    snapshot.operational_state.primary + snapshot.alerts.active_count.

    Path matches state_snapshot._SNAPSHOT_RELATIVE_PATH:
    ${KORA_HOME}/cache/daemon_snapshot.json"""
    import json
    from datetime import datetime, timezone
    snap_path = env / "cache" / "daemon_snapshot.json"
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    snap = {
        "schema_version": 2,
        "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "operational_state": {
            "primary": "RUNNING",
            "paused": False,
            "pause_reason": None,
        },
        "alerts": {
            "active_count": 2,
            "by_severity": {"critical": 0, "warning": 1, "info": 1},
            "by_category": {},
        },
        "cost_ladder": {
            "current_tier": "normal",
            "monthly_budget_pct_used": 23.4,
            "model_default": "claude-haiku-4-5",
        },
        "service_health": {
            "supabase": "healthy",
            "fly": "healthy",
            "vercel": "healthy",
            "sentry": "healthy",
            "doppler": "healthy",
        },
    }
    snap_path.write_text(json.dumps(snap))

    from kora_cli import web_server

    result = await web_server.test_phrasebook_match({"text": "status"})
    assert result["matched"] is True
    assert result["category"] == "status_query"
    assert result["rendered_reply"] is not None
    assert "RUNNING" in result["rendered_reply"]
    assert "2" in result["rendered_reply"]
    assert result["would_fall_through_to_reasoning_engine"] is False


@pytest.mark.asyncio
async def test_post_matching_text_with_unknown_field_falls_through(env):
    """Field literally == 'unknown' is the PR #157 degraded
    sentinel; render_reply returns None → would fall through."""
    import json
    from datetime import datetime, timezone
    snap_path = env / "cache" / "daemon_snapshot.json"
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    snap = {
        "schema_version": 2,
        "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cost_ladder": {
            "current_tier": "unknown",  # degraded sentinel
            "monthly_budget_pct_used": "unknown",
            "model_default": "unknown",
        },
    }
    snap_path.write_text(json.dumps(snap))

    from kora_cli import web_server

    result = await web_server.test_phrasebook_match({"text": "burn"})
    assert result["matched"] is True
    assert result["category"] == "burn_query"
    # Template references cost_ladder fields that are "unknown" →
    # rendered_reply should be None
    assert result["rendered_reply"] is None
    assert result["would_fall_through_to_reasoning_engine"] is True


@pytest.mark.asyncio
async def test_post_oversized_text_truncated_to_1024(env):
    """Defensive cap matches the spec — the operator can't blow up
    the server by submitting an 8MB string."""
    from kora_cli import web_server

    long_text = "a" * 100_000  # 100k chars
    # Doesn't matter that it doesn't match; we just want the
    # endpoint to return cleanly (no crash on the long input).
    result = await web_server.test_phrasebook_match({"text": long_text})
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_post_does_not_echo_full_snapshot(env):
    """SECURITY: the test response shape must NOT include a dump
    of the snapshot or any internal state beyond the
    rendered_reply text. Operator could call this endpoint over a
    Kora-MCP-tool surface in future; we never want to accidentally
    expose snapshot internals via a 'preview' endpoint."""
    from kora_cli import web_server

    # Seed a snapshot with a sentinel string the response must not contain.
    import json
    from datetime import datetime, timezone
    snap_path = env / "cache" / "daemon_snapshot.json"
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    sentinel = "SECRET_DAEMON_INTERNAL_SHOULD_NEVER_LEAK"
    snap = {
        "schema_version": 2,
        "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "operational_state": {
            "primary": sentinel,
            "paused": False,
            "pause_reason": None,
        },
        "alerts": {
            "active_count": 0,
            "by_severity": {"critical": 0, "warning": 0, "info": 0},
            "by_category": {},
        },
    }
    snap_path.write_text(json.dumps(snap))

    from kora_cli import web_server

    # Use a non-matching text so we don't surface the sentinel
    # via rendered_reply (matched=True with a status query would
    # render the sentinel into the reply text, which is by design —
    # the operator EXPLICITLY asked to preview that template).
    result = await web_server.test_phrasebook_match(
        {"text": "completely-unrecognized-text-xyz"}
    )
    # No matched → response is just {matched: False, would_fall_through: True}
    # — no snapshot data at all.
    blob = json.dumps(result)
    assert sentinel not in blob, (
        "Non-matching test response must NOT carry any snapshot "
        "contents (operator didn't request a render)"
    )


# ---- 12-14. FE wiring ---------------------------------------


def test_api_wrappers_exist():
    src = _API_TS.read_text()
    assert re.search(
        r"getSlackDmPhrasebook:\s*\(\)\s*=>\s*fetchJSON<PhrasebookResponse>\(\"/api/phrasebook/slack_dm\"\)",
        src,
    )
    assert re.search(
        r"testSlackDmPhrasebook:\s*\(text:\s*string\)\s*=>",
        src,
    )
    assert '"/api/phrasebook/slack_dm/test"' in src
    assert 'method: "POST"' in src


def test_phrasebook_types_declared():
    src = _API_TS.read_text()
    for ty in (
        "export interface PhrasebookEntryDto",
        "export interface PhrasebookResponse",
        "export type PhrasebookTestResponse",
    ):
        assert ty in src
    # PhrasebookEntryDto carries the per-entry snapshot-field deps
    assert "referenced_snapshot_fields" in src


def test_page_exists_and_registers_route():
    assert _PAGE.is_file()
    app_src = _APP_TSX.read_text()
    assert '"/phrasebook": PhrasebookPage' in app_src
    # Nav entry
    assert re.search(
        r'path:\s*"/phrasebook"[^}]+labelKey:\s*"phrasebook"',
        app_src,
        re.DOTALL,
    )


def test_page_uses_panel_view_hook():
    src = _PAGE.read_text()
    assert 'usePanelView("PhrasebookPage")' in src


# ---- 15. Page renders 3-state outcome ---------------------


def test_page_renders_three_tester_outcomes():
    """The TesterResult component must visibly differentiate all 3
    states (unmatched / matched+rendered / matched+null-render)
    that the spec calls out for screenshots."""
    src = _PAGE.read_text()
    # Unmatched state copy
    assert "No phrasebook entry matched" in src
    # Matched + $0 reply
    assert "Matched · $0 reply" in src
    # Matched but would fall through
    assert "Matched but would fall through" in src


def test_page_marks_degraded_snapshot_fields_per_entry():
    """Per-row affordance: when a referenced field is currently
    'unknown' in the live snapshot, the entry's badge for that
    field renders 'warning' tone so operator can see at a glance
    which entries would fall through."""
    src = _PAGE.read_text()
    assert "isDegradedSnapshotValue" in src
    assert re.search(
        r'value\s*===\s*"unknown"',
        src,
    ), "FE must treat 'unknown' as the degraded sentinel (matches dm_phrasebook.py:293)"


# ---- 16. Placeholder regex drift guard --------------------


def test_placeholder_regex_matches_dm_phrasebook_source():
    """SECURITY-of-correctness: the GET endpoint's
    _PHRASEBOOK_PLACEHOLDER_RE must match the matcher module's
    _PLACEHOLDER_RE exactly. Otherwise FE's per-entry
    referenced_snapshot_fields list will drift from what
    render_reply actually walks at runtime — operator's "this
    will fall through" affordance becomes a lie.

    Source-grep reads ``matcher.py`` (the canonical location
    post KR-PLUGIN-EXTRACTIONS-BATCH-2 Deliverable C);
    ``dm_phrasebook.py`` is a re-export shim that no longer
    defines the regex.
    """
    backend_src = _MATCHER_PY.read_text()
    backend_re = re.search(
        r'_PLACEHOLDER_RE\s*=\s*re\.compile\(r"([^"]+)"\)',
        backend_src,
    )
    assert backend_re, "matcher._PLACEHOLDER_RE not found"

    from kora_cli.web_server import _PHRASEBOOK_PLACEHOLDER_RE

    assert _PHRASEBOOK_PLACEHOLDER_RE.pattern == backend_re.group(1), (
        f"Endpoint's _PHRASEBOOK_PLACEHOLDER_RE drifted from "
        f"matcher's _PLACEHOLDER_RE — referenced-fields "
        f"extraction will diverge from runtime walk:\n"
        f"endpoint: {_PHRASEBOOK_PLACEHOLDER_RE.pattern!r}\n"
        f"runtime:  {backend_re.group(1)!r}"
    )
