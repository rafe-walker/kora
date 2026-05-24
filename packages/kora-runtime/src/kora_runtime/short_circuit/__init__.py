"""Short-circuit sub-plugin — KR-PLUGIN-SHORT-CIRCUIT.

Owns the regex + snapshot interpolation phrasebook matcher that
previously lived in ``kora_cli/short_circuit/dm_phrasebook.py``.
Bundled phrasebook YAML stays at
``kora_cli/short_circuit/default_slack_dm_phrasebook.yml`` so the
phrasebook editor (PR #177) reads/writes it from the same package
data location it has always used.

# Hook ownership status

Short-circuit currently runs **outside** Hermes — the slack DM
handler calls :func:`try_short_circuit` directly before invoking
the reasoning engine. A future ``transform_input`` hook will
move that check to the plugin layer; the stub handler in
``plugin.py`` documents the boundary so the wiring change is a
single-file edit when it lands.
"""

from kora_runtime.short_circuit.constants import (
    BUNDLED_PHRASEBOOK_FILENAME,
    BUNDLED_PHRASEBOOK_PACKAGE,
    OPERATOR_OVERRIDE_RELATIVE,
)
from kora_runtime.short_circuit.matcher import (
    PhrasebookEntry,
    ShortCircuitMatch,
    load_phrasebook,
    match_message,
    render_reply,
    try_short_circuit,
)
from kora_runtime.short_circuit.plugin import (
    register,
    short_circuit_hook,
)

__all__ = [
    "BUNDLED_PHRASEBOOK_FILENAME",
    "BUNDLED_PHRASEBOOK_PACKAGE",
    "OPERATOR_OVERRIDE_RELATIVE",
    "PhrasebookEntry",
    "ShortCircuitMatch",
    "load_phrasebook",
    "match_message",
    "register",
    "render_reply",
    "short_circuit_hook",
    "try_short_circuit",
]
