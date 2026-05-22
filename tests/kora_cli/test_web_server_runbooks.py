"""Tests for the KR-P2-RUNBOOKS-PANEL endpoints.

Bucket §5 scenarios:
  1. GET /api/runbooks returns 200 + manifest shape
  2. Each entry has the required keys; `available` is bool
  3. GET /api/runbooks/{id}/content returns markdown text for available runbooks
  4. GET /api/runbooks/missing/content returns 404
  5. Response size capped at 1MB (test with oversized file)
  6. Path-traversal defense: GET /api/runbooks/../../etc/passwd should NOT escape
  7. Cron-regression sanity
"""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("kora_cli.config.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr(
        "kora_cli.config.get_config_path", lambda: tmp_path / "config.yaml"
    )
    monkeypatch.setattr(
        "kora_cli.config.get_env_path", lambda: tmp_path / ".env"
    )
    return tmp_path


# ---- 1. Manifest: 200 + shape -----------------------------------------


@pytest.mark.asyncio
async def test_list_runbooks_returns_200_and_shape(_isolate_config):
    from kora_cli import web_server

    result = await web_server.list_runbooks()
    assert isinstance(result, dict)
    assert "runbooks" in result
    assert isinstance(result["runbooks"], list)
    assert len(result["runbooks"]) >= 1  # at least deploy_fly_io which is in-repo


# ---- 2. Per-entry shape -----------------------------------------------


@pytest.mark.asyncio
async def test_runbook_entries_have_required_keys(_isolate_config):
    from kora_cli import web_server

    result = await web_server.list_runbooks()
    for entry in result["runbooks"]:
        assert set(entry.keys()) >= {
            "id",
            "title",
            "path",
            "available",
            "size_bytes",
            "last_modified",
        }
        assert isinstance(entry["id"], str) and entry["id"]
        assert isinstance(entry["title"], str) and entry["title"]
        assert isinstance(entry["path"], str) and entry["path"]
        assert isinstance(entry["available"], bool)
        # size_bytes + last_modified are null when unavailable, non-null when available
        if entry["available"]:
            assert isinstance(entry["size_bytes"], int)
            assert entry["size_bytes"] >= 0
            assert isinstance(entry["last_modified"], str)
        else:
            assert entry["size_bytes"] is None
            assert entry["last_modified"] is None


@pytest.mark.asyncio
async def test_deploy_fly_io_runbook_is_available_on_main(_isolate_config):
    """The in-repo docs/deploy-fly-io.md shipped with KR-P2-F-pre ST3
    should always be available — pin the contract so a future repo
    reorganization that breaks it surfaces here."""
    from kora_cli import web_server

    result = await web_server.list_runbooks()
    deploy_entry = next(
        (r for r in result["runbooks"] if r["id"] == "deploy_fly_io"),
        None,
    )
    assert deploy_entry is not None
    assert deploy_entry["available"] is True
    assert deploy_entry["size_bytes"] is not None
    assert deploy_entry["size_bytes"] > 0


# ---- 3. Content endpoint for available runbook ------------------------


@pytest.mark.asyncio
async def test_get_runbook_content_returns_markdown(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_runbook_content("deploy_fly_io")
    # FastAPI Response object — body is bytes
    body = bytes(result.body).decode("utf-8")
    assert len(body) > 0
    assert result.media_type.startswith("text/markdown")


# ---- 4. Missing id → 404 ---------------------------------------------


@pytest.mark.asyncio
async def test_get_runbook_content_returns_404_for_unknown_id(_isolate_config):
    from fastapi import HTTPException
    from kora_cli import web_server

    with pytest.raises(HTTPException) as exc:
        await web_server.get_runbook_content("does_not_exist_id")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_runbook_content_404_for_unauthored_manifest_entry(_isolate_config):
    """Manifest entries pointing at kora_docs/ paths that don't exist in
    this repo (separate kora-docs repo) should 404 from /content with a
    "not yet authored" message — distinguishable from "unknown id" so
    the FE can show the placeholder card.

    Originally pinned ``dr_runbook`` (PR #84). PR #86
    (KR-P2-RUNBOOKS-AUTHOR) vendored that file + token_rotation_runbook,
    so the original assertion went stale — exactly the auto-improvement
    behaviour PR #84's body predicted. Swapped to ``kora_dna``, which
    is still a placeholder (Kora's DNA reference doc lives in the
    separate kora-docs repo and isn't on a near-term vendoring plan).
    """
    from fastapi import HTTPException
    from kora_cli import web_server

    # kora_dna references kora_docs/00_canonical_current_state/kora_dna.md
    # which isn't vendored into this repo — file is missing.
    with pytest.raises(HTTPException) as exc:
        await web_server.get_runbook_content("kora_dna")
    assert exc.value.status_code == 404
    assert "not yet authored" in str(exc.value.detail).lower()


# ---- 5. Size cap → 413 -----------------------------------------------


@pytest.mark.asyncio
async def test_oversize_runbook_returns_413(_isolate_config, monkeypatch, tmp_path):
    """When a manifest entry points at a file > 1 MiB, return 413 rather
    than streaming megabytes through the localhost dashboard."""
    from fastapi import HTTPException
    from kora_cli import web_server

    big_file = tmp_path / "oversize.md"
    big_file.write_bytes(b"# huge\n" + (b"x" * 1_100_000))

    monkeypatch.setitem(
        web_server._RUNBOOK_MANIFEST,
        "test_oversize",
        ("Test oversized runbook", str(big_file)),
    )
    # Override the repo-root resolver so the absolute path stays inside.
    monkeypatch.setattr(
        web_server, "_runbook_repo_root", lambda: tmp_path
    )

    with pytest.raises(HTTPException) as exc:
        await web_server.get_runbook_content("test_oversize")
    assert exc.value.status_code == 413
    assert "cap" in str(exc.value.detail).lower()


# ---- 6. Path-traversal defense ---------------------------------------


@pytest.mark.asyncio
async def test_path_traversal_attempt_returns_404(_isolate_config, caplog):
    """An id with '..' or '/' must be rejected — never used in path
    composition. Defense in depth: even if a future code change started
    composing paths from the id, the regex guard rejects bad input
    first. Verify a WARN is logged so operators can spot attempts."""
    import logging
    from fastapi import HTTPException
    from kora_cli import web_server

    with caplog.at_level(logging.WARNING, logger="kora_cli.web_server"):
        with pytest.raises(HTTPException) as exc:
            await web_server.get_runbook_content("../../etc/passwd")
        assert exc.value.status_code == 404

    assert any(
        "malformed runbook_id" in rec.message
        and "../../etc/passwd" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_path_traversal_with_dotdot_returns_404(_isolate_config):
    """Various traversal patterns all caught by the regex."""
    from fastapi import HTTPException
    from kora_cli import web_server

    for bad in ["..", "../etc", "foo/bar", "foo\\bar", "FOO", "1foo", "foo bar"]:
        with pytest.raises(HTTPException) as exc:
            await web_server.get_runbook_content(bad)
        assert exc.value.status_code == 404, (
            f"id={bad!r} should 404 but got {exc.value.status_code}"
        )


@pytest.mark.asyncio
async def test_manifest_path_outside_root_is_refused(_isolate_config, monkeypatch, tmp_path):
    """Even if a future manifest typo points at an absolute path outside
    the repo root (e.g. uses .. in the manifest), the resolved-path
    check refuses to serve. Belt+braces guard."""
    from fastapi import HTTPException
    from kora_cli import web_server

    outside_file = tmp_path / "outside.md"
    outside_file.write_text("should not be served", encoding="utf-8")

    monkeypatch.setitem(
        web_server._RUNBOOK_MANIFEST,
        "test_outside",
        ("Test outside-root runbook", str(outside_file)),
    )
    # Force the repo root to a sibling directory that DOESN'T contain
    # outside_file — so the resolved path is NOT relative to root.
    fake_root = tmp_path / "fake_root"
    fake_root.mkdir()
    monkeypatch.setattr(
        web_server, "_runbook_repo_root", lambda: fake_root
    )

    with pytest.raises(HTTPException) as exc:
        await web_server.get_runbook_content("test_outside")
    assert exc.value.status_code == 404


# ---- 7. Cron-regression sanity ---------------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_runbooks_registered(_isolate_config):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
