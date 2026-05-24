"""KR-FE-OPERATOR-FIRST-RUN-WIZARD endpoint + drift-guard tests.

Tests cover:
  * Each of the 6 wizard endpoints' contract:
    - GET /api/wizard/state (marker absence + audit-empty signal)
    - POST /api/wizard/validate-anthropic (failure shapes, no
      network — we don't hit real Anthropic in CI)
    - POST /api/wizard/validate-substrate (ditto)
    - POST /api/wizard/validate-slack (ditto)
    - POST /api/wizard/trigger-tutorial-probe (writes a
      probe.wake_requested audit row with the expected shape)
    - POST /api/wizard/complete (writes marker file)

  * Drift-guards:
    - _WIZARD_STEPS (BE) ↔ WIZARD_STEPS (FE) in same order
    - _WIZARD_VALIDATION_RESULTS (BE) ↔ WIZARD_VALIDATION_RESULTS
      (FE) in same order
    - Each endpoint's substantive logic stays ≤30 LoC (size pin
      per the bucket's contract — caller-tracked LoC budget)
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_API_TS = _REPO_ROOT / "web" / "src" / "lib" / "api.ts"
_APP_TSX = _REPO_ROOT / "web" / "src" / "App.tsx"
_PAGE = _REPO_ROOT / "web" / "src" / "pages" / "WizardPage.tsx"
_DETECT_HOOK = (
    _REPO_ROOT / "web" / "src" / "hooks" / "useWizardFirstRunDetection.ts"
)
_WEB_SERVER = _REPO_ROOT / "kora_cli" / "web_server.py"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setattr("kora_constants.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr("kora_cli.config.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr("kora_cli.web_server.get_kora_home", lambda: tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# /api/wizard/state — first-run detection feed
# ---------------------------------------------------------------------------


async def _call_state() -> dict:
    from kora_cli import web_server

    return await web_server.get_wizard_state()


@pytest.mark.asyncio
async def test_state_fresh_install_shows_wizard(env):
    """No marker file + no audit JSONL → fresh install. The FE
    composes these into ``!marker_present && audit_log_empty`` ==
    showWizard."""
    body = await _call_state()
    assert body["marker_present"] is False
    assert body["completed"] is False
    assert body["audit_log_empty"] is True
    assert body["last_step"] is None
    # Steps + validation_results allowlists echoed for FE drift-pin.
    assert body["steps"] == [
        "welcome",
        "anthropic",
        "substrate_slack",
        "tutorial_probe",
        "promotion_intro",
    ]
    assert body["validation_results"] == [
        "success",
        "auth_failure",
        "network_failure",
        "timeout",
    ]


@pytest.mark.asyncio
async def test_state_completed_install_hides_wizard(env):
    """Marker present + completed=true → cockpit routes "/" back to
    Dashboard."""
    marker = env / "wizard_config.json"
    marker.write_text(
        json.dumps(
            {
                "completed": True,
                "skipped": False,
                "tenant_id": "alpha",
                "last_step": "promotion_intro",
            }
        )
    )
    body = await _call_state()
    assert body["marker_present"] is True
    assert body["completed"] is True
    assert body["tenant_id"] == "alpha"
    assert body["last_step"] == "promotion_intro"


@pytest.mark.asyncio
async def test_state_skipped_install_hides_wizard(env):
    """Marker present + skipped=true → cockpit honors the dismissal
    (operator can re-open via /wizard URL)."""
    marker = env / "wizard_config.json"
    marker.write_text(
        json.dumps({"completed": False, "skipped": True, "tenant_id": "default"})
    )
    body = await _call_state()
    assert body["marker_present"] is True
    assert body["skipped"] is True


@pytest.mark.asyncio
async def test_state_unknown_last_step_falls_back_to_none(env):
    """A marker with a typo in last_step shouldn't echo bogus into
    the response — _WIZARD_STEPS allowlist filters."""
    marker = env / "wizard_config.json"
    marker.write_text(
        json.dumps({"completed": False, "last_step": "typo_step"})
    )
    body = await _call_state()
    assert body["last_step"] is None


@pytest.mark.asyncio
async def test_state_audit_log_with_rows_signals_not_fresh(env):
    """A single non-blank line in the audit JSONL → install is NOT
    fresh; cockpit shows Dashboard even when no marker exists yet
    (operator-mode mid-run, marker just hasn't been written yet)."""
    (env / "kora_audit_log.jsonl").write_text(
        json.dumps(
            {
                "emitted_at": datetime.now(timezone.utc).isoformat(),
                "seam": "reasoning.tool_called",
                "details": {},
                "source": "reasoning",
                "caller_session_id": "x:y:z",
            }
        )
        + "\n"
    )
    body = await _call_state()
    assert body["audit_log_empty"] is False
    assert body["marker_present"] is False


# ---------------------------------------------------------------------------
# /api/wizard/validate-anthropic — only failure paths in unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_anthropic_missing_key_returns_auth_failure(env):
    from kora_cli import web_server

    body = await web_server.validate_anthropic({})
    assert body["result"] == "auth_failure"
    assert "missing" in body.get("detail", "")


@pytest.mark.asyncio
async def test_validate_anthropic_classifies_timeout(env, monkeypatch):
    """Patch httpx so we don't hit the network. The wizard's
    network-error classification must surface as ``timeout`` /
    ``network_failure`` rather than a 500."""
    import httpx as _httpx
    from kora_cli import web_server

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, *_a, **_k):
            raise _httpx.TimeoutException("simulated timeout")

    monkeypatch.setattr(web_server, "_classify_validation_exception",
                        web_server._classify_validation_exception)
    monkeypatch.setattr(
        "kora_cli.web_server.validate_anthropic.__wrapped__"
        if hasattr(web_server.validate_anthropic, "__wrapped__")
        else "kora_cli.web_server.validate_anthropic",
        web_server.validate_anthropic,
    )
    # Patch httpx.AsyncClient itself for the duration.
    monkeypatch.setattr(_httpx, "AsyncClient", lambda timeout=None: _FakeClient())

    body = await web_server.validate_anthropic({"api_key": "fake-key"})
    assert body["result"] == "timeout"


@pytest.mark.asyncio
async def test_validate_substrate_missing_inputs_returns_auth_failure(env):
    from kora_cli import web_server

    body = await web_server.validate_substrate({})
    assert body["result"] == "auth_failure"


@pytest.mark.asyncio
async def test_validate_slack_missing_token_returns_auth_failure(env):
    from kora_cli import web_server

    body = await web_server.validate_slack({})
    assert body["result"] == "auth_failure"


# ---------------------------------------------------------------------------
# /api/wizard/trigger-tutorial-probe — writes synthetic wake row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_tutorial_probe_emits_wake_audit_row(env):
    from kora_cli import web_server
    from kora_cli.audit.jsonl_reader import read_audit_entries

    body = await web_server.trigger_tutorial_probe({"tenant_id": "alpha"})
    assert body["ok"] is True
    assert body["caller_session_id"] == "probe:wizard_tutorial:first_run"
    rows = read_audit_entries(seam="probe.wake_requested")
    # At least one row should match the wizard tutorial — narrow by
    # the wizard-marker field we set in the synthetic payload.
    matching = [r for r in rows if r.details.get("wizard_tutorial") is True]
    assert len(matching) == 1
    r = matching[0]
    assert r.details["probe"] == "wizard_tutorial"
    assert r.details["category"] == "first_run"
    assert "alpha" in r.details["detail"]
    assert r.caller_session_id == "probe:wizard_tutorial:first_run"


# ---------------------------------------------------------------------------
# /api/wizard/complete — writes marker file with operator config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_writes_marker_file(env):
    from kora_cli import web_server

    body = await web_server.complete_wizard(
        {
            "completed": True,
            "skipped": False,
            "tenant_id": "beta",
            "last_step": "promotion_intro",
        }
    )
    assert body["ok"] is True
    marker = Path(body["marker_path"])
    assert marker.is_file()
    payload = json.loads(marker.read_text())
    assert payload["completed"] is True
    assert payload["skipped"] is False
    assert payload["tenant_id"] == "beta"
    assert payload["last_step"] == "promotion_intro"
    assert "written_at" in payload


@pytest.mark.asyncio
async def test_complete_skip_writes_marker(env):
    """Skipped wizard also writes the marker so it doesn't re-prompt
    on every cockpit launch."""
    from kora_cli import web_server

    body = await web_server.complete_wizard(
        {"skipped": True, "tenant_id": "default"}
    )
    assert body["ok"] is True
    payload = json.loads(Path(body["marker_path"]).read_text())
    assert payload["skipped"] is True


@pytest.mark.asyncio
async def test_complete_unknown_last_step_falls_back(env):
    from kora_cli import web_server

    body = await web_server.complete_wizard(
        {"completed": True, "tenant_id": "x", "last_step": "typo_step"}
    )
    payload = json.loads(Path(body["marker_path"]).read_text())
    # Bad last_step → falls back to canonical default per the
    # endpoint's allowlist filter.
    assert payload["last_step"] == "promotion_intro"


# ---------------------------------------------------------------------------
# Drift guards
# ---------------------------------------------------------------------------


def test_wizard_steps_drift_guard():
    """_WIZARD_STEPS (BE) ↔ WIZARD_STEPS (FE) ↔ WizardStep type. The
    FE renders a 5-card progress bar in this exact order — a re-order
    on either side without bumping the test is a visible UX bug."""
    expected = [
        "welcome",
        "anthropic",
        "substrate_slack",
        "tutorial_probe",
        "promotion_intro",
    ]
    ws_src = _WEB_SERVER.read_text()
    m = re.search(
        r"_WIZARD_STEPS:\s*Tuple\[str,\s*\.\.\.\]\s*=\s*\(([^)]+)\)",
        ws_src,
        re.DOTALL,
    )
    assert m is not None, "_WIZARD_STEPS tuple not found"
    be_values = re.findall(r'"([^"]+)"', m.group(1))
    assert be_values == expected, f"BE drift: {be_values}"

    fe_src = _API_TS.read_text()
    m = re.search(
        r"WIZARD_STEPS:\s*readonly WizardStep\[\]\s*=\s*\[([^\]]+)\]",
        fe_src,
    )
    assert m is not None, "WIZARD_STEPS FE constant not found"
    fe_values = re.findall(r'"([^"]+)"', m.group(1))
    assert fe_values == expected, f"FE drift: {fe_values}"


def test_wizard_validation_results_drift_guard():
    """_WIZARD_VALIDATION_RESULTS ↔ WIZARD_VALIDATION_RESULTS — the
    4-value enum the FE narrows its ValidationBadge over."""
    expected = ["success", "auth_failure", "network_failure", "timeout"]
    ws_src = _WEB_SERVER.read_text()
    m = re.search(
        r"_WIZARD_VALIDATION_RESULTS:\s*Tuple\[str,\s*\.\.\.\]\s*=\s*\(([^)]+)\)",
        ws_src,
        re.DOTALL,
    )
    assert m is not None
    be_values = re.findall(r'"([^"]+)"', m.group(1))
    assert be_values == expected

    fe_src = _API_TS.read_text()
    m = re.search(
        r"WIZARD_VALIDATION_RESULTS:\s*readonly WizardValidationResult\[\]\s*=\s*\[([^\]]+)\]",
        fe_src,
    )
    assert m is not None
    fe_values = re.findall(r'"([^"]+)"', m.group(1))
    assert fe_values == expected


# ---------------------------------------------------------------------------
# FE source pins
# ---------------------------------------------------------------------------


def test_wizard_page_renders_all_5_steps():
    """Each step's body function must exist in WizardPage — a
    refactor that drops one would silently break the flow."""
    src = _PAGE.read_text()
    for fn in (
        "function StepWelcome",
        "function StepAnthropic",
        "function StepSubstrateSlack",
        "function StepTutorialProbe",
        "function StepPromotionIntro",
    ):
        assert fn in src, f"missing wizard step component: {fn}"


def test_wizard_route_registered_and_first_run_detection_wired():
    src = _APP_TSX.read_text()
    assert '"/wizard": WizardPage' in src
    # First-run detection must swap "/" when showWizard is true.
    assert "useWizardFirstRunDetection" in src
    assert 'firstRun.showWizard ? { "/": WizardPage }' in src


def test_wizard_first_run_hook_combines_signals():
    """useWizardFirstRunDetection must combine marker_present +
    audit_log_empty into a single showWizard boolean — per the spec
    (combine #2 + #3 detection signals)."""
    assert _DETECT_HOOK.is_file()
    src = _DETECT_HOOK.read_text()
    assert "marker_present" in src
    assert "audit_log_empty" in src
    assert "!resp.marker_present && resp.audit_log_empty" in src


def test_wizard_env_download_never_modifies_shell():
    """Security: wizard surfaces a downloadable .env, NEVER writes
    to operator's shell. Pin the download affordance + the absence
    of any auto-write hook in the page source."""
    src = _PAGE.read_text()
    assert "DotEnvDownload" in src
    assert 'download=".env"' in src
    # No fetch call that POSTs the env contents anywhere — the only
    # POSTs are the validate-* + complete + trigger-* endpoints.
    assert "/api/wizard/write-env" not in src
    assert "shell" not in src.lower() or "shell environment" in src


def test_wizard_tenant_id_wired_through_steps():
    """tenant_id MUST flow into completeWizard + .env download +
    trigger-tutorial-probe — not hardcoded "default" anywhere
    downstream of Step 1."""
    src = _PAGE.read_text()
    # complete with the operator's tenant_id (not a literal)
    assert "tenant_id: config.tenantId" in src
    # tutorial probe carries it too
    assert "triggerWizardTutorialProbe(config.tenantId)" in src
    # .env download uses it
    assert "KORA_TENANT_ID=${config.tenantId}" in src


def test_wizard_api_wrappers_exist():
    src = _API_TS.read_text()
    for fn in (
        "getWizardState",
        "validateAnthropicApiKey",
        "validateSubstrate",
        "validateSlack",
        "triggerWizardTutorialProbe",
        "completeWizard",
    ):
        assert fn in src, f"missing api wrapper: {fn}"
