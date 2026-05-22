"""Tests for the KR-WEBHOOK-EVENTS-PANEL stub endpoint.

Bucket §4 scenarios:
  1. GET /api/webhooks/events/recent returns 200
  2. Top-level shape (events + stub:true + generated_at + total_recent_24h)
  3. All 4 expected stub events present
  4. Each event has the required keys + valid status enum
  5. SECURITY: source_ip is OCTET-MASKED (e.g. "54.203.x.x" never
     "54.203.99.142"); regex-pin asserts the mask format
  6. Cron-regression sanity
"""

import re

import pytest


_VALID_STATUS = {"verified", "dead_letter", "rate_limited", "handler_error"}

# Octet-masked IPv4: at least one octet replaced with "x". Real shapes
# the bucket §3 stub uses are "54.203.x.x" (last 2) and "203.0.113.x"
# (last 1). Pin: starts with 1-3 digits, contains at least one
# literal "x" octet, no contiguous 4-digit sequences.
_MASKED_IPV4_PIN = re.compile(r"^(?:\d{1,3}\.){1,3}(?:\d{1,3}|x)(?:\.(?:\d{1,3}|x))*$")
_FULL_IPV4_LEAK = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")


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

    result = await web_server.list_recent_webhook_events()
    assert isinstance(result, dict)


# ---- 2. Top-level shape ----------------------------------------------


@pytest.mark.asyncio
async def test_response_shape_has_required_keys(_isolate_config):
    from kora_cli import web_server

    result = await web_server.list_recent_webhook_events()
    assert set(result.keys()) == {
        "events",
        "stub",
        "generated_at",
        "total_recent_24h",
    }
    assert isinstance(result["events"], list)
    assert isinstance(result["generated_at"], str)
    assert isinstance(result["total_recent_24h"], int)
    assert result["stub"] is True


# ---- 3. Expected stub events ----------------------------------------


@pytest.mark.asyncio
async def test_stub_returns_four_representative_events(_isolate_config):
    """Pin the bucket §3 canonical 4-event stub list. CC#3's per-event
    recording will replace the body but stub shape needs to stay
    stable so the FE that ships off this PR keeps rendering."""
    from kora_cli import web_server

    result = await web_server.list_recent_webhook_events()
    assert len(result["events"]) == 4
    ids = {e["id"] for e in result["events"]}
    assert ids == {"stub-1", "stub-2", "stub-3", "stub-4"}


@pytest.mark.asyncio
async def test_stub_covers_three_status_values(_isolate_config):
    """The 4 stub events deliberately span verified / dead_letter /
    rate_limited so the FE's status-badge color rendering is
    exercised by the panel's manual smoke. Pin so future stub edits
    can't accidentally homogenize."""
    from kora_cli import web_server

    result = await web_server.list_recent_webhook_events()
    statuses = {e["status"] for e in result["events"]}
    assert statuses == {"verified", "dead_letter", "rate_limited"}


# ---- 4. Per-entry shape + enum --------------------------------------


@pytest.mark.asyncio
async def test_each_event_has_required_keys_and_valid_status(_isolate_config):
    from kora_cli import web_server

    result = await web_server.list_recent_webhook_events()
    required = {
        "id",
        "endpoint",
        "received_at",
        "status",
        "source_ip",
        "event_type",
        "details",
    }
    for event in result["events"]:
        assert set(event.keys()) == required
        assert event["status"] in _VALID_STATUS, (
            f"{event['id']}: status={event['status']!r} not in {_VALID_STATUS}"
        )
        assert isinstance(event["endpoint"], str)
        assert event["endpoint"].startswith("/api/webhooks/")
        # event_type is null for rate_limited (request never reached
        # the handler) — that's a documented contract, not a defect
        if event["status"] == "rate_limited":
            assert event["event_type"] is None
        else:
            assert isinstance(event["event_type"], str)
        assert isinstance(event["details"], dict)


# ---- 5. SECURITY: source_ip octet-mask enforcement -----------------


@pytest.mark.asyncio
async def test_source_ip_is_octet_masked_per_security_contract(_isolate_config):
    """Bucket hard-constraint: source_ip values are OCTET-MASKED on
    the wire (e.g. "54.203.x.x" never "54.203.99.142") — operator
    gets geolocation hint without full PII exposure. Pin the mask
    shape so a future stub or real-data drift can't silently leak
    a full IP. 3-layer security pattern from KR-MCP-3 #106:
    backend payload + TS interface + this test."""
    from kora_cli import web_server

    result = await web_server.list_recent_webhook_events()
    for event in result["events"]:
        ip = event["source_ip"]
        assert isinstance(ip, str) and ip
        # Mask shape: contains at least one literal "x" octet
        assert "x" in ip, (
            f"{event['id']}: source_ip={ip!r} contains no 'x' octet — "
            f"contract requires octet-masking for PII"
        )
        # Format matches our pin regex
        assert _MASKED_IPV4_PIN.match(ip), (
            f"{event['id']}: source_ip={ip!r} doesn't match expected "
            f"octet-mask shape (e.g. '54.203.x.x' / '203.0.113.x')"
        )


@pytest.mark.asyncio
async def test_no_full_ipv4_address_leaks_anywhere_in_payload(_isolate_config):
    """Belt+braces: walk the entire payload (top-level + nested dicts
    + event details) and assert no full 4-octet IPv4 address appears
    anywhere. Catches a future drift that adds a "real_source_ip"
    diagnostic field, embeds a full IP in details, etc."""
    from kora_cli import web_server
    import json as _json

    result = await web_server.list_recent_webhook_events()
    blob = _json.dumps(result)
    leaks = _FULL_IPV4_LEAK.findall(blob)
    assert leaks == [], (
        f"payload contains full IPv4 address(es): {leaks} — "
        f"source IPs must be octet-masked everywhere they appear "
        f"(bucket §5 PII contract)"
    )


# ---- 6. Bucket §3 stub values pinned --------------------------------


@pytest.mark.asyncio
async def test_dead_letter_event_carries_reason_in_details(_isolate_config):
    """The stub dead-letter event surfaces a "reason" field in details
    — operator-actionable when investigating a 401. Pin so the
    contract stays even if the real implementation grows other
    detail keys."""
    from kora_cli import web_server

    result = await web_server.list_recent_webhook_events()
    dead_letter = next(e for e in result["events"] if e["status"] == "dead_letter")
    assert "reason" in dead_letter["details"]
    assert dead_letter["event_type"] == "hmac_invalid"


@pytest.mark.asyncio
async def test_endpoints_match_known_webhook_routes(_isolate_config):
    """The stub events reference /api/webhooks/slack/events +
    /api/webhooks/email/inbound — the two routes CC#3 ST3 (PR #104)
    actually shipped. Pin to catch a future stub typo that
    references a route that doesn't exist."""
    from kora_cli import web_server

    known_routes = {
        "/api/webhooks/slack/events",
        "/api/webhooks/email/inbound",
    }
    result = await web_server.list_recent_webhook_events()
    endpoints = {e["endpoint"] for e in result["events"]}
    assert endpoints <= known_routes, (
        f"stub references unknown route(s): {endpoints - known_routes}"
    )


# ---- 7. Cron-regression sanity -------------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_webhook_events_registered(_isolate_config):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
