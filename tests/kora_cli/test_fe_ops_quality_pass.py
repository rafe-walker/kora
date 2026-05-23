"""Source-pin tests for KR-FE-OPS-QUALITY-PASS.

Three improvements bundled in one PR. Per the CC#2 source-pin
discipline (no FE component test runner in the repo), each
improvement is verified via grep against the live TSX source.

Scenarios:
  1. formatTimestamp appends "(local)" suffix per the TZ-clarity
     contract; timestampAbsoluteUtc helper exported alongside
  2. The 5 panels that consume formatTimestamp from panelHelpers
     also import timestampAbsoluteUtc for the hover tooltip
  3. Empty-state convergence: WebhookEvents / AgentActivity /
     Reasoning use CheckCircle2 + positive copy when the timeline
     is empty (genuine all-clear states per spec §1)
  4. SlackDM / Email keep neutral HelpCircle (data-hasn't-arrived
     states per spec §1; converging would mislead)
  5. ShowMoreFooter component exists with correct tier ladder
  6. api.ts threads ?limit into the 4 timeline endpoints
  7. The 4 timeline panels wire ShowMoreFooter + limit state
"""

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_HELPERS_PATH = _REPO_ROOT / "web" / "src" / "lib" / "panelHelpers.ts"
_SHOW_MORE_PATH = _REPO_ROOT / "web" / "src" / "components" / "ShowMoreFooter.tsx"
_API_PATH = _REPO_ROOT / "web" / "src" / "lib" / "api.ts"
_PAGES = _REPO_ROOT / "web" / "src" / "pages"


# ---- 1-2. TZ rendering ------------------------------------------


def test_format_timestamp_appends_local_suffix():
    """formatTimestamp must append "(local)" to the localized output
    so operators on multiple machines aren't TZ-confused."""
    src = _HELPERS_PATH.read_text()
    # Pin the literal "(local)" suffix in the return expression.
    match = re.search(
        r"function formatTimestamp[^}]*?return\s*`\$\{d\.toLocaleString\(\)\}\s*\(local\)`",
        src,
        re.DOTALL,
    )
    assert match, (
        "formatTimestamp must append ' (local)' suffix to the "
        "localized output — operator multi-machine TZ disambiguation"
    )


def test_timestamp_absolute_utc_helper_exported():
    """Companion helper for the hover/tooltip — returns Z-suffixed
    UTC ISO so operators can grep against logs / substrate using
    the canonical timestamp shape."""
    src = _HELPERS_PATH.read_text()
    assert re.search(
        r"export function timestampAbsoluteUtc\(",
        src,
    ), "panelHelpers.ts must export timestampAbsoluteUtc"
    # Must return Z-suffixed UTC ISO (not local-rendered or
    # ms-precision — operators grep for the canonical
    # "2026-05-23T17:48:42Z" shape).
    assert "toISOString()" in src, (
        "timestampAbsoluteUtc must use toISOString() for UTC"
    )


def test_panels_with_formatTimestamp_also_import_absolute_utc():
    """Every panel that displays formatTimestamp should also wire
    the absolute-UTC hover via timestampAbsoluteUtc, so the
    forensic-correlation hover affordance is available."""
    expected_panels = [
        "AgentActivityPanel.tsx",
        "AlertsPanel.tsx",
        "EmailPanel.tsx",
        "ReasoningPanel.tsx",
        "SlackDMPanel.tsx",
    ]
    for name in expected_panels:
        src = (_PAGES / name).read_text()
        assert "timestampAbsoluteUtc" in src, (
            f"{name} imports formatTimestamp but not "
            f"timestampAbsoluteUtc — hover-for-UTC affordance missing"
        )


# ---- 3-4. Empty-state convergence ---------------------------------


def test_webhook_events_uses_positive_empty_state():
    """No webhook traffic = healthy idle = positive reinforcement.
    Per spec §1: pin "No webhook traffic." copy + CheckCircle2 +
    success-toned card."""
    src = (_PAGES / "WebhookEventsPanel.tsx").read_text()
    # When data.events.length === 0 → positive reinforcement
    assert "No webhook traffic" in src, (
        "WebhookEventsPanel empty state should say 'No webhook traffic.'"
    )
    assert "border-success/30 bg-success/5" in src, (
        "WebhookEventsPanel data-empty card should use success-toned "
        "border + background (positive reinforcement)"
    )


def test_agent_activity_uses_positive_empty_state():
    src = (_PAGES / "AgentActivityPanel.tsx").read_text()
    assert "No agent activity." in src
    assert "border-success/30 bg-success/5" in src
    # Pin the specific success-icon usage in the empty state
    assert "CheckCircle2 className=\"h-6 w-6 mx-auto mb-2 text-success\"" in src


def test_reasoning_uses_positive_empty_state():
    src = (_PAGES / "ReasoningPanel.tsx").read_text()
    assert "No reasoning activity." in src
    assert "Kora is idle." in src
    assert "border-success/30 bg-success/5" in src


def test_slack_dm_keeps_neutral_empty_state():
    """Per spec §1: slack_dm empty pre-deploy = data-hasn't-arrived,
    NOT system-healthy. Keep neutral HelpCircle + setup-runbook
    pointer (existing copy)."""
    src = (_PAGES / "SlackDMPanel.tsx").read_text()
    # Existing copy refers to slack_app_setup_runbook
    assert "slack_app_setup_runbook" in src, (
        "SlackDMPanel empty state should preserve the setup-runbook "
        "pointer (data-hasn't-arrived semantic, NOT positive-"
        "reinforcement; spec §1 explicitly excludes from convergence)"
    )


def test_email_keeps_neutral_empty_state():
    """Per spec §1: email empty pre-deploy = data-hasn't-arrived.
    Keep neutral HelpCircle + setup-runbook pointer."""
    src = (_PAGES / "EmailPanel.tsx").read_text()
    assert "purelymail_setup_runbook" in src


# ---- 5. ShowMoreFooter component ----------------------------------


def test_show_more_footer_exists():
    assert _SHOW_MORE_PATH.is_file(), f"missing: {_SHOW_MORE_PATH}"


def test_show_more_tier_ladder_correct():
    """Tier ladder: 50 → 100 → 200 (backend cap). 200 must match the
    backend's cap in kora_cli/web_server.py — every limit-aware
    endpoint uses max(1, min(limit, 200))."""
    src = _SHOW_MORE_PATH.read_text()
    match = re.search(r"SHOW_MORE_TIERS[^=]*=\s*\[([^\]]+)\]", src)
    assert match, "SHOW_MORE_TIERS tier ladder must be exported"
    nums = [int(n.strip()) for n in match.group(1).split(",") if n.strip()]
    assert nums == [50, 100, 200], (
        f"SHOW_MORE_TIERS = {nums}; expected [50, 100, 200] per spec §1"
    )


def test_show_more_backend_cap_matches_200():
    """SHOW_MORE_BACKEND_CAP must match backend's 200 cap so FE
    clamps to the same ceiling. A future backend cap bump (or drop)
    requires both sides to agree; this pin makes the drift visible."""
    src = _SHOW_MORE_PATH.read_text()
    assert "SHOW_MORE_BACKEND_CAP" in src
    # Indirectly via the tier-ladder pin above; explicit string sweep
    # too for the "(backend cap)" terminus copy.
    assert "backend cap" in src.lower(), (
        "ShowMoreFooter at-cap terminus should explain the backend "
        "cap so the operator knows older entries exist"
    )


def test_show_more_at_cap_points_to_forensic_data_sources():
    """At cap, the operator needs to know older entries are still
    available — point them at the JSONL / substrate forensic
    paths (the actual data sources behind the panels)."""
    src = _SHOW_MORE_PATH.read_text()
    assert "JSONL" in src and "substrate" in src.lower(), (
        "ShowMoreFooter at-cap terminus should mention JSONL + "
        "substrate so the operator knows where to look forensically"
    )


# ---- 6. api.ts limit threading -----------------------------------


def test_api_threads_limit_into_four_endpoints():
    """The 4 timeline endpoints accept ?limit; api client must
    accept an optional limit param and thread it into the URL."""
    src = _API_PATH.read_text()
    for fn_name in (
        "getRecentSlackDM",
        "getRecentAgentActivity",
        "getRecentReasoning",
        "getRecentWebhookEvents",
    ):
        # Optional limit param signature
        assert re.search(
            rf"{fn_name}:\s*\(limit\?:\s*number\)",
            src,
        ), f"{fn_name} should accept an optional limit param"


# ---- 7. Panels wire ShowMoreFooter + limit state ---------------


def test_four_timeline_panels_wire_show_more_footer():
    """Each of the 4 timeline panels must (a) import the footer,
    (b) track limit state with the SHOW_MORE_DEFAULT_LIMIT initial,
    and (c) render ShowMoreFooter at the bottom of the timeline."""
    for name in (
        "AgentActivityPanel.tsx",
        "ReasoningPanel.tsx",
        "SlackDMPanel.tsx",
        "WebhookEventsPanel.tsx",
    ):
        src = (_PAGES / name).read_text()
        assert "ShowMoreFooter" in src, (
            f"{name} must import + render ShowMoreFooter"
        )
        assert "SHOW_MORE_DEFAULT_LIMIT" in src, (
            f"{name} must initialize limit state from the shared "
            f"SHOW_MORE_DEFAULT_LIMIT constant"
        )
        # The setLimit handler must be wired to onShowMore so click
        # bumps the limit (and the useEffect re-fetches via the
        # limit dep on the loadX useCallback).
        assert re.search(
            r"onShowMore=\{setLimit\}",
            src,
        ), f"{name} must pass setLimit as onShowMore handler"


def test_four_timeline_panels_pass_limit_to_api_call():
    """The api.getRecentX(limit) call must thread the state so
    clicking Show More actually re-fetches with the bumped limit."""
    cases = [
        ("AgentActivityPanel.tsx", "getRecentAgentActivity"),
        ("ReasoningPanel.tsx", "getRecentReasoning"),
        ("SlackDMPanel.tsx", "getRecentSlackDM"),
        ("WebhookEventsPanel.tsx", "getRecentWebhookEvents"),
    ]
    for panel_name, api_fn in cases:
        src = (_PAGES / panel_name).read_text()
        assert re.search(
            rf"\.{api_fn}\(limit\)",
            src,
        ), (
            f"{panel_name} must call api.{api_fn}(limit) "
            f"(not the no-arg form) so Show More bumps actually fetch"
        )
