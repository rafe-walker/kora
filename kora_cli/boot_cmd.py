"""``kora boot --check-only`` diagnostic CLI subcommand (KR-P2-H ST4).

Runs the R4.1 §9.2 boot gate sequence in diagnostic mode and prints a
tabular summary. Useful for:

  - Pre-deploy smoke check: operators run
    ``flyctl ssh console -- kora boot --check-only`` before flipping
    production traffic.
  - Debugging boot failures in detached environments.
  - CI smoke gates (exit code reflects pass/fail).

Diagnostic-mode semantics (vs production boot):
  - Each gate runs ONCE (no retry on TRANSIENT failures).
  - Failures do NOT short-circuit; every gate's outcome is reported.
  - No holder transitions.
  - No chain-event emits (no ``kora.boot.{ready,failed}``).

Exit code:
  - 0 if all gates passed.
  - 1 if any gate failed (per bucket spec § ST4).
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)


# Tabular output column widths. Tuned so a typical 7-gate run fits in
# a 120-column terminal without wrapping.
_COL_GATE_ID = 36
_COL_CLASS = 10
_COL_OUTCOME = 8
_COL_ATTEMPTS = 9
_COL_ELAPSED = 9
_COL_DETAIL = 60


def cmd_boot(args: argparse.Namespace) -> int:
    """Entry point for ``kora boot`` subcommand.

    Returns the process exit code. ``main`` of ``kora_cli/main.py``
    propagates this via ``sys.exit``.
    """
    if not getattr(args, "check_only", False):
        # Future-extensibility: production boot path could land here
        # too. For now, the CLI only ships --check-only; without the
        # flag, print usage + exit.
        print(
            "kora boot: only --check-only is supported in this build. "
            "Production boot runs automatically via the agent / gateway "
            "entry points; this command is for operator-side dry runs.",
            file=sys.stderr,
        )
        return 2

    try:
        return _run_check_only()
    except KeyboardInterrupt:
        print("\nboot diagnostic interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        # Diagnostic mode should never crash — if it does, that's
        # itself a signal worth reporting + a non-zero exit.
        print(
            f"\nboot diagnostic raised unexpectedly: {exc!r}",
            file=sys.stderr,
        )
        logger.exception("[kora.boot.check_only] unexpected failure")
        return 3


def _run_check_only() -> int:
    """Bootstrap a minimal IsoKron provider + run the diagnostic
    coordinator + render the summary. Returns exit code."""
    import asyncio

    from agent.boot_coordinator import BootResult, run_boot_sequence

    provider = _load_isokron_provider()

    # Diagnostic mode permits holder=None — no transitions to gate.
    summary = asyncio.run(
        run_boot_sequence(
            memory_provider=provider,
            holder=None,
            diagnostic_mode=True,
        )
    )

    _print_summary(summary)

    return 0 if summary.result is BootResult.READY else 1


def _load_isokron_provider() -> Any:
    """Load + initialize the IsoKron memory provider.

    Mirrors the agent_init.py bootstrap path: read config, instantiate
    provider, call ``initialize()``. Returns ``None`` on any failure;
    the diagnostic coordinator handles None-provider gracefully (some
    gates will FAIL, which is correct diagnostic information).
    """
    try:
        from plugins.memory import load_memory_provider

        provider = load_memory_provider("isokron")
        if provider is None:
            print(
                "[boot.check_only] WARNING: isokron memory provider "
                "did not load. Gates that depend on the substrate "
                "will FAIL (this is the expected diagnostic signal).",
                file=sys.stderr,
            )
            return None
        if not provider.is_available():
            print(
                "[boot.check_only] WARNING: isokron provider loaded "
                "but is_available() returned False. Substrate-dependent "
                "gates will FAIL.",
                file=sys.stderr,
            )
            return None
        # initialize() opens the connection.
        provider.initialize()
        return provider
    except Exception as exc:
        print(
            f"[boot.check_only] WARNING: provider bootstrap raised "
            f"{exc!r}. Substrate-dependent gates will FAIL.",
            file=sys.stderr,
        )
        return None


def _print_summary(summary: Any) -> None:
    """Render the BootSummary as an ASCII table to stdout."""
    print("")
    print("R4.1 §9.2 boot gate diagnostic — --check-only")
    print("=" * 80)
    print("")
    print(_format_header())
    print("-" * 132)
    for r in summary.gate_results:
        print(_format_row(r))
    print("")
    if summary.result.value == "ready":
        print(
            f"Result: READY ({len(summary.gate_results)} gates passed)"
        )
    else:
        failed_id = (
            summary.failed_gate.gate_id if summary.failed_gate else "<unknown>"
        )
        print(
            f"Result: STOPPED (first failure: {failed_id})"
        )
    print("")


def _format_header() -> str:
    return (
        f"{'Gate':<{_COL_GATE_ID}}"
        f"{'Class':<{_COL_CLASS}}"
        f"{'Outcome':<{_COL_OUTCOME}}"
        f"{'Attempts':<{_COL_ATTEMPTS}}"
        f"{'Elapsed':<{_COL_ELAPSED}}"
        f"Detail"
    )


def _format_row(r: Any) -> str:
    detail = r.detail
    if len(detail) > _COL_DETAIL:
        detail = detail[: _COL_DETAIL - 3] + "..."
    return (
        f"{_trunc(r.gate_id, _COL_GATE_ID - 1):<{_COL_GATE_ID}}"
        f"{r.gate_class.value:<{_COL_CLASS}}"
        f"{r.outcome.value.upper():<{_COL_OUTCOME}}"
        f"{r.attempts:<{_COL_ATTEMPTS}}"
        f"{r.elapsed_ms}ms{' ' * (_COL_ELAPSED - len(str(r.elapsed_ms)) - 2)}"
        f"{detail}"
    )


def _trunc(s: str, width: int) -> str:
    """Truncate ``s`` to ``width`` chars, adding ellipsis if cut."""
    if len(s) <= width:
        return s
    return s[: width - 3] + "..."


def add_boot_parser(subparsers: argparse._SubParsersAction) -> None:
    """Wire the ``boot`` subcommand into the top-level argparser.

    Called from ``kora_cli/main.py:main`` alongside the other
    subparser.add_parser sites. Keeps boot-specific argparser config
    out of the main CLI file.
    """
    boot_parser = subparsers.add_parser(
        "boot",
        help="Boot diagnostics (R4.1 §9.2 gate sequence dry-run)",
        description=(
            "Run Kora's R4.1 §9.2 boot gate sequence in diagnostic mode. "
            "Useful for pre-deploy smoke checks and debugging boot "
            "failures. Does NOT transition the operational state machine "
            "or emit chain events."
        ),
    )
    boot_parser.add_argument(
        "--check-only",
        action="store_true",
        help=(
            "Run the boot gate sequence in diagnostic mode + print a "
            "tabular summary to stdout. Exits 0 if all gates passed, "
            "1 if any failed. No holder transitions, no chain emits."
        ),
    )
    boot_parser.set_defaults(func=cmd_boot)
