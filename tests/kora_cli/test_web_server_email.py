"""Tests for the KR-EMAIL-PANEL endpoint (post KR-EMAIL-PANEL-FLIP).

After the flip the endpoint reads from
``${KORA_HOME}/email_inbound_log.jsonl`` + ``email_outbound_log.jsonl``
(PR #138 + #124 writers). This module keeps the original PR #121
shape-pin + 4-layer security-guard tests that apply to BOTH the
old stub and the new live endpoint:

  * Top-level response shape (now with ``stub: false`` always)
  * Walk-payload security guards (no raw email addresses outside
    the message_id carve-out, no Purelymail token hints, no
    HMAC/Bearer secret shapes)
  * FE source pins (no dangerouslySetInnerHTML, body rendered as
    JSX child, spoofing-warning chip present)
  * by_direction_24h / by_status_24h reconciliation
  * Cron-regression sanity

JSONL-driven projection tests (per-direction field projection,
merge-and-sort, limit param, malformed-line tolerance, etc.) live
in ``test_web_server_email_panel_flip.py``.
"""

import re
from pathlib import Path

import pytest


_VALID_DIRECTION = {"inbound", "outbound"}
_VALID_STATUS = {
    "received",
    "sent_ok",
    "sent_failed",
    "filtered_non_allowlist",
    "filtered_wrong_recipient",
    "dropped_paused",
    "handler_error",
}

# Email address shape: local@host.tld. Spec §2(a) layer 1 requires
# from_label/to_label to be LABELS — never raw email addresses. This
# regex flags any string containing an "@" with at least one dot in
# the domain, which is the universal "this looks like an address"
# signal. Anchored on \b boundaries so user@host.tld leaks regardless
# of surrounding whitespace; we walk the entire serialized payload.
_EMAIL_ADDRESS = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)

# Purelymail API tokens — Purelymail's API uses Basic auth with an
# API token; the token itself is opaque but is typically distributed
# alongside the literal env-var name `KORA_PURELYMAIL_API_TOKEN` or
# similar. Two guards: explicit env-var-name references (a leak would
# include the var name), and any sufficiently-long base64-ish run.
_PUREMAIL_TOKEN_HINT = re.compile(
    r"\b(?:KORA_PUREMAIL_|KORA_PURELYMAIL_|puremail_|purelymail_)[A-Za-z0-9_]*[A-Za-z0-9]\b"
)

# HMAC secret / API token shapes — 32+ hex (sha-256 hash) or 24+
# base64-shaped continuous run. Catches a future log-entry edit
# that stuffs an inbound-signing secret into a debug field.
_HEX_SECRET_SHAPE = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_BASE64_TOKEN_SHAPE = re.compile(r"\b[A-Za-z0-9+/]{24,}={0,2}\b")

# Bearer-token-style: "Bearer XYZ" or "Authorization: ..."
_BEARER_TOKEN_SHAPE = re.compile(
    r"\b(?:Bearer|Authorization)\s*[: ]\s*[A-Za-z0-9+/_.-]{8,}",
    re.IGNORECASE,
)

# Bucket §2(a): v1 message_id stub shape is "stub-msg-id-N".
_MESSAGE_ID_STUB = re.compile(r"^stub-msg-id-\d+$")


_REPO_ROOT = Path(__file__).resolve().parents[2]
_PANEL_PATH = _REPO_ROOT / "web" / "src" / "pages" / "EmailPanel.tsx"


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
    """Apply CC#2's #137 fixture-isolation discipline: monkeypatch
    ``get_kora_home`` in all 3 module namespaces. The endpoint
    imports it via ``from kora_cli.config import get_kora_home``
    which creates a copy in ``kora_cli.web_server`` — patching the
    upstream alone won't redirect the live call site."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setattr("kora_constants.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr("kora_cli.config.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr(
        "kora_cli.web_server.get_kora_home", lambda: tmp_path
    )
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

    result = await web_server.list_recent_email()
    assert isinstance(result, dict)


# ---- 2. Top-level shape ---------------------------------------------


@pytest.mark.asyncio
async def test_response_shape_has_required_keys(_isolate_config):
    """Top-level response shape — stays the same post-flip; ``stub``
    is now always ``False`` (the endpoint reads from JSONL and an
    empty result is still ``stub: false``, not a re-emergence of
    the v1 stub list)."""
    from kora_cli import web_server

    result = await web_server.list_recent_email()
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
    assert result["stub"] is False


@pytest.mark.asyncio
async def test_empty_jsonl_returns_empty_messages_with_stub_false(
    _isolate_config,
):
    """Both JSONLs absent (fresh deploy / empty inbox) → empty list
    + stub:false. The FE's STUB banner stays hidden in this state."""
    from kora_cli import web_server

    result = await web_server.list_recent_email()
    assert result["messages"] == []
    assert result["stub"] is False
    assert result["total_recent_24h"] == 0
    assert result["by_direction_24h"] == {"inbound": 0, "outbound": 0}
    assert result["by_status_24h"] == {}


# ---- 5. SECURITY: no raw email addresses anywhere in payload -------


@pytest.mark.asyncio
async def test_from_to_labels_are_labels_not_email_addresses(_isolate_config):
    """Bucket §2(a) layer 1: from_label / to_label are LABELS
    (joshua / kora / unknown_sender) — never raw email addresses.
    If a future handler defaults to the raw RFC-822 address when
    no label resolves, this per-field guard catches it."""
    from kora_cli import web_server

    result = await web_server.list_recent_email()
    for msg in result["messages"]:
        for field in ("from_label", "to_label"):
            value = msg[field]
            assert _EMAIL_ADDRESS.search(value) is None, (
                f"{msg['id']}: {field}={value!r} contains an email "
                f"address shape — contract requires a human label"
            )


@pytest.mark.asyncio
async def test_no_email_addresses_anywhere_in_payload(_isolate_config):
    """Walk-the-whole-payload guard (standardizing the pattern from
    KR-WEBHOOK-EVENTS / KR-AGENT-ACTIVITY / KR-SLACK-DM). Asserts
    no field anywhere in the response — top-level, per-message, or
    any nested dict — contains an email-address shape. Catches a
    future drift like adding "raw_from_address" diagnostic or
    leaking an address in subject/body."""
    from kora_cli import web_server
    import json as _json

    result = await web_server.list_recent_email()
    blob = _json.dumps(result)
    leaks = _EMAIL_ADDRESS.findall(blob)
    assert leaks == [], (
        f"payload contains raw email address(es): {leaks} — addresses "
        f"are PII and must be resolved to labels at the API edge "
        f"(bucket §2(a) layer 1 SECURITY contract)"
    )


# ---- 6. SECURITY: no token/HMAC-secret/bearer shapes ---------------


@pytest.mark.asyncio
async def test_no_purelymail_token_hints_in_payload(_isolate_config):
    """Bucket §2(a) layer 4: walk-payload regex catching Purelymail
    API token shapes anywhere. Two guards: env-var-name references
    (a leak typically includes the var name like
    `KORA_PURELYMAIL_API_TOKEN=…`) and `puremail_`/`purelymail_`
    prefix runs. A backend bug or future log-entry edit that
    leaks creds gets caught at the API edge."""
    from kora_cli import web_server
    import json as _json

    result = await web_server.list_recent_email()
    blob = _json.dumps(result)
    leaks = _PUREMAIL_TOKEN_HINT.findall(blob)
    assert leaks == [], (
        f"payload contains Purelymail token hint(s): {leaks} — token "
        f"material must never appear in API responses (bucket §2(a) "
        f"layer 4 SECURITY contract)"
    )


@pytest.mark.asyncio
async def test_no_secret_shapes_in_payload(_isolate_config):
    """Belt+braces companion to the Purelymail-token guard: 32+ hex
    runs (sha-256 / HMAC), 24+ base64 runs (token bodies), and
    Bearer/Authorization headers. None of these should appear in
    the email surface; if they do, something's leaking."""
    from kora_cli import web_server
    import json as _json

    result = await web_server.list_recent_email()
    blob = _json.dumps(result)
    hex_leaks = _HEX_SECRET_SHAPE.findall(blob)
    b64_leaks = _BASE64_TOKEN_SHAPE.findall(blob)
    bearer_leaks = _BEARER_TOKEN_SHAPE.findall(blob)
    assert hex_leaks == [], (
        f"payload contains long-hex string(s): {hex_leaks} — these "
        f"shape-match an HMAC secret or sha-256 hash"
    )
    assert b64_leaks == [], (
        f"payload contains base64-token-shape string(s): {b64_leaks}"
    )
    assert bearer_leaks == [], (
        f"payload contains Bearer/Authorization header shape(s): "
        f"{bearer_leaks}"
    )


# ---- 7. message_id pass-through (post-flip) ----------------------
#
# The v1 stub used a hardcoded `stub-msg-id-N` shape. Post-flip,
# message_id passes through from the JSONL (RFC 5322 format —
# typically `<id@<operator-domain>>`). Per the PM-locked
# message_id carve-out in the bucket spec:
#
#   "message_id may contain operator's domain in RFC 5322 format
#    (e.g. `<id@stormhavenenterprises.com>`). This is technically
#    PII-adjacent BUT operator's domain is not personal identifi-
#    cation. Decision: pass message_id through as-is (FE consumers
#    need it for threading). The walk-payload guard should EXCLUDE
#    message_id field from the email-regex check (false-positive
#    otherwise)."
#
# The empty-JSONL test path doesn't exercise this; comprehensive
# message_id projection + carve-out tests live in
# ``test_web_server_email_panel_flip.py``.


# ---- 8. SECURITY: companion FE pins ------------------------------


def test_panel_uses_no_dangerously_set_inner_html_for_body():
    """Bucket §2(a) layer 3: body_text_truncated_400 rendered as
    PLAIN TEXT. Real email bodies may contain arbitrary HTML /
    phishing payloads / scripts. React's default child escaping
    handles this — this guard catches a future edit that flips to
    dangerouslySetInnerHTML for "rich body rendering" (which
    SHOULD happen in the Purelymail web client, not here)."""
    code = _strip_ts_comments(_PANEL_PATH.read_text())
    assert "dangerouslySetInnerHTML" not in code, (
        "EmailPanel.tsx must not use dangerouslySetInnerHTML — "
        "email bodies may contain arbitrary HTML / phishing content "
        "and must render as plain text only"
    )


def test_panel_renders_body_as_child_text_node():
    """Belt+braces complement: confirm message.body_text_truncated_400
    is rendered as a JSX child expression (escaped) — collapsed
    path via truncateBody helper, expanded path direct. Both must
    be plain JSX children, not innerHTML attributes."""
    src = _PANEL_PATH.read_text()
    assert "message.body_text_truncated_400" in src, (
        "EmailPanel.tsx should reference message.body_text_truncated_400"
    )
    assert "truncateBody(message.body_text_truncated_400)" in src, (
        "EmailPanel.tsx should render the truncated body via the pure "
        "truncateBody() helper (no formatting)"
    )
    # Must appear inside a JSX expression container at least once
    assert re.search(
        r"\{[^{}]*message\.body_text_truncated_400[^{}]*\}",
        src,
    ), (
        "message.body_text_truncated_400 should appear inside a JSX "
        "expression container (rendered as a child, not an attribute)"
    )


def test_panel_renders_spoofing_warning_chip():
    """Bucket §2(b): spoofing-warning chip when spoofing_warning is
    true. Source-pin: the panel branches on spoofing_warning and
    renders a destructive-toned chip in the affected path."""
    src = _PANEL_PATH.read_text()
    assert "message.spoofing_warning" in src, (
        "EmailPanel.tsx should branch on message.spoofing_warning"
    )
    # The visible chip text "spoofing" must appear adjacent to a
    # destructive-tone Badge so the operator-attention affordance
    # can't silently vanish in a refactor.
    assert "spoofing" in src.lower() and "destructive" in src.lower(), (
        "EmailPanel.tsx should render a destructive-toned 'spoofing' "
        "affordance for messages with spoofing_warning: true"
    )


# ---- 9. by_direction_24h reconciliation ---------------------------


@pytest.mark.asyncio
async def test_by_direction_24h_sum_reconciles_to_total(_isolate_config):
    """The 24h direction breakdown must sum to total_recent_24h —
    otherwise the dashboard card's "X emails / Y flagged" headline
    won't reconcile to the panel's per-direction breakdown. Holds
    on the empty-JSONL path (both zero) AND on populated paths
    (see panel-flip tests)."""
    from kora_cli import web_server

    result = await web_server.list_recent_email()
    direction_sum = sum(result["by_direction_24h"].values())
    assert direction_sum == result["total_recent_24h"], (
        f"by_direction_24h sums to {direction_sum} but "
        f"total_recent_24h is {result['total_recent_24h']}"
    )
    assert set(result["by_direction_24h"].keys()) == _VALID_DIRECTION


@pytest.mark.asyncio
async def test_by_status_24h_only_contains_valid_status_values(_isolate_config):
    from kora_cli import web_server

    result = await web_server.list_recent_email()
    invalid = set(result["by_status_24h"].keys()) - _VALID_STATUS
    assert not invalid, (
        f"by_status_24h has unknown status key(s): {invalid} — must "
        f"be drawn from {_VALID_STATUS}"
    )


# ---- 10. Cron-regression sanity ----------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_email_registered(_isolate_config):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
