"""Tests for KR-PROBE-AUDIT-AND-CONVERT — issue detector.

Bucket §2 Phase 3 scenarios:
   1. healthy snapshot → no Issue
   2. unknown snapshot → no Issue (cache-warming / auth-unset is
      operator-config state, not probe-detected issue)
   3. Each probe × {unhealthy, degraded} → Issue with right severity
   4. supabase unhealthy → critical
   5. fly unhealthy → critical; degraded → warning
   6. vercel unhealthy → critical; degraded → warning
   7. sentry — both unhealthy + degraded → warning
      (Sentry-unreachable isn't critical for runtime; observability-only)
   8. doppler unhealthy → critical; degraded → warning
   9. Unknown probe name → no Issue (defensive)
  10. detect_issues across multiple snapshots returns list in order
  11. detect_issue handles bad snapshot (missing attrs) gracefully
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from kora_cli.probes.issue_detector import (
    Issue,
    detect_issue_for_snapshot,
    detect_issues,
)


@dataclass(frozen=True)
class _FakeSnap:
    name: str
    status: str
    error: str | None = None
    details: dict | None = None


def _snap(**kw) -> _FakeSnap:
    return _FakeSnap(details=kw.pop("details", {}) or {}, **kw)


# ===========================================================================
# healthy / unknown — no Issue
# ===========================================================================


def test_healthy_supabase_no_issue():
    assert detect_issue_for_snapshot(_snap(name="supabase", status="healthy")) is None


def test_healthy_fly_no_issue():
    assert detect_issue_for_snapshot(_snap(name="fly", status="healthy")) is None


def test_unknown_status_no_issue():
    """Auth env unset / cache warming surfaces as 'unknown' status —
    that's operator-config state, not a probe-detected issue."""
    assert detect_issue_for_snapshot(_snap(name="supabase", status="unknown")) is None
    assert detect_issue_for_snapshot(_snap(name="fly", status="unknown")) is None


def test_garbage_status_no_issue():
    assert detect_issue_for_snapshot(_snap(name="supabase", status="weird")) is None


# ===========================================================================
# supabase
# ===========================================================================


def test_supabase_unhealthy_critical():
    issue = detect_issue_for_snapshot(
        _snap(name="supabase", status="unhealthy", error="HTTP 502")
    )
    assert issue is not None
    assert issue.severity == "critical"
    assert issue.probe == "supabase"
    assert issue.category == "service_unhealthy"
    assert "Supabase" in issue.title
    assert "Substrate" in issue.detail


def test_supabase_degraded_warning():
    issue = detect_issue_for_snapshot(
        _snap(
            name="supabase",
            status="degraded",
            details={"connections_pct": 85},
        )
    )
    assert issue is not None
    assert issue.severity == "warning"


# ===========================================================================
# fly
# ===========================================================================


def test_fly_unhealthy_critical():
    issue = detect_issue_for_snapshot(
        _snap(name="fly", status="unhealthy", error="HTTP 401")
    )
    assert issue is not None
    assert issue.severity == "critical"
    assert "Fly" in issue.title


def test_fly_degraded_warning():
    issue = detect_issue_for_snapshot(
        _snap(
            name="fly",
            status="degraded",
            details={"apps_running": 1, "deploys_last_24h": "unknown"},
        )
    )
    assert issue is not None
    assert issue.severity == "warning"


# ===========================================================================
# vercel
# ===========================================================================


def test_vercel_unhealthy_critical():
    issue = detect_issue_for_snapshot(
        _snap(name="vercel", status="unhealthy", error="HTTP 500")
    )
    assert issue is not None
    assert issue.severity == "critical"
    assert "Vercel" in issue.title


def test_vercel_degraded_warning_with_error_rate():
    issue = detect_issue_for_snapshot(
        _snap(
            name="vercel",
            status="degraded",
            details={"deployments_last_24h": 20, "error_rate_24h": 0.15},
        )
    )
    assert issue is not None
    assert issue.severity == "warning"
    assert "15.0%" in issue.title


# ===========================================================================
# sentry (warning, not critical, for unreachable — observability-only)
# ===========================================================================


def test_sentry_unhealthy_is_warning_not_critical():
    """Sentry-unreachable shouldn't wake the operator at critical
    severity — the runtime works fine without Sentry. Spec §2 Phase 3
    + module docstring documents this exception."""
    issue = detect_issue_for_snapshot(
        _snap(name="sentry", status="unhealthy", error="HTTP 503")
    )
    assert issue is not None
    assert issue.severity == "warning"


def test_sentry_degraded_warning_with_unresolved_count():
    issue = detect_issue_for_snapshot(
        _snap(
            name="sentry",
            status="degraded",
            details={"unresolved_issues": 42},
        )
    )
    assert issue is not None
    assert issue.severity == "warning"
    assert "42" in issue.title


# ===========================================================================
# doppler
# ===========================================================================


def test_doppler_unhealthy_critical():
    issue = detect_issue_for_snapshot(
        _snap(name="doppler", status="unhealthy", error="HTTP 401")
    )
    assert issue is not None
    assert issue.severity == "critical"


def test_doppler_degraded_warning_with_secret_age():
    issue = detect_issue_for_snapshot(
        _snap(
            name="doppler",
            status="degraded",
            details={"oldest_secret_age_days": 200, "projects_total": 5},
        )
    )
    assert issue is not None
    assert issue.severity == "warning"
    assert "200" in issue.title


# ===========================================================================
# Unknown probe + defensive
# ===========================================================================


def test_unknown_probe_name_no_issue():
    assert (
        detect_issue_for_snapshot(_snap(name="not_a_real_probe", status="unhealthy"))
        is None
    )


def test_detect_issues_multi():
    snaps = [
        _snap(name="supabase", status="healthy"),
        _snap(name="fly", status="unhealthy", error="HTTP 500"),
        _snap(name="vercel", status="degraded", details={"error_rate_24h": 0.12}),
    ]
    issues = detect_issues(snaps)
    assert len(issues) == 2
    assert {i.probe for i in issues} == {"fly", "vercel"}


def test_detect_issue_missing_attrs_no_raise():
    """A duck-typed object missing some expected attrs returns None
    safely (no AttributeError)."""

    class Empty:
        pass

    assert detect_issue_for_snapshot(Empty()) is None
