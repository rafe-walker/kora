"""CI-runnable validation of Marvin's pip-installable surface.

Companion to ``tests/plugins/test_marvin_multi_tenant_proof.py`` (which
proves Option C identity-as-plugin works) — this file proves the
**pip-installable distribution path** introduced by CC#3 #204
(KR-PIP-PACKAGING-FOUNDATION).

# What this DOES NOT do

It does NOT do a full ``pip install`` into a fresh venv on every test
run — that would be slow (~30s per run; bad signal/noise in CI). The
operator-facing dry-run validation lives in
``MARVIN_DEMO_TRANSCRIPT.md`` (kora-docs) as the captured shell
transcript. This file instead **builds the wheel + inspects its
contents** to prove the structural correctness of the packaging.

# What this DOES

  1. Validates ``plugins/marvin/pyproject.toml`` is well-formed
  2. Builds the wheel via ``python -m build`` (subprocess)
  3. Inspects the wheel zip — confirms it contains the expected
     files: ``marvin/__init__.py``, the two data files, the
     entry-point declaration, the canonical metadata
  4. Loads the wheel's METADATA + entry_points.txt + verifies the
     entry-point group is ``hermes_agent.plugins`` and points at
     ``marvin:register``

If THIS test passes, an external operator running
``pip install marvin-runtime`` against a real PyPI server will get a
functional plugin (modulo PyPI publish mechanics which are out of
scope for #204 — see MARVIN_DEMO_TRANSCRIPT.md §7).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "marvin"


def _select_build_command() -> list[str] | None:
    """Pick a wheel-build command available in the current env.

    Returns the argv prefix for the build command (caller appends
    ``--wheel`` + outdir args), or ``None`` if no builder is
    available — the caller then skips the wheel-content tests.

    Order of preference: ``uv build`` (fast; what the repo uses) →
    ``python -m build`` (PEP-517 standard). The CI-runnable form
    succeeds with either.
    """
    uv = shutil.which("uv")
    if uv is not None:
        return [uv, "build"]
    # Fall back to python -m build IF the build module imports.
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import build"],
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0:
            return [sys.executable, "-m", "build"]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory):
    """Build the Marvin wheel once per test-module run; reuse across
    tests in this file. Uses ``uv build`` (preferred) or
    ``python -m build`` (fallback) so the build path is PEP-517
    conformant — same as what ``pip install`` would invoke.
    """
    build_cmd = _select_build_command()
    if build_cmd is None:
        pytest.skip("neither 'uv' nor 'build' is available — cannot build wheel")

    out_dir = tmp_path_factory.mktemp("marvin_wheel")
    argv = build_cmd + ["--wheel", "--out-dir", str(out_dir)]
    result = subprocess.run(
        argv,
        cwd=str(PLUGIN_DIR),
        capture_output=True,
        text=True,
        timeout=120,
    )

    if result.returncode != 0:
        pytest.fail(
            f"wheel build failed (exit {result.returncode}, cmd={argv}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    wheels = list(out_dir.glob("marvin_runtime-*-py3-none-any.whl"))
    assert len(wheels) == 1, (
        f"expected exactly one wheel; got {len(wheels)}: "
        f"{[w.name for w in wheels]}"
    )
    return wheels[0]


# ---------------------------------------------------------------------------
# pyproject.toml structural pins
# ---------------------------------------------------------------------------


def test_pyproject_exists_and_well_formed():
    """``plugins/marvin/pyproject.toml`` must exist + parse cleanly
    + declare the canonical project name + Hermes plugin entry
    point. Pre-build sanity."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    pyproject = PLUGIN_DIR / "pyproject.toml"
    assert pyproject.exists()
    data = tomllib.loads(pyproject.read_text())
    assert data["project"]["name"] == "marvin-runtime"
    assert data["project"]["requires-python"] == ">=3.11"
    # Entry-point group MUST match Hermes's expected group
    # (kora_cli/plugins.py:ENTRY_POINTS_GROUP). If this drifts,
    # plugin discovery breaks silently.
    eps = data["project"]["entry-points"]["hermes_agent.plugins"]
    assert eps["marvin"] == "marvin:register", (
        "entry-point must point at marvin:register so Hermes's "
        "_load_entrypoint_module finds the canonical register()"
    )


def test_pyproject_includes_data_files():
    """The package_data declaration MUST include the markdown
    identity files. Without it, the wheel ships without
    ``data/MARVIN.md`` etc. + the runtime fails on the import-time
    file read."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    pyproject = PLUGIN_DIR / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    pkg_data = data["tool"]["setuptools"]["package-data"]
    assert "marvin" in pkg_data
    assert "data/*.md" in pkg_data["marvin"]


def test_src_layout_intact():
    """The relocatable src/ layout is the load-bearing piece. If a
    future refactor accidentally moves files back to the flat
    layout, the wheel ships broken. Pin the layout structurally."""
    assert (PLUGIN_DIR / "src" / "marvin" / "__init__.py").exists()
    assert (PLUGIN_DIR / "src" / "marvin" / "data" / "MARVIN.md").exists()
    assert (
        PLUGIN_DIR / "src" / "marvin" / "data" / "marvin_system_prompt.md"
    ).exists()


# ---------------------------------------------------------------------------
# Built wheel contents
# ---------------------------------------------------------------------------


def test_wheel_contains_expected_files(built_wheel):
    """The wheel must contain the canonical module + both data files
    + entry-point declaration. If any of these go missing, the
    installed wheel fails at module import time OR at Hermes plugin
    discovery."""
    with zipfile.ZipFile(built_wheel) as zf:
        names = set(zf.namelist())

    required = {
        "marvin/__init__.py",
        "marvin/data/MARVIN.md",
        "marvin/data/marvin_system_prompt.md",
    }
    missing = required - names
    assert not missing, (
        f"wheel missing required files: {sorted(missing)}\n"
        f"actual contents: {sorted(names)}"
    )


def test_wheel_entry_point_declares_hermes_agent_plugins(built_wheel):
    """Inside the wheel's dist-info, the entry_points.txt MUST
    declare the ``hermes_agent.plugins`` group with marvin → register.
    Mirrors what ``importlib.metadata.entry_points()`` discovers
    post-install."""
    with zipfile.ZipFile(built_wheel) as zf:
        ep_names = [n for n in zf.namelist() if n.endswith("entry_points.txt")]
        assert len(ep_names) == 1, (
            f"expected exactly one entry_points.txt in wheel; got {ep_names}"
        )
        ep_text = zf.read(ep_names[0]).decode("utf-8")

    # Configparser-style format; check the section + key/value.
    assert "[hermes_agent.plugins]" in ep_text
    assert "marvin = marvin:register" in ep_text


def test_wheel_metadata_declares_required_python(built_wheel):
    """Pin the Python version requirement at the wheel level so a
    pip install on Python <3.11 fails fast at install time rather
    than at runtime."""
    with zipfile.ZipFile(built_wheel) as zf:
        meta_names = [n for n in zf.namelist() if n.endswith("/METADATA")]
        assert len(meta_names) == 1
        meta = zf.read(meta_names[0]).decode("utf-8")

    assert "Requires-Python: >=3.11" in meta
    assert "Name: marvin-runtime" in meta
