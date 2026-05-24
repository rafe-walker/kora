"""KR-1 ST3 — operator-facing ``~/.hermes`` → ``~/.kora`` migration.

Run via ``kora migrate-hermes-home`` (wired by the CLI subcommand
dispatch in ``kora_cli/main.py``). The script is idempotent and side-
effect-free unless explicitly told to copy or symlink, so it is safe
to run any number of times.

Modes:
    --copy      Copy ``~/.hermes`` to ``~/.kora`` (full deep copy).
                Operator keeps the legacy install for rollback; new
                Kora runtime writes land in ``~/.kora``.

    --symlink   Create ``~/.kora`` as a symlink to ``~/.hermes``.
                Lowest-friction transition; both names resolve to the
                same on-disk data. Recommended for the first KR-1 cut.
                Note that ``~/.hermes`` continues to receive writes
                via the symlink — useful for rollback testing.

    --check     (default) Report what the migration WOULD do; make no
                changes. Always safe.

    --force     Required if ``~/.kora`` already exists (otherwise the
                script refuses to clobber).

Log lines are written to stderr in the event-log style upstream Hermes
uses for boot-time diagnostics — single-line, structured-ish, prefixed
with ``[kora.migrate]``. Routing through Python's ``logging`` would
require ``kora_logging`` to already be initialized; this script can run
before that, so it talks stderr directly.

Sample invocation::

    $ kora migrate-hermes-home --check
    [kora.migrate] event=plan from=~/.hermes to=~/.kora mode=check
    [kora.migrate] event=found legacy=~/.hermes size_bytes=12345678 entries=42
    [kora.migrate] event=no-op target=~/.kora reason=does-not-exist
    [kora.migrate] event=recommend mode=symlink reason=lowest-friction

    $ kora migrate-hermes-home --symlink
    [kora.migrate] event=plan from=~/.hermes to=~/.kora mode=symlink
    [kora.migrate] event=link target=~/.kora dest=~/.hermes
    [kora.migrate] event=ok mode=symlink
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Tuple


_LOG_PREFIX = "[kora.migrate]"


def _log(**fields: object) -> None:
    """Emit a one-line structured log to stderr."""
    parts = [f"{k}={v}" for k, v in fields.items()]
    line = f"{_LOG_PREFIX} " + " ".join(parts)
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def _display(path: Path) -> str:
    """Render a Path with ~/ shorthand when applicable."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def _dir_size(path: Path) -> Tuple[int, int]:
    """Return (total_bytes, file_count) for path. Returns (0, 0) on error."""
    total = 0
    count = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                p = Path(root) / name
                try:
                    total += p.stat().st_size
                    count += 1
                except OSError:
                    continue
    except OSError:
        return (0, 0)
    return (total, count)


def plan_migration(legacy: Path, target: Path) -> dict:
    """Return a plan dict describing what would happen, without changing anything."""
    plan = {
        "legacy_exists": legacy.exists(),
        "legacy_is_dir": legacy.is_dir() if legacy.exists() else False,
        "legacy_is_symlink": legacy.is_symlink(),
        "target_exists": target.exists(),
        "target_is_dir": target.is_dir() if target.exists() else False,
        "target_is_symlink": target.is_symlink(),
        "target_points_to_legacy": False,
        "legacy_size_bytes": 0,
        "legacy_entries": 0,
    }
    if target.is_symlink():
        try:
            plan["target_points_to_legacy"] = (
                target.resolve() == legacy.resolve()
            )
        except OSError:
            pass
    if plan["legacy_is_dir"]:
        size, count = _dir_size(legacy)
        plan["legacy_size_bytes"] = size
        plan["legacy_entries"] = count
    return plan


def do_check(legacy: Path, target: Path) -> int:
    """Implement --check: report state, suggest mode, exit 0."""
    plan = plan_migration(legacy, target)
    _log(
        event="plan",
        from_=_display(legacy),
        to=_display(target),
        mode="check",
    )
    if not plan["legacy_exists"]:
        _log(
            event="no-op",
            legacy=_display(legacy),
            reason="legacy-does-not-exist",
        )
        if plan["target_exists"]:
            _log(
                event="ok",
                target=_display(target),
                state="already-migrated-or-fresh-install",
            )
        else:
            _log(
                event="ok",
                target=_display(target),
                state="no-data-yet-fresh-install-will-create-on-first-write",
            )
        return 0

    _log(
        event="found",
        legacy=_display(legacy),
        size_bytes=plan["legacy_size_bytes"],
        entries=plan["legacy_entries"],
    )

    if plan["target_points_to_legacy"]:
        _log(
            event="ok",
            target=_display(target),
            state="already-symlinked-to-legacy",
        )
        return 0

    if plan["target_exists"]:
        _log(
            event="no-op",
            target=_display(target),
            reason="target-exists-pass-force-to-clobber",
        )
        _log(event="recommend", mode="manual-merge")
        return 0

    _log(event="recommend", mode="symlink", reason="lowest-friction")
    _log(
        event="hint",
        next_step=f"kora migrate-hermes-home --symlink",
    )
    return 0


def do_symlink(legacy: Path, target: Path, force: bool) -> int:
    """Implement --symlink: create target as a symlink to legacy."""
    _log(
        event="plan",
        from_=_display(legacy),
        to=_display(target),
        mode="symlink",
    )

    if not legacy.exists():
        _log(
            event="error",
            reason="legacy-does-not-exist",
            legacy=_display(legacy),
        )
        return 2

    if target.exists() or target.is_symlink():
        if target.is_symlink():
            try:
                if target.resolve() == legacy.resolve():
                    _log(
                        event="ok",
                        target=_display(target),
                        state="already-symlinked-correctly",
                    )
                    return 0
            except OSError:
                pass
        if not force:
            _log(
                event="error",
                reason="target-exists-use-force-to-replace",
                target=_display(target),
            )
            return 3
        # Force-replace
        try:
            if target.is_symlink() or target.is_file():
                target.unlink()
            else:
                shutil.rmtree(target)
            _log(event="cleared", target=_display(target))
        except OSError as exc:
            _log(event="error", reason="cannot-clear-target", err=str(exc))
            return 4

    try:
        target.symlink_to(legacy, target_is_directory=True)
    except OSError as exc:
        _log(event="error", reason="symlink-failed", err=str(exc))
        return 5

    _log(event="link", target=_display(target), dest=_display(legacy))
    _log(event="ok", mode="symlink")
    return 0


def do_copy(legacy: Path, target: Path, force: bool) -> int:
    """Implement --copy: deep copy legacy → target."""
    _log(
        event="plan",
        from_=_display(legacy),
        to=_display(target),
        mode="copy",
    )

    if not legacy.exists():
        _log(
            event="error",
            reason="legacy-does-not-exist",
            legacy=_display(legacy),
        )
        return 2

    if target.exists() or target.is_symlink():
        if not force:
            _log(
                event="error",
                reason="target-exists-use-force-to-replace",
                target=_display(target),
            )
            return 3
        try:
            if target.is_symlink() or target.is_file():
                target.unlink()
            else:
                shutil.rmtree(target)
            _log(event="cleared", target=_display(target))
        except OSError as exc:
            _log(event="error", reason="cannot-clear-target", err=str(exc))
            return 4

    try:
        shutil.copytree(legacy, target, symlinks=True)
    except OSError as exc:
        _log(event="error", reason="copy-failed", err=str(exc))
        return 5

    size, count = _dir_size(target)
    _log(
        event="copied",
        target=_display(target),
        size_bytes=size,
        entries=count,
    )
    _log(event="ok", mode="copy")
    _log(
        event="hint",
        next_step="legacy=~/.hermes is preserved for rollback; "
                  "delete manually once you've validated ~/.kora.",
    )
    return 0


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kora migrate-hermes-home",
        description=(
            "Migrate the legacy ~/.hermes install dir to ~/.kora "
            "(KR-1 ST3 path rename). Idempotent; safe to run repeatedly."
        ),
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="Report what migration would do; make no changes (default).",
    )
    mode.add_argument(
        "--symlink",
        action="store_true",
        help="Create ~/.kora as a symlink to ~/.hermes (lowest friction).",
    )
    mode.add_argument(
        "--copy",
        action="store_true",
        help="Deep-copy ~/.hermes to ~/.kora (operator keeps legacy for rollback).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Replace ~/.kora if it already exists (otherwise refuse to clobber).",
    )
    p.add_argument(
        "--from",
        dest="legacy_path",
        default=str(Path.home() / ".hermes"),
        help="Legacy install dir (default: ~/.hermes).",
    )
    p.add_argument(
        "--to",
        dest="target_path",
        default=str(Path.home() / ".kora"),
        help="Target Kora install dir (default: ~/.kora).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_argparser()
    args = parser.parse_args(argv)

    legacy = Path(args.legacy_path).expanduser()
    target = Path(args.target_path).expanduser()

    if args.symlink:
        return do_symlink(legacy, target, force=args.force)
    if args.copy:
        return do_copy(legacy, target, force=args.force)
    # Default = --check
    return do_check(legacy, target)


if __name__ == "__main__":
    raise SystemExit(main())
