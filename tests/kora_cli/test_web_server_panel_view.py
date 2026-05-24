"""Tests for the KR-PANEL-USE-INSTRUMENTATION /api/panel_view sink.

Per Council R3 lock + sub-cut (c): records which top-level pages /
panels the operator opens. Path B chosen (PM confirmed): separate
``${KORA_HOME}/panel_views.jsonl`` file rather than extending the
audit log's SeamName Literal.

Scenarios:
  1. POST with valid payload → 200 + JSONL line appended with
     {kind, panel_name, session_id, emitted_at}
  2. Empty panel_name → 400 (FE bug, not transient runtime
     condition — surfaces in dev quickly)
  3. Oversized panel_name (>128 chars) → truncated to 128
  4. Missing session_id → recorded as "unknown" (cold tabs still
     produce countable rows)
  5. Empty/blank session_id → "unknown"
  6. Oversized session_id (>64 chars) → truncated to 64
  7. Multiple POSTs → multiple JSONL lines appended (append-only
     semantic)
  8. Each entry has the required keys + valid emitted_at ISO shape
  9. JSONL file is created on first write (KORA_HOME may not exist
     on fresh installs)
 10. JSONL write to read-only path → graceful warning, still
     returns ok:true (instrumentation must never break UX)
 11. SECURITY: no FE-supplied "kind" field can override the
     hardcoded kind="panel_view"
 12. Front-end FS pin: usePanelView hook source exists + posts to
     /api/panel_view
 13. Front-end source-pin: every top-level page/panel imports
     usePanelView (instrumented inventory matches the 34 panels)
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from tests.kora_cli._panel_test_helpers import isolated_kora_home


PANEL_VIEWS_FILENAME = "panel_views.jsonl"

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOOK_PATH = _REPO_ROOT / "web" / "src" / "hooks" / "usePanelView.ts"
_PAGES_DIR = _REPO_ROOT / "web" / "src" / "pages"


@pytest.fixture
def env(tmp_path, monkeypatch):
    return isolated_kora_home(tmp_path, monkeypatch)


def _read_log_lines(env_path: Path) -> List[Dict[str, Any]]:
    log_path = env_path / PANEL_VIEWS_FILENAME
    if not log_path.is_file():
        return []
    return [
        json.loads(line) for line in log_path.read_text().splitlines() if line.strip()
    ]


# ---- 1. Happy path -----------------------------------------------


@pytest.mark.asyncio
async def test_valid_payload_appends_jsonl_line(env):
    from kora_cli import web_server

    result = await web_server.emit_panel_view(
        {"panel_name": "AlertsPanel", "session_id": "sess-abc-123"}
    )
    assert result == {"ok": True}

    rows = _read_log_lines(env)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "panel_view"
    assert row["panel_name"] == "AlertsPanel"
    assert row["session_id"] == "sess-abc-123"
    assert "emitted_at" in row


# ---- 2. Validation: empty panel_name → 400 ----------------------


@pytest.mark.asyncio
async def test_empty_panel_name_returns_400(env):
    from kora_cli import web_server

    with pytest.raises(HTTPException) as exc_info:
        await web_server.emit_panel_view({"panel_name": "", "session_id": "s"})
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_missing_panel_name_returns_400(env):
    from kora_cli import web_server

    with pytest.raises(HTTPException) as exc_info:
        await web_server.emit_panel_view({"session_id": "s"})
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_whitespace_only_panel_name_returns_400(env):
    from kora_cli import web_server

    with pytest.raises(HTTPException) as exc_info:
        await web_server.emit_panel_view({"panel_name": "   "})
    assert exc_info.value.status_code == 400


# ---- 3. Oversized panel_name truncated --------------------------


@pytest.mark.asyncio
async def test_oversized_panel_name_truncated_to_128(env):
    from kora_cli import web_server

    long_name = "X" * 500
    await web_server.emit_panel_view({"panel_name": long_name})
    rows = _read_log_lines(env)
    assert len(rows[0]["panel_name"]) == 128


# ---- 4-6. session_id semantics ----------------------------------


@pytest.mark.asyncio
async def test_missing_session_id_recorded_as_unknown(env):
    from kora_cli import web_server

    await web_server.emit_panel_view({"panel_name": "AlertsPanel"})
    rows = _read_log_lines(env)
    assert rows[0]["session_id"] == "unknown"


@pytest.mark.asyncio
async def test_empty_session_id_recorded_as_unknown(env):
    from kora_cli import web_server

    await web_server.emit_panel_view(
        {"panel_name": "AlertsPanel", "session_id": ""}
    )
    rows = _read_log_lines(env)
    assert rows[0]["session_id"] == "unknown"


@pytest.mark.asyncio
async def test_oversized_session_id_truncated_to_64(env):
    from kora_cli import web_server

    long_sid = "s" * 500
    await web_server.emit_panel_view(
        {"panel_name": "AlertsPanel", "session_id": long_sid}
    )
    rows = _read_log_lines(env)
    assert len(rows[0]["session_id"]) == 64


# ---- 7. Append-only semantic -----------------------------------


@pytest.mark.asyncio
async def test_multiple_posts_append_separate_lines(env):
    from kora_cli import web_server

    for i in range(5):
        await web_server.emit_panel_view(
            {"panel_name": f"Panel{i}", "session_id": f"s{i}"}
        )
    rows = _read_log_lines(env)
    assert len(rows) == 5
    assert [r["panel_name"] for r in rows] == [f"Panel{i}" for i in range(5)]


# ---- 8. Entry shape ---------------------------------------------


@pytest.mark.asyncio
async def test_entry_shape_has_required_keys_and_iso_timestamp(env):
    from kora_cli import web_server

    await web_server.emit_panel_view(
        {"panel_name": "AlertsPanel", "session_id": "s1"}
    )
    rows = _read_log_lines(env)
    row = rows[0]
    assert set(row.keys()) == {"kind", "panel_name", "session_id", "emitted_at"}
    # Z-suffixed UTC ISO, matches the writer's strftime
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", row["emitted_at"]
    )


# ---- 9. KORA_HOME doesn't exist yet ----------------------------


@pytest.mark.asyncio
async def test_writes_when_kora_home_doesnt_exist(tmp_path, monkeypatch):
    """Fresh install: KORA_HOME may not have been created. Endpoint
    must mkdir(parents=True, exist_ok=True) before append."""
    missing_home = tmp_path / "freshly_provisioned" / ".kora"
    assert not missing_home.exists()

    monkeypatch.setenv("HERMES_HOME", str(missing_home))
    monkeypatch.setenv("KORA_HOME", str(missing_home))
    monkeypatch.setattr("kora_constants.get_kora_home", lambda: missing_home)
    monkeypatch.setattr("kora_cli.config.get_kora_home", lambda: missing_home)
    monkeypatch.setattr("kora_cli.web_server.get_kora_home", lambda: missing_home)

    from kora_cli import web_server

    result = await web_server.emit_panel_view(
        {"panel_name": "AlertsPanel"}
    )
    assert result == {"ok": True}
    log_path = missing_home / PANEL_VIEWS_FILENAME
    assert log_path.is_file()


# ---- 10. Write failure returns ok:true with warning -----------


@pytest.mark.asyncio
async def test_write_failure_returns_ok_true_with_warning(tmp_path, monkeypatch):
    """OSError on write must NOT crash the FE caller; instrumentation
    must never break UX. Endpoint logs + returns ok:true with a
    warning field so the operator-facing path stays green."""
    # Point at a path inside a read-only directory so the open() raises.
    readonly_dir = tmp_path / "readonly"
    readonly_dir.mkdir(mode=0o555)  # r-x for owner only
    try:
        monkeypatch.setenv("KORA_HOME", str(readonly_dir))
        monkeypatch.setattr("kora_constants.get_kora_home", lambda: readonly_dir)
        monkeypatch.setattr("kora_cli.config.get_kora_home", lambda: readonly_dir)
        monkeypatch.setattr(
            "kora_cli.web_server.get_kora_home", lambda: readonly_dir
        )

        from kora_cli import web_server

        result = await web_server.emit_panel_view(
            {"panel_name": "AlertsPanel"}
        )
        # FE never sees a 500; the warning is for forensic JSON-parse
        # in case operator queries the response.
        assert result.get("ok") is True
        assert result.get("warning") == "write_failed"
    finally:
        # Restore mode so pytest can clean up tmp_path
        readonly_dir.chmod(0o755)


# ---- 11. SECURITY: FE-supplied "kind" can't override ----------


@pytest.mark.asyncio
async def test_fe_supplied_kind_field_ignored(env):
    """The hardcoded kind="panel_view" must NOT be overrideable by
    FE payload — protects downstream JSONL queries that filter on
    kind from being polluted by a malicious / buggy FE that injects
    a different kind value to evade filters."""
    from kora_cli import web_server

    await web_server.emit_panel_view(
        {
            "panel_name": "AlertsPanel",
            "session_id": "s1",
            "kind": "audit",  # attempted override
        }
    )
    rows = _read_log_lines(env)
    assert rows[0]["kind"] == "panel_view"


# ---- 12. Frontend hook source pin ------------------------------


def test_use_panel_view_hook_source_exists():
    assert _HOOK_PATH.is_file(), f"missing: {_HOOK_PATH}"


def test_use_panel_view_posts_to_panel_view_endpoint():
    src = _HOOK_PATH.read_text()
    # POST to /api/panel_view via fetchJSON wrapper
    assert "/api/panel_view" in src
    assert 'method: "POST"' in src
    # panel_name + session_id in body
    assert "panel_name" in src and "session_id" in src


def test_use_panel_view_swallows_errors():
    """Instrumentation must never break UX — .catch on the POST
    must be present."""
    src = _HOOK_PATH.read_text()
    assert ".catch(" in src


# ---- 13. Every top-level page/panel calls the hook -----------


def test_every_top_level_page_imports_use_panel_view():
    """Inventory pin: every web/src/pages/*.tsx must import +
    invoke usePanelView with its file's component name. Prevents
    a future page being added without instrumentation."""
    missing_import = []
    missing_call = []
    expected_pages = sorted(p.stem for p in _PAGES_DIR.glob("*.tsx"))

    for name in expected_pages:
        src = (_PAGES_DIR / f"{name}.tsx").read_text()
        if "usePanelView" not in src:
            missing_import.append(name)
            continue
        if f'usePanelView("{name}")' not in src:
            missing_call.append(name)

    assert not missing_import, (
        f"pages missing usePanelView import: {missing_import}"
    )
    assert not missing_call, (
        f"pages with usePanelView import but no matching call "
        f"`usePanelView(\"<Name>\")`: {missing_call}"
    )


def test_panel_inventory_count_matches_expected():
    """Inventory: count of top-level pages should match the spec's
    instrumented count. A drift means either a new page was added
    (good — but should appear in the next PR's instrumentation
    audit) or a page was removed (also should be reflected). Pin
    catches both directions."""
    pages = list(_PAGES_DIR.glob("*.tsx"))
    # Current count is 34 per the instrumentation pass. Update this
    # number alongside any page-set change so the pin stays accurate.
    assert len(pages) == 34, (
        f"top-level page count drifted: found {len(pages)}, "
        f"expected 34 (KR-PANEL-USE-INSTRUMENTATION snapshot). "
        f"Update this assertion when adding/removing pages so the "
        f"instrumentation audit stays accurate."
    )
