"""``kora status`` subcommand — KR-KORA-PIP-RESTRUCTURE-PHASE-2.

**Dispatch shim** (see ``PHASE-2-MIGRATION-PATTERN.md``). The
status handler is 570 LOC and imports from ``kora_cli.auth``,
``kora_cli.colors``, ``kora_cli.config``, ``kora_cli.models``,
``kora_cli.nous_subscription``. Full extraction deferred to Phase
2B (task #456).
"""

from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    """Delegate to ``kora_cli.main``'s ``status`` dispatch."""
    try:
        from kora_cli.main import main as _in_tree_main
    except ImportError as exc:
        sys.stderr.write(
            f"kora status: requires the Kora monorepo on the Python "
            f"path (in-tree ``kora_cli`` not importable: {exc}). "
            f"Dispatch-shim handler — Phase 2B carves the implementation.\n"
        )
        return 2
    sys.argv = ["kora", "status", *argv]
    return int(_in_tree_main() or 0)


__all__ = ["main"]
