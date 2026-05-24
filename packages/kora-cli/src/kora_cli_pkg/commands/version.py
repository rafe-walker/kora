"""``kora version`` subcommand — KR-KORA-PIP-RESTRUCTURE-PHASE-2.

Self-contained rewrite of the in-tree ``kora_cli.main:cmd_version``
handler. Uses ``importlib.metadata`` instead of the module-level
constants in ``kora_cli/__init__.py`` so the wheel resolves the
version info without needing the Kora monorepo on the Python path.

Differences from the in-tree handler (intentional Phase 2 trim):

  * Pulls Kora version from ``importlib.metadata.version('kora-cli')``
    rather than ``kora_cli.__version__``. The two move together —
    kora-cli's version IS Kora's CLI version.
  * Pulls Python version from ``sys.version``.
  * Reports the three pip-installed Kora packages individually
    (isokron-client, kora-runtime, kora-cli) rather than the
    Hermes fork-commit + release-date constants — those live in
    ``kora_cli/__init__.py`` and are inappropriate to pull through
    the wheel boundary. The Hermes upstream version is reported as
    "source-only" per the §7.1 operator decision.
  * Skips the optional update-check (which lazy-imports
    ``kora_cli.banner`` + ``kora_cli.config``). The full update
    workflow stays in the in-tree ``kora_cli.main:cmd_version`` and
    fires when the user runs ``kora version`` against the
    monorepo install. The wheel's standalone path keeps it tight.

If the user wants the full in-tree version handler (with
fork-commit info + update check), they can still run it directly
via the in-tree path; this handler is the wheel-installable
fast-path.
"""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version


def main(argv: list[str]) -> int:
    """Print Kora + pip-installed-package versions + Python version.

    ``argv`` is unused — ``kora version`` takes no flags or args.
    Returns 0 on success (always — the report is best-effort and
    missing packages are reported, not failed).
    """
    print(f"Python: {sys.version.split()[0]}")
    print()
    print("Kora pip packages (Phase 1 + Phase 2):")
    for pkg in ("isokron-client", "kora-runtime", "kora-cli"):
        try:
            print(f"  {pkg}: {_pkg_version(pkg)}")
        except PackageNotFoundError:
            print(f"  {pkg}: Not installed (Phase 1/2 sister package missing)")
    print()
    print(
        "Hermes upstream: source-only (per the 2026-05-25 operator "
        "decision; tracked as KR-HERMES-PYPI-PUBLISH for a future phase)."
    )
    return 0


__all__ = ["main"]
