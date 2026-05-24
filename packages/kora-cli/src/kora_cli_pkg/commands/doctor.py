"""``kora doctor`` subcommand — KR-KORA-PIP-RESTRUCTURE-PHASE-2.

**Dispatch shim** (see ``packages/kora-cli/PHASE-2-MIGRATION-PATTERN.md``).
The doctor handler is 1986 LOC and imports from 5+ ``kora_cli.*``
modules (``kora_cli.config``, ``kora_cli.env_loader``,
``kora_cli.colors``, ``kora_cli.models``, plus ``kora_constants``).
Full extraction would require carving each of those helpers too —
deferred to Phase 2B per the operator's #456 queue entry.

This shim lazy-imports the existing in-tree ``kora_cli.doctor`` and
delegates. Requires the Kora monorepo on the Python path at call
time. If ``kora_cli.doctor`` isn't importable (standalone
``pip install kora-cli`` without the monorepo), the shim raises a
friendly :class:`ImportError` so operators know what's missing.
"""

from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    """Delegate to ``kora_cli.doctor``'s entry path.

    The in-tree handler is invoked via ``kora_cli.main.cmd_doctor``
    (the argparse dispatch); we re-route through the same path by
    handing control back to ``kora_cli.main:main`` with the original
    argv reconstituted. That preserves all of the handler's existing
    argparse flags + behavior without duplicating the dispatch logic
    here.
    """
    try:
        from kora_cli.main import main as _in_tree_main
    except ImportError as exc:
        sys.stderr.write(
            f"kora doctor: requires the Kora monorepo on the Python "
            f"path (in-tree ``kora_cli`` module not importable: "
            f"{exc}). The wheel ships a dispatch shim only — the "
            f"1986-LOC doctor implementation is deferred to Phase 2B "
            f"(task #456).\n"
        )
        return 2
    # Reconstitute argv so the in-tree main sees ``kora doctor <flags>``.
    sys.argv = ["kora", "doctor", *argv]
    return int(_in_tree_main() or 0)


__all__ = ["main"]
