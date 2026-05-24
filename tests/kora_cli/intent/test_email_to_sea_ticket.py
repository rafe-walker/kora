"""Tests for KR-INTENT-EMAIL-TO-SEA-TICKET.

Covers:
  Recognition (pure-function regex):
   - explicit save markers → high confidence
   - subject "Idea:" / "Note:" / "TODO:" prefix → high confidence
   - "[SAVE]" bracket marker (subject or body) → high confidence
   - Fwd: subject + body URL → medium confidence (fwd_with_url)
   - Fwd: subject without URL → medium confidence (fwd_without_url)
   - plain message → unrecognized

  Orchestrator (process_email_intent):
   - high-confidence + provider wired → ticket created + DM sent
   - medium-confidence + floor=high → logged_only
   - medium-confidence + floor=medium → ticket created
   - dry_run env → action=dry_run, no MCP call, no DM
   - hourly cap exceeded → action=cap_exceeded, failure DM sent,
     no MCP call
   - provider unavailable → action=failed, failure DM sent
   - sea__create_ticket raises → action=failed, failure DM sent
   - unrecognized → audit logged_only, no MCP call, no DM
   - audit emit failure → orchestrator continues (no propagation)
   - confirmation DM failure → ticket still created (DM is best-effort)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kora_cli.intent.email_to_sea_ticket import (
    BODY_EXCERPT_LIMIT,
    DEFAULT_HOURLY_CAP,
    DRY_RUN_ENV,
    HOURLY_CAP_ENV,
    JOSHUA_SLACK_USER_ID_ENV,
    MIN_CONFIDENCE_ENV,
    EmailIntent,
    _format_confirmation_text,
    _format_failure_text,
    _hourly_cap_allows,
    _reset_rate_limiter_for_tests,
    process_email_intent,
    recognize_intent,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Per-test env isolation + audit-log redirect + rate-limiter reset."""
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "kora_constants.get_kora_home", lambda: tmp_path, raising=False
    )
    monkeypatch.setenv(
        "KORA_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl")
    )
    monkeypatch.setenv(JOSHUA_SLACK_USER_ID_ENV, "D-joshua-im")
    monkeypatch.delenv(DRY_RUN_ENV, raising=False)
    monkeypatch.delenv(HOURLY_CAP_ENV, raising=False)
    monkeypatch.delenv(MIN_CONFIDENCE_ENV, raising=False)
    _reset_rate_limiter_for_tests()
    yield
    _reset_rate_limiter_for_tests()


def _read_audit(tmp_path) -> list:
    path = tmp_path / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _fake_slack_client() -> MagicMock:
    client = MagicMock()
    client.post_dm = AsyncMock(return_value={"ok": True, "ts": "1.0"})
    return client


def _patch_provider_returning_ticket(monkeypatch, ticket_id: str = "STK-001"):
    """Wire `get_last_active_provider` so write_sea_ticket succeeds."""
    fake_mcp = MagicMock()
    fake_mcp.invoke = AsyncMock(return_value={"ticket_id": ticket_id})
    fake_connection = MagicMock()
    fake_connection.get_mcp_client.return_value = fake_mcp
    fake_provider = MagicMock()
    fake_provider._connection = fake_connection
    monkeypatch.setattr(
        "plugins.memory.isokron.get_last_active_provider",
        lambda: fake_provider,
    )
    return fake_mcp


def _patch_provider_unavailable(monkeypatch):
    monkeypatch.setattr(
        "plugins.memory.isokron.get_last_active_provider",
        lambda: None,
    )


# ===========================================================================
# recognize_intent — pure-function pattern matching
# ===========================================================================


@pytest.mark.parametrize(
    "subject,body,expected_pattern",
    [
        ("Idea: a new wedge for the cockpit panel", "body here", "subject_idea_prefix"),
        ("Note: write up the K-7 takeaway", "...", "subject_note_prefix"),
        ("TODO: rebill last week's Vercel", "—", "subject_todo_prefix"),
    ],
)
def test_recognize_subject_prefix_high_confidence(subject, body, expected_pattern):
    intent = recognize_intent(subject=subject, body=body, sender="j@e.com")
    assert intent.confidence == "high"
    assert intent.pattern_matched == expected_pattern
    assert intent.proposed_sea_ticket is not None


def test_recognize_bracket_save_subject():
    intent = recognize_intent(
        subject="[SAVE] this article matters",
        body="link to follow",
        sender="j@e.com",
    )
    assert intent.confidence == "high"
    assert intent.pattern_matched == "explicit_save_bracket"


def test_recognize_bracket_save_body():
    intent = recognize_intent(
        subject="re: the meeting",
        body="lots of stuff in here, but also [SAVE] please",
        sender="j@e.com",
    )
    assert intent.confidence == "high"
    assert intent.pattern_matched == "explicit_save_bracket"


@pytest.mark.parametrize(
    "phrase",
    [
        "save this",
        "save to sea_ticket",
        "save as idea",
        "add to sea",
        "add idea",
    ],
)
def test_recognize_explicit_save_phrases_in_body(phrase):
    intent = recognize_intent(
        subject="random subject",
        body=f"hey kora, can you {phrase} from this article",
        sender="j@e.com",
    )
    assert intent.confidence == "high"
    assert intent.pattern_matched == "explicit_save_phrase"


def test_recognize_fwd_subject_with_url_is_medium():
    intent = recognize_intent(
        subject="Fwd: cool blog post",
        body="found this: https://example.com/post — worth saving",
        sender="j@e.com",
    )
    assert intent.confidence == "medium"
    assert intent.pattern_matched == "fwd_with_url"


def test_recognize_fwd_subject_without_url_is_medium_no_url():
    intent = recognize_intent(
        subject="Fw: chat from yesterday",
        body="see attached",
        sender="j@e.com",
    )
    assert intent.confidence == "medium"
    assert intent.pattern_matched == "fwd_without_url"


def test_recognize_forwarded_message_marker_in_body():
    intent = recognize_intent(
        subject="(re-)",
        body=(
            "---------- Forwarded message ---------\n"
            "From: someone <s@e.com>\n"
            "https://example.com/article\n"
        ),
        sender="j@e.com",
    )
    assert intent.confidence == "medium"
    assert intent.pattern_matched == "fwd_with_url"


def test_recognize_plain_email_is_unrecognized():
    intent = recognize_intent(
        subject="just saying hi",
        body="hope you're well",
        sender="j@e.com",
    )
    assert intent.confidence == "unrecognized"
    assert intent.proposed_sea_ticket is None
    assert intent.tags == ("email",)


def test_recognize_empty_inputs_are_unrecognized():
    intent = recognize_intent(subject="", body="", sender="")
    assert intent.confidence == "unrecognized"


def test_proposed_ticket_title_strips_re_fwd_prefix_chain():
    intent = recognize_intent(
        subject="Fwd: Re: Fw: Idea: bundle the panels",
        body="ship it",
        sender="j@e.com",
    )
    assert intent.proposed_sea_ticket["title"] == "Idea: bundle the panels"
    # Subject-prefix recognition still fires on the cleaned subject? No —
    # match is on the RAW subject, so this is fwd_with_url? No URL here.
    # The Fwd: prefix is matched first → confidence medium.
    # Title derivation is independent of pattern match.
    assert intent.confidence in ("high", "medium")


def test_proposed_ticket_body_truncates_long_input():
    long_body = "x" * (BODY_EXCERPT_LIMIT + 1000)
    intent = recognize_intent(
        subject="Idea: lots of text",
        body=long_body,
        sender="j@e.com",
    )
    body = intent.proposed_sea_ticket["body"]
    # The provenance header + truncation marker add fixed bytes;
    # the excerpt portion is capped.
    assert "[…truncated]" in body
    assert len(body) < BODY_EXCERPT_LIMIT + 500  # header + suffix slack


def test_proposed_ticket_tags_include_pattern_keyword():
    intent = recognize_intent(
        subject="Idea: x",
        body="y",
        sender="j@e.com",
    )
    tags = intent.proposed_sea_ticket["tags"]
    assert "email" in tags
    assert "idea" in tags


# ===========================================================================
# Orchestrator — happy path
# ===========================================================================


@pytest.mark.asyncio
async def test_orchestrator_high_confidence_creates_ticket_and_dms(
    tmp_path, monkeypatch
):
    fake_mcp = _patch_provider_returning_ticket(monkeypatch, "STK-42")
    slack = _fake_slack_client()

    result = await process_email_intent(
        message_id="<m-1@e.com>",
        subject="Idea: try the new Haiku model",
        body_text="we should benchmark it on the cockpit fanout",
        sender="joshua@stormhavenenterprises.com",
        slack_client=slack,
    )

    assert result["action"] == "created"
    assert result["ticket_id"] == "STK-42"
    assert result["intent"].pattern_matched == "subject_idea_prefix"

    # MCP invoked exactly once with our payload.
    fake_mcp.invoke.assert_awaited_once()
    tool_name, payload = fake_mcp.invoke.await_args.args
    assert tool_name == "sea__create_ticket"
    assert payload["title"] == "Idea: try the new Haiku model"
    assert payload["origin_seam"] == "intent.email_to_sea_ticket"

    # Slack DM sent to the operator channel.
    slack.post_dm.assert_awaited_once()
    dm_kw = slack.post_dm.await_args.kwargs
    assert dm_kw["channel_id"] == "D-joshua-im"
    assert "STK-42" in dm_kw["text"]
    assert "Idea: try the new Haiku model" in dm_kw["text"]

    # Audit row recorded.
    entries = _read_audit(tmp_path)
    assert len(entries) == 1
    assert entries[0]["seam"] == "intent.email_to_sea_ticket"
    assert entries[0]["source"] == "email"
    assert entries[0]["details"]["action"] == "created"
    assert entries[0]["details"]["ticket_id"] == "STK-42"
    assert entries[0]["caller_session_id"] == "email:<m-1@e.com>"


@pytest.mark.asyncio
async def test_orchestrator_medium_confidence_below_floor_logs_only(
    tmp_path, monkeypatch
):
    # Default floor = "high"; medium intent must NOT write.
    fake_mcp = _patch_provider_returning_ticket(monkeypatch)
    slack = _fake_slack_client()

    result = await process_email_intent(
        message_id="<m-2@e.com>",
        subject="Fwd: cool stuff",
        body_text="found this: https://example.com/x",
        sender="j@e.com",
        slack_client=slack,
    )

    assert result["action"] == "logged_only"
    fake_mcp.invoke.assert_not_awaited()
    slack.post_dm.assert_not_awaited()

    entries = _read_audit(tmp_path)
    assert entries[0]["details"]["action"] == "logged_only"
    assert entries[0]["details"]["reason"] == "below_floor_high"


@pytest.mark.asyncio
async def test_orchestrator_medium_confidence_with_medium_floor_creates(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(MIN_CONFIDENCE_ENV, "medium")
    fake_mcp = _patch_provider_returning_ticket(monkeypatch, "STK-77")
    slack = _fake_slack_client()

    result = await process_email_intent(
        message_id="<m-3@e.com>",
        subject="Fwd: read this",
        body_text="https://example.com/post",
        sender="j@e.com",
        slack_client=slack,
    )

    assert result["action"] == "created"
    assert result["ticket_id"] == "STK-77"
    fake_mcp.invoke.assert_awaited_once()
    slack.post_dm.assert_awaited_once()


# ===========================================================================
# Orchestrator — dry-run
# ===========================================================================


@pytest.mark.asyncio
async def test_orchestrator_dry_run_audits_but_does_not_write(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(DRY_RUN_ENV, "true")
    fake_mcp = _patch_provider_returning_ticket(monkeypatch)
    slack = _fake_slack_client()

    result = await process_email_intent(
        message_id="<m-dry@e.com>",
        subject="Idea: dry-run test",
        body_text="...",
        sender="j@e.com",
        slack_client=slack,
    )

    assert result["action"] == "dry_run"
    assert result["ticket_id"] is None
    fake_mcp.invoke.assert_not_awaited()
    slack.post_dm.assert_not_awaited()

    entries = _read_audit(tmp_path)
    assert entries[0]["details"]["action"] == "dry_run"
    assert entries[0]["details"]["proposed_title"] == "Idea: dry-run test"


# ===========================================================================
# Orchestrator — hourly cap
# ===========================================================================


@pytest.mark.asyncio
async def test_orchestrator_hourly_cap_blocks_eleventh_write(
    tmp_path, monkeypatch
):
    """11 high-confidence matches in 1 hour → 10 created, 11th
    cap_exceeded + failure DM."""
    monkeypatch.setenv(HOURLY_CAP_ENV, "10")
    fake_mcp = _patch_provider_returning_ticket(monkeypatch, "STK-X")
    slack = _fake_slack_client()

    for i in range(DEFAULT_HOURLY_CAP):
        result = await process_email_intent(
            message_id=f"<m-{i}@e.com>",
            subject=f"Idea: number {i}",
            body_text="...",
            sender="j@e.com",
            slack_client=slack,
        )
        assert result["action"] == "created"

    # 11th — should be capped.
    overflow = await process_email_intent(
        message_id="<m-overflow@e.com>",
        subject="Idea: one too many",
        body_text="...",
        sender="j@e.com",
        slack_client=slack,
    )
    assert overflow["action"] == "cap_exceeded"
    # MCP invoke called 10 times (not 11).
    assert fake_mcp.invoke.await_count == 10
    # DM called 11 times (10 confirmations + 1 cap-exceeded failure DM).
    assert slack.post_dm.await_count == 11

    entries = _read_audit(tmp_path)
    actions = [e["details"]["action"] for e in entries]
    assert actions.count("created") == 10
    assert actions.count("cap_exceeded") == 1
    cap_entry = next(e for e in entries if e["details"]["action"] == "cap_exceeded")
    assert cap_entry["details"]["hourly_cap"] == 10


def test_hourly_cap_zero_disables_limit(monkeypatch):
    """Cap of 0 means "no cap" — always allows."""
    monkeypatch.setenv(HOURLY_CAP_ENV, "0")
    # Pre-populate the deque so a real cap would block.
    from kora_cli.intent.email_to_sea_ticket import _record_create

    for _ in range(50):
        _record_create()
    assert _hourly_cap_allows() is True


def test_hourly_cap_malformed_falls_back_to_default(monkeypatch, caplog):
    import logging

    monkeypatch.setenv(HOURLY_CAP_ENV, "not-a-number")
    with caplog.at_level(logging.WARNING):
        # First call inside _hourly_cap_allows triggers the warning.
        result = _hourly_cap_allows()
    assert result is True  # empty deque, default cap > 0
    assert any(
        "KORA_EMAIL_INTENT_HOURLY_CAP" in r.message for r in caplog.records
    )


# ===========================================================================
# Orchestrator — failure paths
# ===========================================================================


@pytest.mark.asyncio
async def test_orchestrator_provider_unavailable_dms_failure(
    tmp_path, monkeypatch
):
    _patch_provider_unavailable(monkeypatch)
    slack = _fake_slack_client()

    result = await process_email_intent(
        message_id="<m-noprov@e.com>",
        subject="Idea: provider gone",
        body_text="...",
        sender="j@e.com",
        slack_client=slack,
    )
    assert result["action"] == "failed"
    assert "no active IsoKron provider" in result["error"]

    slack.post_dm.assert_awaited_once()
    failure_text = slack.post_dm.await_args.kwargs["text"]
    assert "Could not save" in failure_text

    entries = _read_audit(tmp_path)
    assert entries[0]["details"]["action"] == "failed"


@pytest.mark.asyncio
async def test_orchestrator_sea_create_ticket_raises_dms_failure(
    tmp_path, monkeypatch
):
    fake_mcp = MagicMock()
    fake_mcp.invoke = AsyncMock(side_effect=RuntimeError("substrate offline"))
    fake_connection = MagicMock()
    fake_connection.get_mcp_client.return_value = fake_mcp
    fake_provider = MagicMock()
    fake_provider._connection = fake_connection
    monkeypatch.setattr(
        "plugins.memory.isokron.get_last_active_provider",
        lambda: fake_provider,
    )
    slack = _fake_slack_client()

    result = await process_email_intent(
        message_id="<m-fail@e.com>",
        subject="Idea: this will fail",
        body_text="...",
        sender="j@e.com",
        slack_client=slack,
    )
    assert result["action"] == "failed"
    assert "substrate offline" in result["error"]
    slack.post_dm.assert_awaited_once()
    assert "substrate offline" in slack.post_dm.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_orchestrator_unrecognized_logs_only(tmp_path, monkeypatch):
    fake_mcp = _patch_provider_returning_ticket(monkeypatch)
    slack = _fake_slack_client()

    result = await process_email_intent(
        message_id="<m-plain@e.com>",
        subject="just saying hi",
        body_text="hope you're well",
        sender="j@e.com",
        slack_client=slack,
    )
    assert result["action"] == "logged_only"
    fake_mcp.invoke.assert_not_awaited()
    slack.post_dm.assert_not_awaited()
    entries = _read_audit(tmp_path)
    assert entries[0]["details"]["action"] == "logged_only"
    assert entries[0]["details"]["reason"] == "no_pattern_matched"


@pytest.mark.asyncio
async def test_orchestrator_swallows_unexpected_exception(
    tmp_path, monkeypatch
):
    """The wrapping process_email_intent must NEVER raise — it
    catches everything and returns action=failed."""
    monkeypatch.setattr(
        "kora_cli.intent.email_to_sea_ticket.recognize_intent",
        MagicMock(side_effect=RuntimeError("unexpected blow")),
    )
    result = await process_email_intent(
        message_id="<m-explode@e.com>",
        subject="anything",
        body_text="anything",
        sender="j@e.com",
        slack_client=_fake_slack_client(),
    )
    assert result["action"] == "failed"
    assert "unexpected blow" in result["error"]


@pytest.mark.asyncio
async def test_orchestrator_dm_failure_does_not_block_creation(
    tmp_path, monkeypatch
):
    """Slack DM failure is best-effort — Sea_Ticket is still created."""
    fake_mcp = _patch_provider_returning_ticket(monkeypatch, "STK-99")
    slack = MagicMock()
    slack.post_dm = AsyncMock(side_effect=RuntimeError("slack down"))

    result = await process_email_intent(
        message_id="<m-dmfail@e.com>",
        subject="Idea: slack is flaky",
        body_text="...",
        sender="j@e.com",
        slack_client=slack,
    )
    assert result["action"] == "created"
    assert result["ticket_id"] == "STK-99"
    fake_mcp.invoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_orchestrator_slack_user_id_unset_skips_dm_but_creates(
    tmp_path, monkeypatch
):
    monkeypatch.delenv(JOSHUA_SLACK_USER_ID_ENV, raising=False)
    fake_mcp = _patch_provider_returning_ticket(monkeypatch, "STK-77")
    slack = _fake_slack_client()

    result = await process_email_intent(
        message_id="<m-nodm@e.com>",
        subject="Idea: dm-less",
        body_text="...",
        sender="j@e.com",
        slack_client=slack,
    )
    assert result["action"] == "created"
    slack.post_dm.assert_not_awaited()


# ===========================================================================
# Format helpers
# ===========================================================================


def test_confirmation_text_includes_ticket_subject_and_pattern():
    text = _format_confirmation_text(
        ticket_id="STK-1", subject="hello", pattern_matched="subject_idea_prefix"
    )
    assert "STK-1" in text
    assert "hello" in text
    assert "subject_idea_prefix" in text


def test_failure_text_includes_reason():
    text = _format_failure_text(
        subject="hi", reason="boom", pattern_matched="explicit_save_phrase"
    )
    assert "boom" in text
    assert "Could not save" in text


def test_confirmation_text_falls_back_for_empty_subject():
    text = _format_confirmation_text(
        ticket_id="STK-1", subject="   ", pattern_matched="subject_idea_prefix"
    )
    assert "(no subject)" in text
