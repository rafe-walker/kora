"""Backward-compat shim — KR-KORA-PIP-RESTRUCTURE-PHASE-1B.

The canonical ``KoraControlWriter`` write-side substrate client was
moved to ``packages/isokron-client/src/isokron_client/kora_control_
writer.py`` in this bucket — mirrors the existing
``kora_control_reader`` extraction landed in Phase 1 (#209). The
write side completes the substrate ``kora_control`` SECDEF surface
inside the BYOA library: any Hermes-based agent that needs to
issue STOP-KORA commands now gets both halves (read + write) from
``isokron-client`` with no Kora-CLI dependency.

This module re-exports the public surface from the canonical
location so the 3 existing in-tree callers
(``kora_cli/listeners/mcp_tools.py`` +
``tests/kora_cli/clients/test_kora_control_writer.py`` +
``tests/kora_cli/test_listeners/test_mcp_tools_stop.py``) keep
working unchanged. New code should prefer ``from
isokron_client.kora_control_writer import X``.
"""

from __future__ import annotations

from isokron_client.kora_control_writer import *  # noqa: F401,F403

# Explicit re-export of the names the callers import so static
# analyzers + IDE jump-to-definition resolve cleanly through this
# shim. Mirrors the symbol set in
# ``packages/isokron-client/src/isokron_client/kora_control_writer.
# py`` — keep in sync if that file grows new public symbols.
from isokron_client.kora_control_writer import (  # noqa: F401
    ISSUE_KORA_CONTROL_TIMEOUT_SECONDS,
    InvalidLevelKindError,
    IssueKoraControlResult,
    KoraControlWriter,
    KoraControlWriterError,
    KoraControlWriterTimeout,
    MissingActorIdError,
    SubstrateRejected,
)
