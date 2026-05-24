"""Shim-verification suite for ``kora_cli/short_circuit/dm_phrasebook.py``.

The behavioral tests for the phrasebook matcher moved to
``tests/kora_cli/reasoning/kora_hermes_plugin/short_circuit/
test_matcher.py`` in KR-PLUGIN-EXTRACTIONS-BATCH-2 (Deliverable
C) — mirrors the canonical code location at ``kora_cli/
reasoning/kora_hermes_plugin/short_circuit/matcher.py``.

This file remains as a small assertion suite that the
backward-compat shim at ``kora_cli/short_circuit/dm_phrasebook.py``
still re-exports the matcher's public surface so downstream
callers — slack_dm_handler, web_server, phrasebook_editor — keep
working without modification.

If this file ever fails, the shim has drifted from the canonical
location — fix by re-exporting the missing symbol in
``kora_cli/short_circuit/dm_phrasebook.py``.
"""

from __future__ import annotations


def test_shim_reexports_full_public_surface():
    """The shim must re-export every symbol the historical
    ``kora_cli.short_circuit.dm_phrasebook`` public surface
    advertised. Asserted by identity-against-canonical: each
    shim attr is the SAME object as the canonical module's
    attr (not a separately-imported copy)."""
    from kora_cli.reasoning.kora_hermes_plugin.short_circuit import (
        matcher as canonical_matcher,
    )
    from kora_cli.short_circuit import dm_phrasebook as shim

    for name in [
        # Public API
        "PhrasebookEntry",
        "ShortCircuitMatch",
        "load_phrasebook",
        "match_message",
        "render_reply",
        "try_short_circuit",
        # Private helpers + sentinels (re-exported for back-compat;
        # phrasebook_editor.py uses _parse_entries; tests reach
        # for _MISSING + _PLACEHOLDER_RE in earlier suites)
        "_MISSING",
        "_PLACEHOLDER_RE",
        "_operator_override_path",
        "_parse_entries",
        "_read_bundled_default",
        "_walk_snapshot",
    ]:
        assert getattr(shim, name) is getattr(canonical_matcher, name), (
            f"shim drift: {name} not re-exported (or is a copy, "
            f"not a reference) — fix kora_cli/short_circuit/"
            f"dm_phrasebook.py"
        )


def test_package_level_reexport_still_works():
    """``from kora_cli.short_circuit import try_short_circuit``
    (the package-level import path used by slack_dm_handler.py)
    must keep resolving."""
    from kora_cli.short_circuit import (
        PhrasebookEntry,
        ShortCircuitMatch,
        load_phrasebook,
        match_message,
        render_reply,
        try_short_circuit,
    )

    pb = load_phrasebook()
    assert isinstance(pb, list)
    assert len(pb) > 0
    assert isinstance(pb[0], PhrasebookEntry)

    # No-match path returns None
    assert match_message("xyzzy-not-a-real-query", pb) is None
    # match_message with non-string returns None
    assert match_message(None, pb) is None  # type: ignore[arg-type]
    # render_reply with None snapshot returns None
    assert render_reply(pb[0], None) is None

    # try_short_circuit on no-match → None
    assert try_short_circuit("xyzzy-not-a-real-query", pb, {}) is None


def test_canonical_path_is_the_authoritative_source():
    """Sanity: the canonical path is where the public symbols
    are DEFINED (``__module__`` attribute). The shim re-exports;
    the canonical module owns. Post KR-KORA-PIP-RESTRUCTURE-
    PHASE-1 the canonical path is ``kora_runtime.short_circuit.
    matcher`` — the legacy
    ``kora_cli.reasoning.kora_hermes_plugin.short_circuit`` path
    now resolves through the sys.modules alias installed by the
    back-compat shim at ``kora_cli/reasoning/kora_hermes_plugin/
    __init__.py``."""
    from kora_cli.reasoning.kora_hermes_plugin.short_circuit import (
        PhrasebookEntry,
        try_short_circuit,
    )

    assert (
        PhrasebookEntry.__module__
        == "kora_runtime.short_circuit.matcher"
    )
    assert (
        try_short_circuit.__module__
        == "kora_runtime.short_circuit.matcher"
    )
