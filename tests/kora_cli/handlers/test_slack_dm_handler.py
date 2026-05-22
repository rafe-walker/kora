"""Tests for ``kora_cli.handlers.slack_dm_handler`` — KR-FEAT-SLACK-DM ST1.

Covers:
  - Joshua DM → received + logged + chain-event-log emit
  - Non-Joshua → filtered_non_joshua + logged + no emit
  - Bot message → filtered_bot
  - Subtype event → filtered_subtype
  - Non-IM channel → filtered_non_im
  - PAUSED state → dropped_paused
  - STOPPED state → dropped_stopped
  - Handler internal exception → handler_error + still 200
  - JSONL format: one valid JSON per line, required fields present
  - SECURITY: signing-secret env value never appears in JSONL
  - JOSHUA_USER_ID env unset → all messages filtered (fail-CLOSED)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

import pytest

from kora_cli.handlers import slack_dm_handler as sdm
from kora_cli.handlers.slack_dm_handler import (
    HANDLED_DROPPED_PAUSED,
    HANDLED_DROPPED_STOPPED,
    HANDLED_FILTERED_BOT,
    HANDLED_FILTERED_NON_IM,
    HANDLED_FILTERED_NON_JOSHUA,
    HANDLED_FILTERED_SUBTYPE,
    HANDLED_HANDLER_ERROR,
    HANDLED_RECEIVED,
    JOSHUA_USER_ID_ENV,
    SlackDMHandler,
)


JOSHUA_ID = "UJOSHUA01"


def _make_payload(
    *,
    user: str = JOSHUA_ID,
    channel: str = "D01CHAN01",
    text: str = "hi kora",
    ts: str = "1700000000.001",
    channel_type: str = "im",
    bot_id: str | None = None,
    subtype: str | None = None,
    thread_ts: str | None = None,
    event_type: str = "message",
) -> Dict[str, Any]:
    """Build a Slack Events `event_callback` payload."""
    event: Dict[str, Any] = {
        "type": event_type,
        "user": user,
        "channel": channel,
        "channel_type": channel_type,
        "text": text,
        "ts": ts,
    }
    if bot_id is not None:
        event["bot_id"] = bot_id
    if subtype is not None:
        event["subtype"] = subtype
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    return {"type": "event_callback", "event": event}


@pytest.fixture
def log_path(tmp_path):
    path = tmp_path / "slack_dm_log.jsonl"
    return path


@pytest.fixture
def handler(log_path):
    """ST1 tests focus on inbound filter behavior. After ST2 wired
    the outbound reply, the handler attempts post_dm on identified
    Joshua DMs — inject a no-op mock SlackClient so the outbound
    side runs without env config + the happy-path tests can assert
    against just the inbound JSONL entries (filter by the
    ``handled_status`` key vs the outbound's ``send_status`` key).
    """
    from unittest.mock import AsyncMock

    class _MockClient:
        def __init__(self):
            self.post_dm = AsyncMock(
                return_value={"ok": True, "ts": "1700000001.999"}
            )

    return SlackDMHandler(log_path=log_path, slack_client=_MockClient())


@pytest.fixture(autouse=True)
def _joshua_env(monkeypatch):
    monkeypatch.setenv(JOSHUA_USER_ID_ENV, JOSHUA_ID)


@pytest.fixture(autouse=True)
def _reset_holder(monkeypatch):
    """Default to no operational-state holder so tests don't see
    accidental PAUSED-state drops. Per-test fixtures can override."""
    from agent import operational_state_holder as h_mod

    monkeypatch.setattr(h_mod, "_HOLDER", None)


def _read_log_lines(log_path: Path) -> list[dict]:
    """Read ONLY inbound JSONL entries (filter to those with the
    ``handled_status`` key). After ST2 the JSONL also contains
    outbound entries with ``send_status`` instead — those have
    their own dedicated test surface in
    ``test_slack_dm_reply.py``; ST1 tests filter them out so the
    inbound-filter assertions stay sharp.
    """
    if not log_path.exists():
        return []
    return [
        entry
        for entry in (
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        if "handled_status" in entry
    ]


# ---------------------------------------------------------------------------
# Happy path — Joshua DM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_joshua_dm_received_and_logged(handler, log_path, caplog):
    caplog.set_level(logging.INFO)
    result = await handler.handle_event(_make_payload(text="hello"))
    assert result == {"ok": True}

    lines = _read_log_lines(log_path)
    assert len(lines) == 1
    entry = lines[0]
    assert entry["handled_status"] == HANDLED_RECEIVED
    assert entry["user_id"] == JOSHUA_ID
    assert entry["text"] == "hello"
    assert entry["channel_id"] == "D01CHAN01"
    assert entry["event_ts"] == "1700000000.001"
    # Chain-event-log emit fires for Joshua only.
    assert any(
        "kora.slack_dm.received" in r.getMessage() for r in caplog.records
    )


@pytest.mark.asyncio
async def test_joshua_dm_with_thread_logs_thread_ts(handler, log_path):
    payload = _make_payload(thread_ts="1700000000.000")
    await handler.handle_event(payload)
    [entry] = _read_log_lines(log_path)
    assert entry["thread_ts"] == "1700000000.000"


# ---------------------------------------------------------------------------
# Identity filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_joshua_filtered_no_emit(handler, log_path, caplog):
    caplog.set_level(logging.INFO)
    result = await handler.handle_event(_make_payload(user="USOMEONEELSE"))
    assert result == {"ok": True}

    [entry] = _read_log_lines(log_path)
    assert entry["handled_status"] == HANDLED_FILTERED_NON_JOSHUA
    assert entry["extra"]["actual_user_id"] == "USOMEONEELSE"
    # Chain-event-log emit must NOT fire for non-Joshua.
    assert not any(
        "kora.slack_dm.received" in r.getMessage() for r in caplog.records
    )


@pytest.mark.asyncio
async def test_joshua_env_unset_drops_all(handler, log_path, monkeypatch, caplog):
    """Fail-CLOSED — without JOSHUA_USER_ID env, we can't verify
    sender, so drop everything."""
    monkeypatch.delenv(JOSHUA_USER_ID_ENV, raising=False)
    caplog.set_level(logging.WARNING)
    result = await handler.handle_event(_make_payload(user=JOSHUA_ID))
    assert result == {"ok": True}

    [entry] = _read_log_lines(log_path)
    assert entry["handled_status"] == HANDLED_FILTERED_NON_JOSHUA
    assert entry["extra"]["reason"] == "joshua_id_env_unset"
    assert any(
        JOSHUA_USER_ID_ENV in r.getMessage() and "fail-CLOSED" in r.getMessage()
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Filter precedence — state gate first
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paused_state_drops_before_other_filters(
    handler, log_path, monkeypatch
):
    """Even a valid Joshua DM is dropped when PAUSED."""
    from agent.operational_state import OperationalState, PrimaryState
    from agent.operational_state_holder import OperationalStateHolder
    from agent import operational_state_holder as h_mod

    monkeypatch.setattr(
        h_mod,
        "_HOLDER",
        OperationalStateHolder(
            OperationalState(primary_state=PrimaryState.PAUSED)
        ),
    )

    await handler.handle_event(_make_payload())
    [entry] = _read_log_lines(log_path)
    assert entry["handled_status"] == HANDLED_DROPPED_PAUSED


@pytest.mark.asyncio
async def test_stopped_state_drops(handler, log_path, monkeypatch):
    from agent.operational_state import OperationalState, PrimaryState
    from agent.operational_state_holder import OperationalStateHolder
    from agent import operational_state_holder as h_mod

    monkeypatch.setattr(
        h_mod,
        "_HOLDER",
        OperationalStateHolder(
            OperationalState(primary_state=PrimaryState.STOPPED)
        ),
    )

    await handler.handle_event(_make_payload())
    [entry] = _read_log_lines(log_path)
    assert entry["handled_status"] == HANDLED_DROPPED_STOPPED


@pytest.mark.asyncio
async def test_ready_state_does_not_drop(handler, log_path, monkeypatch):
    from agent.operational_state import OperationalState, PrimaryState
    from agent.operational_state_holder import OperationalStateHolder
    from agent import operational_state_holder as h_mod

    monkeypatch.setattr(
        h_mod,
        "_HOLDER",
        OperationalStateHolder(
            OperationalState(primary_state=PrimaryState.READY)
        ),
    )

    await handler.handle_event(_make_payload())
    [entry] = _read_log_lines(log_path)
    assert entry["handled_status"] == HANDLED_RECEIVED


@pytest.mark.asyncio
async def test_active_state_does_not_drop(handler, log_path, monkeypatch):
    from agent.operational_state import OperationalState, PrimaryState
    from agent.operational_state_holder import OperationalStateHolder
    from agent import operational_state_holder as h_mod

    monkeypatch.setattr(
        h_mod,
        "_HOLDER",
        OperationalStateHolder(
            OperationalState(primary_state=PrimaryState.ACTIVE)
        ),
    )

    await handler.handle_event(_make_payload())
    [entry] = _read_log_lines(log_path)
    assert entry["handled_status"] == HANDLED_RECEIVED


# ---------------------------------------------------------------------------
# Bot / subtype / channel-type filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bot_message_filtered(handler, log_path):
    """Defense against echo-loops: if Kora's own bot ever shows up
    in the chat, ignore its messages."""
    await handler.handle_event(_make_payload(bot_id="B0KORA01"))
    [entry] = _read_log_lines(log_path)
    assert entry["handled_status"] == HANDLED_FILTERED_BOT
    assert entry["extra"]["bot_id"] == "B0KORA01"


@pytest.mark.asyncio
async def test_subtype_message_filtered(handler, log_path):
    """message_changed / message_deleted etc. — drop."""
    await handler.handle_event(_make_payload(subtype="message_changed"))
    [entry] = _read_log_lines(log_path)
    assert entry["handled_status"] == HANDLED_FILTERED_SUBTYPE
    assert entry["extra"]["subtype"] == "message_changed"


@pytest.mark.asyncio
async def test_non_message_event_filtered_as_subtype(handler, log_path):
    """app_mention / reaction_added / etc. — caught by the
    event_type != 'message' branch."""
    await handler.handle_event(_make_payload(event_type="app_mention"))
    [entry] = _read_log_lines(log_path)
    assert entry["handled_status"] == HANDLED_FILTERED_SUBTYPE
    assert entry["extra"]["event_type"] == "app_mention"


@pytest.mark.asyncio
async def test_channel_message_filtered(handler, log_path):
    """channel_type='channel' (not im) — drop."""
    await handler.handle_event(_make_payload(channel_type="channel"))
    [entry] = _read_log_lines(log_path)
    assert entry["handled_status"] == HANDLED_FILTERED_NON_IM


@pytest.mark.asyncio
async def test_group_channel_filtered(handler, log_path):
    """channel_type='group' — drop."""
    await handler.handle_event(_make_payload(channel_type="group"))
    [entry] = _read_log_lines(log_path)
    assert entry["handled_status"] == HANDLED_FILTERED_NON_IM


# ---------------------------------------------------------------------------
# Handler exception path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_internal_exception_returns_ok(
    handler, log_path, monkeypatch
):
    """If _handle_event_inner raises, the outer handle_event catches +
    logs handler_error + still returns 200 to Slack."""

    async def _boom(self, payload):
        raise RuntimeError("simulated handler failure")

    monkeypatch.setattr(SlackDMHandler, "_handle_event_inner", _boom)

    result = await handler.handle_event(_make_payload())
    assert result == {"ok": True}

    [entry] = _read_log_lines(log_path)
    assert entry["handled_status"] == HANDLED_HANDLER_ERROR
    assert "simulated handler failure" in entry["error"]


@pytest.mark.asyncio
async def test_malformed_payload_does_not_crash(handler, log_path):
    """An event missing the inner ``event`` dict shouldn't crash —
    filter logic returns the appropriate status."""
    result = await handler.handle_event({"type": "event_callback"})
    assert result == {"ok": True}
    # No 'event' dict → all extractors return None → subtype filter
    # path (event_type is None, treated as != 'message').
    [entry] = _read_log_lines(log_path)
    assert entry["handled_status"] == HANDLED_FILTERED_SUBTYPE


# ---------------------------------------------------------------------------
# JSONL format
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_jsonl_one_json_per_line(handler, log_path):
    """Multiple events → multiple lines, each parseable as JSON.

    After ST2 each identified Joshua DM also writes an outbound
    JSONL entry. 3 inbound events (2 Joshua + 1 USOMEONE) →
    5 lines total (3 inbound + 2 outbound for the Joshua pair).
    The assertion is "all lines are valid JSON" — schema branches
    on inbound (``received_at``) vs outbound (``sent_at``).
    """
    for i, user in enumerate([JOSHUA_ID, "USOMEONE", JOSHUA_ID]):
        await handler.handle_event(
            _make_payload(user=user, ts=f"170000000{i}.001")
        )
    raw = log_path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    assert len(lines) == 5  # 3 inbound + 2 outbound (Joshua only)
    inbound_count = 0
    outbound_count = 0
    for line in lines:
        entry = json.loads(line)  # parse-or-raise
        if "handled_status" in entry:
            for required in ("received_at", "user_id", "text"):
                assert required in entry
            inbound_count += 1
        else:
            for required in ("sent_at", "channel_id", "send_status"):
                assert required in entry
            outbound_count += 1
    assert inbound_count == 3
    assert outbound_count == 2


@pytest.mark.asyncio
async def test_jsonl_received_at_is_iso(handler, log_path):
    from datetime import datetime

    await handler.handle_event(_make_payload())
    [entry] = _read_log_lines(log_path)
    # Round-trip parse — no exception means it's a valid ISO timestamp.
    datetime.fromisoformat(entry["received_at"])


# ---------------------------------------------------------------------------
# SECURITY — signing secret never logged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signing_secret_never_in_jsonl(handler, log_path, monkeypatch):
    """Even though Joshua's text could theoretically contain the
    secret, the handler MUST NOT write secret material via any of
    its internal paths. The signing secret is verified-and-dropped
    upstream in the listener; we assert by simulating multiple
    events + checking the log file."""
    secret_marker = "topsecret_signing_secret_DO_NOT_LOG_ME"
    monkeypatch.setenv("KORA_SLACK_SIGNING_SECRET", secret_marker)

    # Simulate a diverse set of events that exercise every code path.
    for payload in [
        _make_payload(),  # received
        _make_payload(user="UOTHER"),  # filtered_non_joshua
        _make_payload(bot_id="B"),  # filtered_bot
        _make_payload(subtype="message_changed"),  # filtered_subtype
        _make_payload(channel_type="channel"),  # filtered_non_im
    ]:
        await handler.handle_event(payload)

    contents = log_path.read_text(encoding="utf-8")
    assert secret_marker not in contents, (
        "signing secret env value appeared in JSONL — handler must "
        "NEVER log secret material"
    )


# ---------------------------------------------------------------------------
# Log path resolution
# ---------------------------------------------------------------------------


def test_log_path_env_override(monkeypatch, tmp_path):
    """KORA_SLACK_DM_LOG_PATH env wins over the default."""
    override = tmp_path / "custom.jsonl"
    monkeypatch.setenv("KORA_SLACK_DM_LOG_PATH", str(override))
    handler_obj = SlackDMHandler()
    assert handler_obj._log_path == override


def test_log_path_default_uses_kora_home(monkeypatch, tmp_path):
    """No env override → resolves via kora_constants.get_kora_home()."""
    monkeypatch.delenv("KORA_SLACK_DM_LOG_PATH", raising=False)
    import kora_constants

    monkeypatch.setattr(kora_constants, "get_kora_home", lambda: tmp_path)
    handler_obj = SlackDMHandler()
    assert handler_obj._log_path == tmp_path / "slack_dm_log.jsonl"
