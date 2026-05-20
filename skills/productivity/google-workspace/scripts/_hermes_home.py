"""Resolve HERMES_HOME for standalone skill scripts.

Skill scripts may run outside the Hermes process (e.g. system Python,
nix env, CI) where ``kora_constants`` is not importable.  This module
provides the same ``get_kora_home()`` and ``display_kora_home()``
contracts as ``kora_constants`` without requiring it on ``sys.path``.

When ``kora_constants`` IS available it is used directly so that any
future enhancements (profile resolution, Docker detection, etc.) are
picked up automatically.  The fallback path replicates the core logic
from ``kora_constants.py`` using only the stdlib.

All scripts under ``google-workspace/scripts/`` should import from here
instead of duplicating the ``HERMES_HOME = Path(os.getenv(...))`` pattern.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from kora_constants import display_kora_home as display_kora_home
    from kora_constants import get_kora_home as get_kora_home
except (ModuleNotFoundError, ImportError):

    def get_kora_home() -> Path:
        """Return the Hermes home directory (default: ~/.kora).

        Mirrors ``kora_constants.get_kora_home()``."""
        val = os.environ.get("HERMES_HOME", "").strip()
        return Path(val) if val else Path.home() / ".kora"

    def display_kora_home() -> str:
        """Return a user-friendly ``~/``-shortened display string.

        Mirrors ``kora_constants.display_kora_home()``."""
        home = get_kora_home()
        try:
            return "~/" + str(home.relative_to(Path.home()))
        except ValueError:
            return str(home)
