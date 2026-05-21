"""Orphan check: every Hermes model-callable tool has a capability map entry.

The pre-screen middleware returns INCONCLUSIVE for any tool name not
in :data:`TOOL_CAPABILITY_MAP` — fail-CLOSED, but means the model
loses tool access. This test asserts every ``registry.register(name=...)``
call in ``tools/*.py`` has a corresponding map entry (or is a
substrate-tier ``kora__*`` tool, intentionally excluded so the
pre-screen short-circuit can pass them through without a map entry).

When a new tool is added in ``tools/*.py``, this test fails until the
map is extended — the CI gate keeps the pre-screen + capability map
in lock-step with the tool registry.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent.tool_capability_map import TOOL_CAPABILITY_MAP


_REPO_ROOT = Path(__file__).resolve().parent.parent
_TOOLS_DIR = _REPO_ROOT / "tools"
# Match ``registry.register(`` followed by optional whitespace/newlines,
# then ``name="<lowercase_snake>"``. The DOTALL flag lets us span the
# typical multi-line call style used in tools/*.py.
_REGISTRY_REGISTER_NAME_RE = re.compile(
    r'registry\.register\(\s*name="([a-z_][a-z_0-9]+)"',
    re.DOTALL,
)


def _collect_registered_tool_names() -> set[str]:
    """Greps every ``tools/*.py`` for ``registry.register(name="…")``."""
    names: set[str] = set()
    for path in _TOOLS_DIR.glob("*.py"):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for match in _REGISTRY_REGISTER_NAME_RE.finditer(text):
            names.add(match.group(1))
    return names


def test_every_registered_tool_has_a_capability_mapping():
    """No orphan in tools/*.py — every model-callable name is mapped."""
    registered = _collect_registered_tool_names()
    assert registered, (
        "regex did not find any registry.register(name=...) calls in "
        "tools/*.py — pattern likely needs updating."
    )
    mapped = set(TOOL_CAPABILITY_MAP.keys())
    orphans = {
        n
        for n in registered
        if n not in mapped and not n.startswith("kora__")
    }
    assert not orphans, (
        "The following Hermes tools are registered in tools/*.py but "
        "NOT mapped in agent/tool_capability_map.py:\n"
        f"  {sorted(orphans)}\n\n"
        "Add an entry (or rely on the kora__* substrate short-circuit). "
        "Pre-screen verdict for unmapped tools is INCONCLUSIVE — "
        "operator-adjudication required on every call until mapped."
    )


def test_map_has_no_phantom_entries():
    """No stale entry in the map — every mapped name still exists."""
    registered = _collect_registered_tool_names()
    mapped = set(TOOL_CAPABILITY_MAP.keys())
    phantoms = mapped - registered
    assert not phantoms, (
        "TOOL_CAPABILITY_MAP contains entries that DO NOT correspond "
        "to a tools/*.py registry.register(name=...) call:\n"
        f"  {sorted(phantoms)}\n\n"
        "Remove the entry, or rename to match the registry.register "
        "name= value (typo / tool was removed)."
    )


def test_all_cap_values_have_correct_prefix():
    """Every value is a ``cap_*`` name — typo guard."""
    bad = {
        tool: cap
        for tool, cap in TOOL_CAPABILITY_MAP.items()
        if not cap.startswith("cap_")
    }
    assert not bad, (
        "Non-``cap_*``-prefixed values in TOOL_CAPABILITY_MAP "
        f"(possible typo): {bad}"
    )


def test_no_kora_substrate_tool_in_map():
    """``kora__*`` tools must NOT be mapped — pre-screen short-circuits them."""
    substrate_entries = {
        n for n in TOOL_CAPABILITY_MAP if n.startswith("kora__")
    }
    assert not substrate_entries, (
        "Substrate-tier kora__* tools are NOT supposed to appear in "
        "TOOL_CAPABILITY_MAP — the pre-screen passes them through "
        "without a map lookup. Found: "
        f"{sorted(substrate_entries)}"
    )
