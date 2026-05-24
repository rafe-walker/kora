"""Pre-warmed daemon state snapshot — KR-CHEAP-PRE-WARMED-SNAPSHOT.

Periodic task computes a snapshot of operator-readable state every
5 min. Reasoning engine + status-query MCP tools read from this
snapshot instead of tool-calling for routine status queries
("burn?", "alerts?", "what's open?"). Zero LLM cost for the lookup;
staleness ≤5 min.

Snapshot file: ``${KORA_HOME}/cache/daemon_snapshot.json`` (atomic-
write via :func:`utils.atomic_replace`).

# Read-only consumer contract

This module is a **read-only** consumer of every state holder. The
periodic task NEVER mutates ``agent/operational_state_holder``,
the cost-ladder holder, or the probe snapshot cache. Snapshot
production is purely a projection of live read accessors.

# Graceful degradation

When an accessor is missing / uninitialized / raises, the snapshot
substitutes ``"unknown"`` for the affected field rather than
failing the whole snapshot. Spec §4 explicitly calls this out for
probe-state + cost-ladder source-of-truth. Each per-source
collector is wrapped in try/except — one failure doesn't poison
the rest.

# Schema version

Bumped on any field-shape change so consumers can branch on
``schema_version`` for backwards-compat. v1 ships with:

  - operational_state.primary (PrimaryState value)
  - operational_state.paused (bool)
  - operational_state.pause_reason (str | null)
  - alerts.active_count / by_severity / by_category
  - cost_ladder.current_tier / monthly_budget_pct_used /
    model_default ("unknown" — dynamic downshift not surfaced)
  - tasks.open_count / in_progress_count ("unknown" — substrate
    MCP from cron deferred per spec §4)
  - service_health.{vercel/sentry/doppler/supabase/fly}
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from utils import atomic_replace

logger = logging.getLogger(__name__)


# v1 → v2: added cost_telemetry section (KR-CHEAP-COST-TELEMETRY)
# v2 → v3: cost_ladder.spent_to_date_usd + credit_pool_usd populated;
#          cost_ladder.model_default resolved from KR-HAIKU-ROUTER's
#          DEFAULT_HAIKU_MODEL constant (was "unknown" in v1/v2).
SCHEMA_VERSION = 3
SNAPSHOT_FRESH_THRESHOLD_SECONDS = 600  # 10 min — spec §2(a) is_snapshot_fresh

# Probe names the snapshot exposes. Matches the 5 default probes in
# ``kora_cli/heartbeat_probes/runner.default_probes()`` so the
# snapshot shape is stable even when no probes have run yet.
_KNOWN_PROBES = ("vercel", "sentry", "doppler", "supabase", "fly")

# KR-SNAPSHOT-EXPAND-COST-FIELDS — credit pool env override. Per the
# reference-anthropic-sdk-billing-split memory the default Max 20x
# SDK pool is $200/mo. Operator can override (different account
# tier / multiple pools) via ``KORA_CREDIT_POOL_USD``. Used ONLY as
# fallback when the live cost holder isn't wired — when the holder
# is up, ``holder.current.credit_pool_usd`` is the authoritative
# value (it's the figure rungs are computed against).
CREDIT_POOL_USD_ENV = "KORA_CREDIT_POOL_USD"


def _resolve_credit_pool_usd_fallback() -> float:
    """Read ``KORA_CREDIT_POOL_USD`` with fail-soft default.

    Used when the cost-state holder isn't wired (early-boot windows
    or test paths). Malformed env → log warning + return the
    canonical Max 20x default ($200).
    """
    from agent.cost_state_holder import DEFAULT_CREDIT_POOL_USD

    raw = os.environ.get(CREDIT_POOL_USD_ENV, "").strip()
    if not raw:
        return float(DEFAULT_CREDIT_POOL_USD)
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "[kora.snapshot] malformed %s=%r — falling back to default "
            "$%.2f",
            CREDIT_POOL_USD_ENV,
            raw,
            DEFAULT_CREDIT_POOL_USD,
        )
        return float(DEFAULT_CREDIT_POOL_USD)
    if value <= 0:
        logger.warning(
            "[kora.snapshot] %s=%s must be > 0 — falling back to "
            "default $%.2f",
            CREDIT_POOL_USD_ENV,
            value,
            DEFAULT_CREDIT_POOL_USD,
        )
        return float(DEFAULT_CREDIT_POOL_USD)
    return value


def _resolve_default_model() -> str:
    """Resolve the router-side default model. Returns
    :data:`kora_cli.router.cost_router.DEFAULT_HAIKU_MODEL` if the
    router is importable (post-#165 default), else ``"unknown"``.

    Reading from the router's MODULE CONSTANT (not a holder field /
    accessor) because the router never moves the default at runtime —
    the default IS Haiku per KR-HAIKU-ROUTER's earned-Opus-escalation
    design. Per-call escalators (cost_clamp, force_opus_env,
    iteration_2+, /opus prefix, decision_language) are call-time
    signals, not snapshot state.
    """
    try:
        from kora_cli.router.cost_router import DEFAULT_HAIKU_MODEL

        return str(DEFAULT_HAIKU_MODEL)
    except Exception as exc:
        logger.debug(
            "[kora.snapshot] router DEFAULT_HAIKU_MODEL import failed: "
            "%r — degrading model_default to 'unknown'",
            exc,
        )
        return "unknown"


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


SNAPSHOT_PATH_ENV = "KORA_SNAPSHOT_PATH"
_SNAPSHOT_RELATIVE_PATH = Path("cache") / "daemon_snapshot.json"


def snapshot_path() -> Path:
    """Resolve the snapshot file path. Env override
    (``KORA_SNAPSHOT_PATH``) first, else ``${KORA_HOME}/cache/daemon_snapshot.json``.

    Re-resolves on every call so monkeypatch / per-test isolation
    works without ContextVar plumbing (matches the audit JSONL +
    Slack DM log path-resolution pattern shipped in #137/#141).
    """
    override = os.environ.get(SNAPSHOT_PATH_ENV, "").strip()
    if override:
        return Path(override)
    from kora_constants import get_kora_home

    return get_kora_home() / _SNAPSHOT_RELATIVE_PATH


# ---------------------------------------------------------------------------
# Per-source collectors — each fail-soft + returns the section payload
# ---------------------------------------------------------------------------


def _collect_operational_state() -> Dict[str, Any]:
    """Read operational_state_holder. Returns the operational_state
    section of the snapshot. Degrades to "unknown" / null on any
    failure (holder unwired in early-boot windows or test paths)."""
    try:
        from agent.operational_state import PrimaryState
        from agent.operational_state_holder import get_holder
    except Exception as exc:
        logger.debug(
            "[kora.snapshot] operational_state import failed: %r — "
            "degrading to unknown",
            exc,
        )
        return {"primary": "unknown", "paused": False, "pause_reason": None}

    try:
        holder = get_holder()
    except Exception as exc:
        logger.warning(
            "[kora.snapshot] get_holder raised %r — degrading", exc
        )
        return {"primary": "unknown", "paused": False, "pause_reason": None}
    if holder is None:
        return {"primary": "unknown", "paused": False, "pause_reason": None}

    try:
        state = holder.current  # @property — value snapshot
        primary = state.primary_state
        primary_value = (
            primary.value if isinstance(primary, PrimaryState) else "unknown"
        )
        paused = primary is PrimaryState.PAUSED
        # degradation_reasons is a FrozenSet of enum members on the
        # OperationalState dataclass per #112 K-DG locked block.
        reasons = getattr(state, "degradation_reasons", frozenset())
        pause_reason = None
        if paused and reasons:
            # Stable string projection — first reason name.
            try:
                pause_reason = sorted(r.value for r in reasons)[0]
            except Exception:
                pause_reason = None
        return {
            "primary": primary_value,
            "paused": paused,
            "pause_reason": pause_reason,
        }
    except Exception as exc:
        logger.warning(
            "[kora.snapshot] operational_state read raised %r — "
            "degrading",
            exc,
        )
        return {"primary": "unknown", "paused": False, "pause_reason": None}


def _collect_alerts() -> Dict[str, Any]:
    """Read the alerts aggregator. Returns the alerts section."""
    try:
        from kora_cli.alerts.aggregator import compute_active_alerts
    except Exception as exc:
        logger.debug(
            "[kora.snapshot] alerts import failed: %r — degrading", exc
        )
        return {
            "active_count": 0,
            "by_severity": {"critical": 0, "warning": 0, "info": 0},
            "by_category": {},
        }

    try:
        alerts = list(compute_active_alerts())
    except Exception as exc:
        logger.warning(
            "[kora.snapshot] compute_active_alerts raised %r — degrading",
            exc,
        )
        return {
            "active_count": 0,
            "by_severity": {"critical": 0, "warning": 0, "info": 0},
            "by_category": {},
        }

    by_severity: Dict[str, int] = {"critical": 0, "warning": 0, "info": 0}
    by_category: Dict[str, int] = {}
    for alert in alerts:
        by_severity[alert.severity] = by_severity.get(alert.severity, 0) + 1
        by_category[alert.category] = by_category.get(alert.category, 0) + 1

    return {
        "active_count": len(alerts),
        "by_severity": by_severity,
        "by_category": by_category,
    }


def _collect_cost_ladder() -> Dict[str, Any]:
    """Read the cost-ladder holder. Returns the cost_ladder section.

    Schema v3 changes (KR-SNAPSHOT-EXPAND-COST-FIELDS):
      * ``spent_to_date_usd`` — from ``holder.current.spent_to_date_usd``
        when holder wired; ``"unknown"`` otherwise.
      * ``credit_pool_usd`` — from ``holder.current.credit_pool_usd``
        when holder wired; ``KORA_CREDIT_POOL_USD`` env fallback when
        holder unavailable; ``DEFAULT_CREDIT_POOL_USD`` ($200) when
        both unset / malformed.
      * ``model_default`` — populated from KR-HAIKU-ROUTER's
        ``DEFAULT_HAIKU_MODEL`` constant (post-#165). Per-call
        escalators (cost_clamp / force_opus_env / iteration_2+ /
        /opus prefix / decision_language) are call-time signals,
        NOT snapshot state — the snapshot surfaces the router's
        stable default-path model.
    """
    # Default model is resolvable independent of the holder — the
    # router constant lives in ``kora_cli/router/cost_router.py``.
    model_default = _resolve_default_model()

    try:
        from agent.cost_state_holder import get_cost_holder
    except Exception as exc:
        logger.debug(
            "[kora.snapshot] cost_state_holder import failed: %r — "
            "degrading",
            exc,
        )
        return {
            "current_tier": "unknown",
            "monthly_budget_pct_used": None,
            "model_default": model_default,
            "spent_to_date_usd": "unknown",
            "credit_pool_usd": _resolve_credit_pool_usd_fallback(),
        }

    try:
        holder = get_cost_holder()
    except Exception as exc:
        logger.warning(
            "[kora.snapshot] get_cost_holder raised %r — degrading", exc
        )
        return {
            "current_tier": "unknown",
            "monthly_budget_pct_used": None,
            "model_default": model_default,
            "spent_to_date_usd": "unknown",
            "credit_pool_usd": _resolve_credit_pool_usd_fallback(),
        }
    if holder is None:
        return {
            "current_tier": "unknown",
            "monthly_budget_pct_used": None,
            "model_default": model_default,
            "spent_to_date_usd": "unknown",
            "credit_pool_usd": _resolve_credit_pool_usd_fallback(),
        }

    current_tier = "unknown"
    pct_used: Optional[float] = None
    try:
        rung = holder.active_rung()  # method, not @property (PR #126 catch)
        current_tier = getattr(rung, "name", str(rung))
    except Exception as exc:
        logger.debug(
            "[kora.snapshot] cost active_rung raised %r — tier degraded",
            exc,
        )
    try:
        pct_used = round(holder.current_pct_used() * 100, 2)
    except Exception as exc:
        logger.debug(
            "[kora.snapshot] current_pct_used raised %r — pct degraded",
            exc,
        )
        pct_used = None

    # KR-SNAPSHOT-EXPAND-COST-FIELDS v3 — surface holder state for
    # spend + pool. ``holder.current`` is the @property snapshot of
    # the CostState dataclass (PR #112 K-DG catch + #126 active_rung
    # method confirmation). Both fields are float on the dataclass;
    # defensive try/except guards against future shape drift.
    spent_to_date_usd: Any = "unknown"
    credit_pool_usd: Any = _resolve_credit_pool_usd_fallback()
    try:
        state = holder.current
        spent_raw = getattr(state, "spent_to_date_usd", None)
        if isinstance(spent_raw, (int, float)):
            spent_to_date_usd = round(float(spent_raw), 6)
        pool_raw = getattr(state, "credit_pool_usd", None)
        if isinstance(pool_raw, (int, float)) and pool_raw > 0:
            # Holder-configured pool wins over env fallback when
            # the holder is wired — it's the figure the rungs are
            # actually computed against, so the snapshot must agree.
            credit_pool_usd = round(float(pool_raw), 2)
    except Exception as exc:
        logger.debug(
            "[kora.snapshot] holder.current read raised %r — spend/pool "
            "fields degraded",
            exc,
        )

    return {
        "current_tier": current_tier,
        "monthly_budget_pct_used": pct_used,
        "model_default": model_default,
        "spent_to_date_usd": spent_to_date_usd,
        "credit_pool_usd": credit_pool_usd,
    }


def _collect_service_health() -> Dict[str, Any]:
    """Read the heartbeat-probe snapshot cache. Each probe degrades
    to ``"unknown"`` independently when no observation exists yet
    (cache warming / probe import failure).

    The snapshot shape is STABLE across all 5 expected probe names
    even when the cache is empty — consumers can rely on the dict
    having ``vercel`` / ``sentry`` / ``doppler`` / ``supabase`` /
    ``fly`` keys regardless of probe-side state.
    """
    out: Dict[str, str] = {name: "unknown" for name in _KNOWN_PROBES}
    try:
        from kora_cli.heartbeat_probes.runner import (
            current_service_snapshots,
        )
    except Exception as exc:
        logger.debug(
            "[kora.snapshot] heartbeat_probes import failed: %r — all "
            "probes degraded",
            exc,
        )
        return out

    try:
        snapshots = current_service_snapshots() or {}
    except Exception as exc:
        logger.warning(
            "[kora.snapshot] current_service_snapshots raised %r — "
            "all probes degraded",
            exc,
        )
        return out

    for name, snap in snapshots.items():
        try:
            status = snap.status
        except Exception:
            continue
        # Project the probe status enum (healthy/degraded/unhealthy/
        # unknown) into the snapshot's plain-string slot.
        if isinstance(status, str):
            out[name] = status
        else:
            out[name] = getattr(status, "value", str(status))
    return out


def _collect_tasks() -> Dict[str, Any]:
    """Sea_Tickets open/in-progress counts.

    Deferred in v1 per spec §4: substrate MCP call from a 5-min cron
    is potentially expensive + no in-process accessor exists yet.
    Snapshot keeps the field shape with ``"unknown"`` placeholders so
    consumers can branch on presence without crashing; a follow-on
    bucket can wire a cached substrate-read accessor once one exists.
    """
    return {"open_count": "unknown", "in_progress_count": "unknown"}


def _collect_cost_telemetry() -> Dict[str, Any]:
    """Per-route cost counters projection — KR-CHEAP-COST-TELEMETRY.

    Schema v2 addition. Exposes the two operator-facing windows
    (``rolling_24h`` + ``monthly``); the ``process_lifetime``
    window is intentionally excluded from the snapshot to keep the
    on-disk file size bounded (operator can hit ``/api/cost_telemetry``
    directly for the full window set).

    Fail-soft: missing telemetry singleton (e.g., the cost_telemetry
    listener hasn't booted yet) degrades to empty per-window dicts
    so the snapshot shape is stable.
    """
    try:
        from kora_cli.telemetry import (
            WINDOW_MONTHLY,
            WINDOW_ROLLING_24H,
            get_telemetry,
        )
    except Exception as exc:
        logger.debug(
            "[kora.snapshot] cost_telemetry import failed: %r — "
            "degrading section",
            exc,
        )
        return {"rolling_24h": {}, "monthly": {}}
    try:
        all_windows = get_telemetry().snapshot()
    except Exception as exc:
        logger.warning(
            "[kora.snapshot] telemetry.snapshot() raised %r — "
            "degrading section",
            exc,
        )
        return {"rolling_24h": {}, "monthly": {}}
    return {
        "rolling_24h": all_windows.get(WINDOW_ROLLING_24H, {}),
        "monthly": all_windows.get(WINDOW_MONTHLY, {}),
    }


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def compute_snapshot() -> Dict[str, Any]:
    """Assemble the snapshot dict from all per-source collectors.

    Per-source failures degrade in-place (per the collector
    contracts); this top-level function never raises. Caller can
    treat the returned dict as a safe-to-serialize wire payload.

    Schema v2 (KR-CHEAP-COST-TELEMETRY) adds ``cost_telemetry``
    alongside the v1 sections.
    """
    from kora_time import now

    computed_at = now().strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "schema_version": SCHEMA_VERSION,
        "computed_at": computed_at,
        "operational_state": _collect_operational_state(),
        "alerts": _collect_alerts(),
        "cost_ladder": _collect_cost_ladder(),
        "tasks": _collect_tasks(),
        "service_health": _collect_service_health(),
        "cost_telemetry": _collect_cost_telemetry(),
    }


def write_snapshot(snapshot: Dict[str, Any]) -> None:
    """Atomic-write the snapshot to disk.

    Uses :func:`utils.atomic_replace` (the same pattern
    ``cron/jobs.py`` uses for ``jobs.json``) so a partial-write
    never leaves a malformed snapshot on disk. Parent directory is
    created if missing.

    Fail-soft: write errors log + raise. Caller (the periodic task)
    catches + logs so subsequent ticks can retry.
    """
    target = snapshot_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    # Write to a sibling tmp file then atomic-rename. The
    # ``atomic_replace`` helper handles the rename + cleanup; see
    # ``utils.py:61``.
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=target.parent,
        prefix="daemon_snapshot.",
        suffix=".tmp",
    ) as fp:
        json.dump(snapshot, fp, indent=2, sort_keys=True)
        fp.write("\n")
        tmp_path = fp.name

    atomic_replace(tmp_path, target)


def read_snapshot() -> Optional[Dict[str, Any]]:
    """Read the snapshot file, returning the parsed dict.

    Returns ``None`` when:
      - File doesn't exist (no snapshot yet)
      - File is older than ``SNAPSHOT_FRESH_THRESHOLD_SECONDS``
        (stale; consumers treat as missing)
      - File can't be parsed as JSON (corruption)

    Caller never sees a partial / corrupted snapshot — staleness
    failure mode is identical to no-snapshot.
    """
    path = snapshot_path()
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "[kora.snapshot] read failed for %s: %r", path, exc
        )
        return None
    try:
        snapshot = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            "[kora.snapshot] malformed snapshot at %s: %r", path, exc
        )
        return None
    if not isinstance(snapshot, dict):
        logger.warning(
            "[kora.snapshot] snapshot at %s is not a JSON object", path
        )
        return None
    if not is_snapshot_fresh(snapshot):
        return None
    return snapshot


def is_snapshot_fresh(snapshot: Dict[str, Any]) -> bool:
    """Return True iff ``snapshot['computed_at']`` is within the
    freshness window (``SNAPSHOT_FRESH_THRESHOLD_SECONDS``).

    A malformed / missing ``computed_at`` is treated as stale.
    """
    from datetime import datetime, timezone

    raw = snapshot.get("computed_at")
    if not isinstance(raw, str) or not raw:
        return False
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - ts).total_seconds()
    return 0 <= elapsed <= SNAPSHOT_FRESH_THRESHOLD_SECONDS


# ---------------------------------------------------------------------------
# Periodic-task entry point
# ---------------------------------------------------------------------------


async def run_snapshot_cycle() -> None:
    """One scheduler-tick of the snapshot job.

    Compute → write. Failure paths are caught + logged so the
    heartbeat scheduler keeps ticking. Mirrors the fail-soft
    contract of the email IMAP poll listener
    (:func:`kora_cli.listeners.email_inbound_imap_listener.run_poll_cycle`)
    + the alerts notifier
    (:func:`kora_cli.listeners.alert_notifier_listener.run_notification_cycle`).
    """
    try:
        snapshot = compute_snapshot()
    except Exception as exc:
        logger.warning(
            "[kora.snapshot] compute_snapshot raised %r — skipping cycle",
            exc,
        )
        return
    try:
        write_snapshot(snapshot)
    except Exception as exc:
        logger.warning(
            "[kora.snapshot] write_snapshot raised %r — snapshot not "
            "persisted this cycle",
            exc,
        )
        return
    logger.debug(
        "[kora.snapshot] wrote snapshot @ %s (alerts=%d, tier=%s)",
        snapshot.get("computed_at"),
        snapshot.get("alerts", {}).get("active_count", 0),
        snapshot.get("cost_ladder", {}).get("current_tier", "unknown"),
    )
