"""KR-FE-OUTBOUND-EMAIL-LOG-PANEL — backend endpoint + FE source-pin tests.

Symmetric to test_email_intent_panel.py (PR #180). This bucket
surfaces the tool.email_to_operator_sent audit seam (PR #179)
so operator sees what Kora composed and sent via the
kora__send_email_to_operator reasoning-loop tool.

Tests:

  Backend:
    1. Endpoint registered
    2. Empty audit log → calm zero response
    3. by_status_24h dict initialized with all 3 known statuses + unknown at 0
    4. sent action → projected with smtp_message_id + sent_at
    5. rejected action → projected with rejection_reason + truncated rejection_detail
    6. smtp_failure action → projected with error + smtp_status
    7. Unknown status coerced to "unknown" defensively
    8. Daily-sent 14d bucket math correct (window: [today-13, today])
    9. 24h window cutoff drops older events from counts but keeps in events list
   10. PRIVACY: response never contains subject string or body content
       even if a future writer puts them in details (subject MUST stay
       as subject_chars; body MUST stay as body_chars)
   11. SECURITY: arbitrary fields in details don't leak through projection
   12. rejection_detail truncated to ≤200 chars
   13. error (smtp_failure) truncated to ≤200 chars

  Drift guard (marquee — symmetric to PR #180):
   14. STATUS values match across THREE sources:
       a. BE projection allow-list: _OUTBOUND_EMAIL_STATUS_VALUES
          in kora_cli/web_server.py
       b. BE emitter: STATUS_SENT / STATUS_REJECTED /
          STATUS_SMTP_FAILURE constants in kora_cli/tools/email_to_operator.py
       c. FE constant: OUTBOUND_EMAIL_STATUS_VALUES in
          web/src/lib/api.ts
   15. SeamName Literal includes tool.email_to_operator_sent

  FE source-pins:
   16. api.getOutboundEmailRecent wrapper exists
   17. OutboundEmailEvent + Response TS types declared
   18. OUTBOUND_EMAIL_STATUS_VALUES TS constant exported
   19. OutboundEmailLogPage.tsx exists + uses usePanelView
   20. Route + nav entry registered
   21. Filter chips iterate canonical status values
   22. Sparkline uses plain SVG (no chart-library dep)
   23. PRIVACY: page does NOT reference "subject" as a string field
       (only subject_chars); does NOT reference "body" as a content
       field (only body_chars). Regression guard against someone
       later trying to render hypothetical subject/body fields.
   24. Empty state copy committed (regression guard)
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import pytest

from tests.kora_cli._panel_test_helpers import isolated_kora_home


_REPO_ROOT = Path(__file__).resolve().parents[2]
_API_TS = _REPO_ROOT / "web" / "src" / "lib" / "api.ts"
_APP_TSX = _REPO_ROOT / "web" / "src" / "App.tsx"
_PAGE = _REPO_ROOT / "web" / "src" / "pages" / "OutboundEmailLogPage.tsx"
_WEB_SERVER = _REPO_ROOT / "kora_cli" / "web_server.py"
_TOOL_PY = _REPO_ROOT / "kora_cli" / "tools" / "email_to_operator.py"


@pytest.fixture
def env(tmp_path, monkeypatch):
    return isolated_kora_home(tmp_path, monkeypatch)


def _write_audit_jsonl(env_dir: Path, entries: list) -> None:
    log_path = env_dir / "kora_audit_log.jsonl"
    with log_path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, default=str) + "\n")


def _outbound_entry(
    status: str,
    *,
    emitted_at: datetime,
    subject_chars: int = 42,
    body_chars: int = 256,
    attachment_count: int = 0,
    attachment_total_bytes: int = 0,
    smtp_message_id: str | None = None,
    sent_at: str | None = None,
    rejection_reason: str | None = None,
    rejection_detail: dict | None = None,
    error: str | None = None,
    smtp_status: str | None = None,
    extra: dict | None = None,
    caller_session_id: str = "engine-session-123",
) -> dict:
    details: Dict[str, Any] = {
        "status": status,
        "subject_chars": subject_chars,
        "body_chars": body_chars,
        "attachment_count": attachment_count,
        "attachment_total_bytes": attachment_total_bytes,
    }
    if smtp_message_id is not None:
        details["smtp_message_id"] = smtp_message_id
    if sent_at is not None:
        details["sent_at"] = sent_at
    if rejection_reason is not None:
        details["rejection_reason"] = rejection_reason
    if rejection_detail is not None:
        details["rejection_detail"] = rejection_detail
    if error is not None:
        details["error"] = error
    if smtp_status is not None:
        details["smtp_status"] = smtp_status
    if extra is not None:
        details.update(extra)
    return {
        "emitted_at": emitted_at.isoformat(),
        "seam": "tool.email_to_operator_sent",
        "details": details,
        "source": "reasoning",
        "caller_session_id": caller_session_id,
    }


async def _call_endpoint(env_dir: Path, limit: int = 100) -> dict:
    from kora_cli import web_server

    return await web_server.list_recent_outbound_email(limit=limit)


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


def test_endpoint_registered():
    from kora_cli import web_server

    paths = {getattr(r, "path", None) for r in web_server.app.routes}
    assert "/api/outbound-email/recent" in paths


@pytest.mark.asyncio
async def test_empty_audit_returns_calm_zero_response(env):
    body = await _call_endpoint(env)
    assert body["events"] == []
    assert body["total_recent_24h"] == 0
    for status in ("sent", "rejected", "smtp_failure"):
        assert body["by_status_24h"][status] == 0


@pytest.mark.asyncio
async def test_by_status_24h_initialized_for_all_known_statuses(env):
    body = await _call_endpoint(env)
    for status in ("sent", "rejected", "smtp_failure"):
        assert status in body["by_status_24h"]


@pytest.mark.asyncio
async def test_sent_projected_with_smtp_id_and_sent_at(env):
    now = datetime.now(timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _outbound_entry(
                "sent",
                emitted_at=now - timedelta(minutes=10),
                subject_chars=80,
                body_chars=1800,
                attachment_count=1,
                attachment_total_bytes=184320,
                smtp_message_id="<20260523221409.kora@stormhavenenterprises.com>",
                sent_at="2026-05-23T22:14:09Z",
            )
        ],
    )
    e = (await _call_endpoint(env))["events"][0]
    assert e["status"] == "sent"
    assert (
        e["smtp_message_id"]
        == "<20260523221409.kora@stormhavenenterprises.com>"
    )
    assert e["sent_at"] == "2026-05-23T22:14:09Z"
    assert e["subject_chars"] == 80
    assert e["body_chars"] == 1800
    assert e["attachment_count"] == 1
    assert e["attachment_total_bytes"] == 184320


@pytest.mark.asyncio
async def test_rejected_projected_with_reason_and_truncated_detail(env):
    now = datetime.now(timezone.utc)
    big_detail = {"hourly_cap": 10, "x" * 50: "y" * 1000}
    _write_audit_jsonl(
        env,
        [
            _outbound_entry(
                "rejected",
                emitted_at=now - timedelta(minutes=3),
                rejection_reason="hourly_cap_exceeded",
                rejection_detail=big_detail,
            )
        ],
    )
    e = (await _call_endpoint(env))["events"][0]
    assert e["status"] == "rejected"
    assert e["rejection_reason"] == "hourly_cap_exceeded"
    # rejection_detail truncated to <= 200 chars
    assert len(e["rejection_detail"]) <= 200, (
        "rejection_detail must be truncated defensively — bounded "
        "against a runaway detail dict leaking diagnostic state"
    )


@pytest.mark.asyncio
async def test_smtp_failure_projected_with_error(env):
    now = datetime.now(timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _outbound_entry(
                "smtp_failure",
                emitted_at=now - timedelta(minutes=1),
                error="ConnectionRefusedError",
                smtp_status="failed",
            )
        ],
    )
    e = (await _call_endpoint(env))["events"][0]
    assert e["status"] == "smtp_failure"
    assert e["error"] == "ConnectionRefusedError"
    assert e["smtp_status"] == "failed"


@pytest.mark.asyncio
async def test_unknown_status_coerced(env):
    """Defensive: if a future writer adds a new status without
    updating the projection, coerce to "unknown" so the FE
    renders a known shape rather than propagating arbitrary
    bytes."""
    now = datetime.now(timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _outbound_entry(
                "future_status_we_dont_know",
                emitted_at=now - timedelta(minutes=1),
            )
        ],
    )
    e = (await _call_endpoint(env))["events"][0]
    assert e["status"] == "unknown"


@pytest.mark.asyncio
async def test_daily_sent_14d_bucket_math(env):
    """14 buckets [today-13, today]; only sent action counts;
    events older than 14d ignored."""
    now = datetime.now(timezone.utc)
    entries = []
    # 3 sent today
    for i in range(3):
        entries.append(
            _outbound_entry(
                "sent",
                emitted_at=now - timedelta(hours=i),
                smtp_message_id=f"<msg-{i}>",
            )
        )
    # 1 rejected today (must NOT count toward sparkline)
    entries.append(
        _outbound_entry(
            "rejected",
            emitted_at=now - timedelta(hours=1),
            rejection_reason="hourly_cap_exceeded",
        )
    )
    # 1 sent 5 days ago
    entries.append(
        _outbound_entry(
            "sent",
            emitted_at=now - timedelta(days=5),
            smtp_message_id="<old>",
        )
    )
    # 1 sent 30 days ago (out of window)
    entries.append(
        _outbound_entry(
            "sent",
            emitted_at=now - timedelta(days=30),
            smtp_message_id="<ancient>",
        )
    )
    _write_audit_jsonl(env, entries)
    body = await _call_endpoint(env)
    sl = body["daily_sent_14d"]
    assert len(sl) == 14
    assert [b["date"] for b in sl] == sorted(b["date"] for b in sl)
    assert sum(b["count"] for b in sl) == 4  # 3 today + 1 five-days-ago


@pytest.mark.asyncio
async def test_24h_window_drops_older_from_counts(env):
    now = datetime.now(timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _outbound_entry(
                "sent",
                emitted_at=now - timedelta(hours=1),
                smtp_message_id="<recent>",
            ),
            _outbound_entry(
                "sent",
                emitted_at=now - timedelta(days=2),
                smtp_message_id="<old>",
            ),
        ],
    )
    body = await _call_endpoint(env)
    assert len(body["events"]) == 2
    assert body["total_recent_24h"] == 1
    assert body["by_status_24h"]["sent"] == 1


@pytest.mark.asyncio
async def test_privacy_response_never_contains_subject_or_body_text(env):
    """PRIVACY contract (PR #179): subject + body text are NEVER
    in the audit row — only sizes. Even if a future writer
    accidentally puts the subject string in details, this
    projection must NOT propagate it. The projection's per-
    branch field whitelist enforces this — test injects hostile
    subject + body fields and asserts they don't appear.
    """
    now = datetime.now(timezone.utc)
    secret_subject = "OPERATOR_PII_subject_should_not_leak"
    secret_body = "PRIVATE_BODY_CONTENT_should_not_leak"
    _write_audit_jsonl(
        env,
        [
            _outbound_entry(
                "sent",
                emitted_at=now - timedelta(minutes=1),
                subject_chars=42,
                body_chars=256,
                smtp_message_id="<msg-x>",
                extra={
                    # Future-writer leak vectors. The projection
                    # whitelist must drop these.
                    "subject": secret_subject,
                    "body": secret_body,
                    "body_text": secret_body,
                    "to": "[email protected]",
                    "from": "[email protected]",
                },
            )
        ],
    )
    body = await _call_endpoint(env)
    serialized = json.dumps(body)
    assert secret_subject not in serialized, (
        "PRIVACY REGRESSION: subject string leaked through projection"
    )
    assert secret_body not in serialized, (
        "PRIVACY REGRESSION: body content leaked through projection"
    )
    assert "[email protected]" not in serialized
    assert "[email protected]" not in serialized
    # Size fields still present (these are safe).
    assert serialized.count('"subject_chars": 42') >= 1
    assert serialized.count('"body_chars": 256') >= 1


@pytest.mark.asyncio
async def test_response_does_not_leak_arbitrary_audit_fields(env):
    now = datetime.now(timezone.utc)
    leak_str = "INTERNAL_TOKEN_xoxb-leak-12345"
    _write_audit_jsonl(
        env,
        [
            _outbound_entry(
                "sent",
                emitted_at=now - timedelta(minutes=1),
                smtp_message_id="<m>",
                extra={
                    "operator_token": leak_str,
                    "smtp_headers": {"x-secret": leak_str},
                    "raw_payload": leak_str,
                },
            )
        ],
    )
    body = await _call_endpoint(env)
    serialized = json.dumps(body)
    assert leak_str not in serialized
    assert "operator_token" not in serialized
    assert "smtp_headers" not in serialized


@pytest.mark.asyncio
async def test_error_truncated_to_bounded_length(env):
    now = datetime.now(timezone.utc)
    long_err = "X" * 1000
    _write_audit_jsonl(
        env,
        [
            _outbound_entry(
                "smtp_failure",
                emitted_at=now - timedelta(minutes=1),
                error=long_err,
            )
        ],
    )
    e = (await _call_endpoint(env))["events"][0]
    assert len(e["error"]) == 200


# ---------------------------------------------------------------------------
# Drift guard (marquee — symmetric to PR #180's action_values guard)
# ---------------------------------------------------------------------------


def test_status_values_drift_guard():
    """The 3 status values must match across THREE sources:

      1. BE projection allow-list: _OUTBOUND_EMAIL_STATUS_VALUES
         in kora_cli/web_server.py
      2. BE emitter: STATUS_SENT / STATUS_REJECTED /
         STATUS_SMTP_FAILURE constants in
         kora_cli/tools/email_to_operator.py
      3. FE constant: OUTBOUND_EMAIL_STATUS_VALUES in
         web/src/lib/api.ts

    Drift in any one breaks the panel. Test fails CI on mismatch.
    """
    expected = {"sent", "rejected", "smtp_failure"}

    # 1. BE projection allow-list
    ws_src = _WEB_SERVER.read_text()
    m = re.search(
        r"_OUTBOUND_EMAIL_STATUS_VALUES\s*=\s*\(([^)]+)\)",
        ws_src,
    )
    assert m is not None, (
        "BE constant _OUTBOUND_EMAIL_STATUS_VALUES not found in "
        f"{_WEB_SERVER}"
    )
    be_values = set(re.findall(r'"(\w+)"', m.group(1)))
    assert be_values == expected, (
        f"BE projection allow-list drift: expected {expected}, "
        f"got {be_values}"
    )

    # 2. BE emitter STATUS_* constants
    tool_src = _TOOL_PY.read_text()
    for const_name, expected_val in [
        ("STATUS_SENT", "sent"),
        ("STATUS_REJECTED", "rejected"),
        ("STATUS_SMTP_FAILURE", "smtp_failure"),
    ]:
        match = re.search(
            rf'{const_name}\s*=\s*"({expected_val})"',
            tool_src,
        )
        assert match is not None, (
            f"BE emitter constant {const_name} = \"{expected_val}\" "
            f"not found in {_TOOL_PY} — drift from BE projection"
        )

    # 3. FE constant
    fe_src = _API_TS.read_text()
    m = re.search(
        r"OUTBOUND_EMAIL_STATUS_VALUES[^=]*=\s*\[([^\]]+)\]",
        fe_src,
    )
    assert m is not None, (
        "FE constant OUTBOUND_EMAIL_STATUS_VALUES not found in "
        f"{_API_TS}"
    )
    fe_values = set(re.findall(r'"(\w+)"', m.group(1)))
    assert fe_values == expected, (
        f"FE constant drift: expected {expected}, got {fe_values}"
    )


def test_seam_literal_includes_tool_email_to_operator_sent():
    sink_src = (_REPO_ROOT / "kora_cli" / "audit" / "jsonl_sink.py").read_text()
    assert '"tool.email_to_operator_sent"' in sink_src


# ---------------------------------------------------------------------------
# FE source-pins
# ---------------------------------------------------------------------------


def test_api_wrapper_exists():
    src = _API_TS.read_text()
    assert "getOutboundEmailRecent" in src
    assert "/api/outbound-email/recent" in src


def test_response_types_declared():
    src = _API_TS.read_text()
    for ts_type in (
        "OutboundEmailEvent",
        "OutboundEmailEventsResponse",
        "OutboundEmailStatus",
        "OutboundEmailDailyCount",
    ):
        assert ts_type in src, f"missing TS type: {ts_type}"


def test_fe_status_values_constant_exported():
    src = _API_TS.read_text()
    assert "export const OUTBOUND_EMAIL_STATUS_VALUES" in src


def test_page_exists_and_uses_panel_view():
    assert _PAGE.is_file()
    src = _PAGE.read_text()
    assert 'usePanelView("OutboundEmailLogPage")' in src


def test_route_and_nav_registered():
    src = _APP_TSX.read_text()
    assert "/outbound-email-log" in src
    assert "OutboundEmailLogPage" in src
    assert re.search(
        r'path:\s*"/outbound-email-log"[^}]+labelKey:\s*"outboundEmailLog"',
        src,
        re.DOTALL,
    ), "nav entry for /outbound-email-log missing"


def test_filter_chips_iterate_canonical_status_values():
    src = _PAGE.read_text()
    assert "OUTBOUND_EMAIL_STATUS_VALUES.map" in src


def test_sparkline_uses_plain_svg():
    src = _PAGE.read_text()
    assert "<svg" in src
    assert "<rect" in src
    for lib in ("recharts", "chart.js", "d3", "@nivo", "victory"):
        assert lib not in src, (
            f"chart library '{lib}' import detected — Sparkline must "
            f"stay in plain SVG per the CC#2 discipline"
        )


def test_page_does_not_render_subject_or_body_text():
    """PRIVACY regression guard: the FE page must NEVER reference
    event.subject (the string) or event.body / event.body_text
    fields. Those don't exist in OutboundEmailEvent — only
    subject_chars + body_chars (numeric sizes). A grep for the
    field-access pattern ensures no future patch silently adds
    text-content rendering."""
    src = _PAGE.read_text()
    # Allowed accesses: event.subject_chars, event.body_chars,
    # event.attachment_*. Forbidden: bare event.subject /
    # event.body / event.body_text without _chars suffix.
    forbidden_patterns = [
        r"event\.subject\b(?!_chars)",
        r"event\.body\b(?!_chars)",
        r"event\.body_text\b",
        r"event\.to\b",
        r"event\.from\b",
        r"event\.recipient\b",
    ]
    for pat in forbidden_patterns:
        matches = re.findall(pat, src)
        assert not matches, (
            f"PRIVACY REGRESSION: page references forbidden field "
            f"matching {pat!r} — text content / recipient must NEVER "
            f"be rendered (PR #179 privacy contract)"
        )


def test_empty_state_copy_committed():
    src = _PAGE.read_text()
    assert "No outbound email events" in src
    assert "events match this filter" in src
