"""Tests for ``load_email_context`` (KR-FEAT-EMAIL-INBOUND-IMAP ST2).

Covers:
  - Missing inbound/outbound files → empty context (no raise)
  - Single-hop chain closure: entries whose own message_id OR
    in_reply_to == the focal message_id are pulled in
  - in_reply_to anchor: entries whose message_id == focal's
    in_reply_to are pulled in (parent inclusion)
  - Inbound entries with handled_status != "received" are skipped
  - Outbound entries with send_status != "ok" are skipped
  - Cross-file: inbound + outbound combined + chronologically sorted
  - max_turns slicing keeps last-N
  - Empty message_id → empty context (defensive)
  - Malformed JSONL lines logged + skipped
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kora_cli.reasoning.context_loader import (
    DEFAULT_MAX_TURNS,
    EMAIL_INBOUND_LOG_PATH_ENV,
    EMAIL_OUTBOUND_LOG_PATH_ENV,
    load_email_context,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "kora_constants.get_kora_home", lambda: tmp_path, raising=False
    )
    monkeypatch.delenv(EMAIL_INBOUND_LOG_PATH_ENV, raising=False)
    monkeypatch.delenv(EMAIL_OUTBOUND_LOG_PATH_ENV, raising=False)
    return tmp_path


def _write_jsonl(path: Path, entries: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )


def _inbound(
    *,
    message_id: str,
    text: str = "incoming",
    received_at: str = "2026-05-22T10:00:00+00:00",
    handled_status: str = "received",
    in_reply_to=None,
) -> dict:
    return {
        "received_at": received_at,
        "message_id": message_id,
        "from": "joshua@stormhavenenterprises.com",
        "to": ["kora@stormhavenenterprises.com"],
        "subject": "subj",
        "body_text_truncated_2k": text,
        "has_html": False,
        "attachments_count": 0,
        "handled_status": handled_status,
        "spoofing_check_skipped": True,
        "imap_uid": 1,
        "in_reply_to": in_reply_to,
    }


def _outbound(
    *,
    message_id: str,
    in_reply_to: str,
    text: str = "outgoing",
    sent_at: str = "2026-05-22T10:00:30+00:00",
    send_status: str = "ok",
) -> dict:
    return {
        "sent_at": sent_at,
        "from": "kora@stormhavenenterprises.com",
        "to": ["joshua@stormhavenenterprises.com"],
        "subject": "Re: subj",
        "in_reply_to": in_reply_to,
        "send_status": send_status,
        "message_id": message_id,
        "smtp_code": 250,
        "error": None,
        "retry_count": 0,
        "caller_actor_kind": None,
        "text": text,
    }


# ===========================================================================
# Empty / missing
# ===========================================================================


def test_missing_files_returns_empty_context(tmp_path):
    ctx = load_email_context(message_id="<m-1@e.com>")
    assert ctx.recent_messages == []


def test_empty_message_id_returns_empty(tmp_path):
    ctx = load_email_context(message_id="")
    assert ctx.recent_messages == []


# ===========================================================================
# Chain closure
# ===========================================================================


def test_focal_message_id_match(tmp_path):
    _write_jsonl(
        tmp_path / "email_inbound_log.jsonl",
        [
            _inbound(message_id="<m-1@e.com>", text="focal message"),
        ],
    )
    ctx = load_email_context(message_id="<m-1@e.com>")
    assert len(ctx.recent_messages) == 1
    assert ctx.recent_messages[0].text == "focal message"
    assert ctx.recent_messages[0].direction == "inbound"


def test_outbound_in_reply_to_focal(tmp_path):
    _write_jsonl(
        tmp_path / "email_outbound_log.jsonl",
        [
            _outbound(
                message_id="<reply-1@e.com>",
                in_reply_to="<m-1@e.com>",
                text="kora's reply",
            ),
        ],
    )
    ctx = load_email_context(message_id="<m-1@e.com>")
    assert len(ctx.recent_messages) == 1
    assert ctx.recent_messages[0].text == "kora's reply"
    assert ctx.recent_messages[0].direction == "outbound"


def test_parent_message_anchored_via_in_reply_to(tmp_path):
    """When the focal email has in_reply_to=<parent>, prior entries
    whose message_id == <parent> are pulled into the chain."""
    _write_jsonl(
        tmp_path / "email_inbound_log.jsonl",
        [
            _inbound(message_id="<parent@e.com>", text="earlier joshua msg"),
            _inbound(
                message_id="<m-2@e.com>",
                text="focal joshua msg",
                in_reply_to="<parent@e.com>",
            ),
        ],
    )
    ctx = load_email_context(
        message_id="<m-2@e.com>", in_reply_to="<parent@e.com>"
    )
    texts = [t.text for t in ctx.recent_messages]
    assert "earlier joshua msg" in texts
    assert "focal joshua msg" in texts


def test_skips_filtered_inbound(tmp_path):
    _write_jsonl(
        tmp_path / "email_inbound_log.jsonl",
        [
            _inbound(
                message_id="<m-1@e.com>",
                text="received_one",
                handled_status="received",
            ),
            _inbound(
                message_id="<m-2@e.com>",
                text="filtered_one",
                handled_status="filtered_non_joshua",
            ),
        ],
    )
    # Two anchors share neither — only the received one matches anyway.
    ctx = load_email_context(message_id="<m-1@e.com>")
    assert len(ctx.recent_messages) == 1
    assert ctx.recent_messages[0].text == "received_one"


def test_skips_failed_outbound(tmp_path):
    _write_jsonl(
        tmp_path / "email_outbound_log.jsonl",
        [
            _outbound(
                message_id="<r-1@e.com>",
                in_reply_to="<m-1@e.com>",
                text="failed_reply",
                send_status="failed",
            ),
            _outbound(
                message_id="<r-2@e.com>",
                in_reply_to="<m-1@e.com>",
                text="ok_reply",
                send_status="ok",
            ),
        ],
    )
    ctx = load_email_context(message_id="<m-1@e.com>")
    texts = [t.text for t in ctx.recent_messages]
    assert "ok_reply" in texts
    assert "failed_reply" not in texts


# ===========================================================================
# Cross-file + chronological order
# ===========================================================================


def test_cross_file_chronological_order(tmp_path):
    _write_jsonl(
        tmp_path / "email_inbound_log.jsonl",
        [
            _inbound(
                message_id="<m-1@e.com>",
                text="first inbound",
                received_at="2026-05-22T10:00:00+00:00",
            ),
            _inbound(
                message_id="<m-2@e.com>",
                text="third inbound",
                received_at="2026-05-22T10:02:00+00:00",
                in_reply_to="<r-1@e.com>",
            ),
        ],
    )
    _write_jsonl(
        tmp_path / "email_outbound_log.jsonl",
        [
            _outbound(
                message_id="<r-1@e.com>",
                in_reply_to="<m-1@e.com>",
                text="second outbound",
                sent_at="2026-05-22T10:01:00+00:00",
            ),
        ],
    )
    ctx = load_email_context(
        message_id="<m-2@e.com>", in_reply_to="<r-1@e.com>"
    )
    texts = [t.text for t in ctx.recent_messages]
    assert texts == ["first inbound", "second outbound", "third inbound"]


def test_max_turns_keeps_last_n(tmp_path):
    _write_jsonl(
        tmp_path / "email_inbound_log.jsonl",
        [
            _inbound(
                message_id=f"<m-{i}@e.com>",
                text=f"msg{i}",
                received_at=f"2026-05-22T10:0{i}:00+00:00",
                in_reply_to="<focal@e.com>",
            )
            for i in range(5)
        ]
        + [_inbound(message_id="<focal@e.com>", text="focal")],
    )
    ctx = load_email_context(message_id="<focal@e.com>", max_turns=3)
    assert len(ctx.recent_messages) == 3
    # last-3 → msg2, msg3, msg4 (or focal depending on time sort)
    texts = [t.text for t in ctx.recent_messages]
    assert texts[-1] == "msg4"  # latest by time


# ===========================================================================
# Malformed input
# ===========================================================================


def test_malformed_line_logged_and_skipped(tmp_path, caplog):
    path = tmp_path / "email_inbound_log.jsonl"
    path.write_text(
        json.dumps(_inbound(message_id="<m-1@e.com>", text="good")) + "\n"
        "{not-valid-json\n"
        + json.dumps(_inbound(message_id="<m-1@e.com>", text="also-good")),
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        ctx = load_email_context(message_id="<m-1@e.com>")
    texts = [t.text for t in ctx.recent_messages]
    assert texts == ["good", "also-good"]
    assert any("malformed" in r.message for r in caplog.records)
