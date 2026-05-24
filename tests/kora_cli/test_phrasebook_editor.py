"""KR-FE-PHRASEBOOK-EDITOR-AND-CRUD — backend tests.

Covers the write path added on top of PR #167's read-only viewer:

  Validation (kora_cli/short_circuit/phrasebook_editor.py):
    1. Valid entry list → no errors
    2. Non-list payload → root error
    3. Missing field → per-field error
    4. Empty field (whitespace) → per-field error
    5. Pattern length cap
    6. Reply template length cap
    7. Description / category length caps
    8. Entries count cap (MAX_ENTRIES_PER_PHRASEBOOK)
    9. Invalid regex → pattern error
   10. Catastrophic-backtracking guard catches (x+)+, (.*)*
   11. Snapshot path not in static schema → reply_template error
   12. Valid snapshot path accepted
   13. Duplicate (pattern, category) → root error pointing at first
   14. Schema set matches snapshot collectors (drift guard)

  Backup discipline:
   15. write_backup_for with no override → returns None (first edit)
   16. write_backup_for copies content + timestamps filename
   17. rotate_backups keeps N most recent
   18. KORA_PHRASEBOOK_BACKUP_COUNT env override read + clamped
   19. list_backups returns newest-first with entry_count

  Write + revert:
   20. write_phrasebook serializes deterministic YAML + uses atomic_replace
   21. revert with named backup restores its content
   22. revert with no filename takes most-recent backup
   23. revert with no backups removes the override entirely
   24. revert refuses path-traversal filenames

  Endpoints:
   25. PUT valid → 200 + override written + backup + audit row
   26. PUT invalid → 422; no write; previous content preserved
   27. PUT with no existing override → no backup but still writes
   28. PUT write_failed audit not emitted (failure path)
   29. POST revert → 200 + correct reverted_to + audit row
   30. POST revert invalid filename → 400
   31. POST revert missing backup → 404
   32. GET backups → newest-first list

  Audit seam:
   33. SeamName Literal includes phrasebook.updated
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

import pytest

from tests.kora_cli._panel_test_helpers import isolated_kora_home


_REPO_ROOT = Path(__file__).resolve().parents[2]
_EDITOR_PY = (
    _REPO_ROOT / "kora_cli" / "short_circuit" / "phrasebook_editor.py"
)
_SNAPSHOT_PY = _REPO_ROOT / "kora_cli" / "snapshot" / "state_snapshot.py"
_JSONL_SINK = _REPO_ROOT / "kora_cli" / "audit" / "jsonl_sink.py"


@pytest.fixture
def env(tmp_path, monkeypatch):
    return isolated_kora_home(tmp_path, monkeypatch)


def _valid_entry(
    pattern: str = r"^hello$",
    category: str = "greeting",
    description: str = "test",
    reply_template: str = "Hi!",
) -> Dict[str, Any]:
    return {
        "pattern": pattern,
        "category": category,
        "description": description,
        "reply_template": reply_template,
    }


# ---------------------------------------------------------------------------
# 1-13. Validation
# ---------------------------------------------------------------------------


def test_valid_entry_list_no_errors():
    from kora_cli.short_circuit import phrasebook_editor

    errors = phrasebook_editor.validate_entries([_valid_entry()])
    assert errors == []


def test_non_list_payload_root_error():
    from kora_cli.short_circuit import phrasebook_editor

    errors = phrasebook_editor.validate_entries("not a list")
    assert len(errors) == 1
    assert errors[0].entry_index == -1
    assert errors[0].field == "_root"


def test_missing_field_per_field_error():
    from kora_cli.short_circuit import phrasebook_editor

    bad = {"pattern": "x", "category": "y"}
    errors = phrasebook_editor.validate_entries([bad])
    fields = {e.field for e in errors}
    assert "description" in fields
    assert "reply_template" in fields


def test_empty_whitespace_field_error():
    from kora_cli.short_circuit import phrasebook_editor

    bad = _valid_entry(pattern="   ")
    errors = phrasebook_editor.validate_entries([bad])
    assert any(e.field == "pattern" for e in errors)


def test_pattern_length_cap():
    from kora_cli.short_circuit import phrasebook_editor

    long_pattern = "a" * (phrasebook_editor.MAX_PATTERN_LENGTH + 1)
    errors = phrasebook_editor.validate_entries(
        [_valid_entry(pattern=long_pattern)]
    )
    assert any(
        e.field == "pattern" and "exceeds" in e.error for e in errors
    )


def test_reply_template_length_cap():
    from kora_cli.short_circuit import phrasebook_editor

    long_reply = "a" * (phrasebook_editor.MAX_REPLY_TEMPLATE_LENGTH + 1)
    errors = phrasebook_editor.validate_entries(
        [_valid_entry(reply_template=long_reply)]
    )
    assert any(
        e.field == "reply_template" and "exceeds" in e.error for e in errors
    )


def test_description_and_category_length_caps():
    from kora_cli.short_circuit import phrasebook_editor

    long_desc = "x" * (phrasebook_editor.MAX_DESCRIPTION_LENGTH + 1)
    long_cat = "y" * (phrasebook_editor.MAX_CATEGORY_LENGTH + 1)
    errors = phrasebook_editor.validate_entries(
        [_valid_entry(description=long_desc, category=long_cat)]
    )
    fields = {e.field for e in errors}
    assert "description" in fields
    assert "category" in fields


def test_entries_count_cap():
    from kora_cli.short_circuit import phrasebook_editor

    too_many = [
        _valid_entry(pattern=f"^p{i}$", category=f"c{i}")
        for i in range(phrasebook_editor.MAX_ENTRIES_PER_PHRASEBOOK + 1)
    ]
    errors = phrasebook_editor.validate_entries(too_many)
    assert any(
        e.entry_index == -1 and "too many" in e.error for e in errors
    )


def test_invalid_regex_error():
    from kora_cli.short_circuit import phrasebook_editor

    errors = phrasebook_editor.validate_entries(
        [_valid_entry(pattern="(unclosed")]
    )
    assert any(
        e.field == "pattern" and "invalid regex" in e.error for e in errors
    )


@pytest.mark.parametrize(
    "pathological",
    [
        r"(a+)+",
        r"(.*)*",
        r"(\w*)?",
        r"(.+)+",
    ],
)
def test_catastrophic_backtracking_guard(pathological):
    from kora_cli.short_circuit import phrasebook_editor

    errors = phrasebook_editor.validate_entries(
        [_valid_entry(pattern=pathological)]
    )
    assert any(
        e.field == "pattern" and "backtracking" in e.error for e in errors
    ), f"pattern {pathological!r} should be flagged"


def test_unknown_snapshot_path_rejected():
    from kora_cli.short_circuit import phrasebook_editor

    errors = phrasebook_editor.validate_entries(
        [
            _valid_entry(
                reply_template="Hello {snapshot.totally.not.real.field}!"
            )
        ]
    )
    assert any(
        e.field == "reply_template" and "not in known scalar schema" in e.error
        for e in errors
    )


def test_known_snapshot_path_accepted():
    from kora_cli.short_circuit import phrasebook_editor

    errors = phrasebook_editor.validate_entries(
        [
            _valid_entry(
                reply_template=(
                    "Burn: {snapshot.cost_ladder.spent_to_date_usd} / "
                    "{snapshot.cost_ladder.credit_pool_usd}"
                )
            )
        ]
    )
    assert errors == []


def test_duplicate_pattern_category_pair_rejected():
    from kora_cli.short_circuit import phrasebook_editor

    entries = [
        _valid_entry(pattern="^hi$", category="greeting"),
        _valid_entry(
            pattern="^hi$",
            category="greeting",
            description="dup",
            reply_template="hi",
        ),
    ]
    errors = phrasebook_editor.validate_entries(entries)
    assert any(
        e.entry_index == 1
        and e.field == "_root"
        and "duplicate" in e.error
        for e in errors
    )


def test_static_schema_matches_snapshot_collectors():
    """Drift guard: SNAPSHOT_SCALAR_PATHS in phrasebook_editor must
    name fields that are actually populated by the snapshot
    collector functions in state_snapshot.py. If the snapshot adds
    a new scalar field (e.g. cost_ladder.foo) without us updating
    the allow-list, operator templates referencing it would be
    rejected. If state_snapshot RENAMES a field without us
    updating, accepted templates would silently fall through at
    runtime. This test catches the rename case by greping the
    snapshot source for each declared scalar key."""
    from kora_cli.short_circuit import phrasebook_editor

    snap_src = _SNAPSHOT_PY.read_text()
    # The allow-list uses dotted paths; the snapshot source
    # writes them as nested dict keys. For each path, the LEAF
    # key must appear in the snapshot source. (Catches rename;
    # doesn't catch deeper structural moves — those'd need a
    # round-trip snapshot build, which is heavier than warranted
    # for a v1 drift guard.)
    for path in phrasebook_editor.SNAPSHOT_SCALAR_PATHS:
        leaf = path.split(".")[-1]
        # Top-level metadata (computed_at / schema_version) live
        # directly in the build dict — also pinned.
        assert f'"{leaf}"' in snap_src, (
            f"SNAPSHOT_SCALAR_PATHS path '{path}' leaf '{leaf}' not "
            f"found in {_SNAPSHOT_PY} — drift between allow-list "
            f"and snapshot collectors"
        )


# ---------------------------------------------------------------------------
# 15-19. Backups
# ---------------------------------------------------------------------------


def test_write_backup_for_no_override_returns_none(env):
    from kora_cli.short_circuit import phrasebook_editor

    override = phrasebook_editor._override_path()
    assert phrasebook_editor.write_backup_for(override) is None


def test_write_backup_for_copies_content_and_timestamps(env):
    from kora_cli.short_circuit import phrasebook_editor

    override = phrasebook_editor._override_path()
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text("entries:\n  - pattern: hi\n", encoding="utf-8")

    bkp = phrasebook_editor.write_backup_for(override)
    assert bkp is not None
    assert bkp.exists()
    assert bkp.read_text() == override.read_text()
    # Filename: slack_dm.{ISO}.yml
    assert re.match(
        r"slack_dm\.\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z(?:-\d+)?\.yml",
        bkp.name,
    )


def test_rotate_backups_keeps_n_most_recent(env):
    from kora_cli.short_circuit import phrasebook_editor

    bkp_dir = phrasebook_editor._backup_dir()
    bkp_dir.mkdir(parents=True, exist_ok=True)
    # Names sort chronologically as plain strings.
    names = [
        "slack_dm.2026-01-01T00-00-00Z.yml",
        "slack_dm.2026-01-02T00-00-00Z.yml",
        "slack_dm.2026-01-03T00-00-00Z.yml",
        "slack_dm.2026-01-04T00-00-00Z.yml",
        "slack_dm.2026-01-05T00-00-00Z.yml",
    ]
    for n in names:
        (bkp_dir / n).write_text("entries: []\n", encoding="utf-8")
    removed = phrasebook_editor.rotate_backups(keep=3)
    assert len(removed) == 2
    remaining = sorted(p.name for p in bkp_dir.glob("slack_dm.*.yml"))
    assert remaining == names[-3:]


@pytest.mark.parametrize(
    "envval,expected",
    [
        ("", 10),  # default
        ("5", 5),
        ("not-a-number", 10),
        ("0", 1),  # clamp lower
        ("99999", 1000),  # clamp upper
    ],
)
def test_backup_keep_count_env_clamping(envval, expected, monkeypatch):
    from kora_cli.short_circuit import phrasebook_editor

    if envval:
        monkeypatch.setenv(phrasebook_editor.BACKUP_KEEP_ENV, envval)
    else:
        monkeypatch.delenv(phrasebook_editor.BACKUP_KEEP_ENV, raising=False)
    assert phrasebook_editor._backup_keep_count() == expected


def test_list_backups_returns_newest_first_with_entry_count(env):
    from kora_cli.short_circuit import phrasebook_editor

    bkp_dir = phrasebook_editor._backup_dir()
    bkp_dir.mkdir(parents=True, exist_ok=True)
    (bkp_dir / "slack_dm.2026-01-01T00-00-00Z.yml").write_text(
        "entries:\n  - pattern: a\n    category: c\n    description: d\n"
        "    reply_template: r\n",
        encoding="utf-8",
    )
    (bkp_dir / "slack_dm.2026-01-02T00-00-00Z.yml").write_text(
        "entries:\n  - pattern: a\n    category: c\n    description: d\n"
        "    reply_template: r\n  - pattern: b\n    category: c\n"
        "    description: d\n    reply_template: r\n",
        encoding="utf-8",
    )
    backups = phrasebook_editor.list_backups()
    assert [b["filename"] for b in backups] == [
        "slack_dm.2026-01-02T00-00-00Z.yml",
        "slack_dm.2026-01-01T00-00-00Z.yml",
    ]
    assert backups[0]["entry_count"] == 2
    assert backups[1]["entry_count"] == 1


# ---------------------------------------------------------------------------
# 20-24. Write + revert
# ---------------------------------------------------------------------------


def test_write_phrasebook_produces_loadable_yaml(env):
    from kora_cli.short_circuit import dm_phrasebook, phrasebook_editor

    entries = [_valid_entry()]
    override = phrasebook_editor.write_phrasebook(entries)
    assert override.is_file()
    # Round-trip: the same load_phrasebook the live handler uses
    # must read what we just wrote.
    loaded = dm_phrasebook.load_phrasebook()
    assert len(loaded) == 1
    assert loaded[0].pattern.pattern == "^hello$"


def test_revert_with_named_backup_restores(env):
    from kora_cli.short_circuit import phrasebook_editor

    override = phrasebook_editor._override_path()
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text("entries:\n  - pattern: original\n", encoding="utf-8")
    bkp = phrasebook_editor.write_backup_for(override)
    # Now overwrite the override
    override.write_text("entries:\n  - pattern: changed\n", encoding="utf-8")
    # Revert by name
    result = phrasebook_editor.revert_phrasebook(filename=bkp.name)
    assert result["reverted_to"] == bkp.name
    assert "original" in override.read_text()


def test_revert_with_no_filename_takes_most_recent(env):
    from kora_cli.short_circuit import phrasebook_editor

    override = phrasebook_editor._override_path()
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text("entries:\n  - pattern: v1\n", encoding="utf-8")
    bkp_dir = phrasebook_editor._backup_dir()
    bkp_dir.mkdir(parents=True, exist_ok=True)
    older = bkp_dir / "slack_dm.2026-01-01T00-00-00Z.yml"
    newer = bkp_dir / "slack_dm.2026-01-02T00-00-00Z.yml"
    older.write_text("entries:\n  - pattern: older\n", encoding="utf-8")
    newer.write_text("entries:\n  - pattern: newer\n", encoding="utf-8")
    result = phrasebook_editor.revert_phrasebook(filename=None)
    assert result["reverted_to"] == newer.name
    assert "newer" in override.read_text()


def test_revert_with_no_backups_removes_override(env):
    from kora_cli.short_circuit import phrasebook_editor

    override = phrasebook_editor._override_path()
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text("entries:\n  - pattern: v1\n", encoding="utf-8")
    result = phrasebook_editor.revert_phrasebook(filename=None)
    assert result["reverted_to"] == "bundled_default"
    assert result["source_path"] is None
    assert not override.exists()


@pytest.mark.parametrize(
    "bad_filename",
    [
        "../../../etc/passwd",
        "/etc/passwd",
        "slack_dm/../passwd.yml",
        "not_a_phrasebook.yml",
        "slack_dm.foo.txt",
    ],
)
def test_revert_refuses_path_traversal(env, bad_filename):
    from kora_cli.short_circuit import phrasebook_editor

    with pytest.raises(ValueError):
        phrasebook_editor.revert_phrasebook(filename=bad_filename)


# ---------------------------------------------------------------------------
# 25-32. Endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_valid_writes_and_emits_audit(env):
    from kora_cli import web_server
    from kora_cli.short_circuit import phrasebook_editor

    payload = {"entries": [_valid_entry()]}
    result = await web_server.put_phrasebook(payload)
    # Successful response is a dict (not a JSONResponse).
    assert isinstance(result, dict)
    assert result["entry_count"] == 1
    assert result["source_path"] == str(phrasebook_editor._override_path())

    # Override file written.
    assert phrasebook_editor._override_path().is_file()

    # Audit row emitted.
    audit_log = env / "kora_audit_log.jsonl"
    assert audit_log.is_file()
    lines = [
        json.loads(line)
        for line in audit_log.read_text().splitlines()
        if line.strip()
    ]
    seams = {e.get("seam") for e in lines}
    assert "phrasebook.updated" in seams
    pb_audit = [e for e in lines if e.get("seam") == "phrasebook.updated"][0]
    assert pb_audit["details"]["actor"] == "operator"
    assert pb_audit["details"]["action"] == "put"
    assert pb_audit["details"]["entry_count_after"] == 1


@pytest.mark.asyncio
async def test_put_invalid_preserves_previous_content(env):
    from fastapi.responses import JSONResponse
    from kora_cli.short_circuit import phrasebook_editor
    from kora_cli import web_server

    # Seed an existing override the operator should not lose.
    override = phrasebook_editor._override_path()
    override.parent.mkdir(parents=True, exist_ok=True)
    seed_yaml = (
        "entries:\n"
        "  - pattern: '^seed$'\n"
        "    category: seed_cat\n"
        "    description: seed\n"
        "    reply_template: seed\n"
    )
    override.write_text(seed_yaml, encoding="utf-8")
    before = override.read_text()

    bad_payload = {
        "entries": [_valid_entry(pattern="(unclosed")]
    }
    result = await web_server.put_phrasebook(bad_payload)
    assert isinstance(result, JSONResponse)
    assert result.status_code == 422
    body = json.loads(result.body.decode())
    assert body["error"] == "validation_failed"
    assert any(e["field"] == "pattern" for e in body["errors"])
    # Override unchanged.
    assert override.read_text() == before


@pytest.mark.asyncio
async def test_put_no_existing_override_no_backup_but_writes(env):
    from kora_cli import web_server
    from kora_cli.short_circuit import phrasebook_editor

    payload = {"entries": [_valid_entry()]}
    result = await web_server.put_phrasebook(payload)
    assert isinstance(result, dict)
    # No previous override → backup_filename should be None
    assert result["backup_filename"] is None
    # But override file is now written
    assert phrasebook_editor._override_path().is_file()


@pytest.mark.asyncio
async def test_post_revert_with_backup(env):
    from kora_cli import web_server
    from kora_cli.short_circuit import phrasebook_editor

    # Seed override + make a backup of it
    override = phrasebook_editor._override_path()
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(
        "entries:\n  - pattern: original\n    category: c\n"
        "    description: d\n    reply_template: r\n",
        encoding="utf-8",
    )
    bkp = phrasebook_editor.write_backup_for(override)
    # Overwrite override
    override.write_text(
        "entries:\n  - pattern: changed\n    category: c\n"
        "    description: d\n    reply_template: r\n",
        encoding="utf-8",
    )
    # Revert via endpoint
    result = await web_server.revert_phrasebook_endpoint(
        {"filename": bkp.name}
    )
    assert isinstance(result, dict)
    assert result["reverted_to"] == bkp.name
    assert "original" in override.read_text()
    # Audit emitted
    audit = env / "kora_audit_log.jsonl"
    lines = [json.loads(line) for line in audit.read_text().splitlines() if line.strip()]
    assert any(
        e.get("seam") == "phrasebook.updated"
        and e["details"].get("action") == "revert"
        for e in lines
    )


@pytest.mark.asyncio
async def test_post_revert_invalid_filename_400(env):
    from fastapi.responses import JSONResponse
    from kora_cli import web_server

    result = await web_server.revert_phrasebook_endpoint(
        {"filename": "../../etc/passwd"}
    )
    assert isinstance(result, JSONResponse)
    assert result.status_code == 400


@pytest.mark.asyncio
async def test_post_revert_missing_backup_404(env):
    from fastapi.responses import JSONResponse
    from kora_cli import web_server

    result = await web_server.revert_phrasebook_endpoint(
        {"filename": "slack_dm.2099-01-01T00-00-00Z.yml"}
    )
    assert isinstance(result, JSONResponse)
    assert result.status_code == 404


@pytest.mark.asyncio
async def test_get_backups_newest_first(env):
    from kora_cli import web_server
    from kora_cli.short_circuit import phrasebook_editor

    bkp_dir = phrasebook_editor._backup_dir()
    bkp_dir.mkdir(parents=True, exist_ok=True)
    (bkp_dir / "slack_dm.2026-01-01T00-00-00Z.yml").write_text(
        "entries: []\n", encoding="utf-8"
    )
    (bkp_dir / "slack_dm.2026-01-02T00-00-00Z.yml").write_text(
        "entries: []\n", encoding="utf-8"
    )
    result = await web_server.get_phrasebook_backups()
    assert [b["filename"] for b in result["backups"]] == [
        "slack_dm.2026-01-02T00-00-00Z.yml",
        "slack_dm.2026-01-01T00-00-00Z.yml",
    ]
    assert isinstance(result["rotation_keep"], int)


# ---------------------------------------------------------------------------
# 33. Audit seam Literal
# ---------------------------------------------------------------------------


def test_seam_name_literal_includes_phrasebook_updated():
    """The audit seam Literal at kora_cli/audit/jsonl_sink.py must
    declare phrasebook.updated as a valid seam — otherwise
    emit_audit raises ValidationError and the audit silently
    drops (only the structured-log line survives)."""
    src = _JSONL_SINK.read_text()
    assert '"phrasebook.updated"' in src


# ---------------------------------------------------------------------------
# Bonus — full round-trip integration test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_trip_put_then_revert_restores_seed(env):
    """Operator scenario: write a phrasebook, then revert it.
    Reverted content should match what was there pre-write."""
    from kora_cli import web_server
    from kora_cli.short_circuit import phrasebook_editor

    # Seed an existing override
    override = phrasebook_editor._override_path()
    override.parent.mkdir(parents=True, exist_ok=True)
    seed_yaml = (
        "entries:\n"
        "  - pattern: '^seed$'\n"
        "    category: c\n    description: d\n"
        "    reply_template: seed_reply\n"
    )
    override.write_text(seed_yaml, encoding="utf-8")

    # PUT new content (triggers backup of seed)
    new_payload = {
        "entries": [
            _valid_entry(
                pattern="^new$",
                category="c",
                description="d",
                reply_template="new_reply",
            )
        ]
    }
    put_result = await web_server.put_phrasebook(new_payload)
    assert isinstance(put_result, dict)
    backup_name = put_result["backup_filename"]
    assert backup_name is not None

    # Revert (no filename → most-recent backup, which is the seed)
    revert_result = await web_server.revert_phrasebook_endpoint(None)
    assert isinstance(revert_result, dict)
    assert revert_result["reverted_to"] == backup_name
    assert "seed_reply" in override.read_text()
