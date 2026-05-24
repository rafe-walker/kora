"""``kora`` console-script entry point — KR-KORA-PIP-RESTRUCTURE-PHASE-2.

After ``pip install kora-cli``, pip wires this module's
:func:`cli_entrypoint` to the ``kora`` binary in the venv's
``bin/`` directory (see ``pyproject.toml [project.scripts]``).

# Pre-dispatch routing

The entry point does a LIGHT pre-dispatch:

  1. Parse ``argv[1]`` (the first positional arg) to detect a
     carved-out subcommand.
  2. If the subcommand is one of the 7 carved in Phase 2 (Option
     II), dispatch to the corresponding ``kora_cli_pkg.commands.
     <name>`` handler.
  3. Otherwise fall through to ``kora_cli.main:main`` (the in-tree
     monolith covering the remaining 40 subcommands). That path
     requires the Kora monorepo on the Python path at call time;
     the fallback raises a friendly :class:`ImportError` if not.

This keeps the wheel deployable standalone for the carved set,
while the rest still dispatch through the existing argparse table
in ``kora_cli/main.py``. As Phase 2B/2C carves more subcommands,
the ``_PHASE_2_CARVED`` set grows; once the carve completes the
fallback can be deleted entirely.
"""

from __future__ import annotations

import sys
from typing import Sequence

# The 7 subcommands carved in Phase 2 (KR-KORA-PIP-RESTRUCTURE-
# PHASE-2-CLI Option II per the operator's 2026-05-24 confirmation).
# Each name maps 1:1 to ``kora_cli_pkg.commands.<name>``. Mix of
# full-extraction (migrate-hermes-home, version) and dispatch-shim
# (doctor, status, setup, login, logout) handlers — see
# ``PHASE-2-MIGRATION-PATTERN.md`` for the style decision per
# handler. Subcommand names with hyphens map to module names with
# underscores (Python module convention).
_PHASE_2_CARVED: dict[str, str] = {
    "migrate-hermes-home": "migrate_hermes_home",
    "version": "version",
    "doctor": "doctor",
    "status": "status",
    "setup": "setup",
    "login": "login",
    "logout": "logout",
}


def _first_positional(argv: Sequence[str]) -> str | None:
    """Return the first positional arg from ``argv``, skipping flags.

    Mirrors the logic ``kora_cli.main._first_positional_argv`` uses
    for fast-path subcommand detection — kept light here because we
    only need the subcommand name, not full argparse semantics. If
    no positional arg is found, returns ``None`` (the bare ``kora``
    invocation falls through to the in-tree main, which handles the
    interactive-chat default).
    """
    skip_next = False
    for token in argv:
        if skip_next:
            skip_next = False
            continue
        if not token.startswith("-"):
            return token
        # Flags that take a value — keep the list TIGHT; if we miss
        # one the worst case is the fallback takes over (correct
        # behavior, just slower path).
        if token in ("-m", "--model", "-p", "--provider", "-t", "--toolsets"):
            skip_next = True
    return None


def cli_entrypoint() -> int:
    """``kora`` console-script entry point.

    Routes carved subcommands to ``kora_cli_pkg.commands.<name>:main``
    handlers; everything else falls through to ``kora_cli.main:main``.
    Returns an exit code (0 on success, non-zero on failure).
    """
    argv = sys.argv[1:]
    first = _first_positional(argv)

    if first in _PHASE_2_CARVED:
        module_name = _PHASE_2_CARVED[first]
        # Lazy-import the carved handler so the fast common case
        # (uncarved subcommand → in-tree main fallback) doesn't pay
        # the import cost of every carved handler.
        import importlib

        handler_mod = importlib.import_module(
            f"kora_cli_pkg.commands.{module_name}"
        )
        # Each handler exposes a ``main(argv: list[str]) -> int``
        # following the same signature shape as the in-tree
        # ``kora_cli.migrate_hermes_home.main`` precedent. ``argv``
        # passed in excludes the subcommand name itself — handler
        # owns its argparse from there.
        return int(handler_mod.main(argv[argv.index(first) + 1 :]) or 0)

    # Fallback: delegate to the in-tree monolith. Requires the Kora
    # monorepo on the Python path. If not, surface a friendly
    # message rather than the raw ImportError stack.
    try:
        from kora_cli.main import main as _in_tree_main
    except ImportError as exc:
        sys.stderr.write(
            f"kora: subcommand '{first or '<none>'}' is not yet carved "
            f"into the kora-cli wheel and the in-tree kora_cli "
            f"monolith is not importable: {exc}\n"
            f"\n"
            f"Until Phase 2B/2C completes the carve (task #456), the "
            f"kora-cli wheel can only dispatch the carved subcommands "
            f"standalone: {sorted(_PHASE_2_CARVED)}.\n"
            f"For all other subcommands, install the Kora monorepo "
            f"alongside this wheel (so ``kora_cli`` is importable).\n"
        )
        return 2

    return int(_in_tree_main() or 0)


__all__ = ["cli_entrypoint"]
