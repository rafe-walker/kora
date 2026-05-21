"""KR-P2-F-pre ST2: validate ``docker/entrypoint.sh`` env-var checks.

These tests invoke the entrypoint script as a subprocess with controlled
env vars and assert the fail-closed behavior of R4.1 §9.2 gate 2 plus
the missing-required-var diagnostic.

The entrypoint hardcodes ``INSTALL_DIR=/opt/hermes`` and sources its
venv after our validation block. In failure cases (validation exits 1)
the script never reaches the venv source — exit code and stderr are
authoritative. In the success case (all vars set) the script proceeds
past validation and then dies on the missing venv; the test treats any
non-zero exit as acceptable as long as none of the validation error
patterns appear on stderr.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO_ROOT / "docker" / "entrypoint.sh"


# Required env vars per ST2 spec. Mirror docker/entrypoint.sh — if the
# script's list changes, the test fixture must change too.
_ALL_REQUIRED = {
    "KORA_SERVICE_TOKEN": "wsk_fake_substrate_token",
    "KORA_ISOKRON_DSN": "postgres://fake/isokron",
    "KORA_DEFAULT_WORKSPACE_ID": "00000000-0000-0000-0000-000000000001",
    "KORA_SEA_MCP_ENDPOINT": "stdio://fake-mcp-server",
    "CLAUDE_CODE_OAUTH_TOKEN": "fake-anthropic-oauth-token",
}


def _run_entrypoint(env_overrides: dict[str, str], *, unset: tuple[str, ...] = ()):
    """Run docker/entrypoint.sh with a curated env, return CompletedProcess."""
    env = dict(_ALL_REQUIRED)
    env.update(env_overrides)
    for k in unset:
        env.pop(k, None)

    # Inherit PATH so bash + posix utilities resolve, but otherwise scrub
    # the parent env so a developer's exported ANTHROPIC_API_KEY can't
    # smuggle into the test.
    env["PATH"] = os.environ.get("PATH", "")
    # Point HERMES_HOME at a guaranteed-writeable dir so the bootstrap
    # block doesn't break the test process if validation passes.
    env.setdefault("HERMES_HOME", "/tmp/kora-entrypoint-test")

    return subprocess.run(
        ["bash", str(ENTRYPOINT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.fixture(scope="module", autouse=True)
def _skip_if_no_entrypoint():
    if not ENTRYPOINT.exists():
        pytest.skip("docker/entrypoint.sh not present in this checkout")


# ---------------------------------------------------------------------------
# R4.1 §9.2 gate 2 — ANTHROPIC_* fail-closed
# ---------------------------------------------------------------------------

class TestAnthropicGateFailClosed:
    def test_anthropic_api_key_set_exits_one(self):
        result = _run_entrypoint({"ANTHROPIC_API_KEY": "sk-ant-fake"})
        assert result.returncode == 1
        assert "ANTHROPIC_API_KEY is set" in result.stderr
        assert "R4.1 §9.2 gate 2" in result.stderr
        assert "CLAUDE_CODE_OAUTH_TOKEN exclusively" in result.stderr

    def test_anthropic_auth_token_set_exits_one(self):
        result = _run_entrypoint({"ANTHROPIC_AUTH_TOKEN": "sk-fake"})
        assert result.returncode == 1
        assert "ANTHROPIC_AUTH_TOKEN is set" in result.stderr
        assert "R4.1 §9.2 gate 2" in result.stderr

    def test_anthropic_gate_fires_before_substrate_check(self):
        """If both gate 2 and substrate-vars would fail, gate 2 wins —
        operator must remove the ANTHROPIC_* var FIRST."""
        result = _run_entrypoint(
            {"ANTHROPIC_API_KEY": "sk-ant-fake"},
            unset=("KORA_SERVICE_TOKEN",),
        )
        assert result.returncode == 1
        assert "ANTHROPIC_API_KEY is set" in result.stderr
        # The substrate diagnostic must NOT have run — it would be confusing.
        assert "missing required env vars" not in result.stderr


# ---------------------------------------------------------------------------
# Required-var checks — substrate + Anthropic-oauth
# ---------------------------------------------------------------------------

class TestRequiredEnvVars:
    @pytest.mark.parametrize(
        "missing",
        [
            "KORA_SERVICE_TOKEN",
            "KORA_ISOKRON_DSN",
            "KORA_DEFAULT_WORKSPACE_ID",
            "KORA_SEA_MCP_ENDPOINT",
        ],
    )
    def test_missing_substrate_var_exits_one(self, missing):
        result = _run_entrypoint({}, unset=(missing,))
        assert result.returncode == 1
        assert missing in result.stderr
        assert "Kora startup blocked — missing required env vars" in result.stderr
        # The 3-Doppler-project hint must accompany the error so the
        # operator knows where the var should live.
        assert "kora-runtime-substrate" in result.stderr
        assert "kora-runtime-anthropic" in result.stderr
        assert "kora-runtime-gateways" in result.stderr

    def test_missing_claude_code_oauth_token_exits_one(self):
        result = _run_entrypoint({}, unset=("CLAUDE_CODE_OAUTH_TOKEN",))
        assert result.returncode == 1
        assert "CLAUDE_CODE_OAUTH_TOKEN" in result.stderr
        assert "Kora startup blocked — missing required env vars" in result.stderr

    def test_multiple_missing_vars_listed_together(self):
        result = _run_entrypoint(
            {},
            unset=("KORA_SERVICE_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"),
        )
        assert result.returncode == 1
        assert "KORA_SERVICE_TOKEN" in result.stderr
        assert "CLAUDE_CODE_OAUTH_TOKEN" in result.stderr


# ---------------------------------------------------------------------------
# Slack vars — warn-only when SLACK_GATEWAY_ENABLED is true
# ---------------------------------------------------------------------------

class TestSlackVarsWarnOnly:
    def test_slack_enabled_missing_vars_does_not_exit_one(self):
        """Slack vars are warn-only. With Slack flagged enabled but tokens
        absent, the script must NOT exit 1 from validation — it should
        emit warnings and continue past the validation block.

        The script will still die downstream on the missing venv source;
        what matters here is that validation itself didn't reject the
        deploy, and the WARNING lines reached stderr."""
        result = _run_entrypoint({"SLACK_GATEWAY_ENABLED": "true"})

        # Validation must not be the cause of the exit — none of the
        # ERROR patterns should appear.
        assert "Kora startup blocked" not in result.stderr
        assert "R4.1 §9.2 gate 2" not in result.stderr

        # Warnings must be present for each missing Slack var.
        assert "SLACK_BOT_TOKEN missing" in result.stderr
        assert "SLACK_APP_TOKEN missing" in result.stderr
        assert "SLACK_SIGNING_SECRET missing" in result.stderr

    def test_slack_disabled_no_warnings_emitted(self):
        result = _run_entrypoint({"SLACK_GATEWAY_ENABLED": "false"})
        assert "Slack gateway enabled but" not in result.stderr


# ---------------------------------------------------------------------------
# Success path — validation passes
# ---------------------------------------------------------------------------

class TestSuccessPath:
    def test_all_required_vars_set_validation_passes(self):
        """When every required var is set and no ANTHROPIC_* gate trips,
        validation must proceed past its own block. The script will then
        die on the absent venv (``/opt/hermes/.venv/...``) — that's OK,
        we just need to confirm none of our validation patterns appeared
        on stderr."""
        result = _run_entrypoint({})

        assert "ANTHROPIC_API_KEY is set" not in result.stderr
        assert "ANTHROPIC_AUTH_TOKEN is set" not in result.stderr
        assert "Kora startup blocked" not in result.stderr
        assert "missing required env vars" not in result.stderr


# ---------------------------------------------------------------------------
# KORA_KRONICLE_EVENT_LOG_SSE_URL deliberately absent — see ST1 PR / R4.1 §9.3
# ---------------------------------------------------------------------------

class TestKronicleSseDeliberatelyNotRequired:
    def test_kronicle_sse_url_is_not_in_required_vars(self):
        """The original draft listed KORA_KRONICLE_EVENT_LOG_SSE_URL as a
        required substrate var. PM ruled the SSE consumer is cockpit-BFF
        side, not runtime side (R4.1 §9.3), so it MUST NOT block startup
        if unset. Guarantee that with a regression test."""
        result = _run_entrypoint({}, unset=("KORA_KRONICLE_EVENT_LOG_SSE_URL",))
        # Should not blocked startup on this var — only on the
        # post-validation venv source.
        assert "KORA_KRONICLE_EVENT_LOG_SSE_URL" not in result.stderr
