"""Backward-compat shim — KR-KORA-PIP-RESTRUCTURE-PHASE-2 (2026-05-24).

The ``migrate-hermes-home`` subcommand handler was extracted into
the kora-cli pip package as a Phase 2 Option II "full extraction"
(see ``packages/kora-cli/PHASE-2-MIGRATION-PATTERN.md`` for the
selection criteria). The canonical home is now
``packages/kora-cli/src/kora_cli_pkg/commands/migrate_hermes_home.py``.

This shim re-exports the public surface (``main``) so the 2
existing in-tree callers
(``kora_cli/main.py:cmd_migrate_hermes_home`` +
``tests/test_kora_paths_kr1_st3.py``) keep working unchanged.
"""

from __future__ import annotations

from kora_cli_pkg.commands.migrate_hermes_home import *  # noqa: F401,F403
from kora_cli_pkg.commands.migrate_hermes_home import main  # noqa: F401
