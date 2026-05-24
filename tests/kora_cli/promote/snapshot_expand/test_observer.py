"""Tests for kora_cli.promote.snapshot_expand.observer."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kora_cli.promote.snapshot_expand.observer import (
    KNOWN_BACKED_TOOLS,
    collect_recent_tool_calls,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setenv(
        "KORA_AUDIT_LOG_PATH", str(tmp_path / "kora_audit_log.jsonl")
    )
    return tmp_path


def _write_audit(tmp_path: Path, entries: list) -> None:
    path = tmp_path / "kora_audit_log.jsonl"
    path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )


def _entry(
    *,
    tool_name: str = "kora__open_tickets",
    arguments: dict | None = None,
    caller_session_id: str = "D1JOSH:1700000000.1",
    emitted_at: datetime | None = None,
    seam: str = "reasoning.tool_called",
) -> dict:
    if emitted_at is None:
        emitted_at = datetime.now(timezone.utc) - timedelta(hours=1)
    details: dict = {"tool_name": tool_name}
    if arguments is not None:
        details["arguments"] = arguments
    return {
        "emitted_at": emitted_at.isoformat(),
        "seam": seam,
        "details": details,
        "caller_session_id": caller_session_id,
        "source": "reasoning",
    }


@pytest.mark.asyncio
async def test_collect_returns_tool_called_entries(tmp_path):
    _write_audit(tmp_path, [_entry(tool_name="kora__open_tickets")])
    out = await collect_recent_tool_calls(
        since=datetime.now(timezone.utc) - timedelta(hours=24),
    )
    assert len(out) == 1
    assert out[0].tool_name == "kora__open_tickets"


@pytest.mark.asyncio
async def test_collect_excludes_known_backed_tools(tmp_path):
    """Tools whose answers are already snapshot-backed shouldn't show
    up — proposing a redundant field would just waste operator review
    time."""
    backed = next(iter(KNOWN_BACKED_TOOLS))
    _write_audit(
        tmp_path,
        [
            _entry(tool_name="kora__open_tickets"),
            _entry(tool_name=backed),
        ],
    )
    out = await collect_recent_tool_calls(
        since=datetime.now(timezone.utc) - timedelta(hours=24),
    )
    assert [o.tool_name for o in out] == ["kora__open_tickets"]


@pytest.mark.asyncio
async def test_collect_excludes_other_seams(tmp_path):
    _write_audit(
        tmp_path,
        [
            _entry(tool_name="kora__open_tickets"),
            _entry(tool_name="kora__irrelevant", seam="mcp.tool_called"),
        ],
    )
    out = await collect_recent_tool_calls(
        since=datetime.now(timezone.utc) - timedelta(hours=24),
    )
    assert [o.tool_name for o in out] == ["kora__open_tickets"]


@pytest.mark.asyncio
async def test_collect_respects_since(tmp_path):
    old = datetime.now(timezone.utc) - timedelta(days=30)
    fresh = datetime.now(timezone.utc) - timedelta(hours=1)
    _write_audit(
        tmp_path,
        [
            _entry(tool_name="kora__a", emitted_at=old),
            _entry(tool_name="kora__b", emitted_at=fresh),
        ],
    )
    out = await collect_recent_tool_calls(
        since=datetime.now(timezone.utc) - timedelta(days=7),
    )
    assert [o.tool_name for o in out] == ["kora__b"]


@pytest.mark.asyncio
async def test_collect_projects_arguments_summary(tmp_path):
    _write_audit(
        tmp_path,
        [
            _entry(
                tool_name="kora__filter_tickets",
                arguments={"status": "open", "owner": "kora"},
            )
        ],
    )
    out = await collect_recent_tool_calls(
        since=datetime.now(timezone.utc) - timedelta(hours=24),
    )
    assert len(out) == 1
    assert "status=open" in out[0].arguments_summary
    assert "owner=kora" in out[0].arguments_summary


@pytest.mark.asyncio
async def test_collect_missing_file_returns_empty(tmp_path):
    out = await collect_recent_tool_calls(
        since=datetime.now(timezone.utc) - timedelta(hours=24),
    )
    assert out == []
