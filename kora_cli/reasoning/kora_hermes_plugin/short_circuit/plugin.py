"""Short-circuit sub-plugin — ``transform_input`` hook stub +
``register()``.

KR-PLUGIN-SHORT-CIRCUIT (Deliverable C of
KR-PLUGIN-EXTRACTIONS-BATCH-2).

# Hook ownership status

Short-circuit currently runs **outside** Hermes — the slack DM
handler (``kora_cli/handlers/slack_dm_handler.py``) calls
:func:`...short_circuit.try_short_circuit` directly before
invoking the reasoning engine. The wiring change to move that
check inside a ``transform_input`` hook is intentionally
**out of scope** for this PR (no behavior change across the
batch). This file provides :func:`short_circuit_hook` as the
canonical home for that future handler, and ``register(ctx)``
is intentionally a no-op until KR-HERMES-LOCAL-EXT-REISSUE (or
a dedicated follow-on bucket) wires the move.

The boundary + canonical location are the value extraction this
PR ships.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _is_kora_call(route_value: Any) -> bool:
    """Mirror the orchestrator's gate. Lazy import to break
    plugin-discovery load-order circles."""
    try:
        from kora_cli.reasoning.kora_hermes_plugin.plugin import (
            _is_kora_call as _check,
        )

        return _check(route_value)
    except Exception:
        return False


def short_circuit_hook(
    *,
    route: str = "",
    user_message: str = "",
    **kw,
) -> Optional[dict]:
    """Standalone ``transform_input`` handler — future wiring.

    Today the slack DM handler calls
    :func:`...short_circuit.try_short_circuit` directly. A future
    Hermes hook wiring will move that call here so the handler
    sees a pre-resolved short-circuit reply via the standard
    plugin contract.

    Returns:
      ``{"reply": str}`` on a successful short-circuit match —
      caller (Hermes) skips engine resolution and returns the
      reply directly.
      ``None`` on no match / fall-through / non-Kora route.

    NOT currently registered — see module docstring.
    """
    if not _is_kora_call(route):
        return None

    # Lazy import — module-load order is `cost_ladder ← (this) ←
    # orchestrator`; importing the matcher at module top would
    # be fine but lazy keeps the discovery import light + matches
    # the pattern used by the bundled cost-ladder hook.
    from kora_cli.reasoning.kora_hermes_plugin.short_circuit.matcher import (
        load_phrasebook,
        try_short_circuit,
    )

    try:
        phrasebook = load_phrasebook()
    except Exception as exc:
        logger.warning(
            "[kora_hermes.short_circuit] load_phrasebook raised %r — "
            "falling through to engine",
            exc,
        )
        return None

    # Snapshot accessor — uses the daemon snapshot when available;
    # falls through (None) when not, which trips the matcher's
    # safe-fallback path.
    snapshot: Optional[dict] = None
    try:
        from kora_cli.snapshot.state_snapshot import latest_snapshot_dict

        snapshot = latest_snapshot_dict()
    except Exception:
        snapshot = None

    match = try_short_circuit(user_message or "", phrasebook, snapshot)
    if match is None:
        return None
    return {"reply": match.reply_text}


def register(ctx) -> None:
    """Sub-register — intentionally a no-op today.

    The slack DM handler owns the short-circuit call path in v1;
    moving it into a ``transform_input`` hook is a separate
    behavior-touching change. Registering :func:`short_circuit_hook`
    today without the handler-side rip-out would double-fire the
    match (waste, not incorrect). Kept as a no-op so the
    orchestrator's ``register_short_circuit(ctx)`` exists for
    symmetry — the boundary is in place, ready for a future split.
    """
    logger.debug(
        "[kora_hermes.short_circuit] sub-plugin registered (no-op; "
        "handler owns short-circuit call path in v1)"
    )
