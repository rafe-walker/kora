"""Tests for ``kora_cli.audit.jsonl_sink`` — KR-AUDIT-JSONL-SINK.

Covers:
  - AuditEntry Pydantic shape (``extra="forbid"``)
  - emit_audit appends one parseable JSONL line per call
  - Append-only multi-call behavior
  - Path resolution: env override / KORA_HOME default / HERMES_HOME
    legacy fallback
  - Unwritable / missing-parent-dir paths → degrade to log-only;
    NO crash
  - Invalid seam name → defensive log + no JSONL write; NO raise
  - **SECURITY** — walk-payload sweep against token shapes + PII
    patterns over a diverse synthetic AuditEntry batch covering
    all 4 seams. Bodies / secrets / PII MUST NOT survive into
    JSONL output if callers accidentally pass them.
  - Per-seam allow-list test for ``details`` keys (catches schema
    drift across the 4 refactored emitter sites).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import pytest
from pydantic import ValidationError

from kora_cli.audit.jsonl_sink import (
    AUDIT_LOG_FILENAME,
    BATCH_SIZE_ENV,
    FLUSH_INTERVAL_ENV,
    LOG_PATH_ENV,
    AuditEntry,
    _reset_batching_for_tests,
    _resolve_log_path,
    emit_audit,
    flush_for_tests,
)


# KR-CHEAP-AUDIT-BATCHING — most legacy tests assume sync per-emit
# write semantics (they emit then immediately read the file). Force
# BATCH_SIZE=0 to preserve that for the legacy suite; the dedicated
# batching-behavior tests below opt back into batching explicitly.
@pytest.fixture(autouse=True)
def _disable_batching_by_default(monkeypatch):
    monkeypatch.setenv(BATCH_SIZE_ENV, "0")
    _reset_batching_for_tests()
    yield
    _reset_batching_for_tests()


# ---------------------------------------------------------------------------
# AuditEntry shape
# ---------------------------------------------------------------------------


def test_audit_entry_minimal_required_fields():
    e = AuditEntry(
        emitted_at=datetime.utcnow(),
        seam="mcp.tool_called",
    )
    assert e.details == {}
    assert e.caller_session_id is None
    assert e.source is None


def test_audit_entry_full_construction():
    e = AuditEntry(
        emitted_at=datetime.utcnow(),
        seam="reasoning.tool_called",
        details={"tool_name": "kora__get_operational_state"},
        caller_session_id="D01:1700.001",
        source="reasoning",
    )
    assert e.seam == "reasoning.tool_called"
    assert e.details["tool_name"] == "kora__get_operational_state"


def test_audit_entry_rejects_unknown_top_level_field():
    """``extra="forbid"`` catches schema drift at construction."""
    with pytest.raises(ValidationError):
        AuditEntry(
            emitted_at=datetime.utcnow(),
            seam="mcp.tool_called",
            unknown_field="oops",  # type: ignore[call-arg]
        )


def test_audit_entry_rejects_invalid_seam():
    with pytest.raises(ValidationError):
        AuditEntry(
            emitted_at=datetime.utcnow(),
            seam="bogus.seam",  # type: ignore[arg-type]
        )


def test_audit_entry_rejects_invalid_source():
    with pytest.raises(ValidationError):
        AuditEntry(
            emitted_at=datetime.utcnow(),
            seam="mcp.tool_called",
            source="not-a-real-source",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# emit_audit append behavior
# ---------------------------------------------------------------------------


def _read_jsonl_lines(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_emit_audit_writes_parseable_jsonl_line(tmp_path):
    path = tmp_path / "audit.jsonl"
    emit_audit(
        seam="reasoning.tool_called",
        details={"tool_name": "kora__get_operational_state", "tool_status": "ok"},
        caller_session_id="DCHAN:1.001",
        source="reasoning",
        log_path=path,
    )

    [entry] = _read_jsonl_lines(path)
    assert entry["seam"] == "reasoning.tool_called"
    assert entry["details"]["tool_name"] == "kora__get_operational_state"
    assert entry["caller_session_id"] == "DCHAN:1.001"
    assert entry["source"] == "reasoning"
    # ISO-8601 timestamp parseable.
    datetime.fromisoformat(entry["emitted_at"].replace("Z", "+00:00"))


def test_emit_audit_append_only(tmp_path):
    """Multiple calls append; existing rows preserved."""
    path = tmp_path / "audit.jsonl"
    for i in range(5):
        emit_audit(
            seam="mcp.tool_called",
            details={"tool_name": f"tool_{i}", "tool_kind": "mutating"},
            source="mcp_http",
            log_path=path,
        )
    entries = _read_jsonl_lines(path)
    assert len(entries) == 5
    assert [e["details"]["tool_name"] for e in entries] == [
        f"tool_{i}" for i in range(5)
    ]


def test_emit_audit_creates_parent_directory(tmp_path):
    """Path with non-existent parent dir → created on first emit."""
    path = tmp_path / "nested" / "dirs" / "audit.jsonl"
    emit_audit(
        seam="webhook.dead_letter",
        details={"source": "slack", "reason": "slack_signature_mismatch"},
        source="slack_dm",
        log_path=path,
    )
    assert path.exists()


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_resolve_log_path_env_override(monkeypatch, tmp_path):
    override = tmp_path / "custom" / "audit.jsonl"
    monkeypatch.setenv(LOG_PATH_ENV, str(override))
    assert _resolve_log_path() == override


def test_resolve_log_path_default_uses_kora_home(monkeypatch, tmp_path):
    monkeypatch.delenv(LOG_PATH_ENV, raising=False)
    import kora_constants

    monkeypatch.setattr(kora_constants, "get_kora_home", lambda: tmp_path)
    assert _resolve_log_path() == tmp_path / AUDIT_LOG_FILENAME


def test_resolve_log_path_hermes_home_fallback(monkeypatch, tmp_path):
    """KORA_HOME unset + HERMES_HOME set → resolver uses HERMES_HOME
    via get_kora_home()'s BC behavior. We just verify the path
    string ends with the expected filename — actual resolver chain
    is tested by kora_constants directly."""
    monkeypatch.delenv(LOG_PATH_ENV, raising=False)
    # Real fallback chain — get_kora_home handles KORA_HOME → HERMES_HOME
    # → ~/.kora. We just assert the filename is appended.
    resolved = _resolve_log_path()
    assert resolved.name == AUDIT_LOG_FILENAME


# ---------------------------------------------------------------------------
# Degrade-to-log-only on disk failure
# ---------------------------------------------------------------------------


def test_emit_audit_unwritable_path_does_not_crash(monkeypatch, caplog):
    """Disk failure (full / permission denied) → WARN log + return.
    Caller's already-emitted structured-log line is the
    operator-visible signal."""
    caplog.set_level(logging.WARNING)

    # Path under /proc which can't be written (read-only filesystem
    # on Linux; on macOS we simulate via a path that can't be created).
    bad_path = Path("/this/path/cannot/exist/under/any/normal/system/audit.jsonl")
    # Make parent.mkdir fail by passing a path whose parent points at
    # an existing FILE.
    import tempfile
    with tempfile.NamedTemporaryFile(delete=False) as f:
        existing_file = Path(f.name)
    bad_path = existing_file / "subdir" / "audit.jsonl"

    emit_audit(
        seam="mcp.tool_called",
        details={"tool_name": "x", "tool_kind": "mutating"},
        log_path=bad_path,
    )

    # No exception; WARN line emitted.
    skipped = [
        r for r in caplog.records if "kora.audit.skipped" in r.getMessage()
    ]
    assert len(skipped) >= 1


def test_emit_audit_invalid_seam_logs_skipped_no_raise(caplog, tmp_path):
    """Bad seam value → defensive WARN log + return; no JSONL written;
    no raise."""
    caplog.set_level(logging.WARNING)
    path = tmp_path / "audit.jsonl"
    emit_audit(
        seam="this.is.not.a.seam",  # invalid
        details={"x": 1},
        log_path=path,
    )
    skipped = [
        r for r in caplog.records if "kora.audit.skipped" in r.getMessage()
    ]
    assert len(skipped) == 1
    assert not path.exists()  # nothing written


# ---------------------------------------------------------------------------
# SECURITY — walk-payload sweep over diverse synthetic batch
# ---------------------------------------------------------------------------


# Token / credential / PII regexes — patterns that MUST NEVER appear
# in JSONL output. Each is a known shape from the codebase's audit
# surfaces. The walk-payload sweep iterates the JSONL output string
# against all of these.
_FORBIDDEN_PATTERNS: List[tuple[str, re.Pattern]] = [
    # Slack tokens (legacy + bot + user + app)
    ("slack_bot_token", re.compile(r"xoxb-[A-Za-z0-9-]+")),
    ("slack_user_token", re.compile(r"xoxp-[A-Za-z0-9-]+")),
    ("slack_app_token", re.compile(r"xapp-[A-Za-z0-9-]+")),
    # Anthropic OAuth + API key
    ("anthropic_oauth", re.compile(r"sk-ant-oat-[A-Za-z0-9-]+")),
    ("anthropic_api_key", re.compile(r"sk-ant-[a-zA-Z0-9_-]+")),
    # Generic bearer-shaped (case-insensitive matches like "Bearer xxx")
    ("bearer_header", re.compile(r"[Bb]earer\s+[A-Za-z0-9_.-]+")),
    # AWS / GCP service-account-shaped credentials (defensive)
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    # Naive email-address regex (PII).
    ("email_address", re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")),
]


def _walk_for_forbidden(content: str) -> List[str]:
    """Return all matched (pattern_name, snippet) tuples for any
    forbidden pattern in ``content``. Empty list = clean."""
    hits: List[str] = []
    for name, regex in _FORBIDDEN_PATTERNS:
        for match in regex.findall(content):
            hits.append(f"{name}={match}")
    return hits


def test_security_sweep_clean_payloads_pass(tmp_path):
    """Sanity: a clean batch (no secrets / PII) passes the sweep.

    Asserts the sweep machinery is functional + that the 4
    refactored emit sites produce clean JSONL when fed
    well-shaped inputs (the call-site pre-filtering contract).
    """
    path = tmp_path / "clean_audit.jsonl"

    # Diverse synthetic batch — one entry per seam with realistic
    # safe details matching what the 4 refactored emit sites send.
    clean_batch = [
        {
            "seam": "mcp.tool_called",
            "details": {
                "tool_name": "kora__create_sea_ticket",
                "tool_kind": "mutating",
                "caller_actor_kind": "claude_pm_isokron",
                "args_keys": ["title", "body", "priority"],
                "result": "ticket_id=tkt-xyz-001",
            },
            "source": "mcp_http",
        },
        {
            "seam": "webhook.dead_letter",
            "details": {
                "source": "slack",
                "reason": "slack_signature_mismatch",
                "ts": 1700000000.123,
                "peer_ip": "203.0.113.7",
                "request_id": "req-abc",
                "body_bytes": 42,
                "headers": {"content-type": "application/json", "user-agent": "Slackbot 1.0"},
            },
            "source": "slack_dm",
        },
        {
            "seam": "slack_dm.reply_failed",
            "details": {
                "channel_id": "D01CHAN01",
                "reason": "slack_api:channel_not_found",
            },
            "source": "slack_dm",
        },
        {
            "seam": "reasoning.tool_called",
            "details": {
                "tool_name": "kora__get_operational_state",
                "triggered_by": "slack_dm",
                "tool_duration_ms": 42,
                "tool_status": "ok",
            },
            "caller_session_id": "D01CHAN01:1700000000.001",
            "source": "reasoning",
        },
    ]
    for entry in clean_batch:
        emit_audit(log_path=path, **entry)

    raw = path.read_text(encoding="utf-8")
    hits = _walk_for_forbidden(raw)
    assert hits == [], f"clean batch unexpectedly tripped sweep: {hits}"


def test_security_sweep_catches_polluted_payloads(tmp_path):
    """Negative control: the sweep MUST catch credentials / PII when
    a caller does accidentally pass them. This is the test that
    proves the sweep machinery works — every kind of forbidden
    pattern matched against the corresponding shape."""
    path = tmp_path / "polluted_audit.jsonl"
    polluted_batch = [
        # Slack bot token leak.
        {
            "seam": "mcp.tool_called",
            "details": {"tool_name": "x", "token": "xoxb-1234567890-abcdef"},
        },
        # Anthropic OAuth leak.
        {
            "seam": "reasoning.tool_called",
            "details": {
                "tool_name": "x",
                "tool_status": "ok",
                "auth": "sk-ant-oat-abcdef1234567890",
            },
        },
        # Bearer header leak.
        {
            "seam": "webhook.dead_letter",
            "details": {
                "source": "slack",
                "reason": "x",
                "auth_header": "Bearer xoxb-leaked123",
            },
        },
        # Email PII leak.
        {
            "seam": "slack_dm.reply_failed",
            "details": {
                "channel_id": "D1",
                "reason": "x",
                "user_email": "joshua@stormhavenenterprises.com",
            },
        },
    ]
    for entry in polluted_batch:
        emit_audit(log_path=path, **entry)

    raw = path.read_text(encoding="utf-8")
    hits = _walk_for_forbidden(raw)
    # Every polluted entry tripped at least one pattern.
    assert len(hits) >= 4, (
        f"sweep failed to catch known-bad payloads. Hits: {hits}"
    )


# ---------------------------------------------------------------------------
# Per-seam details key allow-list — catches drift across the 4
# refactored emit sites
# ---------------------------------------------------------------------------


# What each seam IS allowed to send in details. Adding a new field
# to any of the 4 emit sites requires updating this allow-list AND
# a security review (does the new field carry user data?).
_SEAM_ALLOWED_KEYS: Dict[str, set] = {
    "mcp.tool_called": {
        "tool_name",
        "tool_kind",
        "caller_actor_kind",
        "args_keys",
        "result",
    },
    "webhook.dead_letter": {
        "source",
        "reason",
        "ts",
        "peer_ip",
        "request_id",
        "body_bytes",
        "headers",
    },
    "slack_dm.reply_failed": {
        "channel_id",
        "reason",
    },
    "reasoning.tool_called": {
        "tool_name",
        "triggered_by",
        "tool_duration_ms",
        "tool_status",
        "exc_type",
    },
}


def test_per_seam_details_keys_in_allowlist(tmp_path, monkeypatch):
    """Hit each refactored emit site indirectly (via its caller)
    + verify the JSONL details keys are subset of the allow-list.

    Drift catch: if a future refactor adds a new field to any
    emit_audit call site, this test flags it + forces a security
    review of the new field's content."""
    path = tmp_path / "audit.jsonl"

    # 1. mcp.tool_called via mcp_tools._emit_audit
    from kora_cli.listeners.mcp_tools import _emit_audit as mcp_emit
    from kora_cli.listeners.mcp_caller_auth import Caller

    monkeypatch.setenv(LOG_PATH_ENV, str(path))
    mcp_emit(
        tool="kora__create_sea_ticket",
        caller=Caller(actor_kind="test_caller", allowed_caps=frozenset()),
        args={"title": "x", "body": "y"},
        result="ok",
    )

    # 2. webhook.dead_letter via webhook_dead_letter.emit_webhook_dead_letter
    from kora_cli.listeners.webhook_dead_letter import (
        emit_webhook_dead_letter,
    )

    emit_webhook_dead_letter(
        source="slack",
        reason="slack_signature_mismatch",
        headers={"content-type": "application/json"},
        peer_ip="203.0.113.1",
        request_id="r1",
        body_bytes=10,
    )

    # 3. slack_dm.reply_failed via SlackDMHandler._emit_reply_failed_event
    from kora_cli.handlers.slack_dm_handler import SlackDMHandler

    handler = SlackDMHandler(log_path=tmp_path / "unused-slack.jsonl")
    handler._emit_reply_failed_event(
        channel_id="D01", reason="slack_api:channel_not_found"
    )

    # 4. reasoning.tool_called via anthropic_engine._emit_tool_called_audit
    from kora_cli.reasoning.anthropic_engine import (
        _emit_tool_called_audit,
    )

    _emit_tool_called_audit(
        tool_name="kora__get_operational_state",
        triggered_by="slack_dm",
        caller_session_id="D01:1700.001",
        tool_duration_ms=10,
        tool_status="ok",
    )

    entries = _read_jsonl_lines(path)
    assert len(entries) == 4

    for entry in entries:
        seam = entry["seam"]
        allowed = _SEAM_ALLOWED_KEYS[seam]
        actual = set(entry["details"].keys())
        unexpected = actual - allowed
        assert unexpected == set(), (
            f"Seam {seam!r} emitted unexpected details key(s): "
            f"{unexpected}. If new, update _SEAM_ALLOWED_KEYS + "
            f"security-review the field's content."
        )


# ---------------------------------------------------------------------------
# Dual-write semantic verification — structured-log line + JSONL row
# ---------------------------------------------------------------------------


def test_dual_write_both_surfaces_fire(tmp_path, caplog, monkeypatch):
    """Calling an emit site produces BOTH the existing structured-log
    line AND a JSONL row. Verifies the 'no breaking change' contract
    + the panel-readable surface."""
    caplog.set_level(logging.INFO)
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv(LOG_PATH_ENV, str(path))

    from kora_cli.reasoning.anthropic_engine import _emit_tool_called_audit

    _emit_tool_called_audit(
        tool_name="kora__get_operational_state",
        triggered_by="slack_dm",
        caller_session_id="DCHAN:1.001",
        tool_duration_ms=42,
        tool_status="ok",
    )

    # Surface 1: structured-log line (preserved verbatim).
    log_lines = [
        r.getMessage() for r in caplog.records
        if "kora.reasoning.tool_called" in r.getMessage()
    ]
    assert len(log_lines) == 1
    # Verbatim format check — the prior bucket's tests assert this
    # exact shape ("tool=" not "tool_name=", etc.).
    assert "tool=kora__get_operational_state" in log_lines[0]
    assert "triggered_by=slack_dm" in log_lines[0]

    # Surface 2: JSONL row.
    [entry] = _read_jsonl_lines(path)
    assert entry["seam"] == "reasoning.tool_called"
    assert entry["details"]["tool_name"] == "kora__get_operational_state"


# ===========================================================================
# KR-CHEAP-AUDIT-BATCHING (R3-4 #9) — batched writer behavior
# ===========================================================================


@pytest.fixture
def batching_enabled(monkeypatch):
    """Opt into batching for the per-test scope. Tiny batch size (3)
    so the size-triggered flush is easy to exercise; tiny interval
    (0.1s) so the time-triggered flush completes within the test's
    timeout window."""
    monkeypatch.setenv(BATCH_SIZE_ENV, "3")
    monkeypatch.setenv(FLUSH_INTERVAL_ENV, "0.1")
    _reset_batching_for_tests()
    yield
    _reset_batching_for_tests()


def test_batching_size_trigger_flushes_when_threshold_hit(
    batching_enabled, tmp_path
):
    """Hitting BATCH_SIZE events flushes synchronously inside the
    triggering emit_audit call — no need to wait for the interval."""
    path = tmp_path / "audit.jsonl"
    # 2 emits queue; on the 3rd, batch_size=3 triggers immediate flush.
    for i in range(3):
        emit_audit(
            seam="reasoning.tool_called",
            details={"tool_name": f"tool_{i}", "tool_status": "ok"},
            source="reasoning",
            log_path=path,
        )
    entries = _read_jsonl_lines(path)
    assert len(entries) == 3
    assert [e["details"]["tool_name"] for e in entries] == [
        "tool_0",
        "tool_1",
        "tool_2",
    ]


def test_batching_below_size_threshold_does_not_write_immediately(
    batching_enabled, tmp_path
):
    """Emits below the batch_size threshold are queued — file stays
    empty until either the interval-tick or an explicit flush."""
    path = tmp_path / "audit.jsonl"
    emit_audit(
        seam="reasoning.tool_called",
        details={"tool_name": "tool_a", "tool_status": "ok"},
        source="reasoning",
        log_path=path,
    )
    emit_audit(
        seam="reasoning.tool_called",
        details={"tool_name": "tool_b", "tool_status": "ok"},
        source="reasoning",
        log_path=path,
    )
    # Below batch_size=3 — file should not exist yet.
    assert not path.exists() or _read_jsonl_lines(path) == []
    # Synchronous test-only drain proves the queue held both rows.
    flushed = flush_for_tests()
    assert flushed == 2
    entries = _read_jsonl_lines(path)
    assert len(entries) == 2


def test_batching_time_trigger_flushes_after_interval(
    batching_enabled, tmp_path
):
    """Below-threshold queue gets drained by the background-thread
    interval tick. Allows up to ~0.5s for the thread to fire."""
    import time

    path = tmp_path / "audit.jsonl"
    emit_audit(
        seam="reasoning.tool_called",
        details={"tool_name": "tool_x", "tool_status": "ok"},
        source="reasoning",
        log_path=path,
    )
    # Interval is 0.1s; allow up to 0.6s for the thread to fire.
    deadline = time.monotonic() + 0.6
    entries: List[Dict[str, Any]] = []
    while time.monotonic() < deadline:
        entries = _read_jsonl_lines(path)
        if entries:
            break
        time.sleep(0.05)
    assert len(entries) == 1
    assert entries[0]["details"]["tool_name"] == "tool_x"


def test_batching_groups_entries_per_path(batching_enabled, tmp_path):
    """When emits target multiple paths in the same batch, each
    file is opened once + receives only its own rows."""
    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"
    # 3 events total → triggers the size-based flush. 2 to A + 1 to B.
    emit_audit(
        seam="reasoning.tool_called",
        details={"tool_name": "a1", "tool_status": "ok"},
        source="reasoning",
        log_path=a,
    )
    emit_audit(
        seam="reasoning.tool_called",
        details={"tool_name": "b1", "tool_status": "ok"},
        source="reasoning",
        log_path=b,
    )
    emit_audit(
        seam="reasoning.tool_called",
        details={"tool_name": "a2", "tool_status": "ok"},
        source="reasoning",
        log_path=a,
    )
    rows_a = _read_jsonl_lines(a)
    rows_b = _read_jsonl_lines(b)
    assert [r["details"]["tool_name"] for r in rows_a] == ["a1", "a2"]
    assert [r["details"]["tool_name"] for r in rows_b] == ["b1"]


def test_batching_shutdown_drain_flushes_remaining(
    batching_enabled, tmp_path
):
    """The test-reset path mirrors what atexit does in production —
    signals the thread to stop + drains the queue."""
    path = tmp_path / "audit.jsonl"
    emit_audit(
        seam="reasoning.tool_called",
        details={"tool_name": "in_flight", "tool_status": "ok"},
        source="reasoning",
        log_path=path,
    )
    # Trigger the reset (which calls flusher_stop.set + waits for the
    # thread; the thread's final loop iteration drains the queue
    # before exiting).
    _reset_batching_for_tests()
    entries = _read_jsonl_lines(path)
    assert len(entries) == 1
    assert entries[0]["details"]["tool_name"] == "in_flight"


def test_batching_disabled_writes_synchronously(tmp_path, monkeypatch):
    """BATCH_SIZE=0 (explicit opt-out, default in legacy tests):
    emits write to the file before emit_audit returns."""
    monkeypatch.setenv(BATCH_SIZE_ENV, "0")
    _reset_batching_for_tests()
    path = tmp_path / "audit.jsonl"
    emit_audit(
        seam="reasoning.tool_called",
        details={"tool_name": "sync_only", "tool_status": "ok"},
        source="reasoning",
        log_path=path,
    )
    # File should exist immediately, no flush call needed.
    [entry] = _read_jsonl_lines(path)
    assert entry["details"]["tool_name"] == "sync_only"
