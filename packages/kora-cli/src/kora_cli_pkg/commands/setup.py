"""``kora setup`` subcommand — KR-KORA-PIP-RESTRUCTURE-PHASE-2.

**Dispatch shim** (see ``PHASE-2-MIGRATION-PATTERN.md``). The
setup handler is 3557 LOC — the heaviest of the Phase 2 candidate
set — and pulls in ``kora_cli.nous_subscription``,
``tools.tool_backend_helpers``, ``utils``, ``kora_constants``,
``kora_cli.config``, and more. Full extraction deferred to Phase
2B (task #456).
"""

from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    """Delegate to ``kora_cli.main``'s ``setup`` dispatch."""
    try:
        from kora_cli.main import main as _in_tree_main
    except ImportError as exc:
        sys.stderr.write(
            f"kora setup: requires the Kora monorepo on the Python "
            f"path (in-tree ``kora_cli`` not importable: {exc}). "
            f"Dispatch-shim handler — Phase 2B carves the implementation "
            f"(3557 LOC; expected to fan out across multiple "
            f"Phase 2B/2C buckets given the helper-module dependency "
            f"surface).\n"
        )
        return 2
    sys.argv = ["kora", "setup", *argv]
    return int(_in_tree_main() or 0)


__all__ = ["main"]
