"""Tests for the KR-REASONING-PANEL stub endpoint.

Bucket §4 scenarios:
  1. GET /api/reasoning/recent returns 200
  2. Top-level shape (calls + stub:true + generated_at +
     total_recent_24h + by_model_24h + by_status_24h +
     tokens_total_24h)
  3. 4 representative stub calls present
  4. Stub spans ok @ normal + ok @ warn_75 + halted at
     hard_stop_100 + failed sdk_timeout so the operator's
     first look surfaces the cost-ladder behaviour + error
     taxonomy
  5. Per-entry shape + valid status + valid cost_rung + valid
     error_code (when status != ok)
  6. cost_rung_at_call values match the lowercase
     CostLadderRungName literal (engine.py:47-49) — NOT the
     uppercase Enum NAMES — so real data and stub agree
  7. SECURITY: no Anthropic key shapes (sk-ant- prefix +
     32+ char base64-like) anywhere in payload
  8. SECURITY: response_text_truncated_200 contains no PII
     (email regex / Slack user-ID regex) — Kora must not
     leak the inbound user's content into its response
  9. SECURITY: companion FE pin — ReasoningPanel.tsx never
     uses dangerouslySetInnerHTML for the response text
 10. response_text capped at 200 chars at the API edge
 11. tokens_total_24h sum reconciliation
 12. by_status_24h sum reconciles to total_recent_24h
 13. Cron-regression sanity
"""

import re
from pathlib import Path

import pytest


_VALID_STATUS = {"ok", "failed", "halted", "paused"}
# Per agent/cost_state_holder.py:114-117 — the wire format is the
# lowercase Enum VALUES, NOT the uppercase Enum NAMES the spec
# example payload used. Pin the lowercase shape so real CC#3
# data + this stub agree at flip time.
_VALID_COST_RUNG = {
    "normal",
    "warn_75",
    "downshift_90",
    "hard_stop_100",
    "unknown",
}
_VALID_MODEL_OR_NULL = {
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    None,
}
# ReasoningEngine error code taxonomy per PR #126 + engine.py
# docstring (lines 154-156). The 4xx / unknown variants encode
# a code class, so we accept the prefix.
_VALID_ERROR_CODE_OR_NULL_PREFIXES = (
    "sdk_auth",
    "sdk_rate_limited",
    "sdk_5xx",
    "sdk_4xx_",
    "sdk_timeout",
    "sdk_transport",
    "sdk_unknown_",
    "cost_ladder_halted",
    "operational_state_paused",
    "response_projection_failed",
)

# Anthropic API key shape: sk-ant- prefix + base64-like body.
# The real format is sk-ant-api03-<long base64> for Console keys
# and sk-ant-oat01-<long base64> for OAuth tokens; both have a
# multi-char marker + a long body. Match conservatively to
# catch any future leak shape.
_ANTHROPIC_KEY_SHAPE = re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b")

# Email-address PII (same shape as KR-EMAIL-PANEL guard) — Kora's
# generated response must NOT contain identifying email addresses
# from the inbound user's context.
_EMAIL_ADDRESS = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)
# Raw Slack user ID PII (same shape as KR-SLACK-DM-PANEL guard).
_RAW_SLACK_USER_ID = re.compile(r"\bU[A-Z0-9]{8,}\b")


_REPO_ROOT = Path(__file__).resolve().parents[2]
_PANEL_PATH = _REPO_ROOT / "web" / "src" / "pages" / "ReasoningPanel.tsx"


def _strip_ts_comments(src: str) -> str:
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

    result = await web_server.list_recent_reasoning()
    assert isinstance(result, dict)


# ---- 2. Top-level shape ---------------------------------------------


@pytest.mark.asyncio
async def test_response_shape_has_required_keys(_isolate_config):
    from kora_cli import web_server

    result = await web_server.list_recent_reasoning()
    assert set(result.keys()) == {
        "calls",
        "stub",
        "generated_at",
        "total_recent_24h",
        "by_model_24h",
        "by_status_24h",
        "tokens_total_24h",
    }
    assert isinstance(result["calls"], list)
    assert isinstance(result["generated_at"], str)
    assert isinstance(result["total_recent_24h"], int)
    assert isinstance(result["by_model_24h"], dict)
    assert isinstance(result["by_status_24h"], dict)
    assert isinstance(result["tokens_total_24h"], dict)
    assert result["stub"] is True


# ---- 3. Expected stub calls ----------------------------------------


@pytest.mark.asyncio
async def test_stub_returns_four_representative_calls(_isolate_config):
    """Pin the bucket §3(a) canonical 4-call stub list. CC#3's
    KR-FEAT-AI-RESPONSE-LOOP ST2 follow-on will swap the body to
    read reasoning entries from slack_dm_log.jsonl, but the shape
    stays stable so the FE keeps rendering during cut-over."""
    from kora_cli import web_server

    result = await web_server.list_recent_reasoning()
    assert len(result["calls"]) == 4
    ids = {c["id"] for c in result["calls"]}
    assert ids == {"stub-1", "stub-2", "stub-3", "stub-4"}


@pytest.mark.asyncio
async def test_stub_spans_ok_warn_halted_failed(_isolate_config):
    """The 4 stub calls deliberately span the cost-ladder behaviour
    (normal opus + warn_75 sonnet + hard_stop_100 halted) AND the
    error taxonomy (sdk_timeout) so the operator's first look
    shows the four most-important states. Pin so a future stub
    edit can't homogenize."""
    from kora_cli import web_server

    result = await web_server.list_recent_reasoning()
    statuses = {c["status"] for c in result["calls"]}
    rungs = {c["cost_rung_at_call"] for c in result["calls"]}
    error_codes = {c["error_code"] for c in result["calls"]}
    assert "ok" in statuses
    assert "halted" in statuses
    assert "failed" in statuses
    assert "normal" in rungs
    assert "warn_75" in rungs
    assert "hard_stop_100" in rungs
    assert "cost_ladder_halted" in error_codes
    assert "sdk_timeout" in error_codes


# ---- 4. Per-entry shape + enums ------------------------------------


@pytest.mark.asyncio
async def test_each_call_has_required_keys_and_valid_enums(_isolate_config):
    from kora_cli import web_server

    result = await web_server.list_recent_reasoning()
    required = {
        "id",
        "triggered_by",
        "started_at",
        "duration_ms",
        "model_used",
        "cost_rung_at_call",
        "input_tokens",
        "output_tokens",
        "status",
        "error_code",
        "response_text_truncated_200",
    }
    for call in result["calls"]:
        assert set(call.keys()) == required, (
            f"{call.get('id', '?')}: keys mismatch {set(call.keys())}"
        )
        assert call["status"] in _VALID_STATUS
        assert call["cost_rung_at_call"] in _VALID_COST_RUNG, (
            f"{call['id']}: cost_rung={call['cost_rung_at_call']!r} not in "
            f"{_VALID_COST_RUNG} (must be lowercase CostRung.value, not "
            f"the uppercase Enum name)"
        )
        assert call["model_used"] in _VALID_MODEL_OR_NULL, (
            f"{call['id']}: model_used={call['model_used']!r}"
        )
        assert isinstance(call["started_at"], str) and call["started_at"].endswith("Z")
        assert isinstance(call["duration_ms"], int) and call["duration_ms"] >= 0
        assert isinstance(call["input_tokens"], int) and call["input_tokens"] >= 0
        assert isinstance(call["output_tokens"], int) and call["output_tokens"] >= 0
        # error_code is null when status == "ok"; otherwise must
        # match the ReasoningEngine taxonomy (PR #126).
        if call["status"] == "ok":
            assert call["error_code"] is None, (
                f"{call['id']}: ok status must have null error_code"
            )
        else:
            assert call["error_code"] is not None, (
                f"{call['id']}: non-ok status must surface an error_code"
            )
            assert any(
                call["error_code"].startswith(p)
                for p in _VALID_ERROR_CODE_OR_NULL_PREFIXES
            ), (
                f"{call['id']}: error_code={call['error_code']!r} doesn't "
                f"match the ReasoningEngine taxonomy"
            )
        # response_text is plain string or null (capped at 200 chars
        # at the API edge — checked separately)
        assert call["response_text_truncated_200"] is None or isinstance(
            call["response_text_truncated_200"], str
        )


# ---- 5. cost_rung wire-format pin (K-DG catch) ---------------------


@pytest.mark.asyncio
async def test_cost_rung_uses_lowercase_value_strings_not_enum_names(_isolate_config):
    """K-DG drift catch: the bucket spec example payload used the
    uppercase Enum NAMES (NORMAL / WARN_75 / DOWNSHIFT_90 /
    HARD_STOP_100), but the canonical wire format per
    agent/cost_state_holder.py:114-117 is the lowercase Enum
    VALUES ("normal" / "warn_75" / ...). CC#3's real data will
    emit lowercase via CostLadderRungName literal in
    engine.py:47-49. Pin lowercase so a future stub edit
    revertingto the spec's uppercase doesn't masquerade as
    working until the flip."""
    from kora_cli import web_server

    result = await web_server.list_recent_reasoning()
    for call in result["calls"]:
        rung = call["cost_rung_at_call"]
        assert rung == rung.lower(), (
            f"{call['id']}: cost_rung_at_call={rung!r} must be lowercase "
            f"(CostRung.value wire format), not the uppercase Enum NAME"
        )


# ---- 6. SECURITY: no Anthropic key shapes anywhere ----------------


@pytest.mark.asyncio
async def test_no_anthropic_key_shapes_in_payload(_isolate_config):
    """Bucket §3(a) SECURITY layer 2: walk-payload regex catches
    Anthropic key shapes (sk-ant- prefix + base64-like body)
    anywhere in the response. A future error-projection bug or
    log-entry edit that leaks credential material into the
    operator's view gets caught at the API edge, not in their
    browser (where it could end up in diag bundles or screenshots).
    """
    from kora_cli import web_server
    import json as _json

    result = await web_server.list_recent_reasoning()
    blob = _json.dumps(result)
    leaks = _ANTHROPIC_KEY_SHAPE.findall(blob)
    assert leaks == [], (
        f"payload contains Anthropic key shape(s): {leaks} — credential "
        f"material must never appear in API responses (bucket §3(a) "
        f"SECURITY layer 2)"
    )


# ---- 7. SECURITY: no PII in response_text -------------------------


@pytest.mark.asyncio
async def test_response_text_contains_no_pii(_isolate_config):
    """Bucket §3(a) SECURITY layer 3: response_text_truncated_200
    must not leak identifying patterns from the inbound user's
    message context — no email addresses (KR-EMAIL-PANEL shape)
    nor raw Slack user IDs (KR-SLACK-DM-PANEL shape). Kora's
    generated text is operator-visible; the inbound message
    content lives in SLACK-DM-PANEL, not here."""
    from kora_cli import web_server

    result = await web_server.list_recent_reasoning()
    for call in result["calls"]:
        text = call["response_text_truncated_200"]
        if text is None:
            continue
        email_leaks = _EMAIL_ADDRESS.findall(text)
        slack_leaks = _RAW_SLACK_USER_ID.findall(text)
        assert email_leaks == [], (
            f"{call['id']}: response_text contains email address(es): "
            f"{email_leaks}"
        )
        assert slack_leaks == [], (
            f"{call['id']}: response_text contains raw Slack user "
            f"ID(s): {slack_leaks}"
        )


@pytest.mark.asyncio
async def test_no_pii_anywhere_in_payload(_isolate_config):
    """Belt+braces walk-payload sweep — any field in the response
    (not just response_text) that contains email/Slack-ID PII
    fails. Catches a future drift like adding a "context_summary"
    diagnostic that leaks the user's address."""
    from kora_cli import web_server
    import json as _json

    result = await web_server.list_recent_reasoning()
    blob = _json.dumps(result)
    assert _EMAIL_ADDRESS.findall(blob) == [], (
        "reasoning payload contains email address PII anywhere"
    )
    assert _RAW_SLACK_USER_ID.findall(blob) == [], (
        "reasoning payload contains raw Slack user ID PII anywhere"
    )


# ---- 8. SECURITY: companion FE plain-text rendering pin -----------


def test_panel_uses_no_dangerously_set_inner_html_for_response_text():
    """Bucket §3(a) SECURITY layer 1: response_text rendered as
    PLAIN TEXT via React's default child escaping. Real responses
    may contain anything Kora generates (HTML / markdown / script
    fragments). This guard catches a future edit that switches to
    dangerouslySetInnerHTML for 'richer rendering'."""
    code = _strip_ts_comments(_PANEL_PATH.read_text())
    assert "dangerouslySetInnerHTML" not in code, (
        "ReasoningPanel.tsx must not use dangerouslySetInnerHTML — "
        "response text is model-generated content"
    )


def test_panel_renders_response_text_as_child_expression():
    """Belt+braces companion: response_text_truncated_200 rendered
    as a JSX child expression (escaped), via the pure truncateText
    helper for the collapsed view."""
    src = _PANEL_PATH.read_text()
    assert "call.response_text_truncated_200" in src, (
        "ReasoningPanel.tsx should reference response_text_truncated_200"
    )
    assert re.search(
        r"\{[^{}]*call\.response_text_truncated_200[^{}]*\}",
        src,
    ), (
        "response_text should appear inside a JSX expression container "
        "(rendered as a child, not an attribute)"
    )


# ---- 9. 200-char cap pinning --------------------------------------


@pytest.mark.asyncio
async def test_response_text_capped_at_200_chars(_isolate_config):
    """Bucket §3(a): backend caps response_text at 200 chars at the
    API edge (field-name encodes the cap). FE then truncates further
    for the collapsed-view excerpt. Pin so a future backend edit
    can't accidentally start sending unbounded text."""
    from kora_cli import web_server

    result = await web_server.list_recent_reasoning()
    for call in result["calls"]:
        text = call["response_text_truncated_200"]
        if text is None:
            continue
        assert len(text) <= 200, (
            f"{call['id']}: response_text length {len(text)} exceeds the "
            f"200-char API-edge cap"
        )


# ---- 10. Aggregate reconciliation --------------------------------


@pytest.mark.asyncio
async def test_by_status_24h_sum_reconciles_to_total(_isolate_config):
    from kora_cli import web_server

    result = await web_server.list_recent_reasoning()
    status_sum = sum(result["by_status_24h"].values())
    assert status_sum == result["total_recent_24h"], (
        f"by_status_24h sums to {status_sum} but total_recent_24h is "
        f"{result['total_recent_24h']}"
    )


@pytest.mark.asyncio
async def test_by_status_24h_keys_are_valid(_isolate_config):
    from kora_cli import web_server

    result = await web_server.list_recent_reasoning()
    invalid = set(result["by_status_24h"].keys()) - _VALID_STATUS
    assert not invalid, (
        f"by_status_24h has unknown status key(s): {invalid}"
    )


@pytest.mark.asyncio
async def test_tokens_total_24h_has_input_and_output(_isolate_config):
    from kora_cli import web_server

    result = await web_server.list_recent_reasoning()
    assert set(result["tokens_total_24h"].keys()) == {"input", "output"}
    assert all(
        isinstance(v, int) and v >= 0
        for v in result["tokens_total_24h"].values()
    )


# ---- 11. Cron-regression sanity -----------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_reasoning_registered(_isolate_config):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
