"""KR-FE-KORA-ACTIONS-AGGREGATED-PANEL — backend + FE source-pin tests.

Apex "what did Kora do" timeline. Joins 4 (eventually 5) mutating-
action audit seams into one chronological list. This is the
operator-trust surface — single page answering "did Kora do
anything worth my attention today?"

Tests:

  Backend (14):
    1. Endpoint registered
    2. Empty audit → calm zero response with all-zero
       by_category_24h
    3. email_sent: outbound email audit row → email_sent category
    4. sea_ticket_created: intent.email_to_sea_ticket with
       action=created → sea_ticket_created; with action=logged_only
       → NOT included
    5. autofix_attempted: tool.probe_autofix_attempted with
       status=attempted → autofix_attempted; with status=rejected
       → NOT included
    6. phrasebook_proposal_approved: phrasebook.updated with
       actor != "operator" → category populated; with
       actor="operator" → NOT included (operator-driven edits
       go to the per-seam panel, not the kora-DID-something view)
    7. Mixed merge: 4 seams together → all 4 categories
       represented + chronological order respected
    8. Deep-links populated correctly per category
    9. Per-row summary composed correctly per category
   10. Daily sparkline math (14 buckets, all categories combined)
   11. 24h cutoff
   12. limit cap
   13. forward-compat: probe.investigation_completed seam read
       attempt doesn't crash (returns [] when seam not yet in
       SeamName Literal)
   14. SECURITY: per-category summary composers don't leak raw
       details (no operator PII / SMTP headers / etc)

  Drift guard (2):
   15. action_category values match BE projection +
       FE constant (no separate emitter constants for this — the
       categories are FE-defined per spec since they're a
       cross-seam canonical naming, not per-tool emit-time values)
   16. SeamName Literal includes all source seams the panel reads
       from (tool.email_to_operator_sent, intent.email_to_sea_ticket,
       tool.probe_autofix_attempted, phrasebook.updated)

  FE source-pins (8):
   17. api.getKoraActionsRecent wrapper exists
   18. KoraActionItem + Response + Category types declared
   19. KORA_ACTION_CATEGORIES FE constant exported
   20. KoraActionsPage exists + uses usePanelView + uses
       AuditPanelKit (4th consumer)
   21. Route + nav entry registered
   22. KORA_ACTION_CATEGORIES_DEFS used by FilterChips
   23. Deep-link rendered for items with deep_link
   24. Empty state copy ("Kora has been quiet today") committed
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
_APP_TSX = _REPO_ROOT / "web" / "src" / "pages" / "App.tsx"  # patched below
_APP_TSX = _REPO_ROOT / "web" / "src" / "App.tsx"
_PAGE = _REPO_ROOT / "web" / "src" / "pages" / "KoraActionsPage.tsx"
_WEB_SERVER = _REPO_ROOT / "kora_cli" / "web_server.py"
_JSONL_SINK = _REPO_ROOT / "kora_cli" / "audit" / "jsonl_sink.py"


@pytest.fixture
def env(tmp_path, monkeypatch):
    return isolated_kora_home(tmp_path, monkeypatch)


def _write_audit_jsonl(env_dir: Path, entries: list) -> None:
    log_path = env_dir / "kora_audit_log.jsonl"
    with log_path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, default=str) + "\n")


def _email_sent_row(*, emitted_at: datetime, extra: dict | None = None) -> dict:
    details: Dict[str, Any] = {
        "status": "sent",
        "subject_chars": 42,
        "body_chars": 256,
        "attachment_count": 1,
        "attachment_total_bytes": 1024,
        "smtp_message_id": "<msg-x>",
    }
    if extra:
        details.update(extra)
    return {
        "emitted_at": emitted_at.isoformat(),
        "seam": "tool.email_to_operator_sent",
        "details": details,
        "source": "reasoning",
        "caller_session_id": "engine-sess-1",
    }


def _intent_row(
    *, action: str, emitted_at: datetime, ticket_id: str | None = None
) -> dict:
    details: Dict[str, Any] = {
        "action": action,
        "pattern_matched": "subject_idea_prefix",
        "confidence": "high",
        "subject": "Idea: try X",
    }
    if ticket_id is not None:
        details["ticket_id"] = ticket_id
    return {
        "emitted_at": emitted_at.isoformat(),
        "seam": "intent.email_to_sea_ticket",
        "details": details,
        "source": "email",
        "caller_session_id": "email:msg-a",
    }


def _autofix_row(*, status: str, emitted_at: datetime) -> dict:
    details: Dict[str, Any] = {
        "status": status,
        "probe": "fly",
        "action": "restart_machine",
        "action_canonical": "restart_unhealthy_machine",
        "target_id": "1781e9f6c12d83",
        "reason_from_reasoning": "machine unhealthy for 4 ticks",
    }
    if status == "attempted":
        details["executor_duration_ms"] = 3214
        details["before_state"] = {"state": "stopped"}
        details["after_state"] = {"state": "started"}
    elif status == "rejected":
        details["rejection_reason"] = "envelope_disabled"
    return {
        "emitted_at": emitted_at.isoformat(),
        "seam": "tool.probe_autofix_attempted",
        "details": details,
        "source": "reasoning",
        "caller_session_id": "engine-sess-2",
    }


def _phrasebook_row(*, actor: str, emitted_at: datetime) -> dict:
    return {
        "emitted_at": emitted_at.isoformat(),
        "seam": "phrasebook.updated",
        "details": {
            "actor": actor,
            "action": "put",
            "entry_count_before": 5,
            "entry_count_after": 6,
            "backup_filename": "slack_dm.X.yml",
            "rotated_backup_count": 0,
        },
        "source": None,
        "caller_session_id": None,
    }


async def _call_endpoint(env_dir: Path, limit: int = 100) -> dict:
    from kora_cli import web_server

    return await web_server.list_recent_kora_actions(limit=limit)


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


def test_endpoint_registered():
    from kora_cli import web_server

    paths = {getattr(r, "path", None) for r in web_server.app.routes}
    assert "/api/kora-actions/recent" in paths


@pytest.mark.asyncio
async def test_empty_audit_returns_calm_zero_response(env):
    body = await _call_endpoint(env)
    assert body["items"] == []
    assert body["total_recent_24h"] == 0
    for cat in (
        "email_sent",
        "sea_ticket_created",
        "autofix_attempted",
        "investigation_completed",
        "phrasebook_proposal_approved",
        "other",
    ):
        assert body["by_category_24h"][cat] == 0


@pytest.mark.asyncio
async def test_email_sent_category(env):
    now = datetime.now(timezone.utc)
    _write_audit_jsonl(
        env, [_email_sent_row(emitted_at=now - timedelta(minutes=10))]
    )
    body = await _call_endpoint(env)
    assert len(body["items"]) == 1
    assert body["items"][0]["action_category"] == "email_sent"
    assert body["items"][0]["status"] == "sent"
    assert "Sent email to operator" in body["items"][0]["summary"]
    assert body["items"][0]["deep_link"] == "/outbound-email-log"


@pytest.mark.asyncio
async def test_intent_only_created_action_included(env):
    """logged_only / dry_run / cap_exceeded / failed go to the
    per-seam panel; only action=created reaches the apex view."""
    now = datetime.now(timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _intent_row(
                action="created",
                emitted_at=now - timedelta(minutes=5),
                ticket_id="STK-42",
            ),
            _intent_row(
                action="logged_only",
                emitted_at=now - timedelta(minutes=4),
            ),
            _intent_row(
                action="dry_run", emitted_at=now - timedelta(minutes=3)
            ),
            _intent_row(
                action="cap_exceeded", emitted_at=now - timedelta(minutes=2)
            ),
            _intent_row(
                action="failed", emitted_at=now - timedelta(minutes=1)
            ),
        ],
    )
    body = await _call_endpoint(env)
    assert len(body["items"]) == 1
    assert body["items"][0]["action_category"] == "sea_ticket_created"
    assert body["items"][0]["deep_link"] == "/sea-tickets?focus=STK-42"
    assert "STK-42" in body["items"][0]["summary"]


@pytest.mark.asyncio
async def test_autofix_only_attempted_status_included(env):
    now = datetime.now(timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _autofix_row(status="attempted", emitted_at=now - timedelta(minutes=5)),
            _autofix_row(status="rejected", emitted_at=now - timedelta(minutes=4)),
            _autofix_row(
                status="execution_failed", emitted_at=now - timedelta(minutes=3)
            ),
        ],
    )
    body = await _call_endpoint(env)
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["action_category"] == "autofix_attempted"
    assert item["deep_link"] == "/probe-autofix-log"
    # Summary includes the state transition.
    assert "stopped→started" in item["summary"]


@pytest.mark.asyncio
async def test_phrasebook_only_non_operator_actor_included(env):
    """v1: ALL phrasebook updates have actor="operator" so this
    yields zero. Future KR-PROMOTE-PHRASEBOOK with
    actor="kora_proposal_approved" populates the category."""
    now = datetime.now(timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _phrasebook_row(
                actor="operator",
                emitted_at=now - timedelta(minutes=5),
            ),
            _phrasebook_row(
                actor="kora_proposal_approved",
                emitted_at=now - timedelta(minutes=4),
            ),
        ],
    )
    body = await _call_endpoint(env)
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["action_category"] == "phrasebook_proposal_approved"
    assert item["deep_link"] == "/phrasebook"
    assert "5→6" in item["summary"]


@pytest.mark.asyncio
async def test_mixed_merge_chronological(env):
    now = datetime.now(timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _email_sent_row(emitted_at=now - timedelta(minutes=15)),
            _intent_row(
                action="created",
                emitted_at=now - timedelta(minutes=10),
                ticket_id="STK-1",
            ),
            _autofix_row(status="attempted", emitted_at=now - timedelta(minutes=5)),
            _phrasebook_row(
                actor="kora_proposal_approved",
                emitted_at=now - timedelta(minutes=2),
            ),
        ],
    )
    body = await _call_endpoint(env)
    cats = [it["action_category"] for it in body["items"]]
    # Newest first: phrasebook (2m) → autofix (5m) → intent (10m) → email (15m)
    assert cats == [
        "phrasebook_proposal_approved",
        "autofix_attempted",
        "sea_ticket_created",
        "email_sent",
    ]


@pytest.mark.asyncio
async def test_daily_sparkline_combined(env):
    now = datetime.now(timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _email_sent_row(emitted_at=now - timedelta(hours=1)),
            _intent_row(
                action="created", emitted_at=now - timedelta(hours=2), ticket_id="A"
            ),
            _autofix_row(status="attempted", emitted_at=now - timedelta(days=3)),
        ],
    )
    sl = (await _call_endpoint(env))["daily_actions_14d"]
    assert len(sl) == 14
    assert sum(b["count"] for b in sl) == 3


@pytest.mark.asyncio
async def test_24h_cutoff(env):
    now = datetime.now(timezone.utc)
    _write_audit_jsonl(
        env,
        [
            _email_sent_row(emitted_at=now - timedelta(hours=1)),
            _email_sent_row(emitted_at=now - timedelta(days=2)),
        ],
    )
    body = await _call_endpoint(env)
    assert len(body["items"]) == 2  # all items returned regardless of window
    assert body["total_recent_24h"] == 1
    assert body["by_category_24h"]["email_sent"] == 1


@pytest.mark.asyncio
async def test_limit_cap(env):
    now = datetime.now(timezone.utc)
    rows = [
        _email_sent_row(emitted_at=now - timedelta(minutes=i)) for i in range(150)
    ]
    _write_audit_jsonl(env, rows)
    body = await _call_endpoint(env, limit=50)
    assert len(body["items"]) == 50


@pytest.mark.asyncio
async def test_forward_compat_investigation_seam(env):
    """probe.investigation_completed isn't in SeamName Literal yet
    (lands with #406). The endpoint must NOT crash when the seam
    has no entries — it just yields zero rows for that category."""
    body = await _call_endpoint(env)
    assert body["by_category_24h"]["investigation_completed"] == 0
    # And the category appears in the canonical list:
    assert "investigation_completed" in body["action_categories"]


@pytest.mark.asyncio
async def test_per_category_summaries_dont_leak_arbitrary_fields(env):
    """SECURITY: summary composers must whitelist fields. Inject
    hostile fields into each seam's details and assert nothing
    leaks into the response."""
    now = datetime.now(timezone.utc)
    leak = "INTERNAL_TOKEN_kora_actions_leak_xyz"
    _write_audit_jsonl(
        env,
        [
            _email_sent_row(
                emitted_at=now - timedelta(minutes=5),
                extra={"operator_email": "[email protected]", "leak": leak},
            ),
            {
                "emitted_at": (now - timedelta(minutes=4)).isoformat(),
                "seam": "intent.email_to_sea_ticket",
                "details": {
                    "action": "created",
                    "pattern_matched": "x",
                    "subject": "y",
                    "ticket_id": "STK-1",
                    "leak": leak,
                    "operator_email": "[email protected]",
                },
                "source": "email",
                "caller_session_id": "email:msg-z",
            },
            _autofix_row(
                status="attempted", emitted_at=now - timedelta(minutes=3)
            )
            | {"details": {**_autofix_row(
                status="attempted", emitted_at=now - timedelta(minutes=3)
            )["details"], "fly_api_key": leak}},
        ],
    )
    body = await _call_endpoint(env)
    serialized = json.dumps(body)
    assert leak not in serialized
    assert "[email protected]" not in serialized
    assert "fly_api_key" not in serialized


# ---------------------------------------------------------------------------
# Drift guard
# ---------------------------------------------------------------------------


def test_action_categories_drift_guard():
    """action_category values must match between BE projection
    allow-list and FE constant. (No emitter constants here — the
    categories are cross-seam FE-defined.)"""
    expected = {
        "email_sent",
        "sea_ticket_created",
        "autofix_attempted",
        "investigation_completed",
        "phrasebook_proposal_approved",
        "other",
    }

    ws_src = _WEB_SERVER.read_text()
    m = re.search(
        r"_KORA_ACTION_CATEGORIES\s*=\s*\(([^)]+)\)",
        ws_src,
        re.DOTALL,
    )
    assert m is not None, "BE _KORA_ACTION_CATEGORIES not found"
    be_values = set(re.findall(r'"(\w+)"', m.group(1)))
    assert be_values == expected, f"BE drift: {be_values}"

    fe_src = _API_TS.read_text()
    m = re.search(
        r"KORA_ACTION_CATEGORIES[^=]*=\s*\[([^\]]+)\]",
        fe_src,
    )
    assert m is not None, "FE KORA_ACTION_CATEGORIES not found"
    fe_values = set(re.findall(r'"(\w+)"', m.group(1)))
    assert fe_values == expected, f"FE drift: {fe_values}"


def test_seam_literal_includes_all_source_seams():
    """The 4 source seams the panel reads from must all be in the
    SeamName Literal (otherwise read_audit_entries silently returns
    [] + the panel mysteriously shows empty for that category)."""
    sink_src = _JSONL_SINK.read_text()
    for seam in (
        "tool.email_to_operator_sent",
        "intent.email_to_sea_ticket",
        "tool.probe_autofix_attempted",
        "phrasebook.updated",
    ):
        assert f'"{seam}"' in sink_src, (
            f"SeamName missing '{seam}' — KoraActionsPage will "
            f"silently fail to surface this seam's events"
        )


# ---------------------------------------------------------------------------
# FE source-pins
# ---------------------------------------------------------------------------


def test_api_wrapper_exists():
    src = _API_TS.read_text()
    assert "getKoraActionsRecent" in src
    assert "/api/kora-actions/recent" in src


def test_response_types_declared():
    src = _API_TS.read_text()
    for ts_type in (
        "KoraActionItem",
        "KoraActionsResponse",
        "KoraActionCategory",
        "KoraActionDailyCount",
    ):
        assert ts_type in src


def test_fe_categories_constant_exported():
    src = _API_TS.read_text()
    assert "export const KORA_ACTION_CATEGORIES" in src


def test_page_exists_uses_panel_view_and_kit():
    assert _PAGE.is_file()
    src = _PAGE.read_text()
    assert 'usePanelView("KoraActionsPage")' in src
    # 4th consumer of the kit — validates the kit's API across
    # 4 different category enums (action / status / status /
    # action_category).
    assert "@/components/AuditPanelKit" in src


def test_route_and_nav_registered():
    src = _APP_TSX.read_text()
    assert "/kora-actions" in src
    assert "KoraActionsPage" in src
    assert re.search(
        r'path:\s*"/kora-actions"[^}]+labelKey:\s*"koraActions"',
        src,
        re.DOTALL,
    )


def test_filter_chips_iterate_canonical_categories():
    src = _PAGE.read_text()
    assert "KORA_ACTION_CATEGORIES_DEFS" in src
    for cat in (
        "email_sent",
        "sea_ticket_created",
        "autofix_attempted",
        "investigation_completed",
        "phrasebook_proposal_approved",
        "other",
    ):
        assert f'key: "{cat}"' in src
    assert "KORA_ACTION_CATEGORIES" in src


def test_deep_link_rendered_for_items():
    """Per-row card must render a <Link to={item.deep_link}> when
    the item has a deep_link (composer-provided). Forward-compat
    `?` guard so items without a deep_link don't blow up."""
    src = _PAGE.read_text()
    assert "item.deep_link" in src
    assert "<Link" in src or "Link " in src


def test_empty_state_copy_committed():
    src = _PAGE.read_text()
    assert "Kora has been quiet today" in src
    assert "actions match this filter" in src
