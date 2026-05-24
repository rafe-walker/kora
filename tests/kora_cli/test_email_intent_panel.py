"""KR-FE-EMAIL-INTENT-LOG-PANEL — backend endpoint + FE source-pin tests.

The PM-supplied spec assumed a generic /api/audit-events?seam=X
endpoint exists. K-DG showed it doesn't — the established
discipline (PR #155 KR-AUDIT-PANEL-ENDPOINTS) is one endpoint per
audit seam. This bucket adds the minimal per-seam endpoint
following that pattern (anticipated by spec §4 STOP-ASK clause).

Tests:

  Backend:
    1. Endpoint registered + routes match
    2. Empty audit log → calm zero response
    3. by_action_24h dict initialized with all 5 known actions at 0
    4. created action with ticket_id → projected with ticket_id
    5. logged_only action → projected with reason
    6. dry_run action → projected with proposed_title
    7. cap_exceeded action → projected with hourly_cap
    8. failed action → projected with error (truncated at 200)
    9. Unknown action value coerced to "unknown" (defensive)
   10. Daily-created 14d bucket math correct
   11. Window filter: 24h cutoff drops older events from
       by_action_24h while preserving them in events list
   12. SECURITY: response doesn't echo arbitrary fields from
       audit details (only the projected per-branch fields)
   13. SECURITY: subject + error truncated to bounded length

  Drift guard (the bucket's marquee pin):
   14. ACTION_VALUES drift between FE constant + BE
       _EMAIL_INTENT_ACTION_VALUES + the emit_audit call sites
       at kora_cli/intent/email_to_sea_ticket.py

  FE source-pins:
   15. api.getEmailIntentEventsRecent wrapper exists
   16. EmailIntentEvent + Response TS types declared
   17. EMAIL_INTENT_ACTION_VALUES TS constant exported
   18. EmailIntentLogPage.tsx exists + uses usePanelView
   19. Route registered + nav entry present
   20. Filter chips render all 5 action values
   21. Sea_Ticket deep-link uses /sea-tickets?focus=<ticket_id>
   22. Sparkline uses plain SVG (no chart-library dep)
   23. Empty state copy committed (regression guard)
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
_PAGE = _REPO_ROOT / "web" / "src" / "pages" / "EmailIntentLogPage.tsx"
_WEB_SERVER = _REPO_ROOT / "kora_cli" / "web_server.py"
_INTENT_PY = _REPO_ROOT / "kora_cli" / "intent" / "email_to_sea_ticket.py"


@pytest.fixture
def env(tmp_path, monkeypatch):
    return isolated_kora_home(tmp_path, monkeypatch)


def _write_audit_jsonl(env_dir: Path, entries: list) -> None:
    log_path = env_dir / "kora_audit_log.jsonl"
    with log_path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, default=str) + "\n")


def _intent_entry(
    action: str,
    *,
    emitted_at: datetime,
    pattern: str = "subject_idea_prefix",
    confidence: str = "high",
    subject: str = "Idea: try this",
    ticket_id: str | None = None,
    tags: list | None = None,
    reason: str | None = None,
    proposed_title: str | None = None,
    hourly_cap: int | None = None,
    error: str | None = None,
    caller_session_id: str = "email:msg-id-123",
) -> dict:
    details: Dict[str, Any] = {
        "action": action,
        "pattern_matched": pattern,
        "confidence": confidence,
        "subject": subject,
    }
    if ticket_id is not None:
        details["ticket_id"] = ticket_id
    if tags is not None:
        details["tags"] = tags
    if reason is not None:
        details["reason"] = reason
    if proposed_title is not None:
        details["proposed_title"] = proposed_title
    if hourly_cap is not None:
        details["hourly_cap"] = hourly_cap
    if error is not None:
        details["error"] = error
    return {
        "emitted_at": emitted_at.isoformat(),
        "seam": "intent.email_to_sea_ticket",
        "details": details,
        "source": "email",
        "caller_session_id": caller_session_id,
    }


async def _call_endpoint(env_dir: Path, limit: int = 100) -> dict:
    from kora_cli import web_server

    return await web_server.list_recent_email_intent(limit=limit)


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


def test_endpoint_registered():
    from kora_cli import web_server

    paths = {getattr(r, "path", None) for r in web_server.app.routes}
    assert "/api/email-intent/recent" in paths


@pytest.mark.asyncio
async def test_empty_audit_returns_calm_zero_response(env):
    body = await _call_endpoint(env)
    assert body["events"] == []
    assert body["total_recent_24h"] == 0
    assert body["by_action_24h"]["created"] == 0
    assert body["by_action_24h"]["logged_only"] == 0
    assert body["by_action_24h"]["failed"] == 0
    assert body["by_action_24h"]["dry_run"] == 0
    assert body["by_action_24h"]["cap_exceeded"] == 0


@pytest.mark.asyncio
async def test_by_action_24h_initialized_for_all_known_actions(env):
    """Even when zero events of an action exist, the dict must
    carry that key at 0 so the FE summary chips don't have to
    branch on key-presence — operator gets a stable shape."""
    body = await _call_endpoint(env)
    for action in (
        "created",
        "logged_only",
        "dry_run",
        "cap_exceeded",
        "failed",
    ):
        assert action in body["by_action_24h"]


@pytest.mark.asyncio
async def test_created_action_projected_with_ticket_id(env):
    now = datetime.now(timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _intent_entry(
                "created",
                emitted_at=now - timedelta(minutes=10),
                ticket_id="STK-42",
                tags=["idea", "kora-paper"],
            )
        ],
    )
    body = await _call_endpoint(env)
    e = body["events"][0]
    assert e["action"] == "created"
    assert e["ticket_id"] == "STK-42"
    assert e["tags"] == ["idea", "kora-paper"]


@pytest.mark.asyncio
async def test_logged_only_projected_with_reason(env):
    now = datetime.now(timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _intent_entry(
                "logged_only",
                emitted_at=now - timedelta(minutes=5),
                pattern="unrecognized",
                confidence="unrecognized",
                reason="no_pattern_matched",
            )
        ],
    )
    e = (await _call_endpoint(env))["events"][0]
    assert e["action"] == "logged_only"
    assert e["reason"] == "no_pattern_matched"
    # ticket_id MUST NOT be present (writer doesn't emit it on this branch)
    assert "ticket_id" not in e


@pytest.mark.asyncio
async def test_dry_run_projected_with_proposed_title(env):
    now = datetime.now(timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _intent_entry(
                "dry_run",
                emitted_at=now - timedelta(minutes=3),
                proposed_title="Draft Sea_Ticket title (preview)",
            )
        ],
    )
    e = (await _call_endpoint(env))["events"][0]
    assert e["action"] == "dry_run"
    assert e["proposed_title"] == "Draft Sea_Ticket title (preview)"


@pytest.mark.asyncio
async def test_cap_exceeded_projected_with_hourly_cap(env):
    now = datetime.now(timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _intent_entry(
                "cap_exceeded",
                emitted_at=now - timedelta(minutes=2),
                hourly_cap=20,
            )
        ],
    )
    e = (await _call_endpoint(env))["events"][0]
    assert e["action"] == "cap_exceeded"
    assert e["hourly_cap"] == 20


@pytest.mark.asyncio
async def test_failed_projected_with_truncated_error(env):
    now = datetime.now(timezone.utc)
    long_err = "ConnectionError(" + ("x" * 1000) + ")"
    _write_audit_jsonl(
        env,
        [
            _intent_entry(
                "failed",
                emitted_at=now - timedelta(minutes=1),
                error=long_err,
            )
        ],
    )
    e = (await _call_endpoint(env))["events"][0]
    assert e["action"] == "failed"
    assert len(e["error"]) == 200, (
        "error must be truncated to bounded length (200 chars) — "
        "raw repr could leak large stack-traceish content to a "
        "panel consumer"
    )


@pytest.mark.asyncio
async def test_unknown_action_coerced_defensively(env):
    """Defensive: if a future writer adds a new action value
    without us updating the projection allow-list, the
    endpoint must NOT propagate the raw string verbatim
    (could carry attacker-controlled bytes in pathological
    cases). Coerce to 'unknown' so the FE renders a known shape."""
    now = datetime.now(timezone.utc)
    _write_audit_jsonl(
        env,
        [
            {
                "emitted_at": (now - timedelta(minutes=1)).isoformat(),
                "seam": "intent.email_to_sea_ticket",
                "details": {
                    "action": "future_action_we_dont_know_about",
                    "pattern_matched": "x",
                    "confidence": "high",
                    "subject": "test",
                },
                "source": "email",
                "caller_session_id": "email:x",
            }
        ],
    )
    e = (await _call_endpoint(env))["events"][0]
    assert e["action"] == "unknown"


@pytest.mark.asyncio
async def test_daily_created_14d_bucket_math(env):
    """Sparkline expects 14 buckets in chronological order.
    Created counts bucket by emitted_at UTC date; other actions
    don't increment any bucket; events older than 14d are
    ignored."""
    now = datetime.now(timezone.utc)
    entries = []
    # 3 created today
    for i in range(3):
        entries.append(
            _intent_entry(
                "created",
                emitted_at=now - timedelta(hours=i),
                ticket_id=f"STK-{i}",
            )
        )
    # 1 logged_only today (must NOT count toward created sparkline)
    entries.append(
        _intent_entry(
            "logged_only",
            emitted_at=now - timedelta(hours=1),
            reason="no_pattern_matched",
        )
    )
    # 1 created 5 days ago
    entries.append(
        _intent_entry(
            "created",
            emitted_at=now - timedelta(days=5),
            ticket_id="STK-old",
        )
    )
    # 1 created 30 days ago (out of 14d window)
    entries.append(
        _intent_entry(
            "created",
            emitted_at=now - timedelta(days=30),
            ticket_id="STK-ancient",
        )
    )
    _write_audit_jsonl(env, entries)
    body = await _call_endpoint(env)
    sl = body["daily_created_14d"]
    assert len(sl) == 14
    # Chronological order
    dates = [b["date"] for b in sl]
    assert dates == sorted(dates)
    total = sum(b["count"] for b in sl)
    # 3 today + 1 five-days-ago = 4 within the window
    assert total == 4


@pytest.mark.asyncio
async def test_24h_window_drops_older_from_counts(env):
    now = datetime.now(timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _intent_entry(
                "created",
                emitted_at=now - timedelta(hours=1),
                ticket_id="STK-recent",
            ),
            _intent_entry(
                "created",
                emitted_at=now - timedelta(days=2),
                ticket_id="STK-old",
            ),
        ],
    )
    body = await _call_endpoint(env)
    # Both events appear in events list (no window filter on items)
    assert len(body["events"]) == 2
    # But only the recent one counts toward 24h
    assert body["total_recent_24h"] == 1
    assert body["by_action_24h"]["created"] == 1


@pytest.mark.asyncio
async def test_response_does_not_leak_arbitrary_audit_fields(env):
    """If a future writer adds a field to details (e.g. operator
    PII), this projection must NOT propagate it. The endpoint
    whitelists per-action fields rather than echoing details
    verbatim."""
    now = datetime.now(timezone.utc)
    leak_str = "INTERNAL_TOKEN_xoxb-leak-12345"
    _write_audit_jsonl(
        env,
        [
            {
                "emitted_at": (now - timedelta(minutes=1)).isoformat(),
                "seam": "intent.email_to_sea_ticket",
                "details": {
                    "action": "created",
                    "pattern_matched": "x",
                    "confidence": "high",
                    "subject": "test",
                    "ticket_id": "STK-1",
                    # Future-writer leak vector
                    "operator_email_address": "[email protected]",
                    "internal_token": leak_str,
                    "hostile_field": {"deep": leak_str},
                },
                "source": "email",
                "caller_session_id": "email:x",
            }
        ],
    )
    body = await _call_endpoint(env)
    serialized = json.dumps(body)
    assert leak_str not in serialized
    assert "[email protected]" not in serialized
    assert "internal_token" not in serialized
    assert "hostile_field" not in serialized


@pytest.mark.asyncio
async def test_subject_truncated_to_bounded_length(env):
    now = datetime.now(timezone.utc)
    long_subject = "X" * 500
    _write_audit_jsonl(
        env,
        [
            _intent_entry(
                "logged_only",
                emitted_at=now - timedelta(minutes=1),
                subject=long_subject,
                reason="no_pattern_matched",
            )
        ],
    )
    e = (await _call_endpoint(env))["events"][0]
    assert len(e["subject"]) == 200


# ---------------------------------------------------------------------------
# Drift guard (marquee test)
# ---------------------------------------------------------------------------


def test_action_values_drift_guard():
    """The 5 action values must match across THREE sources:

      1. BE projection allow-list: _EMAIL_INTENT_ACTION_VALUES
         in kora_cli/web_server.py
      2. BE emitter: emit_audit "action" string literals at
         the 5 _safe_audit call sites in
         kora_cli/intent/email_to_sea_ticket.py
      3. FE constant: EMAIL_INTENT_ACTION_VALUES in
         web/src/lib/api.ts

    Drift in any one breaks the panel: a writer adding "new_action"
    without updating the projection coerces it to "unknown"
    (defensive — test_unknown_action_coerced_defensively); a
    projection adding a new value without the emitter using it
    just shows a 0 chip; FE drift is the worst case (chips don't
    render for the action). This test pins all 3 against each
    other so any drift fails CI.
    """
    expected = {
        "created",
        "logged_only",
        "dry_run",
        "cap_exceeded",
        "failed",
    }

    # 1. BE projection allow-list
    ws_src = _WEB_SERVER.read_text()
    m = re.search(
        r"_EMAIL_INTENT_ACTION_VALUES\s*=\s*\(([^)]+)\)",
        ws_src,
    )
    assert m is not None, (
        "BE constant _EMAIL_INTENT_ACTION_VALUES not found in "
        f"{_WEB_SERVER}"
    )
    be_values = set(re.findall(r'"(\w+)"', m.group(1)))
    assert be_values == expected, (
        f"BE projection allow-list drift: expected {expected}, "
        f"got {be_values}"
    )

    # 2. BE emitter: grep for "action": "<value>" literals in
    # the _safe_audit call sites
    intent_src = _INTENT_PY.read_text()
    emitter_values = set(re.findall(r'"action":\s*"(\w+)"', intent_src))
    # Emitter source might also reference "no_action" (docstring)
    # or other future actions; we assert the 5 emit_audit literals
    # are present, not strict equality. (no_action exists in the
    # docstring at email_to_sea_ticket.py:602 but isn't emitted.)
    for action in expected:
        assert action in emitter_values, (
            f"BE emitter missing '{action}' literal in "
            f"_safe_audit call sites — drift from BE projection"
        )

    # 3. FE constant
    fe_src = _API_TS.read_text()
    m = re.search(
        r"EMAIL_INTENT_ACTION_VALUES[^=]*=\s*\[([^\]]+)\]",
        fe_src,
    )
    assert m is not None, (
        "FE constant EMAIL_INTENT_ACTION_VALUES not found in "
        f"{_API_TS}"
    )
    fe_values = set(re.findall(r'"(\w+)"', m.group(1)))
    assert fe_values == expected, (
        f"FE constant drift: expected {expected}, got {fe_values}"
    )


def test_seam_literal_includes_intent_email_to_sea_ticket():
    """Sanity: the SeamName Literal must declare the seam the
    endpoint filters on. Without this, emit_audit on the writer
    side silently drops + reader returns []."""
    sink_src = (_REPO_ROOT / "kora_cli" / "audit" / "jsonl_sink.py").read_text()
    assert '"intent.email_to_sea_ticket"' in sink_src


# ---------------------------------------------------------------------------
# FE source-pins
# ---------------------------------------------------------------------------


def test_api_wrapper_exists():
    src = _API_TS.read_text()
    assert "getEmailIntentEventsRecent" in src
    assert "/api/email-intent/recent" in src


def test_response_types_declared():
    src = _API_TS.read_text()
    for ts_type in (
        "EmailIntentEvent",
        "EmailIntentEventsResponse",
        "EmailIntentAction",
        "EmailIntentDailyCount",
    ):
        assert ts_type in src, f"missing TS type: {ts_type}"


def test_fe_action_values_constant_exported():
    src = _API_TS.read_text()
    assert "export const EMAIL_INTENT_ACTION_VALUES" in src


def test_page_exists_and_uses_panel_view():
    assert _PAGE.is_file()
    src = _PAGE.read_text()
    assert 'usePanelView("EmailIntentLogPage")' in src


def test_route_and_nav_registered():
    src = _APP_TSX.read_text()
    assert "/email-intent-log" in src
    assert "EmailIntentLogPage" in src
    # Nav entry
    assert re.search(
        r'path:\s*"/email-intent-log"[^}]+labelKey:\s*"emailIntentLog"',
        src,
        re.DOTALL,
    ), "nav entry for /email-intent-log missing"


def test_filter_chips_iterate_all_action_values():
    """After the KR-FE-PANEL-KIT retrofit, FilterChips comes from
    AuditPanelKit and iterates a CategoryDef[] array
    (EMAIL_INTENT_CATEGORIES) instead of mapping over
    EMAIL_INTENT_ACTION_VALUES directly. Pin both:
      * the CategoryDef array exists + carries every action value
        from EMAIL_INTENT_ACTION_VALUES as a `key`
      * the FilterChips component receives that array
    Without this, adding a new action wouldn't auto-add a chip
    and operator couldn't filter on it."""
    src = _PAGE.read_text()
    assert "EMAIL_INTENT_CATEGORIES" in src
    assert "FilterChips" in src
    # Every action value must appear as a category key.
    for action in ("created", "logged_only", "dry_run", "cap_exceeded", "failed"):
        assert f'key: "{action}"' in src, (
            f"EMAIL_INTENT_CATEGORIES missing key: '{action}' — "
            f"FilterChips will silently drop this filter"
        )
    # And the kit-source-of-truth list is still referenced from
    # the page (drift-guard test_action_values_drift_guard greps
    # for the FE constant import).
    assert "EMAIL_INTENT_ACTION_VALUES" in src


def test_deep_link_uses_focus_query_param():
    src = _PAGE.read_text()
    # The deep-link must be forward-compatible: /sea-tickets
    # with ?focus=<ticket_id> so SeaTicketsPage can opt-in to
    # consume the param without breaking this panel.
    assert "/sea-tickets?focus=" in src
    assert "encodeURIComponent(event.ticket_id" in src


def test_sparkline_uses_plain_svg():
    """Per CC#2 discipline (CostTelemetryPage / etc): no chart
    libraries; build with plain SVG + Tailwind.

    After the KR-FE-PANEL-KIT retrofit, Sparkline lives in
    AuditPanelKit and is shared across panels. Pin both:
      * the page imports Sparkline from the kit (NOT a third-
        party chart lib alias)
      * the kit's Sparkline source itself uses plain SVG
    Belt + suspenders so neither layer sneaks in a chart-lib dep.
    """
    page_src = _PAGE.read_text()
    kit_src = (
        _REPO_ROOT / "web" / "src" / "components" / "AuditPanelKit" / "Sparkline.tsx"
    ).read_text()
    # Page imports from the kit (NOT some external lib named
    # "Sparkline" — guarded by the canonical import path below).
    assert "Sparkline" in page_src
    assert "@/components/AuditPanelKit" in page_src
    # Kit's Sparkline source uses plain SVG.
    assert "<svg" in kit_src
    assert "<rect" in kit_src
    # No chart-library imports in EITHER file.
    for lib in ("recharts", "chart.js", "d3", "@nivo", "victory"):
        for src, label in [(page_src, "page"), (kit_src, "kit")]:
            assert lib not in src, (
                f"chart library '{lib}' import detected in {label} — "
                f"Sparkline must stay in plain SVG per the CC#2 discipline"
            )


def test_empty_state_copy_committed():
    """Spec §2(c): empty state shows a calm message, not an empty
    card list. Pin both the all-filter copy AND the per-filter
    copy so regressions to bare-empty divs trigger a test fail."""
    src = _PAGE.read_text()
    assert "No email-intent events" in src
    assert "events match this filter" in src
