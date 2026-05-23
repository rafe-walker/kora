"""Tests for KR-EMAIL-PANEL-FLIP — JSONL-driven projection.

Bucket §2 + §4 scenarios:

  Endpoint shape / stub flip:
   1. Empty JSONLs → empty messages list + stub:false
   2. Only inbound JSONL present (outbound missing) → projects inbound only
   3. Only outbound JSONL present (inbound missing) → projects outbound only
   4. Both files present → merged + sorted newest-first

  Projection — inbound:
   5. handled_status=received → fe handled_status=received,
      from_label=joshua (matching env), to_label=kora (matching env)
   6. Non-joshua sender → from_label=unknown_sender
   7. To-list missing Kora's address → to_label=other
   8. handled_status=filtered_paused / filtered_stopped → dropped_paused
   9. handled_status=filtered_non_joshua → filtered_non_allowlist (collapse)
  10. body_text_truncated_2k → body_text_truncated_400 truncated to 400
  11. spoofing_check_skipped=True → spoofing_warning=False
  12. Unknown handled_status → entry skipped defensively

  Projection — outbound:
  13. send_status=ok → handled_status=sent_ok; from_label=kora
  14. send_status=failed → handled_status=sent_failed
  15. Recipient matches KORA_EMAIL_JOSHUA_ADDRESS → to_label=joshua
  16. body_text_truncated_400 = placeholder ("(outbound body not logged ...)")
  17. attachments_count=0 + has_html=False (outbound contract)
  18. in_reply_to passes through (may be null)

  Merge + sort:
  19. Newest-first by timestamp descending across both files
  20. Aggregate counts span both files within 24h window
  21. ?limit query param respected; cap at 200
  22. Limit > 200 → capped to 200 (defense against runaway query)
  23. Limit < 1 → clamped to 1

  Tolerance:
  24. Malformed JSONL line → logged + skipped; sibling lines parse
  25. JSON line that's not a dict (e.g., array) → skipped
  26. Inbound entry missing message_id → synthesized inbound-no-id-line-N
  27. Outbound entry missing message_id → synthesized outbound-no-id-line-N
  28. Outbound entry with empty recipients → skipped

  SECURITY:
  29. Walk-payload regex sweep finds NO email-address shape ANYWHERE
      EXCEPT inside message_id values (the carve-out)
  30. message_id carve-out: legitimate `<...@operator-domain>` pattern
      allowed when it appears in message_id; same pattern in OTHER
      fields still flagged
  31. No raw email addresses bleed into subject / from_label / to_label /
      body_text_truncated_400 / in_reply_to (in_reply_to follows the
      same shape as message_id but per spec passes through too —
      treated as message_id-class)
  32. No Purelymail token hints / HMAC secret shapes / Bearer headers
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest


_JOSHUA_ADDR = "joshua@stormhavenenterprises.com"
_KORA_ADDR = "kora@stormhavenenterprises.com"
_OTHER_ADDR = "stranger@evil.example"


_EMAIL_ADDRESS = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)
_PUREMAIL_TOKEN_HINT = re.compile(
    r"\b(?:KORA_PUREMAIL_|KORA_PURELYMAIL_|puremail_|purelymail_)[A-Za-z0-9_]*[A-Za-z0-9]\b"
)
_HEX_SECRET_SHAPE = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_BEARER_TOKEN_SHAPE = re.compile(
    r"\b(?:Bearer|Authorization)\s*[: ]\s*[A-Za-z0-9+/_.-]{8,}",
    re.IGNORECASE,
)

# Fields where the email-address regex IS allowed to match per the
# bucket's message_id carve-out. message_id values are RFC 5322
# format (<id@<operator-domain>>). in_reply_to follows the same
# shape (it IS another message_id), so it gets the same carve-out.
_EMAIL_REGEX_ALLOWED_FIELDS = {"message_id", "in_reply_to"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated KORA_HOME + Joshua/Kora env addresses set.

    Applies the CC#2 #137 fixture-isolation lesson: monkeypatch
    ``get_kora_home`` in all 3 module namespaces (kora_constants,
    kora_cli.config, kora_cli.web_server) because the endpoint
    resolves it from its own module namespace via a
    ``from kora_cli.config import get_kora_home`` re-import.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setenv("KORA_EMAIL_JOSHUA_ADDRESS", _JOSHUA_ADDR)
    monkeypatch.setenv("KORA_EMAIL_KORA_ADDRESS", _KORA_ADDR)
    monkeypatch.setattr("kora_constants.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr("kora_cli.config.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr(
        "kora_cli.web_server.get_kora_home", lambda: tmp_path
    )
    return tmp_path


def _iso(minutes_ago: int = 0) -> str:
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_jsonl(path: Path, entries: List[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def _inbound_entry(
    *,
    message_id: str = "<inbound-1@stormhavenenterprises.com>",
    from_addr: str = _JOSHUA_ADDR,
    to_list: List[str] = None,
    subject: str = "hello",
    body_text_truncated_2k: str = "hello kora",
    has_html: bool = False,
    attachments_count: int = 0,
    handled_status: str = "received",
    spoofing_check_skipped: bool = True,
    imap_uid: int = 1,
    minutes_ago: int = 5,
    in_reply_to: Any = None,
) -> Dict[str, Any]:
    return {
        "received_at": _iso(minutes_ago),
        "message_id": message_id,
        "from": from_addr,
        "to": to_list if to_list is not None else [_KORA_ADDR],
        "subject": subject,
        "body_text_truncated_2k": body_text_truncated_2k,
        "has_html": has_html,
        "attachments_count": attachments_count,
        "handled_status": handled_status,
        "spoofing_check_skipped": spoofing_check_skipped,
        "imap_uid": imap_uid,
        "in_reply_to": in_reply_to,
    }


def _outbound_entry(
    *,
    message_id: str = "<outbound-1@stormhavenenterprises.com>",
    to_list: List[str] = None,
    subject: str = "Re: hello",
    send_status: str = "ok",
    in_reply_to: Any = "<inbound-1@stormhavenenterprises.com>",
    minutes_ago: int = 4,
    smtp_code: int = 250,
    error: Any = None,
    retry_count: int = 0,
) -> Dict[str, Any]:
    return {
        "sent_at": _iso(minutes_ago),
        "from": _KORA_ADDR,
        "to": to_list if to_list is not None else [_JOSHUA_ADDR],
        "subject": subject,
        "in_reply_to": in_reply_to,
        "send_status": send_status,
        "message_id": message_id,
        "smtp_code": smtp_code,
        "error": error,
        "retry_count": retry_count,
        "caller_actor_kind": None,
    }


def _inbound_path(env_path: Path) -> Path:
    return env_path / "email_inbound_log.jsonl"


def _outbound_path(env_path: Path) -> Path:
    return env_path / "email_outbound_log.jsonl"


# ===========================================================================
# Stub-flip: stub is False, empty list when no JSONLs
# ===========================================================================


@pytest.mark.asyncio
async def test_empty_files_returns_empty_with_stub_false(env):
    from kora_cli import web_server

    result = await web_server.list_recent_email()
    assert result["stub"] is False
    assert result["messages"] == []
    assert result["total_recent_24h"] == 0


@pytest.mark.asyncio
async def test_only_inbound_file_present(env):
    from kora_cli import web_server

    _write_jsonl(_inbound_path(env), [_inbound_entry()])
    result = await web_server.list_recent_email()
    assert result["stub"] is False
    assert len(result["messages"]) == 1
    assert result["messages"][0]["direction"] == "inbound"


@pytest.mark.asyncio
async def test_only_outbound_file_present(env):
    from kora_cli import web_server

    _write_jsonl(_outbound_path(env), [_outbound_entry()])
    result = await web_server.list_recent_email()
    assert result["stub"] is False
    assert len(result["messages"]) == 1
    assert result["messages"][0]["direction"] == "outbound"


# ===========================================================================
# Projection — inbound
# ===========================================================================


@pytest.mark.asyncio
async def test_inbound_joshua_received_projects_correctly(env):
    from kora_cli import web_server

    _write_jsonl(
        _inbound_path(env),
        [
            _inbound_entry(
                from_addr=_JOSHUA_ADDR,
                to_list=[_KORA_ADDR],
                subject="status?",
                body_text_truncated_2k="how are you",
            )
        ],
    )
    result = await web_server.list_recent_email()
    msg = result["messages"][0]
    assert msg["direction"] == "inbound"
    assert msg["from_label"] == "joshua"
    assert msg["to_label"] == "kora"
    assert msg["handled_status"] == "received"
    assert msg["subject"] == "status?"
    assert msg["body_text_truncated_400"] == "how are you"
    assert msg["spoofing_warning"] is False
    assert msg["id"].startswith("inbound-")


@pytest.mark.asyncio
async def test_inbound_non_joshua_sender_resolves_to_unknown(env):
    from kora_cli import web_server

    _write_jsonl(
        _inbound_path(env),
        [_inbound_entry(from_addr=_OTHER_ADDR)],
    )
    result = await web_server.list_recent_email()
    assert result["messages"][0]["from_label"] == "unknown_sender"


@pytest.mark.asyncio
async def test_inbound_to_list_without_kora_resolves_to_other(env):
    from kora_cli import web_server

    _write_jsonl(
        _inbound_path(env),
        [_inbound_entry(to_list=["someone-else@example.com"])],
    )
    result = await web_server.list_recent_email()
    assert result["messages"][0]["to_label"] == "other"


@pytest.mark.asyncio
async def test_inbound_paused_collapses_to_dropped_paused(env):
    from kora_cli import web_server

    _write_jsonl(
        _inbound_path(env),
        [_inbound_entry(handled_status="filtered_paused")],
    )
    result = await web_server.list_recent_email()
    assert result["messages"][0]["handled_status"] == "dropped_paused"


@pytest.mark.asyncio
async def test_inbound_stopped_collapses_to_dropped_paused(env):
    from kora_cli import web_server

    _write_jsonl(
        _inbound_path(env),
        [_inbound_entry(handled_status="filtered_stopped")],
    )
    result = await web_server.list_recent_email()
    assert result["messages"][0]["handled_status"] == "dropped_paused"


@pytest.mark.asyncio
async def test_inbound_non_joshua_collapses_to_non_allowlist(env):
    from kora_cli import web_server

    _write_jsonl(
        _inbound_path(env),
        [_inbound_entry(handled_status="filtered_non_joshua")],
    )
    result = await web_server.list_recent_email()
    assert (
        result["messages"][0]["handled_status"] == "filtered_non_allowlist"
    )


@pytest.mark.asyncio
async def test_inbound_body_truncated_to_400(env):
    from kora_cli import web_server

    long_body = "x" * 1000
    _write_jsonl(
        _inbound_path(env),
        [_inbound_entry(body_text_truncated_2k=long_body)],
    )
    result = await web_server.list_recent_email()
    assert len(result["messages"][0]["body_text_truncated_400"]) == 400


@pytest.mark.asyncio
async def test_inbound_spoofing_check_skipped_means_no_warning(env):
    from kora_cli import web_server

    _write_jsonl(
        _inbound_path(env),
        [_inbound_entry(spoofing_check_skipped=True)],
    )
    result = await web_server.list_recent_email()
    assert result["messages"][0]["spoofing_warning"] is False


@pytest.mark.asyncio
async def test_inbound_spoofing_check_ran_means_warning(env):
    """If a future bucket adds real spoofing detection, an entry
    with spoofing_check_skipped=False signals the check ran;
    spoofing_warning becomes True. Documents the semantic flip in
    the projection."""
    from kora_cli import web_server

    _write_jsonl(
        _inbound_path(env),
        [_inbound_entry(spoofing_check_skipped=False)],
    )
    result = await web_server.list_recent_email()
    assert result["messages"][0]["spoofing_warning"] is True


@pytest.mark.asyncio
async def test_inbound_unknown_handled_status_skipped(env):
    from kora_cli import web_server

    _write_jsonl(
        _inbound_path(env),
        [_inbound_entry(handled_status="not_a_real_status")],
    )
    result = await web_server.list_recent_email()
    assert result["messages"] == []


# ===========================================================================
# Projection — outbound
# ===========================================================================


@pytest.mark.asyncio
async def test_outbound_ok_projects_correctly(env):
    from kora_cli import web_server

    _write_jsonl(
        _outbound_path(env),
        [_outbound_entry(send_status="ok")],
    )
    result = await web_server.list_recent_email()
    msg = result["messages"][0]
    assert msg["direction"] == "outbound"
    assert msg["from_label"] == "kora"
    assert msg["to_label"] == "joshua"
    assert msg["handled_status"] == "sent_ok"
    assert msg["has_html"] is False
    assert msg["attachments_count"] == 0
    assert msg["body_text_truncated_400"] == (
        "(outbound body not logged for size + privacy)"
    )


@pytest.mark.asyncio
async def test_outbound_failed_projects_sent_failed(env):
    from kora_cli import web_server

    _write_jsonl(
        _outbound_path(env),
        [_outbound_entry(send_status="failed", error="boom")],
    )
    result = await web_server.list_recent_email()
    assert result["messages"][0]["handled_status"] == "sent_failed"


@pytest.mark.asyncio
async def test_outbound_to_non_joshua_resolves_to_other(env):
    from kora_cli import web_server

    _write_jsonl(
        _outbound_path(env),
        [_outbound_entry(to_list=["random@example.com"])],
    )
    result = await web_server.list_recent_email()
    assert result["messages"][0]["to_label"] == "other"


@pytest.mark.asyncio
async def test_outbound_in_reply_to_passes_through(env):
    from kora_cli import web_server

    _write_jsonl(
        _outbound_path(env),
        [_outbound_entry(in_reply_to="<original@stormhavenenterprises.com>")],
    )
    result = await web_server.list_recent_email()
    assert result["messages"][0]["in_reply_to"] == (
        "<original@stormhavenenterprises.com>"
    )


@pytest.mark.asyncio
async def test_outbound_in_reply_to_null_passes_through(env):
    from kora_cli import web_server

    _write_jsonl(
        _outbound_path(env),
        [_outbound_entry(in_reply_to=None)],
    )
    result = await web_server.list_recent_email()
    assert result["messages"][0]["in_reply_to"] is None


@pytest.mark.asyncio
async def test_outbound_empty_recipients_skipped(env):
    from kora_cli import web_server

    _write_jsonl(
        _outbound_path(env),
        [_outbound_entry(to_list=[])],
    )
    result = await web_server.list_recent_email()
    assert result["messages"] == []


@pytest.mark.asyncio
async def test_outbound_unknown_send_status_skipped(env):
    from kora_cli import web_server

    _write_jsonl(
        _outbound_path(env),
        [_outbound_entry(send_status="pending")],
    )
    result = await web_server.list_recent_email()
    assert result["messages"] == []


# ===========================================================================
# Merge + sort + limit
# ===========================================================================


@pytest.mark.asyncio
async def test_merge_sorts_newest_first(env):
    from kora_cli import web_server

    _write_jsonl(
        _inbound_path(env),
        [
            _inbound_entry(
                message_id="<m-old@e.com>",
                subject="old inbound",
                minutes_ago=20,
            ),
            _inbound_entry(
                message_id="<m-new@e.com>",
                subject="new inbound",
                minutes_ago=2,
            ),
        ],
    )
    _write_jsonl(
        _outbound_path(env),
        [
            _outbound_entry(
                message_id="<m-mid@e.com>",
                subject="Re: mid",
                minutes_ago=10,
            )
        ],
    )
    result = await web_server.list_recent_email()
    subjects = [m["subject"] for m in result["messages"]]
    assert subjects == ["new inbound", "Re: mid", "old inbound"]


@pytest.mark.asyncio
async def test_limit_query_param_respected(env):
    from kora_cli import web_server

    entries = [
        _inbound_entry(
            message_id=f"<m-{i}@e.com>",
            subject=f"msg-{i}",
            minutes_ago=i,
        )
        for i in range(20)
    ]
    _write_jsonl(_inbound_path(env), entries)
    result = await web_server.list_recent_email(limit=5)
    assert len(result["messages"]) == 5


@pytest.mark.asyncio
async def test_limit_caps_at_200(env):
    from kora_cli import web_server

    entries = [
        _inbound_entry(
            message_id=f"<m-{i}@e.com>",
            subject=f"msg-{i}",
            minutes_ago=i,
        )
        for i in range(220)
    ]
    _write_jsonl(_inbound_path(env), entries)
    result = await web_server.list_recent_email(limit=500)
    assert len(result["messages"]) == 200


@pytest.mark.asyncio
async def test_limit_below_one_clamps_to_one(env):
    from kora_cli import web_server

    _write_jsonl(_inbound_path(env), [_inbound_entry()])
    result = await web_server.list_recent_email(limit=0)
    assert len(result["messages"]) == 1


# ===========================================================================
# Aggregate counts within 24h window
# ===========================================================================


@pytest.mark.asyncio
async def test_aggregate_counts_within_24h_window(env):
    from kora_cli import web_server

    _write_jsonl(
        _inbound_path(env),
        [
            _inbound_entry(message_id="<m-a@e.com>", minutes_ago=5),
            _inbound_entry(message_id="<m-b@e.com>", minutes_ago=10),
            # Outside 24h window — should NOT count
            _inbound_entry(
                message_id="<m-c@e.com>",
                minutes_ago=60 * 25,
            ),
        ],
    )
    _write_jsonl(
        _outbound_path(env),
        [_outbound_entry(message_id="<m-r@e.com>", minutes_ago=4)],
    )
    result = await web_server.list_recent_email()
    assert result["total_recent_24h"] == 3
    assert result["by_direction_24h"] == {"inbound": 2, "outbound": 1}
    assert result["by_status_24h"]["received"] == 2
    assert result["by_status_24h"]["sent_ok"] == 1


# ===========================================================================
# Tolerance
# ===========================================================================


@pytest.mark.asyncio
async def test_malformed_line_logged_and_skipped(env, caplog):
    from kora_cli import web_server

    path = _inbound_path(env)
    path.write_text(
        json.dumps(_inbound_entry(message_id="<good-1@e.com>")) + "\n"
        "{not-valid-json\n"
        + json.dumps(_inbound_entry(message_id="<good-2@e.com>")) + "\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        result = await web_server.list_recent_email()
    assert len(result["messages"]) == 2
    assert any(
        "malformed JSON" in record.message for record in caplog.records
    )


@pytest.mark.asyncio
async def test_non_dict_line_skipped(env):
    from kora_cli import web_server

    path = _inbound_path(env)
    path.write_text(
        json.dumps([1, 2, 3]) + "\n"
        + json.dumps(_inbound_entry()) + "\n",
        encoding="utf-8",
    )
    result = await web_server.list_recent_email()
    assert len(result["messages"]) == 1


@pytest.mark.asyncio
async def test_inbound_missing_message_id_synthesized(env):
    from kora_cli import web_server

    entry = _inbound_entry()
    del entry["message_id"]
    _write_jsonl(_inbound_path(env), [entry])
    result = await web_server.list_recent_email()
    assert "inbound-no-id-line-" in result["messages"][0]["message_id"]


@pytest.mark.asyncio
async def test_outbound_missing_message_id_synthesized(env):
    from kora_cli import web_server

    entry = _outbound_entry()
    del entry["message_id"]
    _write_jsonl(_outbound_path(env), [entry])
    result = await web_server.list_recent_email()
    assert "outbound-no-id-line-" in result["messages"][0]["message_id"]


# ===========================================================================
# SECURITY — walk-payload sweep with message_id carve-out
# ===========================================================================


def _walk_payload(value: Any, *, exclude_fields: set) -> str:
    """Serialize ``value`` to JSON for regex walking, but replace any
    excluded-field value with a placeholder so the email-regex
    sweep doesn't false-positive on the carve-out fields."""
    if isinstance(value, dict):
        scrubbed = {
            k: ("<EXCLUDED>" if k in exclude_fields else _walk_payload(
                v, exclude_fields=exclude_fields
            ))
            for k, v in value.items()
        }
        return json.dumps(scrubbed)
    if isinstance(value, list):
        return json.dumps(
            [
                json.loads(
                    _walk_payload(item, exclude_fields=exclude_fields)
                )
                if isinstance(item, (dict, list))
                else item
                for item in value
            ]
        )
    return json.dumps(value)


@pytest.mark.asyncio
async def test_no_email_addresses_outside_message_id_carve_out(env):
    """Walk-payload regex sweep: NO email-address shape anywhere
    EXCEPT in message_id / in_reply_to (carve-out per bucket §2(b)
    — RFC 5322 message-ids legitimately contain the operator
    domain)."""
    from kora_cli import web_server

    _write_jsonl(
        _inbound_path(env),
        [
            _inbound_entry(
                message_id="<msg-a@stormhavenenterprises.com>",
                from_addr=_JOSHUA_ADDR,
                to_list=[_KORA_ADDR],
                subject="status",
                body_text_truncated_2k="how's it going",
                in_reply_to="<earlier@stormhavenenterprises.com>",
            )
        ],
    )
    _write_jsonl(
        _outbound_path(env),
        [
            _outbound_entry(
                message_id="<msg-b@stormhavenenterprises.com>",
                in_reply_to="<msg-a@stormhavenenterprises.com>",
            )
        ],
    )
    result = await web_server.list_recent_email()
    blob = _walk_payload(result, exclude_fields=_EMAIL_REGEX_ALLOWED_FIELDS)
    leaks = _EMAIL_ADDRESS.findall(blob)
    assert leaks == [], (
        f"payload contains email-address shape(s) OUTSIDE the "
        f"message_id/in_reply_to carve-out: {leaks}"
    )


@pytest.mark.asyncio
async def test_message_id_carve_out_allows_rfc5322_format(env):
    """The carve-out test: legitimate `<id@operator-domain>` IS
    allowed in message_id even though it shape-matches an email
    address. FE consumers need it for threading."""
    from kora_cli import web_server

    _write_jsonl(
        _inbound_path(env),
        [
            _inbound_entry(
                message_id="<msg-1@stormhavenenterprises.com>",
            )
        ],
    )
    result = await web_server.list_recent_email()
    msg = result["messages"][0]
    # message_id contains an @ + domain — that's allowed.
    assert "@" in msg["message_id"]
    assert msg["message_id"] == "<msg-1@stormhavenenterprises.com>"


@pytest.mark.asyncio
async def test_no_email_in_subject_or_body(env):
    """Defense: if an inbound subject or body contains an email
    address (e.g., a quoted thread or signature), it does end up
    in the payload — but the walk-payload sweep at the panel
    layer treats subject+body as user content. We DON'T strip
    email addresses from those fields because doing so would
    mangle Joshua's actual messages.

    Instead this test pins the EXPECTED state: a subject + body
    that DON'T contain emails come through clean. A separate
    follow-on bucket can decide whether to strip user-content
    addresses for display — out of scope for the flip."""
    from kora_cli import web_server

    _write_jsonl(
        _inbound_path(env),
        [
            _inbound_entry(
                subject="status check (no addresses)",
                body_text_truncated_2k="just plain text without addresses",
            )
        ],
    )
    result = await web_server.list_recent_email()
    blob = _walk_payload(result, exclude_fields=_EMAIL_REGEX_ALLOWED_FIELDS)
    leaks = _EMAIL_ADDRESS.findall(blob)
    assert leaks == []


@pytest.mark.asyncio
async def test_no_purelymail_token_hints_anywhere(env):
    from kora_cli import web_server

    _write_jsonl(_inbound_path(env), [_inbound_entry()])
    _write_jsonl(_outbound_path(env), [_outbound_entry()])
    result = await web_server.list_recent_email()
    blob = json.dumps(result)
    leaks = _PUREMAIL_TOKEN_HINT.findall(blob)
    assert leaks == [], (
        f"payload contains Purelymail token hint(s): {leaks}"
    )


@pytest.mark.asyncio
async def test_no_hex_secret_or_bearer_anywhere(env):
    from kora_cli import web_server

    _write_jsonl(_inbound_path(env), [_inbound_entry()])
    _write_jsonl(_outbound_path(env), [_outbound_entry()])
    result = await web_server.list_recent_email()
    blob = json.dumps(result)
    assert _HEX_SECRET_SHAPE.findall(blob) == []
    assert _BEARER_TOKEN_SHAPE.findall(blob) == []
