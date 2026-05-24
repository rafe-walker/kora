"""FE source-pin tests for KR-FE-PHRASEBOOK-EDITOR-AND-CRUD.

The endpoint + module behavior is covered in
test_phrasebook_editor.py (44 tests). This file pins the FE
wiring that's hard to test without a browser:

  1. api wrappers: putSlackDmPhrasebook + revertSlackDmPhrasebook
     + getSlackDmPhrasebookBackups all exist + post to the right
     URLs with the right verbs
  2. TS types declared: PhrasebookEntryWrite / PhrasebookPutResponse
     / PhrasebookValidationErrorEntry / PhrasebookValidationErrorBody
     / PhrasebookRevertResponse / PhrasebookBackupItem /
     PhrasebookBackupsResponse
  3. PhrasebookEditor.tsx file exists with the expected exports
  4. PhrasebookPage delegates to the editor in edit-mode
  5. Edit-mode controls wired: Edit button + Backups button +
     Save / Cancel / Add visible only in edit-mode
  6. Validation-error display: 422 body parsing branch present
  7. ClientSidePreview mirrors the backend regex + walk semantics
     (case-insensitive regex + "unknown" sentinel triggers
     fall-through)
  8. SnapshotResponse TS type already declares the v4 sections
     PhrasebookEditor's static validation references
"""

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_API_TS = _REPO_ROOT / "web" / "src" / "lib" / "api.ts"
_PAGE = _REPO_ROOT / "web" / "src" / "pages" / "PhrasebookPage.tsx"
_EDITOR = _REPO_ROOT / "web" / "src" / "pages" / "PhrasebookEditor.tsx"


# ---------------------------------------------------------------------------
# 1. api wrappers
# ---------------------------------------------------------------------------


def test_put_wrapper_exists():
    src = _API_TS.read_text()
    assert re.search(
        r'putSlackDmPhrasebook:\s*\(entries:\s*PhrasebookEntryWrite\[\]\)',
        src,
    )
    # Verb + URL pin — without this, the wrapper would GET (which
    # would 405) or hit a different path.
    assert (
        '"/api/phrasebook/slack_dm"' in src
        and '"PUT"' in src
    )


def test_revert_wrapper_exists():
    src = _API_TS.read_text()
    assert "revertSlackDmPhrasebook:" in src
    assert '"/api/phrasebook/slack_dm/revert"' in src
    assert '"POST"' in src


def test_backups_list_wrapper_exists():
    src = _API_TS.read_text()
    assert "getSlackDmPhrasebookBackups:" in src
    assert '"/api/phrasebook/slack_dm/backups"' in src


# ---------------------------------------------------------------------------
# 2. TS types declared
# ---------------------------------------------------------------------------


def test_write_types_declared():
    src = _API_TS.read_text()
    for ts_type in (
        "PhrasebookEntryWrite",
        "PhrasebookPutResponse",
        "PhrasebookValidationErrorEntry",
        "PhrasebookValidationErrorBody",
        "PhrasebookRevertResponse",
        "PhrasebookBackupItem",
        "PhrasebookBackupsResponse",
    ):
        assert f"export interface {ts_type}" in src or (
            f"export type {ts_type}" in src
        ), f"missing TS type: {ts_type}"


def test_validation_error_shape_matches_backend():
    """PhrasebookValidationErrorEntry must have entry_index +
    field + error to match the backend's EntryValidationError.
    as_dict() shape. Drift here makes the editor render wrong
    fields' errors."""
    src = _API_TS.read_text()
    # Find the interface block + check the 3 fields appear within
    # ~20 lines of the declaration.
    m = re.search(
        r"export interface PhrasebookValidationErrorEntry\s*\{([^}]+)\}",
        src,
    )
    assert m is not None
    body = m.group(1)
    assert "entry_index" in body
    assert "field" in body
    assert "error" in body


# ---------------------------------------------------------------------------
# 3. PhrasebookEditor.tsx
# ---------------------------------------------------------------------------


def test_editor_file_exists_and_exports():
    assert _EDITOR.is_file()
    src = _EDITOR.read_text()
    for export in (
        "EntryEditorRow",
        "EditModeControls",
        "BackupsDialog",
        "ClientSidePreview",
        "makeEmptyEntry",
        "toEditableEntries",
        "clientSidePreview",
    ):
        assert f"export function {export}" in src or (
            f"export const {export}" in src
            or f"export interface {export}" in src
            or f"export type {export}" in src
        ), f"missing export: {export}"


# ---------------------------------------------------------------------------
# 4. PhrasebookPage delegates to editor in edit-mode
# ---------------------------------------------------------------------------


def test_page_imports_editor_components():
    src = _PAGE.read_text()
    for import_name in (
        "BackupsDialog",
        "ClientSidePreview",
        "EditModeControls",
        "EntryEditorRow",
        "makeEmptyEntry",
        "toEditableEntries",
    ):
        assert import_name in src, f"PhrasebookPage missing import: {import_name}"


def test_page_has_edit_mode_state():
    src = _PAGE.read_text()
    # editingEntries === null ≡ view mode (a discriminator the
    # render branch checks). Pin the literal so a refactor doesn't
    # change the discriminator and silently break the render.
    assert "editingEntries" in src
    assert "editingEntries === null" in src


def test_page_renders_editor_rows_in_edit_mode():
    src = _PAGE.read_text()
    assert "<EntryEditorRow" in src
    assert "<EditModeControls" in src
    assert "<ClientSidePreview" in src


# ---------------------------------------------------------------------------
# 5. Edit / Backups buttons visible only in view mode
# ---------------------------------------------------------------------------


def test_edit_button_present_when_not_editing():
    """When editingEntries === null, the page renders Edit +
    Backups buttons. When editing, those buttons hide
    (EditModeControls takes over)."""
    src = _PAGE.read_text()
    assert "Edit phrasebook" in src
    assert "Backups" in src
    # The Edit / Backups buttons appear inside the
    # `editingEntries === null && (...)` branch so they hide
    # during edit mode. Pin the conditional pattern.
    assert re.search(
        r"editingEntries\s*===\s*null\s*&&\s*\(\s*<>",
        src,
    ), "Edit/Backups buttons must be inside an editingEntries===null guard"


# ---------------------------------------------------------------------------
# 6. Validation-error display: 422 body parsing branch present
# ---------------------------------------------------------------------------


def test_page_parses_422_validation_body():
    """fetchJSON throws Error('STATUS: BODY') on non-2xx. The
    Save handler must parse 422 specially to surface per-field
    errors back to the editor rows. Without this branch, the
    user sees a generic 'Save failed' toast and no inline errors."""
    src = _PAGE.read_text()
    # Pin the 422 detection + JSON parse
    assert '"422"' in src or "'422'" in src
    assert "validation_failed" in src
    assert "setValidationErrors" in src


# ---------------------------------------------------------------------------
# 7. ClientSidePreview mirrors backend semantics
# ---------------------------------------------------------------------------


def test_client_side_preview_uses_case_insensitive_regex():
    """dm_phrasebook compiles with re.IGNORECASE; the FE preview
    must mirror so what the operator sees in the preview matches
    what the live handler will do."""
    src = _EDITOR.read_text()
    assert re.search(r'new RegExp\([^,]+,\s*"i"\)', src), (
        "ClientSidePreview must use 'i' flag (case-insensitive) "
        "to match dm_phrasebook's re.IGNORECASE"
    )


def test_client_side_preview_falls_through_on_unknown_sentinel():
    """Mirrors render_reply at dm_phrasebook.py:293 — value ==
    "unknown" triggers fall-through. Pin the literal so a typo
    later doesn't silently change the FE preview from the runtime."""
    src = _EDITOR.read_text()
    assert (
        'value === "unknown"' in src
        or "value === 'unknown'" in src
    )


def test_client_side_preview_falls_through_on_null_snapshot():
    """Mirrors render_reply at dm_phrasebook.py:285-286 — null
    snapshot triggers UNIVERSAL fall-through (even for templates
    with no placeholders). Without this branch, the FE preview
    would falsely show "matched + rendered" for a null snapshot
    when the live handler would actually fall through."""
    src = _EDITOR.read_text()
    assert "snapshot === null" in src
    assert "snapshot unavailable" in src.lower()


# ---------------------------------------------------------------------------
# 8. SnapshotResponse type already covers v4 — referenced by the
#    static SNAPSHOT_SCALAR_PATHS allow-list on the BE side; if the
#    TS type drifts the FE preview won't be able to read the same
#    paths the backend validation accepts.
# ---------------------------------------------------------------------------


def test_snapshot_response_includes_daemon_health_for_template_paths():
    """The static SNAPSHOT_SCALAR_PATHS allow-list on the BE
    includes daemon_health.* paths. SnapshotResponse TS type must
    have a corresponding daemon_health section so the FE preview
    can walk these paths against the live snapshot."""
    src = _API_TS.read_text()
    assert "daemon_health" in src
    assert "overall_status" in src
    assert "uptime_seconds" in src
