"""Tests for the KR-SLACK-DM-PANEL stub endpoint.

Bucket §3 scenarios:
  1. GET /api/slack-dm/recent returns 200
  2. Top-level shape (messages + stub:true + generated_at +
     total_recent_24h + by_direction_24h + by_status_24h)
  3. 4 representative stub messages present
  4. Stub spans inbound + outbound + filtered_non_joshua so the
     operator's first look surfaces filtering posture
  5. Per-entry shape + valid direction + valid handled_status enum
  6. SECURITY: no raw Slack user IDs (U[A-Z0-9]{8,}) anywhere in
     payload — walk-whole-payload guard
  7. SECURITY: no Slack token shapes (xoxb-/xoxp-/xapp-/xoxa-) or
     signing-secret-shaped hex runs anywhere in payload — walk-whole
  8. channel_id v1 stub shape (D_<label>)
  9. SECURITY: companion FE pin — SlackDMPanel.tsx never uses
     dangerouslySetInnerHTML for message text (comment-stripped grep)
 10. by_direction_24h sum reconciles to total_recent_24h
 11. Cron-regression sanity
"""

import re
from pathlib import Path

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

# Raw Slack user IDs are "U" + 8+ uppercase alphanumerics
# (e.g. U01ABCDEF). The payload's user_id_label is a LABEL only
# (joshua / kora_bot / unknown_user); a Slack ID showing up would
# signal the backend leaked the raw value through.
_RAW_SLACK_USER_ID = re.compile(r"\bU[A-Z0-9]{8,}\b")

# Slack token shapes — bot tokens (xoxb-), user tokens (xoxp-),
# app tokens (xapp-), legacy/test tokens (xoxa-), workflow tokens
# (xoxr-). Match the prefix + at least the first segment so we
# catch real tokens but not the bare "xoxb-" word in code.
_SLACK_TOKEN_SHAPE = re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{8,}\b")

# Slack signing secrets are 32-char hex strings. The DM endpoint
# should never carry one — they live in env-var settings, not the
# wire format. Catch any 32-char hex run as a precaution.
_SIGNING_SECRET_SHAPE = re.compile(r"\b[0-9a-f]{32}\b")

# Bucket §2(a): v1 channel_id is a STUB label of shape D_<label>.
# Real channel IDs (C…, G…, D… + 8+ alphanumerics) must be
# hashed/truncated by CC#3 when real data flips; this pin ensures
# the v1 stub doesn't accidentally use a real-shaped value.
_CHANNEL_ID_STUB = re.compile(r"^D_[A-Z0-9]+$")
_REAL_SLACK_CHANNEL_SHAPE = re.compile(r"\b[CGD][A-Z0-9]{8,}\b")


_REPO_ROOT = Path(__file__).resolve().parents[2]
_PANEL_PATH = _REPO_ROOT / "web" / "src" / "pages" / "SlackDMPanel.tsx"


def _strip_ts_comments(src: str) -> str:
    """Strip /* … */ block comments, // line comments, and {/* … */}
    JSX block comments so source-pin tests check live code only, not
    explanatory prose that may legitimately mention the banned pattern.
    """
    src = re.sub(r"\{/\*.*?\*/\}", "", src, flags=re.DOTALL)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"(^|[^:])//[^\n]*", r"\1", src)
    return src


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


# ---- 1. 200 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_returns_200(_isolate_config):
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    assert isinstance(result, dict)


# ---- 2. Top-level shape ---------------------------------------------


@pytest.mark.asyncio
async def test_response_shape_has_required_keys(_isolate_config):
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
    assert isinstance(result["messages"], list)
    assert isinstance(result["generated_at"], str)
    assert isinstance(result["total_recent_24h"], int)
    assert isinstance(result["by_direction_24h"], dict)
    assert isinstance(result["by_status_24h"], dict)
    assert result["stub"] is True


# ---- 3. Expected stub messages --------------------------------------


@pytest.mark.asyncio
async def test_stub_returns_four_representative_messages(_isolate_config):
    """Pin the bucket §2(a) canonical 4-message stub list. CC#3's
    KR-FEAT-SLACK-DM ST2 will swap the body to read from
    ${HERMES_HOME}/slack_dm_log.jsonl, but the shape stays stable
    so the FE keeps rendering during cut-over."""
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    assert len(result["messages"]) == 4
    ids = {m["id"] for m in result["messages"]}
    assert ids == {"stub-1", "stub-2", "stub-3", "stub-4"}


@pytest.mark.asyncio
async def test_stub_spans_inbound_outbound_and_filtered(_isolate_config):
    """The 4 stub messages deliberately span inbound + outbound +
    filtered_non_joshua so the operator's first look shows what the
    filtering posture looks like. Pin so a future stub edit can't
    accidentally homogenize."""
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    directions = {m["direction"] for m in result["messages"]}
    statuses = {m["handled_status"] for m in result["messages"]}
    assert directions == {"inbound", "outbound"}
    assert "filtered_non_joshua" in statuses
    assert "received" in statuses
    assert "sent_ok" in statuses


# ---- 4. Per-entry shape + enums ------------------------------------


@pytest.mark.asyncio
async def test_each_message_has_required_keys_and_valid_enums(_isolate_config):
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    required = {
        "id",
        "direction",
        "timestamp",
        "channel_id",
        "thread_ts",
        "user_id_label",
        "text",
        "handled_status",
    }
    for msg in result["messages"]:
        assert set(msg.keys()) == required, (
            f"{msg.get('id', '?')}: keys mismatch {set(msg.keys())}"
        )
        assert msg["direction"] in _VALID_DIRECTION, (
            f"{msg['id']}: direction={msg['direction']!r}"
        )
        assert msg["handled_status"] in _VALID_STATUS, (
            f"{msg['id']}: handled_status={msg['handled_status']!r} not in "
            f"{_VALID_STATUS}"
        )
        assert isinstance(msg["timestamp"], str) and msg["timestamp"].endswith("Z")
        assert isinstance(msg["channel_id"], str)
        # thread_ts is either null or a Slack timestamp string
        assert msg["thread_ts"] is None or isinstance(msg["thread_ts"], str)
        assert isinstance(msg["user_id_label"], str) and msg["user_id_label"]
        assert isinstance(msg["text"], str)


# ---- 5. SECURITY: no raw Slack user IDs in payload -----------------


@pytest.mark.asyncio
async def test_user_id_label_is_label_not_raw_slack_id(_isolate_config):
    """Bucket §2(a) HARD CONSTRAINT: user_id_label is a LABEL
    (joshua / kora_bot / unknown_user) — never a raw Slack user ID
    (U + 8+ uppercase alphanumerics). If the real handler ever
    defaults to the raw Slack ID when no label resolves, this
    guard catches the leak before it ships to the operator's
    browser."""
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    for msg in result["messages"]:
        label = msg["user_id_label"]
        assert _RAW_SLACK_USER_ID.search(label) is None, (
            f"{msg['id']}: user_id_label={label!r} matches raw Slack "
            f"user ID shape — contract requires a human label only"
        )


@pytest.mark.asyncio
async def test_no_raw_slack_user_ids_anywhere_in_payload(_isolate_config):
    """Walk-the-whole-payload guard (standardizing the pattern from
    KR-WEBHOOK-EVENTS / KR-AGENT-ACTIVITY). Asserts no field
    anywhere in the response — top-level, per-message, or any
    nested dict — contains a raw Slack user ID shape. Catches a
    future drift like adding "raw_user_id" diagnostic or stuffing
    a user ID into the message text."""
    from kora_cli import web_server
    import json as _json

    result = await web_server.list_recent_slack_dm()
    blob = _json.dumps(result)
    leaks = _RAW_SLACK_USER_ID.findall(blob)
    assert leaks == [], (
        f"payload contains raw Slack user ID(s): {leaks} — Slack "
        f"user IDs are PII and must be resolved to labels at the API "
        f"edge (bucket §2(a) SECURITY contract)"
    )


# ---- 6. SECURITY: no Slack token / signing-secret shapes -----------


@pytest.mark.asyncio
async def test_no_slack_token_shapes_anywhere_in_payload(_isolate_config):
    """Bucket §2(a) SECURITY: walk-payload regex catching xoxb-/xoxp-/
    xapp-/xoxa-/xoxr- Slack token shapes anywhere. A backend bug or
    future log entry that leaks creds gets caught at the API edge,
    not in the operator's browser (where it could end up in
    diag-bundles, screenshots, etc.)."""
    from kora_cli import web_server
    import json as _json

    result = await web_server.list_recent_slack_dm()
    blob = _json.dumps(result)
    leaks = _SLACK_TOKEN_SHAPE.findall(blob)
    assert leaks == [], (
        f"payload contains Slack token shape(s): {leaks} — Slack "
        f"tokens must never appear in API responses (bucket §2(a) "
        f"SECURITY contract layer 4)"
    )


@pytest.mark.asyncio
async def test_no_signing_secret_shapes_anywhere_in_payload(_isolate_config):
    """Belt+braces companion to the Slack-token guard: 32-char hex
    runs are the shape of a Slack signing secret. The DM endpoint
    has no business carrying one — they live in env-var settings,
    not in the wire format."""
    from kora_cli import web_server
    import json as _json

    result = await web_server.list_recent_slack_dm()
    blob = _json.dumps(result)
    leaks = _SIGNING_SECRET_SHAPE.findall(blob)
    assert leaks == [], (
        f"payload contains 32-char hex string(s): {leaks} — these "
        f"shape-match a Slack signing secret; the DM endpoint must "
        f"never carry one"
    )


# ---- 7. channel_id stub shape pinning ------------------------------


@pytest.mark.asyncio
async def test_channel_id_uses_stub_label_format(_isolate_config):
    """Bucket §2(a): v1 channel_id is a STUB label of shape D_<label>
    (e.g. D_STUB1, D_STUB2). Real Slack channel IDs (C…/G…/D… +
    8+ alphanumerics) must be hashed/truncated by CC#3 before the
    flip — this pin ensures the v1 stub doesn't accidentally use a
    real-shaped value that would mask the missing-redaction step."""
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    for msg in result["messages"]:
        cid = msg["channel_id"]
        assert _CHANNEL_ID_STUB.match(cid), (
            f"{msg['id']}: channel_id={cid!r} doesn't match the v1 stub "
            f"shape D_<label>"
        )
        # And explicitly: must NOT match the real Slack channel shape
        assert not _REAL_SLACK_CHANNEL_SHAPE.match(cid), (
            f"{msg['id']}: channel_id={cid!r} looks like a real Slack "
            f"channel ID — v1 stub must use the D_<label> placeholder"
        )


# ---- 8. SECURITY: companion FE pin for plain-text rendering --------


def test_panel_uses_no_dangerously_set_inner_html_for_message_text():
    """Bucket §2(a) layer 3: message text rendered as PLAIN TEXT.
    React's default child escaping handles this — but this guard
    catches a future edit that flips to dangerouslySetInnerHTML
    for "richer message formatting". Real DM text may contain
    arbitrary user-typed content (Markdown, HTML, scripts); an
    HTML-injection vector here would execute attacker-controlled
    content in the operator's browser."""
    code = _strip_ts_comments(_PANEL_PATH.read_text())
    assert "dangerouslySetInnerHTML" not in code, (
        "SlackDMPanel.tsx must not use dangerouslySetInnerHTML — "
        "message text is arbitrary user-typed content and must "
        "render as plain text only"
    )


def test_panel_renders_message_text_as_child_text_node():
    """Belt+braces complement: confirm message.text is rendered as a
    JSX child (escaped) not assigned to an innerHTML attribute or
    piped through a markdown lib. The collapsed view uses
    truncateText(message.text); both paths must be plain child
    expressions."""
    src = _PANEL_PATH.read_text()
    # message.text must be referenced as a JSX child expression
    # somewhere in the file. The exact wrapping varies (ternary
    # between expanded/collapsed paths), so pin the substring rather
    # than the full JSX shape.
    assert "message.text" in src, (
        "SlackDMPanel.tsx should reference message.text in the render"
    )
    # Collapsed path: via truncateText, also as a child expression —
    # this pins that the truncation goes through the pure helper, not
    # an inline regex or a markdown-aware renderer.
    assert "truncateText(message.text)" in src, (
        "SlackDMPanel.tsx should render the truncated message text "
        "via the pure truncateText() helper (no formatting)"
    )
    # Confirm message.text appears inside a JSX expression container
    # at least once (i.e. between { and } in a JSX position), as a
    # weak structural guarantee that it's used as a child not an
    # innerHTML-assigned attribute string.
    assert re.search(r"\{[^{}]*message\.text[^{}]*\}", src), (
        "message.text should appear inside a JSX expression "
        "container, indicating it's rendered as a child / value"
    )


# ---- 9. by_direction_24h reconciliation ---------------------------


@pytest.mark.asyncio
async def test_by_direction_24h_sum_reconciles_to_total(_isolate_config):
    """The 24h direction breakdown must sum to total_recent_24h —
    otherwise the dashboard card's "X msgs / Y drops" headline
    won't reconcile to the panel's per-direction breakdown."""
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    direction_sum = sum(result["by_direction_24h"].values())
    assert direction_sum == result["total_recent_24h"], (
        f"by_direction_24h sums to {direction_sum} but "
        f"total_recent_24h is {result['total_recent_24h']}"
    )
    assert set(result["by_direction_24h"].keys()) == _VALID_DIRECTION


@pytest.mark.asyncio
async def test_by_status_24h_only_contains_valid_status_values(_isolate_config):
    """Every key in the status breakdown must be a documented
    handled_status enum value — keeps the dashboard's status
    pivot reconcilable with the per-message badges."""
    from kora_cli import web_server

    result = await web_server.list_recent_slack_dm()
    invalid = set(result["by_status_24h"].keys()) - _VALID_STATUS
    assert not invalid, (
        f"by_status_24h has unknown status key(s): {invalid} — must "
        f"be drawn from {_VALID_STATUS}"
    )


# ---- 10. Cron-regression sanity ------------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_slack_dm_registered(_isolate_config):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
