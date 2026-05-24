"""Pre-warmed daemon snapshot — KR-CHEAP-PRE-WARMED-SNAPSHOT.

See :mod:`kora_cli.snapshot.state_snapshot` for the full module
docstring + per-source collector contracts.

Public surface:
  - :func:`compute_snapshot` — assemble snapshot dict (no I/O)
  - :func:`write_snapshot` — atomic-write to disk
  - :func:`read_snapshot` — read parsed snapshot or ``None``
  - :func:`is_snapshot_fresh` — staleness check (>10 min = stale)
  - :func:`snapshot_path` — resolve the snapshot file path
  - :func:`run_snapshot_cycle` — periodic-task entry point
  - :func:`get_snapshot_for_routing` — convenience for the future
    reasoning-engine routing-layer short-circuit (KR-SNAPSHOT-INTO-
    ROUTING bucket); returns the fresh snapshot or ``None`` if
    routing should fall back to live tool-calling
  - ``SCHEMA_VERSION`` — wire-stable schema version int
"""

from typing import Any, Dict, Optional

from kora_cli.snapshot.state_snapshot import (
    SCHEMA_VERSION,
    SNAPSHOT_FRESH_THRESHOLD_SECONDS,
    compute_snapshot,
    is_snapshot_fresh,
    read_snapshot,
    run_snapshot_cycle,
    snapshot_path,
    write_snapshot,
)


def get_snapshot_for_routing() -> Optional[Dict[str, Any]]:
    """Convenience for the reasoning-engine routing-layer short-
    circuit (separate bucket: KR-SNAPSHOT-INTO-ROUTING).

    Returns the fresh snapshot dict OR ``None`` if no fresh snapshot
    exists. Caller uses ``None`` as the signal to fall back to live
    tool-calling for the status query.

    Identical semantics to :func:`read_snapshot` today — separate
    surface preserved so the consumer-side bucket can extend it
    (e.g., add per-field nullness checks) without changing the
    underlying read API.
    """
    return read_snapshot()


__all__ = [
    "SCHEMA_VERSION",
    "SNAPSHOT_FRESH_THRESHOLD_SECONDS",
    "compute_snapshot",
    "get_snapshot_for_routing",
    "is_snapshot_fresh",
    "read_snapshot",
    "run_snapshot_cycle",
    "snapshot_path",
    "write_snapshot",
]
