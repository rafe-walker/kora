"""Pure file-reading helpers for the identity sub-plugin.

The plugin handler in ``plugin.py`` orchestrates the resolution
(env-override → fallback to default paths → build IdentitySpec);
this module holds the pure functions it composes with so they're
independently testable.

Two responsibilities:

  1. :func:`resolve_system_prompt_path` — read env override or
     return canonical default.
  2. :func:`resolve_soul_md_path` — read env override or return
     canonical default.
  3. :func:`load_kora_identity` — orchestrate the read; build the
     full IdentitySpec.

All functions are fail-soft at the load level — they return
empty strings + log DEBUG when a file is missing. The PLUGIN
handler decides whether empty content should fall through to
the engine's file-read default (yes — emptiness is treated as
"no claim on identity").
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from agent.identity_spec import IdentitySpec

from kora_cli.reasoning.kora_hermes_plugin.identity.constants import (
    DEFAULT_AGENT_NAME,
    DEFAULT_AGENT_VERSION,
    DEFAULT_PLUGIN_NAME,
    DEFAULT_SOUL_MD_PATH,
    DEFAULT_SYSTEM_PROMPT_PATH,
    ENV_SOUL_MD_PATH,
    ENV_SYSTEM_PROMPT_PATH,
)

logger = logging.getLogger(__name__)


def resolve_system_prompt_path() -> Path:
    """Return the resolved system-prompt path.

    Honors :data:`ENV_SYSTEM_PROMPT_PATH` (operator override) and
    falls back to :data:`DEFAULT_SYSTEM_PROMPT_PATH` when unset.
    Empty/whitespace env values are treated as unset.
    """
    raw = os.environ.get(ENV_SYSTEM_PROMPT_PATH, "").strip()
    if raw:
        return Path(raw)
    return DEFAULT_SYSTEM_PROMPT_PATH


def resolve_soul_md_path() -> Path:
    """Return the resolved SOUL.md path.

    Honors :data:`ENV_SOUL_MD_PATH` (operator override) and falls
    back to :data:`DEFAULT_SOUL_MD_PATH` when unset.
    """
    raw = os.environ.get(ENV_SOUL_MD_PATH, "").strip()
    if raw:
        return Path(raw)
    return DEFAULT_SOUL_MD_PATH


def _read_file_fail_soft(path: Path, label: str) -> str:
    """Read a file, returning "" on any IO error + logging at DEBUG.

    Used for the SOUL.md side where missing files are non-fatal
    (SOUL.md is an OPTIONAL operator-tunable override historically).
    The reasoning-engine system prompt is fail-CLOSED at the engine
    level — that fail-closed semantic is enforced by the engine, not
    by this loader, so the loader stays uniformly soft.
    """
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug(
            "[kora_hermes.identity] %s unreadable at %s: %r — using "
            "empty string",
            label,
            path,
            exc,
        )
        return ""


def load_kora_identity() -> Optional[IdentitySpec]:
    """Load Kora's canonical identity into an :class:`IdentitySpec`.

    Returns ``None`` when the system-prompt file is unreadable OR
    empty — that signals "no identity claim from Kora" to the
    hook firing site, which then falls through to the engine's
    file-read default (preserving the pre-Option-C behavior).

    SOUL.md is OPTIONAL — when missing the spec still resolves, but
    ``soul_md_content`` is empty. The engine ignores ``soul_md_content``
    today (it only consumes ``system_prompt_content``); SOUL.md is
    carried in the IdentitySpec so future consumers (prompt_builder,
    skin layers) can resolve identity via the plugin surface instead
    of the filesystem.

    All metadata fields under :data:`DEFAULT_*` constants populate
    the ``identity_metadata`` dict for cockpit + telemetry consumers.
    """
    system_prompt_path = resolve_system_prompt_path()
    system_prompt_content = _read_file_fail_soft(
        system_prompt_path, "kora_system_prompt.md"
    )
    if not system_prompt_content.strip():
        # Empty system prompt → no claim. Engine will fall back to
        # its own file-read default (which will then fail-CLOSED if
        # also empty, surfacing the underlying issue at engine init).
        logger.debug(
            "[kora_hermes.identity] system prompt empty at %s — "
            "yielding to engine file-read default",
            system_prompt_path,
        )
        return None

    soul_md_path = resolve_soul_md_path()
    soul_md_content = _read_file_fail_soft(soul_md_path, "SOUL.md")

    return IdentitySpec(
        soul_md_content=soul_md_content,
        system_prompt_content=system_prompt_content,
        identity_metadata={
            "agent_name": DEFAULT_AGENT_NAME,
            "agent_version": DEFAULT_AGENT_VERSION,
            "plugin_name": DEFAULT_PLUGIN_NAME,
            "system_prompt_path": str(system_prompt_path),
            "soul_md_path": str(soul_md_path),
        },
    )


__all__ = [
    "load_kora_identity",
    "resolve_soul_md_path",
    "resolve_system_prompt_path",
]
