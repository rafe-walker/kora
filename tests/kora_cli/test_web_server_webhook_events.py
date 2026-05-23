"""Tests for the KR-WEBHOOK-EVENTS-PANEL endpoint (post audit-JSONL flip).

After KR-AUDIT-PANEL-ENDPOINTS, the endpoint reads
``${KORA_HOME}/kora_audit_log.jsonl`` filtered to
``seam=webhook.dead_letter`` rows and projects to ``WebhookEvent``
shape.

Limitations (per endpoint docstring):
  * Verified happy-path events NOT in audit (chain log only).
    Panel shows only dead-letters for this flip.
  * Rate-limited events from slowapi don't emit_audit yet.

Scenarios:
  1. Empty audit log → empty list + stub:false
  2. Top-level shape
  3. webhook.dead_letter row projects to WebhookEvent shape
  4. status="dead_letter" for all (this seam only emits dead-letters)
  5. endpoint derived from details.source: slack → /api/webhooks/slack/events
  6. SECURITY: source_ip OCTET-MASKED — raw peer_ip never reaches wire
  7. SECURITY: walk-payload no full IPv4 addresses anywhere
  8. SECURITY: details sub-set to FE-shaped fields only
  9. Other seams filtered out
 10. Newest-first ordering
 11. ?limit cap
 12. Cron-regression sanity
"""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from kora_cli.audit.jsonl_sink import AUDIT_LOG_FILENAME


# Full IPv4 leak guard — KR-WEBHOOK-EVENTS-PANEL #109 contract.
_FULL_IPV4_LEAK = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
# Octet-mask shape: at least one literal "x" octet.
_MASKED_IPV4_PIN = re.compile(r"^\d{1,3}\.\d{1,3}\.x\.x$")


from tests.kora_cli._panel_test_helpers import isolated_kora_home  # noqa: E402


@pytest.fixture
def audit_env(tmp_path, monkeypatch):
    return isolated_kora_home(tmp_path, monkeypatch)


def _dead_letter(
    *,
    minutes_ago: int = 5,
    source: str = "slack",
    reason: str = "signature_mismatch",
    peer_ip: str = "54.203.99.142",
    header_present: bool = True,
    **extra: Any,
) -> Dict[str, Any]:
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    details = {
        "source": source,
        "reason": reason,
        "ts": ts.timestamp(),
        "peer_ip": peer_ip,
        "request_id": "req-123",
        "body_bytes": 1024,
        "headers": {},
        "header_present": header_present,
    }
    details.update(extra)
    return {
        "emitted_at": ts.isoformat(),
        "seam": "webhook.dead_letter",
        "details": details,
        "caller_session_id": None,
        "source": "slack_dm" if source == "slack" else "email",
    }


def _other_seam(seam: str) -> Dict[str, Any]:
    ts = datetime.now(timezone.utc)
    return {
        "emitted_at": ts.isoformat(),
        "seam": seam,
        "details": {},
        "caller_session_id": None,
        "source": None,
    }


def write_log(env_path: Path, entries: List[Dict[str, Any]]) -> Path:
    log_path = env_path / AUDIT_LOG_FILENAME
    with log_path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return log_path


# ---- 1. Empty ---------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_returns_empty_list_when_audit_log_missing(audit_env):
    from kora_cli import web_server

    result = await web_server.list_recent_webhook_events()
    assert result["events"] == []
    assert result["stub"] is False


# ---- 2. Top-level shape ----------------------------------------


@pytest.mark.asyncio
async def test_response_shape_has_required_keys(audit_env):
    write_log(audit_env, [_dead_letter()])
    from kora_cli import web_server

    result = await web_server.list_recent_webhook_events()
    assert set(result.keys()) == {
        "events",
        "stub",
        "generated_at",
        "total_recent_24h",
    }
    assert result["stub"] is False


# ---- 3. Projection shape ---------------------------------------


@pytest.mark.asyncio
async def test_dead_letter_projects_to_webhook_event_shape(audit_env):
    write_log(audit_env, [
        _dead_letter(source="slack", reason="signature_mismatch")
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_webhook_events()
    assert len(result["events"]) == 1
    event = result["events"][0]
    assert set(event.keys()) == {
        "id",
        "endpoint",
        "received_at",
        "status",
        "source_ip",
        "event_type",
        "details",
    }
    assert event["endpoint"] == "/api/webhooks/slack/events"
    assert event["status"] == "dead_letter"
    assert event["event_type"] == "signature_mismatch"


@pytest.mark.asyncio
async def test_email_source_maps_to_email_endpoint(audit_env):
    write_log(audit_env, [_dead_letter(source="email")])
    from kora_cli import web_server

    result = await web_server.list_recent_webhook_events()
    assert result["events"][0]["endpoint"] == "/api/webhooks/email/inbound"


@pytest.mark.asyncio
async def test_all_audit_rows_status_is_dead_letter(audit_env):
    """This seam ONLY emits dead-letters. The status field is
    pinned by the projector, not derived from details — so any
    future audit-row that lands as webhook.dead_letter renders
    consistently in the panel."""
    write_log(audit_env, [
        _dead_letter(source="slack"),
        _dead_letter(source="email", reason="hmac_invalid"),
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_webhook_events()
    for event in result["events"]:
        assert event["status"] == "dead_letter"


# ---- 4. SECURITY: source_ip octet-mask ------------------------


@pytest.mark.asyncio
async def test_source_ip_octet_masked_in_projection(audit_env):
    """SECURITY: the audit writer passes RAW peer_ip (e.g.
    54.203.99.142); the endpoint MUST mask it per the panel's
    PII contract (KR-WEBHOOK-EVENTS-PANEL #109). Endpoint enforces
    via _mask_ipv4_last_two_octets."""
    write_log(audit_env, [_dead_letter(peer_ip="54.203.99.142")])
    from kora_cli import web_server

    result = await web_server.list_recent_webhook_events()
    event = result["events"][0]
    assert event["source_ip"] == "54.203.x.x", (
        f"source_ip={event['source_ip']!r} should be octet-masked; "
        f"raw audit peer_ip leaked into wire"
    )
    assert _MASKED_IPV4_PIN.match(event["source_ip"])


@pytest.mark.asyncio
async def test_no_full_ipv4_anywhere_in_payload(audit_env):
    """Walk-payload sweep: NO full 4-octet IPv4 address appears
    anywhere in the response — top-level, per-event, nested
    details. Catches a future drift that surfaces peer_ip
    unmasked via details passthrough or a diagnostic field."""
    write_log(audit_env, [
        _dead_letter(peer_ip="54.203.99.142"),
        _dead_letter(peer_ip="203.0.113.42", source="email"),
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_webhook_events()
    blob = json.dumps(result)
    leaks = _FULL_IPV4_LEAK.findall(blob)
    assert leaks == [], (
        f"payload contains full IPv4 address(es): {leaks} — "
        f"source IPs must be octet-masked everywhere"
    )


@pytest.mark.asyncio
async def test_ipv6_or_dash_peer_ip_falls_through_to_em_dash(audit_env):
    """Defensive: when peer_ip isn't IPv4 (IPv6, "-", missing),
    the projector returns "—" so the panel always sees a stringable
    value AND we never accidentally leak an unmasked IPv6."""
    write_log(audit_env, [
        _dead_letter(peer_ip="2001:db8::42"),
        _dead_letter(peer_ip="-", source="email"),
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_webhook_events()
    for event in result["events"]:
        assert event["source_ip"] == "—"


# ---- 5. SECURITY: details sub-set -----------------------------


@pytest.mark.asyncio
async def test_details_sub_set_to_fe_shaped_fields(audit_env):
    """SECURITY: don't pass the full audit details dict through to
    the FE — that could surface internal fields (body_bytes,
    request_id, headers) the panel hasn't vetted. Projector keeps
    only reason + header_present."""
    write_log(audit_env, [_dead_letter(
        source="slack",
        reason="signature_mismatch",
        header_present=True,
        body_bytes=999,
        request_id="should-not-appear",
    )])
    from kora_cli import web_server

    result = await web_server.list_recent_webhook_events()
    details = result["events"][0]["details"]
    assert set(details.keys()) == {"reason", "header_present"}, (
        "details must be subset to FE-shaped fields only — "
        f"got extra keys: {set(details.keys()) - {'reason', 'header_present'}}"
    )
    # Belt+braces: ensure nothing slipped through the projection
    blob = json.dumps(result)
    assert "should-not-appear" not in blob


# ---- 6. Filtering --------------------------------------------


@pytest.mark.asyncio
async def test_other_seams_filtered_out(audit_env):
    write_log(audit_env, [
        _dead_letter(),
        _other_seam("mcp.tool_called"),
        _other_seam("reasoning.tool_called"),
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_webhook_events()
    assert len(result["events"]) == 1


# ---- 7. Newest-first ordering --------------------------------


@pytest.mark.asyncio
async def test_newest_first_ordering(audit_env):
    write_log(audit_env, [
        _dead_letter(minutes_ago=30, reason="oldest"),
        _dead_letter(minutes_ago=5, reason="newest"),
        _dead_letter(minutes_ago=15, reason="middle"),
    ])
    from kora_cli import web_server

    result = await web_server.list_recent_webhook_events()
    assert [e["event_type"] for e in result["events"]] == [
        "newest",
        "middle",
        "oldest",
    ]


# ---- 8. ?limit cap ------------------------------------------


@pytest.mark.asyncio
async def test_limit_query_param_respected(audit_env):
    write_log(audit_env, [_dead_letter(minutes_ago=i) for i in range(10)])
    from kora_cli import web_server

    result = await web_server.list_recent_webhook_events(limit=3)
    assert len(result["events"]) == 3


@pytest.mark.asyncio
async def test_limit_capped_at_200(audit_env):
    write_log(audit_env, [_dead_letter(minutes_ago=i) for i in range(5)])
    from kora_cli import web_server

    result = await web_server.list_recent_webhook_events(limit=99999)
    assert len(result["events"]) <= 200


# ---- 9. Cron-regression sanity -----------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_webhook_events_registered(audit_env):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
