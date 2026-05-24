"""Backward-compat shim — KR-KORA-PIP-RESTRUCTURE-PHASE-1.

The canonical Kora plugin code now lives at ``packages/kora-runtime/
src/kora_runtime/`` (pip-installable as ``kora-runtime``). This
package retains the legacy import path so existing in-tree imports
of the form ``from kora_cli.reasoning.kora_hermes_plugin.<sub>
import X`` continue working unchanged via the sys.modules aliases
installed below.

# sys.path bootstrap

Mirrors the ``plugins/marvin/__init__.py`` POC (#204) + the
``plugins/memory/isokron/__init__.py`` shim (this same bucket).
After ``pip install ./packages/kora-runtime`` the bootstrap is a
no-op — ``import kora_runtime`` resolves via site-packages and the
duplicate sys.path entry is skipped.

# Re-exports

The 7 sub-modules (``identity``, ``cost_ladder``, ``caching``,
``audit``, ``short_circuit``, ``state_holders``, ``haiku_router``)
plus the top-level ``plugin`` module are aliased in sys.modules so
both forms keep working:

  * ``from kora_cli.reasoning.kora_hermes_plugin.identity import register``
  * ``from kora_cli.reasoning.kora_hermes_plugin import KoraHermesPlugin``

New code should prefer ``from kora_runtime.<sub> import X`` and
``from kora_runtime import KoraHermesPlugin``.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# ---------------------------------------------------------------------------
# sys.path bootstrap (Marvin POC pattern from #204)
# ---------------------------------------------------------------------------
_KORA_RUNTIME_SRC = (
    _Path(__file__).resolve().parents[3]
    / "packages"
    / "kora-runtime"
    / "src"
)
if _KORA_RUNTIME_SRC.is_dir() and str(_KORA_RUNTIME_SRC) not in _sys.path:
    _sys.path.insert(0, str(_KORA_RUNTIME_SRC))

# ---------------------------------------------------------------------------
# Re-export the top-level public surface (matches the pre-extraction
# ``__all__`` so consumers that did ``from kora_cli.reasoning.kora_hermes_
# plugin import KoraHermesPlugin, register`` continue working.)
# ---------------------------------------------------------------------------
import kora_runtime as _kora_runtime  # noqa: E402
from kora_runtime import KoraHermesPlugin, register  # noqa: E402,F401

# ---------------------------------------------------------------------------
# sys.modules aliasing for the 7 sub-modules + the orchestrator. After
# this loop, ``from kora_cli.reasoning.kora_hermes_plugin.<sub>.<file>
# import X`` resolves to the same module object as ``from kora_runtime.
# <sub>.<file> import X``. Aliasing is recursive so leaf modules
# (e.g. ``identity.constants``) are reachable via the legacy dotted
# path without per-file shim files.
# ---------------------------------------------------------------------------
_SUBS = (
    "identity",
    "cost_ladder",
    "caching",
    "audit",
    "short_circuit",
    "state_holders",
    "haiku_router",
    "plugin",  # the orchestrator module
)
for _sub in _SUBS:
    # Trigger the sub-package import so its module object is realized
    # in sys.modules. ``__import__`` returns the top-level package;
    # we then walk the dotted name to get the leaf.
    __import__(f"kora_runtime.{_sub}")
    _mod = _sys.modules[f"kora_runtime.{_sub}"]
    _sys.modules[f"{__name__}.{_sub}"] = _mod
    globals()[_sub] = _mod
    # For sub-packages (the 7 plugin dirs), also alias their leaf modules.
    if hasattr(_mod, "__path__"):
        _sub_dir = _Path(_mod.__file__).parent
        for _leaf_path in _sub_dir.glob("*.py"):
            _leaf_name = _leaf_path.stem
            if _leaf_name == "__init__":
                continue
            # Trigger the leaf import so the sub-module gets into
            # sys.modules under its kora_runtime path.
            _leaf_mod = __import__(
                f"kora_runtime.{_sub}.{_leaf_name}",
                fromlist=[_leaf_name],
            )
            _sys.modules[f"{__name__}.{_sub}.{_leaf_name}"] = _leaf_mod
del _sub, _mod, _SUBS, _kora_runtime

__all__ = ["KoraHermesPlugin", "register"]
