"""Bundled-plugin compat shim — re-exports from the relocatable
``src/marvin/`` package.

Per CC#3 #204 (KR-PIP-PACKAGING-FOUNDATION): Marvin's code lives at
``plugins/marvin/src/marvin/`` so the package is pip-installable as
``marvin-runtime`` (see ``pyproject.toml``). The bundled-plugin
discovery path Hermes uses for in-tree development requires a
``plugins/<name>/__init__.py`` that exposes ``register(ctx)``. This
shim bridges the two:

  - Adds ``plugins/marvin/src`` to ``sys.path`` (idempotent).
  - Imports + re-exports ``register`` + ``marvin_identity_provider``
    from the relocated package.

After this shim runs, ``from plugins.marvin import register`` works
(in-tree dev path) AND ``from marvin import register`` works (post-
pip-install path). Existing tests at
``tests/plugins/test_marvin_multi_tenant_proof.py`` use the first
form and continue to pass without modification.

When the operator actually publishes Marvin to PyPI in a future
bucket, this shim stays in the repo for in-tree dev convenience —
publishing doesn't remove this file.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Idempotent sys.path adjustment so ``import marvin`` resolves to
# the relocatable package at ``src/marvin/``. After pip install, the
# relocatable package is found via site-packages instead and this
# block is a no-op (the same module name resolves; sys.path stays
# clean).
_SRC_DIR = Path(__file__).resolve().parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# Re-exports — same surface the bundled-plugin discovery + existing
# tests expect. The actual implementation lives in src/marvin/.
from marvin import (  # noqa: E402 — sys.path adjustment must precede the import
    marvin_identity_provider,
    register,
)

__all__ = ["marvin_identity_provider", "register"]
