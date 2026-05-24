"""kora-cli — Kora operator command-line, distributed as a pip package.

Phase 2 of the KR-KORA-PIP-RESTRUCTURE program (CC#3, 2026-05-24).
Provides the ``kora`` console script + a per-subcommand handler
surface for the 7 carved-out subcommands (Phase 2 Option II per
the operator's 2026-05-24 confirmation).

# Two extraction styles

This package demonstrates two patterns for migrating CLI
subcommands out of the in-tree ``kora_cli.main`` monolith and into
the pip-installable ``kora_cli_pkg`` namespace:

  1. **Full extraction** — handler file moves bodily into
     ``kora_cli_pkg.commands.<name>``. The implementation has no
     ``kora_cli.*`` dependencies (or only trivially extracted ones)
     and runs standalone from the wheel. Examples in this Phase:
     ``migrate_hermes_home`` (358 LOC, pure stdlib) and ``version``
     (~30 LOC, rewritten to use ``importlib.metadata`` instead of
     module-level constants from ``kora_cli/__init__.py``).

  2. **Dispatch shim** — handler file in
     ``kora_cli_pkg.commands.<name>`` is a thin wrapper that
     lazy-imports the existing in-tree ``kora_cli.<name>`` handler
     and delegates. Useful for heavily-coupled handlers that aren't
     worth fully extracting in Phase 2 (doctor, setup, status,
     login, logout). The Kora monorepo must be on the Python path
     at call time; the dispatch shim raises a clear error if not.
     Future Phase 2B/2C carves the implementations as appetite
     allows.

The choice between the two is per-handler — see
``PHASE-2-MIGRATION-PATTERN.md`` (next to this README) for the
selection criteria.

# Argparse pre-dispatch

``kora_cli_pkg.main:cli_entrypoint`` is the console-script entry
point. It does a LIGHT pre-dispatch: if the first positional
argument matches one of the carved subcommands, the corresponding
``kora_cli_pkg.commands.<name>`` handler runs. Otherwise the
existing ``kora_cli.main:main`` (the 13K-line monolith) takes
over — backward compatible for the 40 remaining subcommands
until they're carved in subsequent phases.

# Two install modes (same as the Marvin POC pattern from #204)

  1. **In-tree dev**: the top-level Kora ``pyproject.toml``'s
     ``[project.scripts] kora = "kora_cli.main:main"`` still
     resolves; tests + dev workflows continue working unchanged.
  2. **Pip-installed**: ``pip install kora-cli`` (after
     ``pip install -e ../hermes-agent`` per §7.1 + with the Kora
     monorepo on the Python path for the dispatch-shim fallback
     until Phase 2B/2C). The wheel's
     ``hermes_agent.plugins``-style entry-point registers ``kora``
     pointing at THIS package's ``cli_entrypoint``.
"""

__version__ = "0.1.0a1"

__all__ = ["__version__"]
