"""Per-probe issue-criteria detection — KR-PROBE-AUDIT-AND-CONVERT.

Pure functions that classify a :class:`ServiceHealthSnapshot`
observation into an :class:`Issue` when the per-probe criterion
fires. Routine probing remains in
``kora_cli/heartbeat_probes/`` and stays $0 LLM cost; this module
is a READ-only classifier layered on top.

# Per-probe criteria (spec §2 Phase 3)

| Probe | Criterion → severity |
|---|---|
| supabase | status=unhealthy → ``critical`` (connection failure / probe error). status=degraded → ``warning`` (high connections pct, when surfaced). |
| fly | status=unhealthy → ``critical`` (no apps reachable / no machines started). status=degraded → ``warning`` (some machines unhealthy / staging app failing). |
| vercel | status=unhealthy → ``critical`` (API error). status=degraded → ``warning`` (error_rate_24h > 10%). |
| sentry | status=unhealthy → ``warning`` (API unreachable — itself a low-priority issue). status=degraded → ``warning`` (>10 unresolved issues). |
| doppler | status=unhealthy → ``critical`` (credential surface unreachable). status=degraded → ``warning`` (oldest_secret_age_days > 180). |

``unknown`` status (cache warming / auth env unset) → no Issue.
That's an operator-config state, not a probe-detected issue.

``healthy`` → no Issue.

# Consecutive-failure debouncing

Per spec example ("supabase: connection failure ≥3 consecutive
probes"), the issue-detector's caller may pass an observation
history. This module's ``detect_issues`` accepts the LATEST
observation only (single-cycle decision); the caller (the
periodic post-hook task) is responsible for maintaining a debounce
buffer. v1 ships the single-cycle classifier; debounce buffering
is a follow-on for tuning false-positive rates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, List, Literal, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Issue value type
# ---------------------------------------------------------------------------


IssueSeverity = Literal["critical", "warning", "info"]


@dataclass(frozen=True, slots=True)
class Issue:
    """One probe-derived operator-attention issue.

    Wire-shape mirror of the ``Alert`` model in
    ``kora_cli/alerts/aggregator.py`` so a consumer that already
    handles Alerts can render Issues with no shape adapter.

    ``probe`` names which probe surfaced it; ``category`` aligns
    to the alerts vocabulary (``service_unhealthy`` from the
    existing alerts taxonomy).
    """

    id: str
    probe: str
    severity: IssueSeverity
    category: str
    title: str
    detail: str
    details: dict  # snapshot.details verbatim, for the consumer to triage


# ---------------------------------------------------------------------------
# Per-probe classifier table
# ---------------------------------------------------------------------------


def detect_issue_for_snapshot(snapshot: object) -> Optional[Issue]:
    """Classify ONE :class:`ServiceHealthSnapshot` into an Issue (or
    ``None`` when no criterion fires).

    Snapshot is duck-typed via ``getattr`` so callers don't have to
    import the Pydantic class — this module stays decoupled from
    the heartbeat_probes package's import graph.
    """
    name = getattr(snapshot, "name", "") or ""
    status = getattr(snapshot, "status", "") or ""
    error = getattr(snapshot, "error", None)
    details = getattr(snapshot, "details", {}) or {}

    if status in ("healthy", "unknown"):
        return None
    if status not in ("degraded", "unhealthy"):
        return None
    if name not in _PER_PROBE_RULES:
        return None

    rule = _PER_PROBE_RULES[name]
    severity = rule["unhealthy_severity" if status == "unhealthy" else "degraded_severity"]
    title_fn = rule["unhealthy_title" if status == "unhealthy" else "degraded_title"]
    title = title_fn(error=error, details=details)
    detail = rule["detail_template"].format(
        status=status,
        error=error or "(no error)",
    )

    return Issue(
        id=f"probe_issue:{name}:{status}",
        probe=name,
        severity=severity,
        category="service_unhealthy",
        title=title,
        detail=detail,
        details=dict(details),
    )


def detect_issues(snapshots: Iterable[object]) -> List[Issue]:
    """Run the classifier across every snapshot; return non-``None``
    Issues. Order matches the input snapshots iteration order.
    """
    out: List[Issue] = []
    for snap in snapshots:
        try:
            issue = detect_issue_for_snapshot(snap)
        except Exception as exc:
            logger.warning(
                "[kora.probes] detect_issue_for_snapshot raised %r — "
                "skipping that snapshot",
                exc,
            )
            continue
        if issue is not None:
            out.append(issue)
    return out


# Per-probe rule literal. Each entry maps status → severity + title.
# Keeping the criteria in code (not external config) so an audit can
# diff them across versions.
_PER_PROBE_RULES = {
    "supabase": {
        "unhealthy_severity": "critical",
        "degraded_severity": "warning",
        "unhealthy_title": lambda *, error, details: (
            "Supabase unreachable"
            + (f": {error}" if error else "")
        ),
        "degraded_title": lambda *, error, details: (
            "Supabase degraded "
            f"({details.get('connections_pct', 'unknown')}% connections)"
        ),
        "detail_template": (
            "Supabase probe reported status={status}. error={error!r}. "
            "Substrate-write impact: any write to Sea_Tickets / event_log "
            "/ snapshots may fail until Supabase recovers."
        ),
    },
    "fly": {
        "unhealthy_severity": "critical",
        "degraded_severity": "warning",
        "unhealthy_title": lambda *, error, details: (
            "Fly app(s) unreachable"
            + (f": {error}" if error else "")
        ),
        "degraded_title": lambda *, error, details: (
            f"Fly degraded "
            f"({details.get('apps_running', 'unknown')} app(s) running)"
        ),
        "detail_template": (
            "Fly probe reported status={status}. error={error!r}. "
            "Deploy-control impact: app machines may be unreachable; "
            "scale-down / restart actions may not complete."
        ),
    },
    "vercel": {
        "unhealthy_severity": "critical",
        "degraded_severity": "warning",
        "unhealthy_title": lambda *, error, details: (
            "Vercel API unreachable"
            + (f": {error}" if error else "")
        ),
        "degraded_title": lambda *, error, details: (
            f"Vercel error-rate elevated "
            f"({float(details.get('error_rate_24h', 0)) * 100:.1f}%)"
        ),
        "detail_template": (
            "Vercel probe reported status={status}. error={error!r}. "
            "Recent-deploy impact: if a production deploy is failing, "
            "the website may be serving stale state."
        ),
    },
    "sentry": {
        # Sentry being unreachable is operator-attention but NOT
        # critical for the runtime — the Kora daemon still works,
        # operator just loses error-aggregation visibility for the
        # duration. Keeping unhealthy as warning (NOT critical).
        "unhealthy_severity": "warning",
        "degraded_severity": "warning",
        "unhealthy_title": lambda *, error, details: (
            "Sentry API unreachable"
            + (f": {error}" if error else "")
        ),
        "degraded_title": lambda *, error, details: (
            f"Sentry: {details.get('unresolved_issues', 'unknown')} "
            "unresolved issue(s)"
        ),
        "detail_template": (
            "Sentry probe reported status={status}. error={error!r}. "
            "Observability impact: error-aggregation visibility is "
            "degraded; runtime itself unaffected."
        ),
    },
    "doppler": {
        "unhealthy_severity": "critical",
        "degraded_severity": "warning",
        "unhealthy_title": lambda *, error, details: (
            "Doppler API unreachable"
            + (f": {error}" if error else "")
        ),
        "degraded_title": lambda *, error, details: (
            f"Doppler secret-age elevated "
            f"({details.get('oldest_secret_age_days', 'unknown')} days)"
        ),
        "detail_template": (
            "Doppler probe reported status={status}. error={error!r}. "
            "Credential-surface impact: secret reads + rotations may "
            "fail; redeploys may surface stale envs."
        ),
    },
}
