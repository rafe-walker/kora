"""Tests for the KR-SLACK-DM-PANEL-FLIP endpoint.

After PR #134's stub shipped, the endpoint reads from the live
``${KORA_HOME}/slack_dm_log.jsonl`` written by the handler in
``kora_cli/handlers/slack_dm_handler.py`` (PR #119 inbound, #122
outbound, #131 reasoning meta, #130 caller_actor_kind). The
4-layer security contract from the original panel is preserved.

Scenarios:
  1. Empty / missing JSONL → empty list + stub:false
  2. Single inbound entry → projects with handled_status pass-through
  3. Single outbound entry → projects with handled_status=sent_<send_status>
  4. user_id_label resolution: joshua / kora_bot / unknown_user
  5. Newest-first ordering by timestamp
  6. ?limit query param respected; capped at 200
  7. Malformed JSONL line → logged + skipped; other lines parsed
  8. JSON-object-but-not-dict (e.g., array on a line) → skipped
  9. Entry missing both received_at + sent_at → skipped (defensive)
 10. channel_id_truncated companion field present + correctly masked
 11. SECURITY: user_id_label is a label, NOT raw U... Slack user ID
 12. SECURITY: walk-payload sweep finds no raw U... anywhere
 13. SECURITY: walk-payload sweep finds no xoxb-/xoxp- Slack token shapes
 14. SECURITY: channel_id starts with "D" (DM channel shape)
 15. SECURITY: FE companion pin — no dangerouslySetInnerHTML
 16. SECURITY: FE companion pin — text rendered as JSX child node
 17. by_direction_24h sum reconciles to total_recent_24h
 18. by_status_24h keys are drawn from the valid enum
 19. Aggregate counts use 24h window (not the limited slice)
 20. Cron-regression sanity
"""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest


_VALID_DIRECTION = {"inbound", "outbound"}
_VALID_STATUS = {
    "received",
    "sent_ok",
    "sent_failed",
    "filtered_non_joshua",
    "filtered_bot",
    "filtered_subtype",
    "handler_error",
    "dropped_paused",
}

_RAW_SLACK_USER_ID = re.compile(r"\bU[A-Z0-9]{8,}\b")
_SLACK_TOKEN_SHAPE = re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{8,}\b")
_SIGNING_SECRET_SHAPE = re.compile(r"\b[0-9a-f]{32}\b")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PANEL_PATH = _REPO_ROOT / "web" / "src" / "pages" / "SlackDMPanel.tsx"


def _strip_ts_comments(src: str) -> str:
    src = re.sub(r"\{/\*.*?\*/\}", "", src, flags=re.DOTALL)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"(^|[^:])//[^\n]*", r"\1", src)
    return src


# ---- Test env isolation + JSONL fixture helpers --------------------


# Match the env-var name the handler reads (slack_dm_handler.py:59).
_JOSHUA_USER_ID = "U01JOSHUA"
_OTHER_USER_ID = "U02OTHER"
_DM_CHANNEL = "D0123456789ABCDEF"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated KORA_HOME + Joshua env var set to a known U... ID.

    Monkeypatches the ``get_kora_home`` symbol DIRECTLY in
    ``kora_cli.web_server`` (where the endpoint imports it via
    ``from kora_cli.config import get_kora_home`` — a copy in the
    module namespace, not a transparent re-export). Also patches the
    upstream ``kora_constants`` reference for code paths that import
    it from there. Doing both belt-and-braces because the
    set_kora_home_override ContextVar approach leaks across parallel
    pytest-xdist tests within the same worker process — each test
    rebinds the override but the ContextVar chain isn't restored
    cleanly across multiple fixtures, leaving stale state.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setenv("KORA_SLACK_JOSHUA_USER_ID", _JOSHUA_USER_ID)
    monkeypatch.setattr(
        "kora_cli.config.get_config_path",
        lambda: tmp_path / "config.yaml",
    )
    monkeypatch.setattr(
        "kora_cli.config.get_env_path", lambda: tmp_path / ".env"
    )
    monkeypatch.setattr("kora_constants.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr("kora_cli.config.get_kora_home", lambda: tmp_path)
    # Critical: the endpoint resolves get_kora_home from its own
    # module namespace (line 5233), not via a fresh import. Monkeypatch
    # there too.
    monkeypatch.setattr("kora_cli.web_server.get_kora_home", lambda: tmp_path)
    return tmp_path


def write_log(env_path: Path, lines: List[Dict[str, Any]]) -> Path:
    """Write a JSONL fixture at ``${KORA_HOME}/slack_dm_log.jsonl``."""
    log_path = env_path / "slack_dm_log.jsonl"
    with log_path.open("w", encoding="utf-8") as f:
        for entry in lines:
            f.write(json.dumps(entry) + "\n")
    return log_path


def _iso(minutes_ago: int = 0) -> str:
    """ISO-8601 UTC Z-suffixed, N minutes in the past. Matches the
    writer's ``_now_iso()`` output shape."""
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _inbound(
    *,
    user_id: str = _JOSHUA_USER_ID,
    text: str = "hello",
    handled_status: str = "received",
    minutes_ago: int = 5,
    thread_ts: Any = None,
    channel_id: str = _DM_CHANNEL,
) -> Dict[str, Any]:
    """Inbound JSONL entry shape per slack_dm_handler.py:302-310."""
    return {
        "received_at": _iso(minutes_ago),
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "user_id": user_id,
        "text": text,
        "event_ts": f"{int(datetime.now().timestamp())}.000000",
        "handled_status": handled_status,
    }


def _outbound(
    *,
    text: str = "ack",
    send_status: str = "ok",
    minutes_ago: int = 4,
    thread_ts: str = "1779380123.456",
    channel_id: str = _DM_CHANNEL,
    **extra: Any,
) -> Dict[str, Any]:
    """Outbound JSONL entry shape per slack_dm_handler.py:775-805."""
    entry: Dict[str, Any] = {
        "sent_at": _iso(minutes_ago),
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "text": text,
        "slack_message_ts": f"{int(datetime.now().timestamp())}.000001",
        "send_status": send_status,
    }
    entry.update(extra)
    return entry


# ---- 1. Endpoint always returns stub:false (no more hardcoded stub)


@pytest.mark.asyncio
async def test_endpoint_returns_200_with_stub_false(env):
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    assert isinstance(result, dict)
    assert result["stub"] is False, (
        "Post-flip endpoint must always return stub:false (no more "
        "hardcoded sample data)"
    )


@pytest.mark.asyncio
async def test_empty_log_returns_empty_messages_list(env):
    """Fresh daemon with no DMs yet → log file missing → empty list
    + stub:false. NOT an error; FE renders existing empty-state UI."""
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    assert result["messages"] == []
    assert result["total_recent_24h"] == 0
    assert result["stub"] is False


# ---- 2. Top-level shape ---------------------------------------------


@pytest.mark.asyncio
async def test_response_shape_has_required_keys(env):
    write_log(env, [_inbound()])
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    assert set(result.keys()) == {
        "messages",
        "stub",
        "generated_at",
        "total_recent_24h",
        "by_direction_24h",
        "by_status_24h",
    }


# ---- 3. Inbound entry projection -----------------------------------


@pytest.mark.asyncio
async def test_inbound_entry_projects_to_inbound_direction(env):
    write_log(env, [_inbound(handled_status="received")])
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    assert len(result["messages"]) == 1
    msg = result["messages"][0]
    assert msg["direction"] == "inbound"
    assert msg["handled_status"] == "received"
    assert msg["user_id_label"] == "joshua"
    assert msg["channel_id"] == _DM_CHANNEL


@pytest.mark.asyncio
async def test_inbound_filtered_non_joshua_passes_status_through(env):
    """handled_status enum values flow through unchanged for inbound."""
    write_log(env, [
        _inbound(user_id=_OTHER_USER_ID, handled_status="filtered_non_joshua")
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    msg = result["messages"][0]
    assert msg["direction"] == "inbound"
    assert msg["handled_status"] == "filtered_non_joshua"
    assert msg["user_id_label"] == "unknown_user", (
        "Non-Joshua inbound must label as unknown_user — never echo "
        "the raw user_id"
    )


# ---- 4. Outbound entry projection ----------------------------------


@pytest.mark.asyncio
async def test_outbound_entry_projects_to_outbound_direction(env):
    write_log(env, [_outbound(send_status="ok")])
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    msg = result["messages"][0]
    assert msg["direction"] == "outbound"
    assert msg["user_id_label"] == "kora_bot", (
        "Outbound always Kora's bot identity"
    )
    assert msg["handled_status"] == "sent_ok", (
        "Outbound send_status:ok → handled_status:sent_ok per FE enum"
    )


@pytest.mark.asyncio
async def test_outbound_failed_status_maps_to_sent_failed(env):
    write_log(env, [_outbound(send_status="failed", failure_reason="rate_limited")])
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    msg = result["messages"][0]
    assert msg["handled_status"] == "sent_failed"


@pytest.mark.asyncio
async def test_outbound_with_reasoning_meta_still_projects(env):
    """PR #131 outbound entries may include model_used / tokens /
    reasoning_error. The projection function must ignore these
    gracefully — extra fields don't break parsing."""
    write_log(env, [
        _outbound(
            model_used="claude-opus-4-7",
            input_tokens=120,
            output_tokens=34,
            reasoning_duration_ms=890,
        )
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    assert len(result["messages"]) == 1


@pytest.mark.asyncio
async def test_outbound_with_caller_actor_kind_still_projects(env):
    """PR #130 may add caller_actor_kind to outbound entries."""
    write_log(env, [_outbound(caller_actor_kind="kora_drone_7")])
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    assert len(result["messages"]) == 1


# ---- 5. user_id_label resolution ----------------------------------


@pytest.mark.asyncio
async def test_user_id_label_resolves_joshua_via_env_match(env):
    write_log(env, [_inbound(user_id=_JOSHUA_USER_ID)])
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    assert result["messages"][0]["user_id_label"] == "joshua"


@pytest.mark.asyncio
async def test_user_id_label_falls_back_to_unknown_user(env):
    """Non-Joshua inbound (or Joshua env unset) → unknown_user. NEVER
    the raw user_id."""
    write_log(env, [_inbound(user_id=_OTHER_USER_ID)])
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    msg = result["messages"][0]
    assert msg["user_id_label"] == "unknown_user"
    assert _OTHER_USER_ID not in msg["user_id_label"]


# ---- 6. Newest-first ordering -------------------------------------


@pytest.mark.asyncio
async def test_newest_first_ordering(env):
    """File-on-disk order doesn't matter — endpoint sorts by timestamp
    descending so the newest event is messages[0]."""
    write_log(env, [
        _inbound(text="oldest", minutes_ago=30),
        _outbound(text="middle", minutes_ago=20),
        _inbound(text="newest", minutes_ago=5),
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    texts = [m["text"] for m in result["messages"]]
    assert texts == ["newest", "middle", "oldest"]


# ---- 7. ?limit query param + 200 cap ------------------------------


@pytest.mark.asyncio
async def test_limit_query_param_caps_returned_messages(env):
    write_log(env, [
        _inbound(text=f"msg-{i}", minutes_ago=i) for i in range(20)
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm(limit=5)
    assert len(result["messages"]) == 5


@pytest.mark.asyncio
async def test_limit_query_param_capped_at_200(env):
    """Performance guard: callers can't request unbounded message
    counts — limit=99999 caps at 200."""
    write_log(env, [
        _inbound(text=f"msg-{i}", minutes_ago=i) for i in range(5)
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm(limit=99999)
    assert len(result["messages"]) <= 200


@pytest.mark.asyncio
async def test_limit_zero_or_negative_clamps_to_at_least_one(env):
    """Defensive: limit=0 / limit=-5 shouldn't return everything OR
    crash. Clamp to a minimum of 1."""
    write_log(env, [_inbound()])
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm(limit=0)
    assert isinstance(result["messages"], list)


# ---- 8. Malformed JSONL handling ----------------------------------


@pytest.mark.asyncio
async def test_malformed_json_line_skipped_other_lines_parsed(env, caplog):
    """A corrupt line (partial write / disk-full truncation) must
    not bring down the endpoint. Log + skip; other entries return."""
    log_path = env / "slack_dm_log.jsonl"
    with log_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(_inbound(text="before")) + "\n")
        f.write("{NOT VALID JSON{{{\n")
        f.write(json.dumps(_inbound(text="after")) + "\n")

    from kora_cli import web_server

    import logging
    with caplog.at_level(logging.WARNING):
        result = await web_server.list_recent_slack_dm()

    texts = sorted(m["text"] for m in result["messages"])
    assert texts == ["after", "before"]


@pytest.mark.asyncio
async def test_json_array_line_skipped(env):
    """A line that parses as JSON but is an array (not a dict) gets
    skipped — projection expects dict shape."""
    log_path = env / "slack_dm_log.jsonl"
    with log_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(_inbound(text="ok-1")) + "\n")
        f.write(json.dumps([1, 2, 3]) + "\n")
        f.write(json.dumps(_inbound(text="ok-2")) + "\n")

    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    texts = sorted(m["text"] for m in result["messages"])
    assert texts == ["ok-1", "ok-2"]


@pytest.mark.asyncio
async def test_entry_missing_both_received_at_and_sent_at_skipped(env):
    """Defensive against partial-shape entries (handler bug or
    forward-compat experiment). Skip without crashing."""
    log_path = env / "slack_dm_log.jsonl"
    with log_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(_inbound()) + "\n")
        f.write(json.dumps({"random_field": "value"}) + "\n")

    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    assert len(result["messages"]) == 1


@pytest.mark.asyncio
async def test_empty_lines_skipped(env):
    """Blank lines in JSONL files are common (manual editing). Must
    be tolerated."""
    log_path = env / "slack_dm_log.jsonl"
    with log_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(_inbound(text="one")) + "\n")
        f.write("\n")
        f.write("   \n")
        f.write(json.dumps(_inbound(text="two")) + "\n")

    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    assert len(result["messages"]) == 2


# ---- 9. channel_id_truncated companion field ----------------------


@pytest.mark.asyncio
async def test_channel_id_truncated_field_present_and_masked(env):
    """Per spec §2(b): backend exposes the masked form. FE follow-on
    bucket (KR-SLACK-DM-PANEL-CHANNEL-MASK) renders it. Pinned so
    the field stays available for the follow-on PR to consume."""
    write_log(env, [_inbound(channel_id="D0123456789ABCDEF")])
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    msg = result["messages"][0]
    assert "channel_id_truncated" in msg
    assert msg["channel_id_truncated"] == "D012…CDEF", (
        f"channel_id_truncated should mask middle: got "
        f"{msg['channel_id_truncated']!r}"
    )
    assert msg["channel_id"] == "D0123456789ABCDEF"


@pytest.mark.asyncio
async def test_short_channel_id_passes_through_unmasked(env):
    """Pathological short channel IDs (test fixtures, weird daemons)
    pass through unchanged — masking a short string would expose
    more by implication."""
    write_log(env, [_inbound(channel_id="DABC")])
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    msg = result["messages"][0]
    assert msg["channel_id_truncated"] == "DABC"


# ---- 10. SECURITY: user_id_label layer 1 ---------------------------


@pytest.mark.asyncio
async def test_user_id_label_never_contains_raw_slack_id(env):
    """Per-field SECURITY pin: the resolved user_id_label must
    NEVER be a raw U... shape, regardless of writer behavior."""
    write_log(env, [
        _inbound(user_id=_JOSHUA_USER_ID),
        _inbound(user_id=_OTHER_USER_ID, handled_status="filtered_non_joshua"),
        _inbound(user_id="UABCDEFG12", handled_status="filtered_non_joshua"),
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    for msg in result["messages"]:
        label = msg["user_id_label"]
        assert _RAW_SLACK_USER_ID.search(label) is None, (
            f"user_id_label={label!r} matches raw Slack user ID shape"
        )


@pytest.mark.asyncio
async def test_no_raw_slack_user_ids_anywhere_in_payload(env):
    """Walk-payload SECURITY sweep: regardless of writer behavior,
    NO raw U... pattern appears anywhere in the response. Catches
    a future drift where the projector accidentally surfaces
    entry.user_id into a diagnostic field."""
    write_log(env, [
        _inbound(user_id=_JOSHUA_USER_ID),
        _inbound(user_id=_OTHER_USER_ID, handled_status="filtered_non_joshua"),
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    blob = json.dumps(result)
    leaks = _RAW_SLACK_USER_ID.findall(blob)
    assert leaks == [], (
        f"payload contains raw Slack user ID(s): {leaks}"
    )


# ---- 11. SECURITY: Slack token shapes -----------------------------


@pytest.mark.asyncio
async def test_no_slack_token_shapes_anywhere_in_payload(env):
    """Walk-payload guard for xoxb-/xoxp- token shapes — backend
    bug or future field that leaks credential material gets caught."""
    write_log(env, [_inbound(text="legitimate user message")])
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    blob = json.dumps(result)
    leaks = _SLACK_TOKEN_SHAPE.findall(blob)
    assert leaks == [], (
        f"payload contains Slack token shape(s): {leaks}"
    )


@pytest.mark.asyncio
async def test_no_signing_secret_shapes_anywhere_in_payload(env):
    """32-char hex run = Slack signing secret shape."""
    write_log(env, [_inbound()])
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    blob = json.dumps(result)
    leaks = _SIGNING_SECRET_SHAPE.findall(blob)
    assert leaks == [], (
        f"payload contains 32-char hex string(s): {leaks}"
    )


# ---- 12. SECURITY: channel_id starts with D ----------------------


@pytest.mark.asyncio
async def test_channel_id_starts_with_d_for_dm_channels(env):
    """Per spec §2(b): real DM channel IDs start with D. Pin so a
    future writer change (e.g., starting to log group/channel chat
    accidentally) gets caught — DM-panel should only ever show DMs."""
    write_log(env, [
        _inbound(channel_id="D0123456789ABCDEF"),
        _outbound(channel_id="D0123456789ABCDEF"),
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    for msg in result["messages"]:
        assert msg["channel_id"].startswith("D"), (
            f"channel_id={msg['channel_id']!r} should be a Slack DM "
            f"channel (D-prefix). The DM panel should only ever show "
            f"DM channels — group/public channels would leak via this "
            f"endpoint."
        )


# ---- 13. SECURITY: FE companion pins ------------------------------


def test_panel_uses_no_dangerously_set_inner_html_for_message_text():
    code = _strip_ts_comments(_PANEL_PATH.read_text())
    assert "dangerouslySetInnerHTML" not in code


def test_panel_renders_message_text_as_child_text_node():
    src = _PANEL_PATH.read_text()
    assert "message.text" in src
    assert "truncateText(message.text)" in src
    assert re.search(r"\{[^{}]*message\.text[^{}]*\}", src)


# ---- 14. Aggregate reconciliation --------------------------------


@pytest.mark.asyncio
async def test_by_direction_24h_sum_reconciles_to_total(env):
    write_log(env, [
        _inbound(text="i1"),
        _inbound(text="i2"),
        _outbound(text="o1"),
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    direction_sum = sum(result["by_direction_24h"].values())
    assert direction_sum == result["total_recent_24h"]


@pytest.mark.asyncio
async def test_by_status_24h_only_contains_valid_status_values(env):
    write_log(env, [
        _inbound(handled_status="received"),
        _outbound(send_status="ok"),
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    invalid = set(result["by_status_24h"].keys()) - _VALID_STATUS
    assert not invalid, (
        f"by_status_24h has unknown status key(s): {invalid}"
    )


@pytest.mark.asyncio
async def test_aggregate_counts_use_full_24h_window_not_limited_slice(env):
    """If limit=5 but 20 entries fell within 24h, total_recent_24h
    must still be 20. Otherwise the headline number on the dashboard
    misrepresents activity."""
    write_log(env, [
        _inbound(text=f"msg-{i}", minutes_ago=i) for i in range(20)
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm(limit=5)
    assert len(result["messages"]) == 5
    assert result["total_recent_24h"] == 20


@pytest.mark.asyncio
async def test_entries_older_than_24h_excluded_from_aggregate(env):
    """An entry from 25h ago appears in the messages list (no
    timestamp filter on the slice) but doesn't count toward
    total_recent_24h."""
    write_log(env, [
        _inbound(text="recent", minutes_ago=10),
        _inbound(text="ancient", minutes_ago=25 * 60),
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    assert result["total_recent_24h"] == 1


# ---- 15. Cron-regression sanity ----------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_slack_dm_registered(env):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
