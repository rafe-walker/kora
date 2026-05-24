"""Standalone smoke test for the isokron-client wheel.

Mirrors the §A.5 dry-run install check from the
KR-KORA-PIP-RESTRUCTURE-PHASE-1 bucket. CI runs this against a
freshly-built wheel installed into a clean venv to verify the BYOA
contract: ``pip install isokron-client`` then ``from isokron_client
import X`` works with ZERO Kora / Hermes / agent.* dependencies on
the path.

These tests do NOT touch a live substrate (no DSN required); they
only exercise the import surface + the side-effect-free helpers
(``TTLCache`` round-trip + ``parse_mcp_transport`` branches).
Substrate-touching behavior is covered by ``tests/plugins/memory/``
in the Kora tree (which imports via the back-compat shim).
"""

from __future__ import annotations


def test_package_imports_with_zero_kora_or_hermes_deps_on_path():
    """The BYOA contract: ``import isokron_client`` resolves with
    nothing from the Kora tree on the path."""
    import isokron_client

    assert isokron_client.__version__ == "0.1.0a1"
    # 20 sub-modules — the 8 conceptual surfaces (events / reads /
    # control / scratchpad / policy / constitution / capability /
    # sea_tickets) decompose into individual files + 5 infra modules
    # (connection / mcp_client / config / models / cache) + 5
    # standalone concept modules (claim_heartbeat / dr_epoch /
    # kora_operation_ledger / session_context / relationlink).
    assert len(isokron_client.__all__) == 20


def test_eight_conceptual_surfaces_are_importable():
    """The operator-confirmed BYOA vocabulary — every concept surface
    in the README must be importable from the wheel."""
    # events
    from isokron_client.events import emit_kora_event, read_recent_kora_events  # noqa: F401
    # reads (also covers ``policy`` — subset of reads.py)
    from isokron_client.reads import (  # noqa: F401
        policies_as_mapping,
        read_active_role_charter,
        read_kora_policy_registry,
    )
    # control (kora_control_reader + observed_kora_control)
    from isokron_client.kora_control_reader import (  # noqa: F401
        KoraControlCommand,
        KoraControlReader,
    )
    from isokron_client.observed_kora_control import (  # noqa: F401
        get_observed_state_via_provider,
    )
    # scratchpad
    from isokron_client.scratchpad import (  # noqa: F401
        ScratchpadEntry,
        ScratchpadKind,
        VisibilityScope,
        write_scratchpad_entry,
    )
    # constitution
    from isokron_client.constitution import read_active_constitution_revision  # noqa: F401
    # capability (capability_check + capability_matrix_mirror)
    from isokron_client.capability_check import (  # noqa: F401
        CapabilityDeniedError,
        actor_has_capability,
    )
    from isokron_client.capability_matrix_mirror import (  # noqa: F401
        ACTOR_CAPABILITY_MATRIX_KORA_COLUMN,
    )
    # sea_tickets (assigned_sea_tickets + cost_deferred_tickets)
    from isokron_client.assigned_sea_tickets import read_assigned_sea_tickets  # noqa: F401
    from isokron_client.cost_deferred_tickets import (  # noqa: F401
        read_deferred_cost_limit_tickets,
    )


def test_ttl_cache_roundtrips_a_value():
    """``TTLCache`` is the substrate-agnostic primitive composed into
    the provider's per-workspace read caches. Side-effect-free, safe
    to exercise without a live substrate."""
    from isokron_client.cache import TTLCache

    cache: TTLCache[str] = TTLCache(ttl_seconds=10.0)
    cache.put("k1", "v1")
    assert cache.get("k1") == "v1"
    assert cache.get("missing") is None


def test_parse_mcp_transport_discriminates_stdio_vs_http():
    """``parse_mcp_transport`` is the only piece of substrate-side
    config logic that's pure (no env / pool / socket). Branch
    coverage proves the package's config layer ships intact."""
    from isokron_client.config import parse_mcp_transport

    assert parse_mcp_transport("stdio://node ./sea-mcp.js") == "stdio"
    assert parse_mcp_transport("http://substrate.local:8080/mcp") == "http"
    assert parse_mcp_transport("https://substrate.local:8443/mcp") == "http"


def test_capability_check_resolves_known_kora_caps():
    """``actor_has_capability`` reads the C2 mirror at module import
    time and answers Kora-row questions synchronously. Confirms the
    capability_matrix_mirror cross-reference resolves inside the
    installed wheel (relative ``from .capability_matrix_mirror import
    X`` works post-install)."""
    from isokron_client.capability_check import actor_has_capability

    # cap_write_agent_scratchpad — Kora is the primary writer (Plan 02).
    assert actor_has_capability("cap_write_agent_scratchpad") is True
    # cap_override_security_or_policy_verdict — operator-only (Cell C).
    assert actor_has_capability("cap_override_security_or_policy_verdict") is False
