"""KR-1 ST4 operational smokes — Kora runtime end-to-end verification.

Closes KR-1 by exercising the surfaces the operator and the PM-driven
MCP/HTTP-API callers actually use. Each test is intentionally lightweight
(import-level or subprocess-with-short-timeout) so the full smoke set runs
in under 10 seconds. Slow / network / interactive surfaces are out of
scope — those are validated manually via the KR-1 quickstart doc.

Tests in this module run **serially** (no xdist concerns) because they
invoke subprocesses and assert on stdout/stderr; xdist's worker shuffling
would scramble process startup ordering and produce flake.

The Joshua binding (saved to PM memory at
`reference_kora_must_accept_mcp_and_api_commands.md` 2026-05-20): BOTH
`kora mcp serve` AND the HTTP API server (APIServerAdapter) must start
cleanly so both PMs (claude_pm on Kora + claude_pm on IsoKron) can drive
Kora programmatically. Two dedicated tests below enforce that contract.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _kora(args: list[str], *, env: dict | None = None, stdin: str | None = None, timeout: float = 10.0) -> subprocess.CompletedProcess:
    """Invoke the in-repo `./kora` launcher via the active venv Python.

    Returns CompletedProcess (does not raise on non-zero exit; tests
    assert on returncode explicitly).
    """
    venv_python = REPO_ROOT / ".venv" / "bin" / "python"
    python = str(venv_python) if venv_python.exists() else sys.executable
    cmd = [python, str(REPO_ROOT / "kora"), *args]
    return subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        env=env if env is not None else {**os.environ},
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _hermes_shim(args: list[str], *, env: dict | None = None, timeout: float = 10.0) -> subprocess.CompletedProcess:
    """Invoke the legacy `./hermes` BC wrapper."""
    venv_python = REPO_ROOT / ".venv" / "bin" / "python"
    python = str(venv_python) if venv_python.exists() else sys.executable
    cmd = [python, str(REPO_ROOT / "hermes"), *args]
    return subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        env=env if env is not None else {**os.environ},
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Console-script smokes (KR-1 ST4 §10 bullets 1, "Console-script rename")
# ---------------------------------------------------------------------------


class TestKoraVersion:
    def test_version_starts_with_kora_0_1_0(self):
        proc = _kora(["--version"])
        assert proc.returncode == 0, proc.stderr
        assert "Kora 0.1.0" in proc.stdout

    def test_version_attributes_hermes_origin(self):
        proc = _kora(["--version"])
        assert "Hermes-derived runtime" in proc.stdout
        assert "fork of NousResearch/hermes-agent@" in proc.stdout

    def test_version_carries_fork_commit_short_sha(self):
        proc = _kora(["--version"])
        # ST1 recon recorded the merge-base at 5e743559e. ST4 ships
        # that commit in __hermes_fork_commit_short__. If the fork is
        # later synced with upstream, this assertion is the only place
        # that needs updating (along with __hermes_fork_commit_*).
        assert "5e743559e" in proc.stdout

    def test_version_lists_inherited_hermes_version(self):
        proc = _kora(["--version"])
        assert "v0.14.0" in proc.stdout  # __hermes_inherited_version__


class TestKoraHelpTopLevel:
    def test_help_prog_name_is_kora(self):
        proc = _kora(["--help"])
        assert proc.returncode == 0
        assert "usage: kora" in proc.stdout
        # Negative: must not still say `usage: hermes`.
        assert "usage: hermes" not in proc.stdout

    def test_help_description_mentions_kora_identity(self):
        proc = _kora(["--help"])
        assert "Kora — Joshua's personal frontier-tier orchestration agent" in proc.stdout

    def test_help_lists_critical_subcommands(self):
        proc = _kora(["--help"])
        # The four surfaces ST4 verifies + the new migrate command.
        for subcommand in ("chat", "gateway", "mcp", "setup", "migrate-hermes-home"):
            assert subcommand in proc.stdout, f"--help missing subcommand: {subcommand}"


class TestKoraSubcommandHelp:
    def test_chat_help_mentions_kora(self):
        proc = _kora(["chat", "--help"])
        assert proc.returncode == 0
        assert "with Kora" in proc.stdout

    def test_setup_help_mentions_kora(self):
        proc = _kora(["setup", "--help"])
        assert proc.returncode == 0
        assert "Kora runtime" in proc.stdout

    def test_mcp_help_lists_serve(self):
        proc = _kora(["mcp", "--help"])
        assert proc.returncode == 0
        assert "serve" in proc.stdout

    def test_gateway_help_lists_run(self):
        proc = _kora(["gateway", "--help"])
        assert proc.returncode == 0
        assert "run" in proc.stdout


# ---------------------------------------------------------------------------
# Joshua binding — BOTH kora mcp serve AND HTTP API server must start
# (memory: reference_kora_must_accept_mcp_and_api_commands.md, 2026-05-20)
# ---------------------------------------------------------------------------


class TestPMDrivenSurfacesStartClean:
    """Joshua's 2026-05-20 directive: both PMs need direct programmatic
    access to drive Kora. MCP and HTTP API are the two non-messaging
    PM-driven surfaces. Both must start cleanly (smoke level — no full
    handshake, just verify the surface launches without import or init
    errors). KR-6 will wire capability + Constitution checks; here we
    only validate the binary path.
    """

    def test_mcp_serve_starts_and_exits_on_eof(self):
        """`kora mcp serve` with closed stdin should launch, read EOF, exit 0."""
        proc = _kora(["mcp", "serve"], stdin="", timeout=8.0)
        # Closed stdin triggers a clean shutdown of the MCP stdio loop.
        # Any non-zero exit means the server crashed on startup.
        assert proc.returncode == 0, (
            f"mcp serve exited {proc.returncode}; stderr was:\n{proc.stderr}"
        )

    def test_http_api_server_adapter_imports(self):
        """The HTTP API server adapter (gateway/platforms/api_server.py)
        must be importable and report its requirements OK. This is what
        `kora gateway run` instantiates when the api_server adapter is
        enabled in config.yaml.
        """
        from gateway.platforms.api_server import (
            APIServerAdapter,
            check_api_server_requirements,
        )
        from gateway.platforms.base import BasePlatformAdapter

        # Class wiring intact.
        assert issubclass(APIServerAdapter, BasePlatformAdapter), (
            "APIServerAdapter must subclass BasePlatformAdapter"
        )
        # check_api_server_requirements() returns True when the optional
        # dependency surface (aiohttp + companion packages) is installed.
        # KR-1 ST1 documented the curated --extra list that provides this.
        assert check_api_server_requirements() is True, (
            "check_api_server_requirements returned False — "
            "ST1's curated extras (messaging/web/homeassistant/sms) may "
            "not be installed."
        )

    def test_gateway_help_lists_api_server_via_messaging_setup(self):
        """The HTTP API surface is configured in `config.yaml` (api_server
        plugin block) and managed through `kora gateway`. Verify the
        gateway subcommand surfaces at all (smoke for the wiring).
        """
        proc = _kora(["gateway", "--help"])
        assert proc.returncode == 0
        # Gateway dispatch lists the standard adapter-management actions.
        assert "run" in proc.stdout
        assert "start" in proc.stdout
        assert "stop" in proc.stdout


# ---------------------------------------------------------------------------
# hermes BC wrapper (ST3 deprecation-shim verification)
# ---------------------------------------------------------------------------


class TestHermesBCWrapper:
    def test_hermes_shim_runs_kora_main(self):
        proc = _hermes_shim(["--version"])
        assert proc.returncode == 0
        # Same version output as kora.
        assert "Kora 0.1.0" in proc.stdout

    def test_hermes_shim_emits_deprecation_warning(self):
        proc = _hermes_shim(["--version"])
        assert "[deprecation]" in proc.stderr
        assert "Migrate to `kora`" in proc.stderr

    def test_hermes_shim_silent_when_quiet_env_set(self):
        env = {**os.environ, "KORA_HERMES_DEPRECATION_QUIET": "1"}
        proc = _hermes_shim(["--version"], env=env)
        assert proc.returncode == 0
        # Deprecation warning should be suppressed; other stderr (e.g.
        # the KORA_HOME bc fallback message) may still appear.
        assert "[deprecation]" not in proc.stderr


# ---------------------------------------------------------------------------
# Migration smoke (tmp env, no real ~/.hermes touched)
# ---------------------------------------------------------------------------


class TestMigrationSmokeViaCLI:
    """Exercises `kora migrate-hermes-home` end-to-end through the CLI
    rather than the module-level main() (which the ST3 tests cover).
    Validates the subparser wiring + argv pass-through.
    """

    def test_cli_migrate_check_handles_empty_tmp(self, tmp_path):
        legacy = tmp_path / ".hermes"
        target = tmp_path / ".kora"
        proc = _kora([
            "migrate-hermes-home",
            "--check",
            "--from", str(legacy),
            "--to", str(target),
        ])
        # --check is always safe; rc=0 even with empty source.
        assert proc.returncode == 0
        assert "[kora.migrate]" in proc.stderr
        assert "legacy-does-not-exist" in proc.stderr

    def test_cli_migrate_symlink_creates_link(self, tmp_path):
        # Populate a fake ~/.hermes.
        legacy = tmp_path / ".hermes"
        legacy.mkdir()
        (legacy / "config.yaml").write_text("model:\n  provider: kora\n", encoding="utf-8")
        target = tmp_path / ".kora"

        proc = _kora([
            "migrate-hermes-home",
            "--symlink",
            "--from", str(legacy),
            "--to", str(target),
        ])
        assert proc.returncode == 0, proc.stderr
        assert target.is_symlink()
        assert target.resolve() == legacy.resolve()
        # File visible through the symlink.
        assert (target / "config.yaml").read_text(encoding="utf-8").startswith("model:")

    def test_cli_migrate_copy_does_deep_copy(self, tmp_path):
        legacy = tmp_path / ".hermes"
        legacy.mkdir()
        (legacy / "MEMORY.md").write_text("- existing memory\n", encoding="utf-8")
        target = tmp_path / ".kora"

        proc = _kora([
            "migrate-hermes-home",
            "--copy",
            "--from", str(legacy),
            "--to", str(target),
        ])
        assert proc.returncode == 0, proc.stderr
        assert target.is_dir()
        assert not target.is_symlink()
        assert (target / "MEMORY.md").read_text(encoding="utf-8") == "- existing memory\n"
        # Legacy still present (rollback intact).
        assert legacy.is_dir()


# ---------------------------------------------------------------------------
# Identity wiring (ST2 reaffirmation through subprocess boundary)
# ---------------------------------------------------------------------------


class TestKoraIdentityVisibleAtRuntime:
    """ST2 verified DEFAULT_AGENT_IDENTITY in-process. ST4 verifies the
    identity survives the full subprocess boundary — i.e. when the CLI
    builds the system prompt for a chat session, it still says
    "You are Kora.".
    """

    def test_identity_loadable_through_subprocess(self):
        proc = subprocess.run(
            [
                str(REPO_ROOT / ".venv" / "bin" / "python"),
                "-c",
                "from agent.prompt_builder import DEFAULT_AGENT_IDENTITY; "
                "first_line = DEFAULT_AGENT_IDENTITY.split(chr(10))[0]; "
                "print(first_line)",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "You are Kora."
