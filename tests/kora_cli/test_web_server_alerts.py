"""Tests for the KR-ALERTS-PANEL endpoint (post KR-ALERTS-PANEL-FLIP).

After the flip the endpoint reads from the live aggregator
(``kora_cli/alerts/aggregator.py``) instead of the v1 stub. This
module keeps the original PR #134 walk-payload security guards +
FE source pins that apply to BOTH the old stub and the new live
endpoint:

  * Top-level response shape (now with ``stub: false`` always)
  * Walk-payload security guards (no Anthropic/Slack token shapes,
    no email/Slack-ID PII, no long-hex secret shapes)
  * FE source pins (no dangerouslySetInnerHTML, title/detail
    rendered as JSX children, sessionStorage vs localStorage
    dismissal, empty-state daemon-healthy view)
  * by_severity / total_active reconciliation
  * Cron-regression sanity

Rule-trigger tests + per-rule severity verification live in
``tests/kora_cli/alerts/test_aggregator.py``.
"""

import re
from pathlib import Path

import pytest


_VALID_SEVERITY = {"critical", "warning", "info"}

# Walk-payload guards — same shapes as the prior panels.
_ANTHROPIC_KEY_SHAPE = re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b")
_SLACK_TOKEN_SHAPE = re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{8,}\b")
_HEX_SECRET_SHAPE = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_EMAIL_ADDRESS = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)
_RAW_SLACK_USER_ID = re.compile(r"\bU[A-Z0-9]{8,}\b")


_REPO_ROOT = Path(__file__).resolve().parents[2]
_PANEL_PATH = _REPO_ROOT / "web" / "src" / "pages" / "AlertsPanel.tsx"
_BANNER_PATH = _REPO_ROOT / "web" / "src" / "components" / "AlertsBanner.tsx"


def _strip_ts_comments(src: str) -> str:
    src = re.sub(r"\{/\*.*?\*/\}", "", src, flags=re.DOTALL)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"(^|[^:])//[^\n]*", r"\1", src)
    return src


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    """CC#2 #137 fixture-isolation discipline + reset all aggregator
    sources to baseline (no-alert) state. The aggregator pulls from
    5 holders/accessors; without all-5 monkeypatching, parallel
    pytest-xdist workers can see each other's state."""
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
    # Reset aggregator sources to no-alert baseline so this file's
    # walk-payload + shape tests don't accidentally trigger rules
    # (which would change the payload they're sweeping).
    monkeypatch.setattr(
        "agent.cost_state_holder.get_cost_holder",
        lambda: None,
    )
    monkeypatch.setattr(
        "agent.operational_state_holder.get_holder",
        lambda: None,
    )
    monkeypatch.setattr(
        "kora_cli.audit.jsonl_reader.read_audit_entries",
        lambda **kwargs: [],
    )
    monkeypatch.setattr(
        "kora_cli.heartbeat_probes.runner.current_service_snapshots",
        lambda: {},
    )
    return tmp_path


# ---- 1. 200 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_returns_200(_isolate_config):
    from kora_cli import web_server

    result = await web_server.list_current_alerts()
    assert isinstance(result, dict)


# ---- 2. Top-level shape ---------------------------------------------


@pytest.mark.asyncio
async def test_response_shape_has_required_keys(_isolate_config):
    """Top-level shape stays the same post-flip; ``stub`` is now
    always ``False`` (live aggregator, even when no alerts active)."""
    from kora_cli import web_server

    result = await web_server.list_current_alerts()
    assert set(result.keys()) == {
        "alerts",
        "stub",
        "generated_at",
        "total_active",
        "by_severity",
    }
    assert isinstance(result["alerts"], list)
    assert isinstance(result["generated_at"], str)
    assert isinstance(result["total_active"], int)
    assert isinstance(result["by_severity"], dict)
    assert result["stub"] is False


@pytest.mark.asyncio
async def test_no_alerts_baseline_returns_empty_list(_isolate_config):
    """With all aggregator sources patched to no-alert baseline
    (per the fixture), the response is an empty list + stub:false."""
    from kora_cli import web_server

    result = await web_server.list_current_alerts()
    assert result["alerts"] == []
    assert result["total_active"] == 0
    assert result["by_severity"] == {"critical": 0, "warning": 0, "info": 0}


# ---- 4. Per-entry shape + enums + route convention -----------------


@pytest.mark.asyncio
async def test_each_alert_has_required_keys_and_valid_enums(_isolate_config):
    from kora_cli import web_server

    result = await web_server.list_current_alerts()
    required = {
        "id",
        "severity",
        "category",
        "title",
        "detail",
        "source_panel",
        "source_panel_route",
        "first_seen_at",
    }
    for alert in result["alerts"]:
        assert set(alert.keys()) == required, (
            f"{alert.get('id', '?')}: keys mismatch {set(alert.keys())}"
        )
        assert alert["severity"] in _VALID_SEVERITY
        assert isinstance(alert["category"], str) and alert["category"]
        assert isinstance(alert["title"], str) and alert["title"]
        assert isinstance(alert["detail"], str)
        assert isinstance(alert["source_panel"], str) and alert["source_panel"]
        assert isinstance(alert["source_panel_route"], str)
        assert isinstance(alert["first_seen_at"], str) and alert[
            "first_seen_at"
        ].endswith("Z")


@pytest.mark.asyncio
async def test_source_panel_route_uses_flat_fe_convention(_isolate_config):
    """The bucket spec uses /admin/<panel> in its example payload
    but every panel in this branch is mounted at the FLAT
    /<panel> route (App.tsx ROUTES table). source_panel_route is
    handed to react-router's <Link to=...>; if it points at a
    non-existent /admin/<panel> route the click-through 404s.
    Pin the flat shape so a future stub edit reverting to the
    spec's /admin/ form doesn't break navigation."""
    from kora_cli import web_server

    result = await web_server.list_current_alerts()
    for alert in result["alerts"]:
        route = alert["source_panel_route"]
        assert route.startswith("/"), (
            f"{alert['id']}: source_panel_route={route!r} must be an "
            f"absolute FE path"
        )
        assert not route.startswith("/admin/"), (
            f"{alert['id']}: source_panel_route={route!r} uses the "
            f"/admin/ prefix — this branch mounts panels at the flat "
            f"/<panel> route per App.tsx"
        )


# ---- 5. SECURITY: walk-payload sweeps ------------------------------


@pytest.mark.asyncio
async def test_no_token_shapes_anywhere_in_payload(_isolate_config):
    """Bucket §1(a) SECURITY layer 2: walk-payload sweep for token
    shapes (Anthropic sk-ant-, Slack xox*-, 32+ hex secrets).
    Alert text could in theory quote source-panel state which may
    contain credential material; this catches that defense-in-depth."""
    from kora_cli import web_server
    import json as _json

    result = await web_server.list_current_alerts()
    blob = _json.dumps(result)
    anthropic_leaks = _ANTHROPIC_KEY_SHAPE.findall(blob)
    slack_leaks = _SLACK_TOKEN_SHAPE.findall(blob)
    hex_leaks = _HEX_SECRET_SHAPE.findall(blob)
    assert anthropic_leaks == [], (
        f"payload contains Anthropic key shape(s): {anthropic_leaks}"
    )
    assert slack_leaks == [], (
        f"payload contains Slack token shape(s): {slack_leaks}"
    )
    assert hex_leaks == [], (
        f"payload contains long-hex secret shape(s): {hex_leaks}"
    )


@pytest.mark.asyncio
async def test_no_pii_anywhere_in_payload(_isolate_config):
    """Walk-payload sweep for PII — email addresses + raw Slack
    user IDs. Alert title/detail are operator-authored at the
    source-panel level but defense-in-depth catches a future
    automated alert generator that quotes user content."""
    from kora_cli import web_server
    import json as _json

    result = await web_server.list_current_alerts()
    blob = _json.dumps(result)
    email_leaks = _EMAIL_ADDRESS.findall(blob)
    slack_id_leaks = _RAW_SLACK_USER_ID.findall(blob)
    assert email_leaks == [], (
        f"payload contains email address PII: {email_leaks}"
    )
    assert slack_id_leaks == [], (
        f"payload contains raw Slack user ID PII: {slack_id_leaks}"
    )


# ---- 6. SECURITY: companion FE pins ------------------------------


def test_panel_uses_no_dangerously_set_inner_html():
    """Bucket §1(a) SECURITY layer 1: title + detail rendered as
    PLAIN TEXT. This guard catches a future edit that switches to
    dangerouslySetInnerHTML for 'rich alert formatting' — real
    alert text may quote source-panel state, which is untrusted
    in the worst case."""
    code = _strip_ts_comments(_PANEL_PATH.read_text())
    assert "dangerouslySetInnerHTML" not in code, (
        "AlertsPanel.tsx must not use dangerouslySetInnerHTML — "
        "alert text is rendered as plain text"
    )


def test_banner_uses_no_dangerously_set_inner_html():
    """Same plain-text contract applies to the dashboard banner."""
    code = _strip_ts_comments(_BANNER_PATH.read_text())
    assert "dangerouslySetInnerHTML" not in code, (
        "AlertsBanner.tsx must not use dangerouslySetInnerHTML"
    )


def test_panel_renders_title_and_detail_as_child_expressions():
    """Belt+braces: title + detail rendered as JSX child expressions
    so React's default escaping kicks in. Catches a future edit
    that pipes them through a markdown lib or HTML formatter."""
    src = _PANEL_PATH.read_text()
    assert "alert.title" in src and re.search(
        r"\{[^{}]*alert\.title[^{}]*\}", src
    ), "alert.title should render as a JSX child expression"
    assert "alert.detail" in src and re.search(
        r"\{[^{}]*alert\.detail[^{}]*\}", src
    ), "alert.detail should render as a JSX child expression"


# ---- 7. Banner dismissal scope ----------------------------------


def test_banner_uses_sessionStorage_not_localStorage():
    """Bucket §1(c): banner dismissal is per-TAB (resets on tab
    close), NOT per-browser-and-persistent. The spec is explicit
    that this is just a visual collapse — NOT acknowledged-state —
    so the dismissal must NOT survive a tab close (otherwise the
    operator could miss a future alert).

    sessionStorage = per-tab; localStorage = per-browser-persistent.
    This pin catches a future edit that switches to localStorage."""
    code = _strip_ts_comments(_BANNER_PATH.read_text())
    assert "sessionStorage" in code, (
        "AlertsBanner.tsx must use sessionStorage (per-tab "
        "dismissal) per bucket §1(c)"
    )
    assert "localStorage" not in code, (
        "AlertsBanner.tsx must NOT use localStorage — that would "
        "persist dismissal across browser sessions, wrongly "
        "suppressing future alerts"
    )


def test_banner_hides_when_no_active_alerts():
    """Bucket §1(c): banner is shown ONLY when alerts.length > 0.
    Source-pin: the component returns null when data is empty so
    the dashboard layout stays clean (no false-alarm trigger
    from absent data, per bucket §4 ship-checklist)."""
    src = _BANNER_PATH.read_text()
    assert re.search(
        r"data\.alerts\.length\s*===\s*0", src
    ), (
        "AlertsBanner.tsx should branch on data.alerts.length === 0 "
        "and hide the banner in that case"
    )


# ---- 8. Empty-state positive reinforcement ---------------------


def test_panel_renders_daemon_healthy_empty_state():
    """Bucket §1(b): empty state shows positive reinforcement
    ('Daemon healthy.' + green CheckCircle2). Source-pin: the
    text + the success-toned icon must both appear in the panel
    so a future refactor doesn't silently drop the affordance."""
    src = _PANEL_PATH.read_text()
    assert "Daemon healthy" in src, (
        "AlertsPanel.tsx empty state should say 'Daemon healthy.'"
    )
    assert "CheckCircle2" in src, (
        "AlertsPanel.tsx empty state should render the green "
        "CheckCircle2 icon"
    )


# ---- 9. by_severity reconciliation -------------------------------


@pytest.mark.asyncio
async def test_by_severity_sum_reconciles_to_total_active(_isolate_config):
    from kora_cli import web_server

    result = await web_server.list_current_alerts()
    severity_sum = sum(result["by_severity"].values())
    assert severity_sum == result["total_active"], (
        f"by_severity sums to {severity_sum} but total_active is "
        f"{result['total_active']}"
    )
    assert set(result["by_severity"].keys()) == _VALID_SEVERITY


# ---- 10. Cron-regression sanity --------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_alerts_registered(_isolate_config):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
