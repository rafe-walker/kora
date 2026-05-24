"""Marvin — Paranoid-android plugin POC for Hermes Option C identity-as-plugin.

Canonical module of the ``marvin-runtime`` pip package. Validates the
KR-PLUGIN-IDENTITY Option C architecture from #199 + the pip-installable
distribution surface foundation from #204.

# What this module does

Reads ``data/MARVIN.md`` + ``data/marvin_system_prompt.md`` at import
time + exposes a ``register(ctx)`` function that registers an identity
provider returning Marvin's ``IdentitySpec``.

# Two installation modes

  1. **Bundled (in-tree dev)**: a thin shim at ``plugins/marvin/__init__.py``
     adds ``plugins/marvin/src`` to ``sys.path`` and re-exports symbols
     from this module. Lets the existing 11 tests at
     ``tests/plugins/test_marvin_multi_tenant_proof.py`` continue
     importing ``from plugins.marvin import marvin_identity_provider``
     without modification.

  2. **Pip-installed**: ``pip install marvin-runtime`` (built from
     ``plugins/marvin/pyproject.toml``) registers the
     ``hermes_agent.plugins`` entry point ``marvin = marvin:register``.
     Hermes's ``PluginManager._scan_entry_points`` discovers it at
     boot. Same ``register`` callable; same ``IdentitySpec``.

# Data-file resolution (relocatability)

The two identity files (``MARVIN.md`` + ``marvin_system_prompt.md``)
live at ``<package>/data/`` and are resolved via ``Path(__file__)
.resolve().parent / "data" / ...``. This works identically in both
modes — in-tree the path resolves to ``plugins/marvin/src/marvin/
data/``; installed it resolves to ``<site-packages>/marvin/data/``.
``pyproject.toml`` declares ``[tool.setuptools.package-data] marvin
= ["data/*.md"]`` so the wheel includes them.

# First-non-None-wins ordering

When BOTH Marvin and Kora plugins are loaded, the engine consumes the
FIRST non-None ``IdentitySpec`` returned by the hook. Order is
controlled by ``plugins.enabled`` in Hermes config (FIFO).

# Operator activation (pip-installed mode)

::

    pip install marvin-runtime           # future: from PyPI
    pip install plugins/marvin           # today: from source (the dry-
                                         # run-validation path)

Then add to ``~/.hermes/config.yaml``::

    plugins:
      enabled:
        - marvin
"""

from __future__ import annotations

import logging
from pathlib import Path

from agent.identity_spec import IdentitySpec

logger = logging.getLogger(__name__)


# Identity-file paths inside the package — works in both bundled-mode
# (plugins/marvin/src/marvin/data/) and pip-installed-mode
# (<site-packages>/marvin/data/). See module docstring §3.
_PACKAGE_DIR = Path(__file__).resolve().parent
_DATA_DIR = _PACKAGE_DIR / "data"

# Read at import time. Daemon restart re-reads (same hot-reload
# semantic as Kora's identity provider — file edits take effect on
# next boot, not mid-session).
_MARVIN_SOUL = (_DATA_DIR / "MARVIN.md").read_text(encoding="utf-8")
_MARVIN_SYSTEM_PROMPT = (
    _DATA_DIR / "marvin_system_prompt.md"
).read_text(encoding="utf-8")


def marvin_identity_provider(*, engine=None, **kw):
    """Return Marvin's ``IdentitySpec`` for the
    ``pre_agent_identity_set`` hook.

    Unconditional today: when Marvin plugin loads, Marvin claims
    identity. The ``engine`` kwarg is the firing-engine instance;
    carried here for future use (per-engine routing, multi-Marvin
    variants). Ignored today.
    """
    return IdentitySpec(
        soul_md_content=_MARVIN_SOUL,
        system_prompt_content=_MARVIN_SYSTEM_PROMPT,
        identity_metadata={
            "agent_name": "Marvin",
            "agent_version": "0.1.0",
            "plugin_name": "marvin",
            "persona": "paranoid_android_demo",
        },
    )


def register(ctx) -> None:
    """Plugin entry point. Called by Hermes's ``PluginManager`` with
    a ``PluginContext`` — either via bundled-plugin discovery (in-
    tree, via the ``plugins/marvin/__init__.py`` shim) OR via
    entry-point discovery (post-pip-install, via the
    ``hermes_agent.plugins`` entry point declared in
    ``pyproject.toml``). Same callable in both modes.
    """
    ctx.register_identity_provider(marvin_identity_provider)
    logger.info(
        "[marvin] identity provider registered "
        "(soul_chars=%d, system_prompt_chars=%d)",
        len(_MARVIN_SOUL),
        len(_MARVIN_SYSTEM_PROMPT),
    )


__all__ = ["marvin_identity_provider", "register"]
