"""``kora logout`` subcommand — KR-KORA-PIP-RESTRUCTURE-PHASE-2.

**Dispatch shim** (see ``PHASE-2-MIGRATION-PATTERN.md``). Logout
is implemented inline in ``kora_cli/main.py`` and routes through
``kora_cli.auth`` for the token-clearing flow. Full extraction
deferred to Phase 2B (task #456) — paired with ``login``.
"""

from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    """Delegate to ``kora_cli.main``'s ``logout`` dispatch."""
    try:
        from kora_cli.main import main as _in_tree_main
    except ImportError as exc:
        sys.stderr.write(
            f"kora logout: requires the Kora monorepo on the Python "
            f"path (in-tree ``kora_cli`` not importable: {exc}). "
            f"Dispatch-shim handler — Phase 2B carves with ``login``.\n"
        )
        return 2
    sys.argv = ["kora", "logout", *argv]
    return int(_in_tree_main() or 0)


__all__ = ["main"]
