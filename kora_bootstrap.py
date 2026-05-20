"""Windows UTF-8 bootstrap + Kora-home env var BC for Kora entry points.

Two unrelated startup concerns share this module because both must run
*before any other import*:

A) Windows UTF-8 bootstrap (inherited from upstream Hermes). Fixes the
   ``cp1252`` defaults on Windows console + child processes so
   ``print("café")`` doesn't crash.

B) Kora-home env var BC. Bidirectionally syncs ``KORA_HOME`` and
   ``HERMES_HOME`` so legacy code reading ``HERMES_HOME`` directly
   still works, and a user who sets only one variable gets the other
   propagated for free. Warns once (to stderr) when only the legacy
   ``HERMES_HOME`` is set, recommending migration to ``KORA_HOME``.

Python on Windows has two long-standing text-encoding footguns:

1. ``sys.stdout`` / ``sys.stderr`` are bound to the console code page
   (``cp1252`` on US-locale installs), so ``print("café")`` crashes with
   ``UnicodeEncodeError: 'charmap' codec can't encode character``.

2. Child processes spawned via ``subprocess`` don't know to use UTF-8
   unless ``PYTHONUTF8`` and/or ``PYTHONIOENCODING`` are set in their
   environment — so any Python subprocess (the execute_code sandbox,
   delegation children, linter subprocesses, etc.) inherits the same
   cp1252 defaults and hits the same UnicodeEncodeError.

This module fixes both on Windows *only* — POSIX is untouched.  It
should be imported at the very top of every Kora entry point
(``kora``, ``kora-agent``, ``kora-acp``, ``python -m gateway.run``,
``batch_runner.py``, ``cron/scheduler.py``) before any other imports
that might do file I/O or print to stdout.

What this module does on Windows:

  - Sets ``os.environ["PYTHONUTF8"] = "1"`` (PEP 540 UTF-8 mode) so
    every child process we spawn uses UTF-8 for ``open()`` and stdio.
  - Sets ``os.environ["PYTHONIOENCODING"] = "utf-8"`` for belt-and-
    suspenders — some tools read this instead of / in addition to
    ``PYTHONUTF8``.
  - Reconfigures ``sys.stdout`` / ``sys.stderr`` to UTF-8 in the current
    process, using the ``reconfigure()`` API (Python 3.7+).  This fixes
    ``print("café")`` in the parent without a re-exec.

What this module does NOT do:

  - It does not re-exec Python with ``-X utf8``, so ``open()`` calls in
    the *current* process still default to locale encoding.  Those need
    an explicit ``encoding="utf-8"`` at the call site (lint rule
    ``PLW1514`` / ``PYI058``).  Ruff is the right tool for that sweep.

What this module does on POSIX:

  - Nothing.  POSIX systems are already UTF-8 by default in 99% of cases,
    and we don't want to touch ``LANG``/``LC_*`` behavior that users may
    have configured intentionally.  If someone hits a C/POSIX locale on
    Linux, they can export ``PYTHONUTF8=1`` themselves — we won't override.

Idempotent: safe to call multiple times.  ``_bootstrap_once`` guards
against double-reconfigure.
"""

from __future__ import annotations

import os
import sys

_IS_WINDOWS = sys.platform == "win32"
_bootstrap_applied = False


def apply_windows_utf8_bootstrap() -> bool:
    """Apply the Windows UTF-8 bootstrap if we're on Windows.

    Returns True if bootstrap was applied (i.e. we're on Windows and
    haven't already done this), False otherwise.  The return value is
    advisory — callers normally don't need it, but tests may want to
    assert the path was taken.

    Idempotent: subsequent calls after the first are a no-op.
    """
    global _bootstrap_applied

    if not _IS_WINDOWS:
        return False
    if _bootstrap_applied:
        return False

    # 1. Child processes inherit these and run in UTF-8 mode.
    #    We use setdefault() rather than overwriting so the user can
    #    explicitly opt out by setting PYTHONUTF8=0 in their environment
    #    (or PYTHONIOENCODING=something-else) if they really want to.
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    # 2. Reconfigure the current process's stdio to UTF-8.  Needed
    #    because os.environ changes don't retroactively rebind sys.stdout
    #    — those were bound at interpreter startup based on the console
    #    code page.  ``reconfigure`` is a TextIOWrapper method since 3.7.
    #
    #    errors="replace" means that if we ever *read* something from
    #    stdin that isn't UTF-8 (unlikely but possible with piped input
    #    from legacy tools), we'll get U+FFFD replacement chars rather
    #    than a crash.  Output is pure UTF-8.
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            # Not a TextIOWrapper (could be redirected to a BytesIO in
            # tests, or a non-standard stream in some embedded cases).
            # Skip silently — the env-var fix is still in effect for
            # child processes, which is the bigger win.
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            # Already closed, or someone replaced it with something
            # non-reconfigurable.  Non-fatal.
            pass

    # stdin is reconfigured separately with errors="replace" too — input
    # from a legacy pipe shouldn't crash the process.
    stdin = getattr(sys, "stdin", None)
    if stdin is not None:
        reconfigure = getattr(stdin, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

    _bootstrap_applied = True
    return True


_kora_home_env_init_applied = False
_kora_home_env_warned = False


def init_kora_home_env() -> bool:
    """Synchronize HERMES_* and KORA_* env vars at process start.

    For every ``HERMES_*`` variable in ``os.environ``, the corresponding
    ``KORA_*`` variable is set (if not already set), and vice versa.
    This provides bidirectional backwards-compat without requiring every
    raw env-var reader in the codebase to learn the new contract:

        * If both ``HERMES_FOO`` and ``KORA_FOO`` are set, leave alone
          (operator has chosen both — assume intent).
        * If only ``KORA_FOO`` is set, mirror it to ``HERMES_FOO`` so
          legacy callers that read ``HERMES_FOO`` directly still
          resolve the right value.
        * If only ``HERMES_FOO`` is set, mirror it to ``KORA_FOO`` so
          new callers reading ``KORA_FOO`` get the same value. Emits a
          one-time stderr warning recommending migration to the
          ``KORA_*`` names. The warning lists the set of legacy
          variables once; individual subsequent mirrors are silent.
        * If neither is set: do nothing — resolver defaults handle it.

    The variables covered are determined dynamically by scanning
    ``os.environ`` — any new ``HERMES_*`` variable upstream introduces
    automatically gets BC mirroring on the next process start. This
    closes the KR-1 ST3 gap where only ``HERMES_HOME`` was synced.

    Idempotent. Safe to call multiple times. Returns True if this call
    mutated any env entry, False if it was a no-op (already applied or
    nothing to sync).
    """
    global _kora_home_env_init_applied, _kora_home_env_warned

    if _kora_home_env_init_applied:
        return False

    legacy_keys: list[str] = []
    mutated = False

    # Collect a snapshot — we mutate os.environ during the loop, so
    # we can't iterate the live mapping.
    env_snapshot = dict(os.environ)

    for key, value in env_snapshot.items():
        if not value.strip():
            continue
        if key.startswith("HERMES_"):
            kora_key = "KORA_" + key[len("HERMES_"):]
            if not os.environ.get(kora_key, "").strip():
                os.environ[kora_key] = value
                mutated = True
                legacy_keys.append(key)
        elif key.startswith("KORA_"):
            hermes_key = "HERMES_" + key[len("KORA_"):]
            if not os.environ.get(hermes_key, "").strip():
                os.environ[hermes_key] = value
                mutated = True

    if legacy_keys and not _kora_home_env_warned:
        _kora_home_env_warned = True
        try:
            joined = ", ".join(sorted(legacy_keys))
            sys.stderr.write(
                f"[KORA_HOME bc] Legacy HERMES_* env vars detected "
                f"({joined}). Mirroring to KORA_* for this process. "
                "Migrate to the KORA_* names; HERMES_* support will be "
                "removed after KR-2.\n"
            )
            sys.stderr.flush()
        except Exception:
            pass

    _kora_home_env_init_applied = True
    return mutated


# Apply on import — entry points just need ``import kora_bootstrap``
# (or ``from kora_bootstrap import apply_windows_utf8_bootstrap``) at
# the very top of their module, before importing anything else.  The
# import side effects do the right thing: env-var BC first (so every
# subsequent ``os.environ.get("HERMES_HOME", ...)`` and the resolver in
# ``kora_constants`` both see a consistent value), then the Windows
# UTF-8 fix.
init_kora_home_env()
apply_windows_utf8_bootstrap()
