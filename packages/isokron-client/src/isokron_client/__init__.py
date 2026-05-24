"""isokron-client — IsoKron substrate client library for BYOA Hermes agents.

This package is the PURE LIBRARY half of the KR-2 IsoKron memory provider
extraction (KR-KORA-PIP-RESTRUCTURE-PHASE-1). It contains every substrate-
functional file that previously lived at ``plugins/memory/isokron/`` — with
ZERO Kora-specific or Hermes-plugin coupling. The Hermes ``MemoryProvider``
shape (``provider.py``), gateway-lifecycle singletons (``active_provider.py``,
``active_poller.py``, ``sea_ticket_poller*``), and Hermes-discovery entry
(``plugins/memory/isokron/__init__.py``) stay in the Kora tree as a thin
plugin that imports from THIS library.

# Public surface — 8 conceptual sub-modules

The operator-confirmed BYOA vocabulary (drop the strawman audit/conversation/
workspace from the original bucket proposal — those don't map to what's
actually here). The list below maps each concept to the underlying file(s):

  - ``events``         → ``isokron_client.events``
                         (``emit_kora_event``, ``read_recent_kora_events``,
                         ``RecentChainEvent``, ``ChainEventRow``)
  - ``reads``          → ``isokron_client.reads``
                         (``read_active_role_charter``, ``read_kora_policy_
                         registry``, ``read_kora_capability_row``, ``policies
                         _as_mapping``)
  - ``control``        → ``isokron_client.kora_control_reader`` +
                         ``isokron_client.observed_kora_control``
                         (``KoraControlReader``, ``KoraControlCommand``,
                         ``get_observed_state_via_provider``)
  - ``scratchpad``     → ``isokron_client.scratchpad``
                         (``read_own_scratchpad``, ``read_cross_agent_
                         scratchpad``, ``write_scratchpad_entry``,
                         ``ScratchpadEntry``, ``ScratchpadKind``,
                         ``VisibilityScope``)
  - ``policy``         → subset of ``isokron_client.reads`` (the
                         ``read_kora_policy_registry`` + ``policies_as_
                         mapping`` pair)
  - ``constitution``   → ``isokron_client.constitution``
                         (``read_active_constitution_revision``)
  - ``capability``     → ``isokron_client.capability_check`` +
                         ``isokron_client.capability_matrix_mirror``
                         (``actor_has_capability``, ``assert_kora_can_
                         perform``, ``CapabilityDeniedError``, capability
                         matrix parity helpers)
  - ``sea_tickets``    → ``isokron_client.assigned_sea_tickets`` +
                         ``isokron_client.cost_deferred_tickets``
                         (``read_assigned_sea_tickets``, ``read_deferred_
                         cost_limit_tickets``)

# Infrastructure modules

``connection``, ``mcp_client``, ``config``, ``models``, ``cache`` — the
substrate plumbing every concept module composes against. BYOA agents
typically construct an ``IsoKronConnection`` (from ``connection.py``) and
hand it to the concept modules' functions.

# Other concept modules (no obvious sub-group)

``claim_heartbeat``, ``dr_epoch``, ``kora_operation_ledger``,
``session_context``, ``relationlink`` — each is a focused substrate
surface with no peer to group with. Surfaced individually rather than
forced into an artificial bucket.

# Backward compatibility

The Kora tree retains a shim at ``plugins/memory/isokron/`` that re-
exports the public surface from here (sys.path bootstrap mirrors the
``plugins/marvin/`` POC pattern from #204). Existing imports of the
form ``from plugins.memory.isokron.<name> import X`` continue to work
unchanged. New code should prefer ``from isokron_client.<name> import X``.

# Source-only dependency note

isokron-client itself is pip-installable (``pip install ./packages/
isokron-client``) with no source-only dependencies. The Kora tree's
Hermes-plugin half (``plugins/memory/isokron/``) imports from this
library AND from ``agent.memory_provider`` (Hermes, source-only) —
that source-only dep lives in the PLUGIN half, not in this library.
A BYOA agent that wants only the substrate primitives can ``pip
install isokron-client`` with nothing else.
"""

__version__ = "0.1.0a1"

# Concept-module re-exports — the documented public surface. Consumers
# can either ``from isokron_client.<module> import X`` (preferred for
# clarity) or use the namespace aliases below for the 8 conceptual
# groupings.
from . import (
    assigned_sea_tickets,
    cache,
    capability_check,
    capability_matrix_mirror,
    claim_heartbeat,
    config,
    connection,
    constitution,
    cost_deferred_tickets,
    dr_epoch,
    events,
    kora_control_reader,
    kora_operation_ledger,
    mcp_client,
    models,
    observed_kora_control,
    reads,
    relationlink,
    scratchpad,
    session_context,
)

__all__ = [
    "assigned_sea_tickets",
    "cache",
    "capability_check",
    "capability_matrix_mirror",
    "claim_heartbeat",
    "config",
    "connection",
    "constitution",
    "cost_deferred_tickets",
    "dr_epoch",
    "events",
    "kora_control_reader",
    "kora_operation_ledger",
    "mcp_client",
    "models",
    "observed_kora_control",
    "reads",
    "relationlink",
    "scratchpad",
    "session_context",
]
