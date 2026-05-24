"""``kora login`` subcommand — KR-KORA-PIP-RESTRUCTURE-PHASE-2.

**Dispatch shim** (see ``PHASE-2-MIGRATION-PATTERN.md``). Login is
implemented inline in ``kora_cli/main.py`` (no standalone handler
file) and routes through ``kora_cli.auth`` for the actual OAuth /
PKCE / token-storage flow. Full extraction deferred to Phase 2B
(task #456) — would carve ``kora_cli.auth`` first, then the inline
``main.py`` dispatch.
"""

from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    """Delegate to ``kora_cli.main``'s ``login`` dispatch."""
    try:
        from kora_cli.main import main as _in_tree_main
    except ImportError as exc:
        sys.stderr.write(
            f"kora login: requires the Kora monorepo on the Python "
            f"path (in-tree ``kora_cli`` not importable: {exc}). "
            f"Dispatch-shim handler — Phase 2B carves ``kora_cli.auth`` "
            f"first, then the inline login dispatch.\n"
        )
        return 2
    sys.argv = ["kora", "login", *argv]
    return int(_in_tree_main() or 0)


__all__ = ["main"]
