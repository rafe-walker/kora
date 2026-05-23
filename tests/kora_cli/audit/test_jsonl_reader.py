"""Tests for kora_cli.audit.jsonl_reader.read_audit_entries.

Companion to KR-AUDIT-JSONL-SINK's writer tests. Covers the shared
reader used by the 3 panel endpoints flipped in
KR-AUDIT-PANEL-ENDPOINTS.

Scenarios:
  1. Missing file → empty list (fresh daemon)
  2. Empty file → empty list
  3. Valid mixed-seam entries → all returned newest-first
  4. seam filter → only matching entries returned
  5. since filter → drops entries older than cutoff
  6. since with naive datetime → assumes UTC
  7. limit cap → returns the N newest
  8. Malformed JSON line → log + skip; others parsed
  9. Non-dict JSON line (array) → skipped
 10. Pydantic ValidationError on a single line → skipped, others ok
 11. Blank lines → skipped
 12. KORA_AUDIT_LOG_PATH env override honored
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kora_cli.audit.jsonl_reader import read_audit_entries
from kora_cli.audit.jsonl_sink import AUDIT_LOG_FILENAME, LOG_PATH_ENV


@pytest.fixture
def audit_log(tmp_path, monkeypatch):
    """Fixture: tmp KORA_HOME + audit log path resolution monkeypatched
    in the THREE namespaces per the KR-SLACK-DM-PANEL-FLIP (#137)
    lesson. Yields a writer helper."""
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setattr("kora_constants.get_kora_home", lambda: tmp_path)

    log_path = tmp_path / AUDIT_LOG_FILENAME

    def write(*entries):
        with log_path.open("a", encoding="utf-8") as f:
            for entry in entries:
                if isinstance(entry, str):
                    f.write(entry + "\n")
                else:
                    f.write(json.dumps(entry) + "\n")
        return log_path

    yield write


def _entry(
    *,
    seam: str = "mcp.tool_called",
    minutes_ago: int = 5,
    details: dict | None = None,
    caller_session_id: str | None = None,
    source: str | None = "mcp_http",
) -> dict:
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {
        "emitted_at": ts.isoformat(),
        "seam": seam,
        "details": details or {"tool_name": "kora__test", "caller_actor_kind": "test"},
        "caller_session_id": caller_session_id,
        "source": source,
    }


# ---- 1-2. Empty / missing file --------------------------------------


def test_missing_file_returns_empty_list(tmp_path, monkeypatch):
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setattr("kora_constants.get_kora_home", lambda: tmp_path)
    assert read_audit_entries() == []


def test_empty_file_returns_empty_list(audit_log):
    audit_log()  # touches no file
    assert read_audit_entries() == []


# ---- 3. Valid entries returned newest-first ------------------------


def test_valid_entries_returned_newest_first(audit_log):
    audit_log(
        _entry(minutes_ago=30, details={"name": "oldest"}),
        _entry(minutes_ago=10, details={"name": "newest"}),
        _entry(minutes_ago=20, details={"name": "middle"}),
    )
    entries = read_audit_entries()
    assert len(entries) == 3
    names = [e.details["name"] for e in entries]
    assert names == ["newest", "middle", "oldest"]


# ---- 4. seam filter ------------------------------------------------


def test_seam_filter_returns_only_matching(audit_log):
    audit_log(
        _entry(seam="mcp.tool_called"),
        _entry(seam="webhook.dead_letter", source="slack_dm"),
        _entry(seam="reasoning.tool_called", source="reasoning"),
    )
    result = read_audit_entries(seam="webhook.dead_letter")
    assert len(result) == 1
    assert result[0].seam == "webhook.dead_letter"


def test_seam_filter_unknown_value_matches_nothing(audit_log):
    audit_log(_entry())
    result = read_audit_entries(seam="not.a.seam")
    assert result == []


# ---- 5-6. since filter ---------------------------------------------


def test_since_filter_drops_older_entries(audit_log):
    audit_log(
        _entry(minutes_ago=60),  # older than cutoff
        _entry(minutes_ago=10),  # within cutoff
    )
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
    result = read_audit_entries(since=cutoff)
    assert len(result) == 1


def test_since_naive_datetime_assumed_utc(audit_log):
    audit_log(_entry(minutes_ago=10))
    naive_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).replace(tzinfo=None)
    result = read_audit_entries(since=naive_cutoff)
    assert len(result) == 1


# ---- 7. limit cap --------------------------------------------------


def test_limit_returns_n_newest(audit_log):
    audit_log(
        *[_entry(minutes_ago=i, details={"name": f"e-{i}"}) for i in range(10)]
    )
    result = read_audit_entries(limit=3)
    assert len(result) == 3
    names = [e.details["name"] for e in result]
    assert names == ["e-0", "e-1", "e-2"]


def test_limit_zero_returns_all(audit_log):
    audit_log(_entry(), _entry())
    result = read_audit_entries(limit=0)
    # limit > 0 check in reader; 0 falls through to "no cap"
    assert len(result) == 2


# ---- 8-11. Malformed input tolerance ------------------------------


def test_malformed_json_line_skipped(audit_log, caplog):
    audit_log(
        _entry(details={"name": "ok-1"}),
        "{NOT VALID JSON{{{",
        _entry(details={"name": "ok-2"}),
    )
    import logging
    with caplog.at_level(logging.WARNING):
        result = read_audit_entries()
    names = sorted(e.details["name"] for e in result)
    assert names == ["ok-1", "ok-2"]


def test_non_dict_json_line_skipped(audit_log):
    audit_log(
        _entry(details={"name": "ok-1"}),
        json.dumps([1, 2, 3]),
        _entry(details={"name": "ok-2"}),
    )
    result = read_audit_entries()
    assert len(result) == 2


def test_pydantic_validation_failure_skipped(audit_log):
    """A line with extra top-level fields fails AuditEntry's
    extra='forbid' validation; reader logs + skips."""
    audit_log(
        _entry(details={"name": "ok"}),
        {
            "emitted_at": datetime.now(timezone.utc).isoformat(),
            "seam": "mcp.tool_called",
            "details": {},
            # Unknown top-level field triggers extra='forbid'.
            "rogue_field": "boom",
        },
    )
    result = read_audit_entries()
    assert len(result) == 1
    assert result[0].details["name"] == "ok"


def test_blank_lines_skipped(audit_log):
    log_path = audit_log()
    with log_path.open("a") as f:
        f.write(json.dumps(_entry(details={"name": "one"})) + "\n")
        f.write("\n")
        f.write("   \n")
        f.write(json.dumps(_entry(details={"name": "two"})) + "\n")
    result = read_audit_entries()
    assert len(result) == 2


# ---- 12. Env override --------------------------------------------


def test_kora_audit_log_path_env_override(tmp_path, monkeypatch):
    """LOG_PATH_ENV takes precedence over KORA_HOME / get_kora_home."""
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    explicit_log = other_dir / "custom_audit.jsonl"
    with explicit_log.open("w") as f:
        f.write(json.dumps(_entry(details={"name": "from-env-path"})) + "\n")

    # KORA_HOME points somewhere else; reader must honor env override
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setattr("kora_constants.get_kora_home", lambda: tmp_path)
    monkeypatch.setenv(LOG_PATH_ENV, str(explicit_log))

    result = read_audit_entries()
    assert len(result) == 1
    assert result[0].details["name"] == "from-env-path"
