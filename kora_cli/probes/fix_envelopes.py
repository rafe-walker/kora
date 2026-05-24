"""Per-probe fix-attempt envelope declarations — KR-PROBE-AUDIT-AND-CONVERT.

Declarative envelopes only. Each envelope:

  1. Names a single narrow auto-fix action (e.g., "restart 1 fly
     machine").
  2. Documents what's IN the envelope (eligible failures) and
     what's OUT (operator-required).
  3. Is gated on a per-probe enable env (default OFF per
     ``feedback-fail-closed-by-default-for-security-infra``).

v1 SCOPE — declarations + envelope-enabled gate ONLY. Actual
fix-attempt execution (calling the Fly API to restart a machine,
etc.) is deferred to a follow-on bucket
(KR-PROBE-AUTOFIX-EXECUTION) so the capability-matrix /
SECDEF / audit story can be reviewed before Kora's daemon starts
mutating cloud infrastructure.

This module ships the envelope vocabulary + the env-flag plumbing;
the wake-event consumer (also follow-on) will read these envelopes
to decide what Kora's reasoning loop is permitted to attempt.

# Envelope severity by probe

| Probe | v1 envelope | Why operator-required (rest) |
|---|---|---|
| supabase | (none) | Substrate is THE critical surface. Any automated retry/recovery touches Joshua's whole data layer. Operator decides. |
| fly | restart 1 unhealthy machine in 1 app | Deploy rollbacks + scale changes + multi-machine actions risk cascading outage. |
| vercel | (none) | Failed deploys may indicate code issues; rolling back blindly can revert intended changes. |
| sentry | (none) | Investigation-only — Sentry issues themselves are bugs in code Kora can't fix at runtime. |
| doppler | (none) | Credential surface. Auto-rotation could lock the runtime out of itself. Operator decides. |

Only ``fly`` has a v1 envelope; everything else is observation-only
operator-attention. This matches the spec's Phase 4 explicit
guidance.

# Operator opt-in pattern

```
# Default — all envelopes disabled
$ unset KORA_PROBE_AUTOFIX_FLY_ENABLED

# Per-probe opt-in (operator reviews the envelope first)
$ export KORA_PROBE_AUTOFIX_FLY_ENABLED=true
```

Per the fail-CLOSED memory, ``true`` / ``1`` / ``yes`` / ``on``
enable; everything else (including ``false`` / unset) keeps the
envelope OFF.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, FrozenSet


@dataclass(frozen=True, slots=True)
class FixEnvelope:
    """One probe's fix-attempt envelope declaration.

    Wire-stable shape — declarative; the executor (follow-on
    bucket) reads these to know what's allowed.
    """

    probe: str
    fix_name: str  # short stable id (e.g., "restart_unhealthy_machine")
    enable_env: str  # env var name; truthy value enables
    description: str  # operator-readable summary
    in_envelope: FrozenSet[str] = field(default_factory=frozenset)
    out_of_envelope: FrozenSet[str] = field(default_factory=frozenset)
    requires_capability: str = ""  # capability-matrix gate (when executor lands)


# ---------------------------------------------------------------------------
# Per-probe envelope table
# ---------------------------------------------------------------------------


ENABLE_ENV_SUPABASE = "KORA_PROBE_AUTOFIX_SUPABASE_ENABLED"
ENABLE_ENV_FLY = "KORA_PROBE_AUTOFIX_FLY_ENABLED"
ENABLE_ENV_VERCEL = "KORA_PROBE_AUTOFIX_VERCEL_ENABLED"
ENABLE_ENV_SENTRY = "KORA_PROBE_AUTOFIX_SENTRY_ENABLED"
ENABLE_ENV_DOPPLER = "KORA_PROBE_AUTOFIX_DOPPLER_ENABLED"


_FLY_ENVELOPE = FixEnvelope(
    probe="fly",
    fix_name="restart_unhealthy_machine",
    enable_env=ENABLE_ENV_FLY,
    description=(
        "Restart exactly ONE Fly machine whose state != 'started' "
        "via the Machines API. No multi-machine action, no deploy "
        "rollback, no scale change."
    ),
    in_envelope=frozenset(
        {
            "single_machine_not_started",
            # Health-endpoint-fails-on-one-machine (per spec §2 Phase 4)
            # subsumed here — the "not_started" + "stopped" flag captures
            # the operator-recoverable cases without requiring per-app
            # health-endpoint introspection.
        }
    ),
    out_of_envelope=frozenset(
        {
            "deploy_rollback",
            "scale_up",
            "scale_down",
            "multi_machine_restart",
            "machine_destroy",
            "app_create",
            "config_change",
        }
    ),
    # Capability literal reserved; the executor follow-on bucket
    # will add the cap-matrix gate. v1 declares the requirement so
    # the matrix audit can include this expected surface.
    requires_capability="probe_autofix_fly_restart",
)


# v1 — only the fly envelope is non-trivial; the others ship as
# explicit "none" declarations so a future bucket can populate them
# without touching the envelope-resolution code path.
_SUPABASE_ENVELOPE = FixEnvelope(
    probe="supabase",
    fix_name="(none)",
    enable_env=ENABLE_ENV_SUPABASE,
    description=(
        "No v1 auto-fix envelope. Substrate is critical — operator "
        "decides on any recovery action."
    ),
)

_VERCEL_ENVELOPE = FixEnvelope(
    probe="vercel",
    fix_name="(none)",
    enable_env=ENABLE_ENV_VERCEL,
    description=(
        "No v1 auto-fix envelope. Failed deploys may indicate code "
        "issues; rolling back blindly can revert intended changes. "
        "Operator decides."
    ),
)

_SENTRY_ENVELOPE = FixEnvelope(
    probe="sentry",
    fix_name="(none)",
    enable_env=ENABLE_ENV_SENTRY,
    description=(
        "No v1 auto-fix envelope. Sentry issues themselves are bugs "
        "in code Kora can't fix at runtime. Investigation-only."
    ),
)

_DOPPLER_ENVELOPE = FixEnvelope(
    probe="doppler",
    fix_name="(none)",
    enable_env=ENABLE_ENV_DOPPLER,
    description=(
        "No v1 auto-fix envelope. Credential surface — auto-rotation "
        "could lock the runtime out of itself. Operator decides."
    ),
)


ENVELOPES: Dict[str, FixEnvelope] = {
    "supabase": _SUPABASE_ENVELOPE,
    "fly": _FLY_ENVELOPE,
    "vercel": _VERCEL_ENVELOPE,
    "sentry": _SENTRY_ENVELOPE,
    "doppler": _DOPPLER_ENVELOPE,
}


# Truthy env values per the fail-CLOSED memory + the AUTO_REPLY env
# pattern in email_inbound_handler. Anything else (unset, "false",
# garbage) keeps the envelope OFF.
_TRUTHY_VALUES = frozenset({"true", "1", "yes", "on"})


def is_envelope_enabled(probe: str) -> bool:
    """Return ``True`` iff the probe's auto-fix envelope env is
    explicitly truthy AND the envelope is non-empty (fix_name !=
    "(none)").

    Fail-CLOSED default per ``feedback-fail-closed-by-default-for-
    security-infra``: operator must explicitly opt in, AND a v1
    envelope must actually exist for the probe.
    """
    envelope = ENVELOPES.get(probe)
    if envelope is None:
        return False
    if envelope.fix_name == "(none)":
        return False
    raw = os.environ.get(envelope.enable_env, "").strip().lower()
    return raw in _TRUTHY_VALUES
