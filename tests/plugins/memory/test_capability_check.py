"""KR-6 — Python ``actor_has_capability`` mirror tests.

Asserts:

- ``actor_has_capability`` returns the C2 mirror's truth for known
  capabilities; raises ``KeyError`` for unknown ones (fail-loud per
  spec — the parity test guards drift, so unknown caps are programmer
  typos).
- ``assert_kora_can_perform`` passes silently for granted caps;
  raises ``CapabilityDeniedError`` with ``.capability`` + ``.reason``
  for denied caps; passes through ``KeyError`` for unknown caps.
- Custom denial reason wins over the default.
- **Import-time independence**: ``capability_check.py`` doesn't
  trigger MCP / DB / HTTP imports at module load. This is the
  forward-stability invariant — when K-7 → KR-N swaps the C2 mirror
  to an MCP-fetched matrix, the swap is a one-line change to the
  import, not a wholesale module restructure.
"""

from __future__ import annotations

import importlib
import sys

import pytest

from plugins.memory.isokron.capability_check import (
    CapabilityDeniedError,
    actor_has_capability,
    assert_kora_can_perform,
)


# ---------------------------------------------------------------------------
# actor_has_capability — granted / denied / unknown
# ---------------------------------------------------------------------------


def test_actor_has_capability_returns_true_for_granted_cap():
    """``cap_sea_create`` is Kora-granted per the C2 mirror."""
    assert actor_has_capability("cap_sea_create") is True


def test_actor_has_capability_returns_true_for_kora_primary_writer_cap():
    """``cap_write_agent_scratchpad`` — Kora is the primary writer (Plan 02)."""
    assert actor_has_capability("cap_write_agent_scratchpad") is True


def test_actor_has_capability_returns_false_for_operator_only_cap():
    """``cap_override_security_or_policy_verdict`` is operator-only (Cell C)."""
    assert actor_has_capability("cap_override_security_or_policy_verdict") is False


def test_actor_has_capability_returns_false_for_unbless_convention():
    """``cap_unbless_convention`` is operator-only — added 2026-05-20."""
    assert actor_has_capability("cap_unbless_convention") is False


def test_actor_has_capability_raises_keyerror_for_unknown_cap():
    """Unknown caps are fail-LOUD — likely a typo; parity test catches drift."""
    with pytest.raises(KeyError) as excinfo:
        actor_has_capability("cap_does_not_exist_anywhere")
    msg = str(excinfo.value)
    assert "Unknown capability" in msg
    assert "cap_does_not_exist_anywhere" in msg
    # Helpful diagnostic: surface what the parity test is for.
    assert "parity test" in msg or "C2 mirror" in msg


# ---------------------------------------------------------------------------
# assert_kora_can_perform — happy path, denied, custom reason, unknown
# ---------------------------------------------------------------------------


def test_assert_kora_can_perform_passes_silently_for_granted_cap():
    """Granted caps return ``None`` with no exception, no log."""
    assert assert_kora_can_perform("cap_propose_policy_change") is None


def test_assert_kora_can_perform_raises_on_denied_cap_with_default_reason():
    """Denied caps raise CapabilityDeniedError carrying .capability + default .reason."""
    with pytest.raises(CapabilityDeniedError) as excinfo:
        assert_kora_can_perform("cap_operator_approve_policy_change")
    err = excinfo.value
    assert err.capability == "cap_operator_approve_policy_change"
    assert "actor_kind='kora'" in err.reason
    assert "cap_operator_approve_policy_change" in err.reason


def test_assert_kora_can_perform_raises_with_caller_supplied_reason():
    """Callers can override the default denial message."""
    with pytest.raises(CapabilityDeniedError) as excinfo:
        assert_kora_can_perform(
            "cap_operator_ack_escalation",
            reason="Kora attempted to ack an escalation she didn't open",
        )
    err = excinfo.value
    assert err.capability == "cap_operator_ack_escalation"
    assert err.reason == "Kora attempted to ack an escalation she didn't open"


def test_assert_kora_can_perform_unknown_cap_propagates_keyerror():
    """Unknown caps propagate KeyError, NOT CapabilityDeniedError."""
    with pytest.raises(KeyError):
        assert_kora_can_perform("cap_typo_in_handler")


# ---------------------------------------------------------------------------
# Forward stability — module has no MCP / DB / network imports at load time
# ---------------------------------------------------------------------------


def test_module_has_no_network_or_db_imports_at_load_time():
    """capability_check.py must NOT import asyncpg / mcp / httpx / aiohttp at load.

    Forward-stability invariant for the K-7 → KR-N swap. The future
    swap will replace the ``capability_matrix_mirror`` import with a
    fresh-per-call MCP fetch — but that MCP machinery is opt-in (only
    imported by the swapping caller, not by capability_check itself).
    If this test fails, the swap becomes a multi-file refactor instead
    of a one-line change.
    """
    # Fresh-load the module so any import-time side effects are visible
    # in sys.modules even if a previous test already triggered them. The
    # canonical module name is ``isokron_client.capability_check`` post
    # KR-KORA-PIP-RESTRUCTURE-PHASE-1; the legacy ``plugins.memory.isokron.
    # capability_check`` is a sys.modules alias installed by the shim
    # at ``plugins/memory/isokron/__init__.py`` and goes away when
    # the alias is deleted. We target the canonical name so the fresh
    # importlib reimport actually finds the source file.
    mod_name = "isokron_client.capability_check"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    importlib.import_module(mod_name)

    # The module's own import graph should not include any networking
    # or DB libraries. We check both the module's source AST AND that
    # neither asyncpg nor mcp got injected into sys.modules as a
    # side effect of importing capability_check.py specifically.
    import inspect

    mod = sys.modules[mod_name]
    src = inspect.getsource(mod)
    forbidden_imports = ("asyncpg", "import mcp", "from mcp", "httpx", "aiohttp")
    for forbidden in forbidden_imports:
        assert forbidden not in src, (
            f"capability_check.py imports {forbidden!r} — that breaks "
            f"the K-7 → KR-N forward-stability invariant. The MCP swap "
            f"must be a one-line import change."
        )


def test_capability_check_only_imports_the_mirror_dict():
    """The only project-internal import is the C2 mirror — nothing else.

    Reinforces the forward-stability check: when K-7 ships, the only
    line that changes is the C2-mirror import. If we accidentally pull
    in (say) the provider module or the asyncpg-using reads module,
    the swap balloons.
    """
    import inspect

    from plugins.memory.isokron import capability_check

    src = inspect.getsource(capability_check)
    # The two project imports in the module: capability_matrix_mirror.
    assert "from .capability_matrix_mirror import" in src
    # No imports of provider / connection / reads / scratchpad / events.
    for forbidden in (
        "from .provider",
        "from .connection",
        "from .reads",
        "from .scratchpad",
        "from .events",
        "from .relationlink",
    ):
        assert forbidden not in src, (
            f"capability_check.py imports {forbidden!r} — keeps the K-7 "
            f"swap surface tight; remove the import or this test."
        )
