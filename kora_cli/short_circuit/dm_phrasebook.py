"""Backward-compat shim — KR-PLUGIN-EXTRACTIONS-BATCH-2 (Deliverable C).

The phrasebook matcher moved to the canonical Hermes plugin
location at ``kora_cli/reasoning/kora_hermes_plugin/short_circuit/
matcher.py`` (single canonical home for plugin-resident logic).
This module re-exports every symbol the prior public surface
advertised so downstream callers continue working without
modification:

  - ``kora_cli/handlers/slack_dm_handler.py`` — calls
    :func:`try_short_circuit` on every inbound DM.
  - ``kora_cli/web_server.py`` — phrasebook-editor read endpoints.
  - ``kora_cli/short_circuit/phrasebook_editor.py`` — read
    helpers reuse the loader.
  - ``kora_cli/short_circuit/__init__.py`` — package-level
    re-export.

If this module ever drifts from the canonical location, the
identity-against-canonical tests in
``tests/kora_cli/short_circuit/test_dm_phrasebook.py`` will
fail loud.

The bundled YAML phrasebook stays at
``kora_cli/short_circuit/default_slack_dm_phrasebook.yml`` —
the phrasebook editor (PR #177) reads/writes it from the same
package data location it always has.
"""

from __future__ import annotations

# Re-export the public surface.
from kora_cli.reasoning.kora_hermes_plugin.short_circuit.matcher import (
    _MISSING,
    _PLACEHOLDER_RE,
    _operator_override_path,
    _parse_entries,
    _read_bundled_default,
    _walk_snapshot,
    PhrasebookEntry,
    ShortCircuitMatch,
    load_phrasebook,
    match_message,
    render_reply,
    try_short_circuit,
)

# Backward-compat module-level constants that previously lived
# in this file. Resolved from the canonical constants module
# (one source of truth for the YAML filename + override path).
from kora_cli.reasoning.kora_hermes_plugin.short_circuit.constants import (
    BUNDLED_PHRASEBOOK_FILENAME as _DEFAULT_PHRASEBOOK_FILENAME,
    OPERATOR_OVERRIDE_RELATIVE as _OPERATOR_OVERRIDE_RELATIVE,
)

__all__ = [
    "PhrasebookEntry",
    "ShortCircuitMatch",
    "load_phrasebook",
    "match_message",
    "render_reply",
    "try_short_circuit",
]
