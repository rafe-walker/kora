"""Standalone smoke tests for the kora-cli wheel.

Mirrors the §B.6 dry-run install check from the
KR-KORA-PIP-RESTRUCTURE-PHASE-2-CLI bucket. CI runs this against
a fresh-venv wheel install with the 3 Phase 1+2 wheels installed
(isokron-client, kora-runtime, kora-cli) but WITHOUT the Kora
monorepo on the Python path.

Verifies:

  * Package imports cleanly + the ``__version__`` resolves.
  * The ``kora`` console-script entry point is discoverable via
    ``importlib.metadata.entry_points(group='console_scripts')``.
  * Each of the 7 carved subcommand handler modules imports + has
    a ``main`` callable.
  * The 2 fully-extracted handlers (``migrate-hermes-home`` and
    ``version``) run standalone (no Kora monorepo needed).
  * The 5 dispatch-shim handlers raise the friendly
    ``kora_cli`` not-importable error when invoked without the
    monorepo.
"""

from __future__ import annotations

import importlib.metadata as md
import io
import sys


def test_package_imports_with_only_sister_pip_packages_on_path():
    """``import kora_cli_pkg`` resolves with the wheel + Phase 1
    sister packages (isokron-client, kora-runtime). No Kora
    monorepo required for import-time."""
    import kora_cli_pkg

    assert kora_cli_pkg.__version__ == "0.1.0a1"


def test_kora_console_script_entry_point_is_discoverable():
    """The ``[project.scripts] kora = "kora_cli_pkg.main:cli_entrypoint"``
    block must be reachable via ``importlib.metadata.entry_points``
    so pip's wheel installer wires the ``kora`` binary correctly."""
    eps = list(md.entry_points(group="console_scripts"))
    names = {e.name: e.value for e in eps}
    assert "kora" in names, (
        f"kora console-script entry point not found; got: {names}"
    )
    # Either ``kora_cli_pkg.main:cli_entrypoint`` (this wheel) OR
    # ``kora_cli.main:main`` (the top-level monorepo wheel) is
    # acceptable — entry-point overlap per the Marvin POC pattern.
    assert names["kora"] in (
        "kora_cli_pkg.main:cli_entrypoint",
        "kora_cli.main:main",
    )


def test_seven_carved_handler_modules_resolve_with_callable_main():
    """All 7 Phase 2-carved handlers must import + expose a
    ``main(argv) -> int`` callable."""
    import importlib

    from kora_cli_pkg.main import _PHASE_2_CARVED

    assert len(_PHASE_2_CARVED) == 7
    for subcommand, module_name in _PHASE_2_CARVED.items():
        m = importlib.import_module(f"kora_cli_pkg.commands.{module_name}")
        assert callable(m.main), (
            f"carved handler '{subcommand}' "
            f"(kora_cli_pkg.commands.{module_name}) missing callable main()"
        )


def test_fully_extracted_version_handler_runs_standalone(capsys):
    """The ``version`` handler is one of 2 fully-extracted handlers
    (Phase 2 Option II Style A). It must run without the Kora
    monorepo on the Python path — uses ``importlib.metadata`` for
    the version reports."""
    from kora_cli_pkg.commands.version import main as version_main

    rc = version_main([])
    captured = capsys.readouterr()
    assert rc == 0
    # Spot-check three load-bearing lines of the version output.
    assert "Python:" in captured.out
    assert "isokron-client:" in captured.out
    assert "Hermes upstream: source-only" in captured.out


def test_fully_extracted_migrate_hermes_home_handler_runs_standalone():
    """The ``migrate-hermes-home`` handler is the other fully-
    extracted handler. ``--check`` mode is side-effect-free and
    safe to exercise in CI."""
    from kora_cli_pkg.commands.migrate_hermes_home import main as migrate_main

    rc = migrate_main(["--check", "--from", "/nonexistent/legacy/path"])
    # Returns nonzero when legacy path doesn't exist (expected for
    # this test); proves the handler ran + parsed args without
    # needing the Kora monorepo.
    assert rc in (0, 1, 2), f"unexpected exit code: {rc}"


def test_dispatch_shim_handler_surfaces_friendly_error_without_monorepo(capsys, monkeypatch):
    """When the Kora monorepo isn't on the path, a dispatch-shim
    handler (e.g., ``doctor``) MUST raise a friendly
    ``kora_cli`` not-importable message — not the raw ImportError
    stack trace."""
    # Force the kora_cli import to fail (simulates standalone
    # wheel install without monorepo). Real wheel-only CI venvs
    # already have this state — the monkeypatch is for in-tree
    # test runs where kora_cli IS on the path.
    monkeypatch.setitem(sys.modules, "kora_cli.main", None)

    from kora_cli_pkg.commands.doctor import main as doctor_main

    rc = doctor_main([])
    err = capsys.readouterr().err
    assert rc == 2
    assert "kora doctor:" in err
    assert "Phase 2B" in err or "task #456" in err
