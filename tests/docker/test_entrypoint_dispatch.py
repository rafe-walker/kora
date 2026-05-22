"""Tests for ``docker/dispatch.sh`` — KR-D-DEPLOY ST1.

The dispatch script's job is to decide what ``exec ...`` line runs at
the bottom of the kora-runtime container's entrypoint. The decision
depends on argv + ``KORA_DEPLOY_ENV`` + which executables are on PATH.

We test the decision matrix in isolation by running ``dispatch.sh``
under ``KORA_DISPATCH_DRY_RUN=1`` (which makes the script ``echo`` the
resolved argv instead of ``exec``'ing) with a controlled PATH
containing stub ``hermes`` / ``doppler`` / ``bash`` / ``sleep``
executables. The actual ``exec`` behavior is verified end-to-end
when the container runs in CI / on Fly; that's a deploy-side smoke
not a unit test.

Run from the repo root:

  pytest tests/docker/test_entrypoint_dispatch.py -q
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Dict, Iterable

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
DISPATCH_SH = REPO_ROOT / "docker" / "dispatch.sh"


# ---------------------------------------------------------------------------
# Fixtures — synthetic PATH with stub binaries
# ---------------------------------------------------------------------------


def _make_stub(dir_: Path, name: str, body: str = "") -> None:
    """Create a small executable shell script that prints its name +
    argv when invoked. Used as a stub for hermes / doppler / etc."""
    path = dir_ / name
    if body:
        path.write_text(body)
    else:
        path.write_text(f"#!/bin/bash\necho 'STUB:{name}'\n")
    st = path.stat()
    path.chmod(st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def stub_path(tmp_path):
    """Yield a directory populated with stub hermes + doppler + a few
    canonical Unix tools. Returns the directory; tests inject it as
    the only PATH entry so ``command -v`` resolves deterministically.
    """
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    _make_stub(bin_, "hermes")
    _make_stub(bin_, "doppler")
    # Real-ish binaries that the dispatch escape hatch should exec
    # directly.
    _make_stub(bin_, "bash")
    _make_stub(bin_, "sleep")
    _make_stub(bin_, "gosu")
    return bin_


def _run_dispatch(
    stub_path: Path,
    args: Iterable[str],
    env: Dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run dispatch.sh in dry-run mode with the given args + env.

    The returned process has the resolved ``EXEC: ...`` line on stdout.
    PATH is set to ``stub_path`` ONLY so ``command -v`` resolves only
    to our stubs (no system bash, etc.).
    """
    full_env = {
        "PATH": str(stub_path),
        "KORA_DISPATCH_DRY_RUN": "1",
    }
    if env:
        full_env.update(env)
    return subprocess.run(
        ["/bin/bash", str(DISPATCH_SH), *args],
        capture_output=True,
        text=True,
        env=full_env,
        timeout=10,
    )


# ---------------------------------------------------------------------------
# 1. No args → defaults to `hermes daemon`
# ---------------------------------------------------------------------------


def test_no_args_defaults_to_hermes_daemon(stub_path):
    r = _run_dispatch(stub_path, [], env={"KORA_DEV": "1"})
    assert r.returncode == 0, r.stderr
    # KORA_DEV=1 path skips the Doppler wrap → direct hermes daemon.
    assert "EXEC: hermes daemon" in r.stdout


# ---------------------------------------------------------------------------
# 2. Direct-exec escape hatch for non-hermes/non-kora binaries
# ---------------------------------------------------------------------------


def test_bash_first_arg_execs_bash_directly(stub_path):
    """sleep infinity / bash / etc. must NOT be wrapped through hermes."""
    r = _run_dispatch(stub_path, ["bash", "-c", "true"])
    assert r.returncode == 0
    assert "EXEC: bash -c true" in r.stdout


def test_sleep_first_arg_execs_sleep_directly(stub_path):
    """Long-lived sandbox containers (`sleep infinity`) must bypass
    the daemon dispatch — see tools/environments/docker.py."""
    r = _run_dispatch(stub_path, ["sleep", "infinity"])
    assert r.returncode == 0
    assert "EXEC: sleep infinity" in r.stdout


def test_gosu_first_arg_execs_gosu_directly(stub_path):
    """The gosu escape hatch (used by re-exec patterns) must survive."""
    r = _run_dispatch(stub_path, ["gosu", "hermes", "echo", "hi"])
    assert r.returncode == 0
    assert "EXEC: gosu hermes echo hi" in r.stdout


# ---------------------------------------------------------------------------
# 3. hermes/kora as first arg → does NOT take the escape hatch
# ---------------------------------------------------------------------------


def test_hermes_first_arg_flows_through_wrap_in_prd(stub_path):
    """`docker run kora-runtime hermes daemon` in prd → must wrap with
    Doppler, NOT exec hermes directly (which would skip secret injection)."""
    r = _run_dispatch(
        stub_path,
        ["hermes", "daemon"],
        env={"KORA_DEPLOY_ENV": "prd"},
    )
    assert r.returncode == 0
    # Order matters: substrate first, then anthropic, then gateways.
    assert "doppler run -p kora-runtime-substrate" in r.stdout
    assert "doppler run -p kora-runtime-anthropic" in r.stdout
    assert "doppler run -p kora-runtime-gateways" in r.stdout
    assert "hermes hermes daemon" not in r.stdout  # not double-wrapped
    assert "hermes daemon" in r.stdout


def test_hermes_first_arg_in_dev_does_not_wrap(stub_path):
    """KORA_DEPLOY_ENV=dev skips the Doppler wrap even with doppler on PATH.

    When first arg is already ``hermes``, dispatch.sh does NOT prepend
    another ``hermes`` — argv is exec'd verbatim.
    """
    r = _run_dispatch(
        stub_path,
        ["hermes", "daemon"],
        env={"KORA_DEPLOY_ENV": "dev"},
    )
    assert r.returncode == 0
    assert "doppler" not in r.stdout
    # Verbatim, NOT double-hermes.
    assert "EXEC: hermes daemon" in r.stdout
    assert "EXEC: hermes hermes daemon" not in r.stdout


# ---------------------------------------------------------------------------
# 4. Doppler wrap — prd path
# ---------------------------------------------------------------------------


def test_doppler_wrap_in_prd(stub_path):
    r = _run_dispatch(
        stub_path,
        ["chat", "-q", "hi"],
        env={"KORA_DEPLOY_ENV": "prd"},
    )
    assert r.returncode == 0
    # All three Doppler projects appear with -c prd.
    assert "doppler run -p kora-runtime-substrate -c prd" in r.stdout
    assert "doppler run -p kora-runtime-anthropic -c prd" in r.stdout
    assert "doppler run -p kora-runtime-gateways -c prd" in r.stdout
    # hermes is the final command, with the original argv preserved.
    assert "hermes chat -q hi" in r.stdout


def test_doppler_wrap_in_staging(stub_path):
    """The Doppler config name = KORA_DEPLOY_ENV, so staging works
    automatically once the operator creates a 'staging' Doppler config."""
    r = _run_dispatch(
        stub_path,
        ["daemon"],
        env={"KORA_DEPLOY_ENV": "staging"},
    )
    assert r.returncode == 0
    assert "doppler run -p kora-runtime-substrate -c staging" in r.stdout
    assert "-c staging" in r.stdout
    assert "hermes daemon" in r.stdout


# ---------------------------------------------------------------------------
# 5. Doppler wrap skip paths
# ---------------------------------------------------------------------------


def test_no_doppler_on_path_skips_wrap(tmp_path):
    """If doppler isn't installed (local dev, broken image), fall back
    to direct hermes exec — don't fail."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    _make_stub(bin_, "hermes")
    # NO doppler stub.
    r = subprocess.run(
        ["/bin/bash", str(DISPATCH_SH), "daemon"],
        capture_output=True,
        text=True,
        env={
            "PATH": str(bin_),
            "KORA_DISPATCH_DRY_RUN": "1",
            "KORA_DEPLOY_ENV": "prd",
        },
        timeout=10,
    )
    assert r.returncode == 0
    assert "doppler" not in r.stdout
    assert "EXEC: hermes daemon" in r.stdout


def test_kora_deploy_env_unset_skips_wrap(stub_path):
    """Unset KORA_DEPLOY_ENV → no wrap (deploy-env gate will catch
    this at the daemon level later)."""
    r = _run_dispatch(stub_path, ["daemon"])
    assert r.returncode == 0
    assert "doppler" not in r.stdout
    assert "EXEC: hermes daemon" in r.stdout


def test_kora_deploy_env_dev_skips_wrap(stub_path):
    """Explicit dev opt-out."""
    r = _run_dispatch(
        stub_path, ["daemon"], env={"KORA_DEPLOY_ENV": "dev"}
    )
    assert r.returncode == 0
    assert "doppler" not in r.stdout
    assert "EXEC: hermes daemon" in r.stdout


# ---------------------------------------------------------------------------
# 6. Argv preservation
# ---------------------------------------------------------------------------


def test_argv_with_spaces_preserved(stub_path):
    """A chat -q "hello world" invocation must keep the quoted argument
    intact through the wrap."""
    r = _run_dispatch(
        stub_path,
        ["chat", "-q", "hello world"],
        env={"KORA_DEV": "1"},
    )
    assert r.returncode == 0
    # bash %q escapes spaces, producing either 'hello world' (single-quoted)
    # or hello\ world (backslash-escaped). Both are acceptable as long as
    # the receiving process sees one arg, not two.
    out = r.stdout
    assert "hello world" in out or "hello\\ world" in out