"""Short-circuit pre-filter for trivial DM queries — answers from
snapshot at zero LLM cost. See ``dm_phrasebook.py`` for the full
phrasebook + render + match surface."""

from kora_cli.short_circuit.dm_phrasebook import (
    PhrasebookEntry,
    ShortCircuitMatch,
    load_phrasebook,
    match_message,
    render_reply,
    try_short_circuit,
)

__all__ = [
    "PhrasebookEntry",
    "ShortCircuitMatch",
    "load_phrasebook",
    "match_message",
    "render_reply",
    "try_short_circuit",
]
