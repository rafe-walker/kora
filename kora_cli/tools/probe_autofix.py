"""Probe autofix executor — KR-PROBE-AUTOFIX-EXECUTION.

Vision-completion bucket per ``feedback-kora-is-unified-operator-
interface``: "Kora investigates + attempts fix where safe + DMs
you with what happened, what was tried, what's left for you to
decide." PR #166 (KR-PROBE-WAKE-CONSUMER) shipped investigate +
DM; this module ships the attempt-fix layer.

# Architecture

```
Probe wake → reasoning starts with probe context (PR #166)
       ↓
[Reasoning sees envelope is enabled (e.g., fly restart_unhealthy_machine)]
       ↓
[Reasoning invokes kora__attempt_probe_autofix(
    probe="fly", action="restart_machine",
    target_id="<machine_id>", reason="..."
)]
       ↓
[This module: re-checks envelope enabled + action in whitelist +
 target_id matches a real unhealthy machine; executes via Fly
 Machines API; emits audit; returns before/after state]
       ↓
[Reasoning includes outcome in DM to operator]
```

# Defense in depth

The reasoning loop sees the envelope status from probe context
(PR #166), but this module RE-CHECKS at execution time. Stale
context, env-flipped-mid-investigation, or a Claude that ignored
the envelope status all fail at the executor boundary. The
envelope gates are the only canonical truth.

Validation pipeline per invocation:

  1. ``probe`` is one of the 5 known names (matches the audit
     SeamName universe).
  2. ``is_envelope_enabled(probe)`` — re-reads the env (one of
     ``KORA_PROBE_AUTOFIX_<PROBE>_ENABLED``); rejects with
     ``envelope_disabled`` on miss.
  3. ``action`` matches the envelope's declared ``fix_name``
     exactly. Free-form actions (even similar-looking ones) are
     rejected as ``action_not_in_envelope``. v1: only
     ``fly + restart_machine`` exists.
  4. ``target_id`` is non-empty + matches a sanity pattern
     (alphanumeric + dashes; max length 64). Rejects malformed
     ids without ever calling Fly's API.
  5. Per-probe executor's own pre-checks (e.g., for fly:
     target_id must resolve to a real machine on the
     configured app(s) AND must not already be ``"started"``).

# v1 scope

Only ``fly + restart_machine`` is an actual envelope.
``supabase`` / ``vercel`` / ``sentry`` / ``doppler`` envelopes
exist in :mod:`kora_cli.probes.fix_envelopes` as ``(none)``
declarations. This module rejects requests for those probes with
``action_not_in_envelope`` regardless of what action string was
passed (the envelope has no actions to whitelist against).

Future envelopes register by extending the per-probe executor
table at the bottom of this module + the envelope declaration in
``fix_envelopes.py``.

# Fail-soft

Every failure path returns a structured dict; the function never
raises. The reasoning engine sees ``status`` + branches:

  * ``attempted`` — fix tried; reasoning includes outcome in DM
  * ``rejected`` — envelope / validation gate fired; reasoning
    explains to operator + suggests manual action
  * ``execution_failed`` — fix tried but API call broke;
    reasoning includes error code in DM
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from kora_cli.audit.jsonl_sink import emit_audit
from kora_cli.probes.fix_envelopes import ENVELOPES, is_envelope_enabled

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Probe + status enums (wire-stable)
# ---------------------------------------------------------------------------


KNOWN_PROBES = ("supabase", "fly", "vercel", "sentry", "doppler")

STATUS_ATTEMPTED = "attempted"
STATUS_REJECTED = "rejected"
STATUS_EXECUTION_FAILED = "execution_failed"

REASON_UNKNOWN_PROBE = "unknown_probe"
REASON_ENVELOPE_DISABLED = "envelope_disabled"
REASON_ACTION_NOT_IN_ENVELOPE = "action_not_in_envelope"
REASON_TARGET_ID_INVALID = "target_id_invalid"
REASON_TARGET_NOT_FOUND = "target_not_found"
REASON_TARGET_ALREADY_HEALTHY = "target_already_healthy"
REASON_FLY_API_TOKEN_UNSET = "fly_api_token_unset"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


# Conservative target_id pattern. Fly machine ids are 14-char
# base32-like strings; we accept the broader alphanumeric-plus-
# dash shape (length 1..64) for forward-compat with other probe
# target shapes.
_TARGET_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def _validate_target_id(target_id: str) -> bool:
    return bool(target_id) and bool(_TARGET_ID_RE.match(target_id))


# ---------------------------------------------------------------------------
# Fly client (httpx-based, mirrors heartbeat_probes/fly.py)
# ---------------------------------------------------------------------------


FLY_API_TOKEN_ENV = "KORA_FLY_API_TOKEN"
FLY_STAGING_APP_NAME_ENV = "KORA_FLY_STAGING_APP_NAME"
FLY_API_BASE = "https://api.machines.dev"
FLY_DEFAULT_PROD_APP = "kora-runtime"
FLY_API_TIMEOUT_SECONDS = 10.0


async def _fly_list_machines(
    *, client: httpx.AsyncClient, token: str, app_name: str
) -> Optional[List[Dict[str, Any]]]:
    """GET /v1/apps/{app}/machines → list of machine dicts.

    Returns ``None`` on any non-200 (caller treats as "this app
    not reachable; try next") to keep the multi-app search
    fail-soft.
    """
    try:
        response = await client.get(
            f"{FLY_API_BASE}/v1/apps/{app_name}/machines",
            headers={"Authorization": f"Bearer {token}"},
        )
    except Exception as exc:
        logger.warning(
            "[kora.tool.probe_autofix] fly list_machines app=%s "
            "raised %r",
            app_name,
            exc,
        )
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, list):
        return None
    return payload


def _project_machine(machine: Dict[str, Any]) -> Dict[str, Any]:
    """Tighten the Fly machine dict to the fields we care about
    for before/after recording. Drops anything the API may return
    that we don't want in audit (region-internal hints,
    timestamps that differ across reads but aren't meaningful).
    """
    return {
        "id": machine.get("id"),
        "name": machine.get("name"),
        "state": machine.get("state"),
        "region": machine.get("region"),
        "instance_id": machine.get("instance_id"),
    }


async def _execute_fly_restart_machine(
    *,
    target_id: str,
    http_client_factory: Optional[Any] = None,
) -> Dict[str, Any]:
    """Restart a single Fly machine by id.

    Pre-checks:
      * ``KORA_FLY_API_TOKEN`` set
      * target_id resolves to exactly one machine across
        configured Fly apps (prod + optional staging)
      * resolved machine.state != "started" (operator-only path
        — Kora doesn't restart healthy machines)

    On success returns:
      ``{"status": "attempted", "action_taken": "restart_machine",
         "before_state": {...}, "after_state": {...},
         "fly_app": "...", "executor_duration_ms": N}``

    On failure returns either ``rejected`` (pre-check failure) or
    ``execution_failed`` (API call broke after pre-checks
    passed).
    """
    started_ms = time.monotonic()

    token = os.environ.get(FLY_API_TOKEN_ENV, "").strip()
    if not token:
        return {
            "status": STATUS_REJECTED,
            "rejection_reason": REASON_FLY_API_TOKEN_UNSET,
            "executor_duration_ms": int(
                (time.monotonic() - started_ms) * 1000
            ),
        }

    apps: List[str] = [FLY_DEFAULT_PROD_APP]
    staging = os.environ.get(FLY_STAGING_APP_NAME_ENV, "").strip()
    if staging:
        apps.append(staging)

    factory = http_client_factory or (
        lambda: httpx.AsyncClient(timeout=FLY_API_TIMEOUT_SECONDS)
    )

    found_machine: Optional[Dict[str, Any]] = None
    found_app: Optional[str] = None
    async with factory() as client:
        for app_name in apps:
            machines = await _fly_list_machines(
                client=client, token=token, app_name=app_name
            )
            if not machines:
                continue
            for m in machines:
                if not isinstance(m, dict):
                    continue
                if m.get("id") == target_id:
                    found_machine = m
                    found_app = app_name
                    break
            if found_machine is not None:
                break

        if found_machine is None or found_app is None:
            return {
                "status": STATUS_REJECTED,
                "rejection_reason": REASON_TARGET_NOT_FOUND,
                "rejection_detail": {
                    "target_id": target_id,
                    "searched_apps": apps,
                },
                "executor_duration_ms": int(
                    (time.monotonic() - started_ms) * 1000
                ),
            }

        before_state = _project_machine(found_machine)
        if before_state.get("state") == "started":
            return {
                "status": STATUS_REJECTED,
                "rejection_reason": REASON_TARGET_ALREADY_HEALTHY,
                "rejection_detail": {
                    "target_id": target_id,
                    "state": before_state.get("state"),
                },
                "fly_app": found_app,
                "before_state": before_state,
                "executor_duration_ms": int(
                    (time.monotonic() - started_ms) * 1000
                ),
            }

        # POST restart. Body is empty; Fly's API takes optional
        # timeout / signal params we don't override.
        try:
            restart_resp = await client.post(
                f"{FLY_API_BASE}/v1/apps/{found_app}/machines/"
                f"{target_id}/restart",
                headers={"Authorization": f"Bearer {token}"},
            )
        except Exception as exc:
            return {
                "status": STATUS_EXECUTION_FAILED,
                "error": f"{type(exc).__name__}",
                "fly_app": found_app,
                "before_state": before_state,
                "executor_duration_ms": int(
                    (time.monotonic() - started_ms) * 1000
                ),
            }
        if restart_resp.status_code >= 400:
            return {
                "status": STATUS_EXECUTION_FAILED,
                "error": f"http_{restart_resp.status_code}",
                "fly_app": found_app,
                "before_state": before_state,
                "executor_duration_ms": int(
                    (time.monotonic() - started_ms) * 1000
                ),
            }

        # Re-fetch the machine to capture after_state. One round-
        # trip is enough — Fly's restart endpoint is sync enough
        # that the state usually flips before the return; if it
        # hasn't, after_state captures the in-progress state
        # (e.g. "replacing") which is still operator-useful.
        after_machines = await _fly_list_machines(
            client=client, token=token, app_name=found_app
        )
        after_state: Optional[Dict[str, Any]] = None
        if after_machines:
            for m in after_machines:
                if isinstance(m, dict) and m.get("id") == target_id:
                    after_state = _project_machine(m)
                    break

    return {
        "status": STATUS_ATTEMPTED,
        "action_taken": "restart_machine",
        "fly_app": found_app,
        "before_state": before_state,
        "after_state": after_state,
        "executor_duration_ms": int(
            (time.monotonic() - started_ms) * 1000
        ),
    }


# ---------------------------------------------------------------------------
# Per-probe executor registry
# ---------------------------------------------------------------------------


# Each entry: probe → {action_name → async executor coroutine}.
# Adding a new probe envelope means adding a new entry here +
# extending the envelope declaration in fix_envelopes.py + adding
# the per-action env to docs.
_PROBE_EXECUTORS: Dict[str, Dict[str, Any]] = {
    "fly": {
        # fix_envelopes.py declares fix_name=
        # "restart_unhealthy_machine"; the reasoning-facing action
        # name is the shorter, more intuitive ``restart_machine``.
        # We accept both spellings as a usability concession (a
        # smart reasoning model may reach for the shorter one) but
        # the canonical envelope name remains restart_unhealthy_machine.
        "restart_machine": _execute_fly_restart_machine,
        "restart_unhealthy_machine": _execute_fly_restart_machine,
    },
    # supabase / vercel / sentry / doppler intentionally absent —
    # their envelopes are "(none)". Adding a new probe with an
    # envelope means populating this dict.
}


# ---------------------------------------------------------------------------
# Audit emission
# ---------------------------------------------------------------------------


def _safe_audit(
    *, details: Dict[str, Any], caller_session_id: Optional[str]
) -> None:
    """Best-effort audit emit. Mirrors the other tools' pattern."""
    try:
        emit_audit(
            "tool.probe_autofix_attempted",
            details,
            caller_session_id=caller_session_id,
            source="reasoning",
        )
    except Exception as exc:
        logger.warning(
            "[kora.tool.probe_autofix.audit_failed] %r — tool continues",
            exc,
        )


def _reject(
    *,
    reason: str,
    detail: Optional[Dict[str, Any]] = None,
    audit_details: Dict[str, Any],
    caller_session_id: Optional[str],
) -> Dict[str, Any]:
    audit_payload = dict(audit_details)
    audit_payload["status"] = STATUS_REJECTED
    audit_payload["rejection_reason"] = reason
    if detail:
        audit_payload["rejection_detail"] = detail
    _safe_audit(
        details=audit_payload, caller_session_id=caller_session_id
    )
    out: Dict[str, Any] = {
        "status": STATUS_REJECTED,
        "rejection_reason": reason,
    }
    if detail:
        out["rejection_detail"] = detail
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _envelope_action_matches(
    probe: str, action: str
) -> Tuple[bool, Optional[str]]:
    """True if (probe, action) is in a registered envelope.

    Returns ``(matches, canonical_action_name)`` — canonical name
    is what the audit + the envelope declaration use (e.g. the
    ``restart_machine`` alias resolves to
    ``restart_unhealthy_machine``).
    """
    envelope = ENVELOPES.get(probe)
    if envelope is None or envelope.fix_name == "(none)":
        return (False, None)
    executors = _PROBE_EXECUTORS.get(probe) or {}
    if action not in executors:
        return (False, None)
    # Canonical name from the envelope itself (not from the
    # caller-provided alias).
    return (True, envelope.fix_name)


async def attempt_probe_autofix(
    *,
    probe: str,
    action: str,
    target_id: str,
    reason: str,
    caller_session_id: Optional[str] = None,
    http_client_factory: Optional[Any] = None,
) -> Dict[str, Any]:
    """Top-level entry. Always returns a structured dict; never
    raises.

    Args:
      probe: one of :data:`KNOWN_PROBES`.
      action: action name from the envelope (e.g.,
        ``"restart_machine"``).
      target_id: per-probe target identifier (Fly machine id for
        the fly envelope).
      reason: operator-facing rationale recorded verbatim in the
        audit. Required (the audit panel + future cockpit need
        this for "what did Kora decide and why").
      caller_session_id: correlation key threaded into audit
        (engine passes its own per-respond id).
      http_client_factory: override for tests; production passes
        ``None`` and the executor uses ``httpx.AsyncClient``.

    Returns one of:
      ``{"status": "attempted", "action_taken": "...",
         "fly_app": "...", "before_state": {...},
         "after_state": {...} | None, "executor_duration_ms": N}``
      ``{"status": "rejected", "rejection_reason": "...",
         "rejection_detail": {...} | None}``
      ``{"status": "execution_failed", "error": "...",
         "before_state": {...} | None, ...}``
    """
    audit_details: Dict[str, Any] = {
        "probe": probe,
        "action": action,
        "target_id": target_id,
        "reason_from_reasoning": reason,
    }

    # 1. Probe in known universe.
    if probe not in KNOWN_PROBES:
        return _reject(
            reason=REASON_UNKNOWN_PROBE,
            detail={"received": probe, "expected": list(KNOWN_PROBES)},
            audit_details=audit_details,
            caller_session_id=caller_session_id,
        )

    # 2. Envelope env gate (canonical truth — re-checks the env
    # every call so a mid-investigation env-flip closes the gate).
    if not is_envelope_enabled(probe):
        envelope = ENVELOPES.get(probe)
        return _reject(
            reason=REASON_ENVELOPE_DISABLED,
            detail={
                "enable_env": envelope.enable_env if envelope else None,
                "fix_name": envelope.fix_name if envelope else None,
            },
            audit_details=audit_details,
            caller_session_id=caller_session_id,
        )

    # 3. Action is in the envelope's whitelist.
    matches, canonical_action = _envelope_action_matches(probe, action)
    if not matches:
        envelope = ENVELOPES.get(probe)
        return _reject(
            reason=REASON_ACTION_NOT_IN_ENVELOPE,
            detail={
                "received_action": action,
                "envelope_fix_name": envelope.fix_name if envelope else None,
            },
            audit_details=audit_details,
            caller_session_id=caller_session_id,
        )

    # 4. target_id sanity (cheap; before any API call).
    if not _validate_target_id(target_id):
        return _reject(
            reason=REASON_TARGET_ID_INVALID,
            detail={"received": target_id},
            audit_details=audit_details,
            caller_session_id=caller_session_id,
        )

    # 5. Dispatch to per-probe executor. The executor returns its
    # own structured dict; we splice executor fields into the
    # audit payload + return verbatim.
    executor = _PROBE_EXECUTORS[probe][action]
    try:
        result = await executor(
            target_id=target_id,
            http_client_factory=http_client_factory,
        )
    except Exception as exc:
        logger.exception(
            "[kora.tool.probe_autofix] executor raised %r probe=%s "
            "action=%s target_id=%s",
            exc,
            probe,
            action,
            target_id,
        )
        failure_payload = {
            **audit_details,
            "status": STATUS_EXECUTION_FAILED,
            "error": f"{type(exc).__name__}",
        }
        _safe_audit(
            details=failure_payload, caller_session_id=caller_session_id
        )
        return {
            "status": STATUS_EXECUTION_FAILED,
            "error": f"{type(exc).__name__}",
        }

    # Merge audit-level fields with executor-returned fields. The
    # rejection branch's audit was already emitted by the
    # executor's internal reject path? No — the executor doesn't
    # call _safe_audit itself; it returns the structured dict and
    # this top-level handles the single audit emission.
    audit_payload = {**audit_details, **result}
    # Use the executor's canonical action name when available so
    # the audit reflects the envelope's declared fix_name, not
    # the (possibly aliased) action the reasoning loop passed.
    if canonical_action and result.get("status") != STATUS_REJECTED:
        audit_payload["action_canonical"] = canonical_action
    _safe_audit(
        details=audit_payload, caller_session_id=caller_session_id
    )
    return result
