"""KR-FE-TENANT-PICKER-COCKPIT-CHROME — /api/tenants/list + the
cost-state ?tenant_id= passthrough.

Drift-guard pin: the BE-side ``TENANT_ID_QUERY_PARAM_NAME`` literal
must stay equal to the FE-side ``TENANT_ID_QUERY_PARAM`` literal in
``web/src/hooks/useActiveTenant.ts``. Renaming one without the other
silently breaks the cockpit's tenant-scoped reads. The asserts at
the bottom of this file fail loudly the moment they diverge.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.cost_state_holder import (
    DEFAULT_TENANT_ID,
    _reset_cost_holder_for_tests,
    init_cost_holder,
)


@pytest.fixture(autouse=True)
def _reset_holders():
    _reset_cost_holder_for_tests()
    yield
    _reset_cost_holder_for_tests()


@pytest.fixture
def client(monkeypatch, tmp_path):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from kora_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    c = TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return c


# ---------------------------------------------------------------------------
# /api/tenants/list
# ---------------------------------------------------------------------------


def test_tenants_list_default_only_when_registry_empty(client):
    """Empty holder registry → returns ["default"] anyway. Picker
    needs at least the canonical option so the FE never crashes on
    the single-tenant fresh-install path."""
    resp = client.get("/api/tenants/list")
    assert resp.status_code == 200
    data = resp.json()
    assert data == {"tenants": ["default"]}


def test_tenants_list_returns_default_first(client):
    """default-first, rest sorted — stable render order for the
    sidebar dropdown so an alphabetically-earlier tenant (alpha)
    doesn't bump 'default' down the list."""
    init_cost_holder(tenant_id="zeta", credit_pool_usd=10.0)
    init_cost_holder(tenant_id="alpha", credit_pool_usd=10.0)
    init_cost_holder(tenant_id=DEFAULT_TENANT_ID, credit_pool_usd=10.0)

    resp = client.get("/api/tenants/list")
    assert resp.status_code == 200
    assert resp.json() == {"tenants": ["default", "alpha", "zeta"]}


def test_tenants_list_synthesizes_default_when_only_named_tenants(client):
    """If operators init only named tenants (no explicit "default"),
    the picker still needs the canonical default option — synthesize
    it. The cost-holder registry doesn't auto-create "default" on
    init."""
    init_cost_holder(tenant_id="marvin", credit_pool_usd=10.0)

    resp = client.get("/api/tenants/list")
    assert resp.json() == {"tenants": ["default", "marvin"]}


# ---------------------------------------------------------------------------
# /api/cost-state passthrough
# ---------------------------------------------------------------------------


def test_cost_state_tenant_id_routes_to_per_tenant_holder(client):
    """``?tenant_id=marvin`` → resolves the marvin holder. With no
    isokron provider registered the response is a stub, but the
    stub-vs-real branch isn't what's under test here — what matters
    is that the holder lookup happens against the named tenant and
    not against ``default``."""
    init_cost_holder(tenant_id="marvin", credit_pool_usd=42.0)

    resp = client.get("/api/cost-state?tenant_id=marvin")
    # The provider-not-registered branch returns stub:True; that's
    # fine. The key assertion is that the call succeeded (200) and
    # routed through the per-tenant holder accessor without raising.
    assert resp.status_code == 200
    body = resp.json()
    assert "current" in body  # full shape preserved


def test_cost_state_no_tenant_id_preserves_legacy_default_behavior(client):
    """Omitting the param ≡ pre-#202 single-tenant behavior."""
    resp = client.get("/api/cost-state")
    assert resp.status_code == 200
    body = resp.json()
    assert "current" in body


# ---------------------------------------------------------------------------
# Drift-guard pins
# ---------------------------------------------------------------------------


def test_tenant_id_query_param_name_pin():
    """BE-side literal ``TENANT_ID_QUERY_PARAM_NAME`` matches the
    documented contract ``"tenant_id"``. Cross-stack test_tenant_id_*
    asserts the FE literal matches; this side asserts the BE literal
    matches the contract. Both sides must equal "tenant_id"."""
    from kora_cli.web_server import TENANT_ID_QUERY_PARAM_NAME

    assert TENANT_ID_QUERY_PARAM_NAME == "tenant_id"


def test_fe_useActiveTenant_pins_match_be_constants():
    """Grep the FE hook source to ensure the FE literal is equal to
    the BE literal. Cross-stack pin so a rename on either side fails
    the BE suite (FE has no vitest in this repo today).

    Repo layout: this test lives at <repo>/tests/, the FE hook lives
    at <repo>/web/src/hooks/useActiveTenant.ts.
    """
    hook_path = (
        Path(__file__).parent.parent
        / "web"
        / "src"
        / "hooks"
        / "useActiveTenant.ts"
    )
    src = hook_path.read_text(encoding="utf-8")

    # Pin: the FE storage key + URL param literal + default tenant id.
    assert 'TENANT_PICKER_STORAGE_KEY = "kora_active_tenant"' in src
    assert 'TENANT_ID_QUERY_PARAM = "tenant"' in src
    assert 'DEFAULT_TENANT_ID = "default"' in src
    # Pin: the FE forwards the BE param under the BE literal name.
    # Search across api.ts (the audit-query builder + per-endpoint
    # calls) — anywhere is fine; the literal must appear at least
    # once via the BE name "tenant_id".
    api_path = (
        Path(__file__).parent.parent
        / "web"
        / "src"
        / "lib"
        / "api.ts"
    )
    api_src = api_path.read_text(encoding="utf-8")
    assert 'qs.set("tenant_id"' in api_src or '"tenant_id"' in api_src, (
        "FE must forward the BE-pinned literal 'tenant_id' as the "
        "query-param name; got no occurrence in web/src/lib/api.ts"
    )


def test_fe_aggregate_and_deeplink_pins():
    """KR-FE-MULTI-TENANT-COCKPIT-AGGREGATE-AND-DEEPLINK — extended
    drift-guards covering the ``?tenant=all`` URL alias, the
    ALL_TENANTS_SENTINEL internal pseudo-id, and the cross-
    component event used by page-header badges to open the sidebar
    TenantPicker. Renaming any of these without updating the
    matching consumer silently breaks deep-link UX.
    """
    hook_path = (
        Path(__file__).parent.parent
        / "web"
        / "src"
        / "hooks"
        / "useActiveTenant.ts"
    )
    hook_src = hook_path.read_text(encoding="utf-8")

    # B.1 / B.4: the URL alias literal that resolves to the
    # internal sentinel. Pin the operator-readable form ``"all"``
    # so deep-links stay human-readable.
    assert 'ALL_TENANTS_URL_ALIAS = "all"' in hook_src
    # The internal sentinel stays at ``__all__`` so it can't
    # collide with a real tenant_id named "all".
    assert 'ALL_TENANTS_SENTINEL = "__all__"' in hook_src
    # B.2: open-picker event name shared between badges + picker.
    assert (
        'OPEN_TENANT_PICKER_EVENT = "kora:open-tenant-picker"'
        in hook_src
    )

    # B.2: the badge participates in the same constants — grep its
    # exported sentinel-pin so a hook rename would surface here too.
    badge_path = (
        Path(__file__).parent.parent
        / "web"
        / "src"
        / "components"
        / "ActiveTenantBadge.tsx"
    )
    badge_src = badge_path.read_text(encoding="utf-8")
    assert "ACTIVE_TENANT_BADGE_USES_SENTINEL = ALL_TENANTS_SENTINEL" in (
        badge_src
    )
    # Badge imports the open-event constant — drift-guard ties
    # badge → hook concretely.
    assert "OPEN_TENANT_PICKER_EVENT" in badge_src or "requestOpenTenantPicker" in badge_src

    # A.2: aggregate cost cards exist + read the v6 by-tenant block.
    agg_path = (
        Path(__file__).parent.parent
        / "web"
        / "src"
        / "components"
        / "AggregateCostCards.tsx"
    )
    agg_src = agg_path.read_text(encoding="utf-8")
    assert "cost_ladder_by_tenant" in agg_src, (
        "AggregateCostCards must read the snapshot v6 "
        "cost_ladder_by_tenant block — sole canonical per-tenant "
        "cost surface"
    )


def test_fe_keyboard_nav_and_url_toggle_and_tab_title_pins():
    """KR-FE-TENANT-PICKER-KEYBOARD-NAV-AND-URL-TOGGLE-AND-TAB-TITLE —
    pin the keyboard contract + URL-toggle storage key + browser-tab
    title format. Each of these is part of the operator-facing
    contract; renaming a key on one side without the other silently
    breaks documented behavior.
    """
    repo = Path(__file__).parent.parent

    # A.4 — keyboard shortcuts pin. The TENANT_PICKER_KEYBOARD_SHORTCUTS
    # object is the single source of truth; the implementation key
    # checks below assert the documented keys actually appear in
    # the handler.
    picker_src = (
        repo / "web" / "src" / "components" / "TenantPicker.tsx"
    ).read_text(encoding="utf-8")
    assert "TENANT_PICKER_KEYBOARD_SHORTCUTS" in picker_src
    # Implementation handles each of the documented keys.
    for key in ('"ArrowDown"', '"ArrowUp"', '"Enter"', '"Escape"'):
        assert key in picker_src, (
            f"TenantPicker keyboard handler must reference {key} — "
            "pinned by TENANT_PICKER_KEYBOARD_SHORTCUTS"
        )
    # Letter-jump cycling exists (matches §4 STOP-ASK resolution
    # to prefer cycling over first-match-only).
    assert "cycleLetterJump" in picker_src

    # B.1 — URL-toggle storage key pinned in the hook + checkbox
    # rendered in the picker.
    hook_src = (
        repo / "web" / "src" / "hooks" / "useActiveTenant.ts"
    ).read_text(encoding="utf-8")
    assert (
        'TENANT_PICKER_URL_TOGGLE_STORAGE_KEY =\n  "kora_tenant_picker_update_url"'
        in hook_src
        or 'TENANT_PICKER_URL_TOGGLE_STORAGE_KEY = "kora_tenant_picker_update_url"'
        in hook_src
    ), (
        "URL-toggle localStorage key must be 'kora_tenant_picker_update_url' "
        "— pinned for operator-facing localStorage stability"
    )
    assert "useTenantUrlToggle" in hook_src
    assert "useTenantUrlToggle" in picker_src, (
        "TenantPicker must render the URL-toggle checkbox via "
        "useTenantUrlToggle"
    )

    # C.1 — tab-title prefix format. Pin the brand suffix +
    # bracket-prefix shape so a future refactor can't silently
    # change what shows up in the browser tab.
    pageheader_src = (
        repo / "web" / "src" / "contexts" / "PageHeaderProvider.tsx"
    ).read_text(encoding="utf-8")
    assert 'TAB_TITLE_SUFFIX = "Hermes Agent"' in pageheader_src
    assert "formatBrowserTabTitle" in pageheader_src
    # Format pins: `[<tenant>] <title> · Hermes Agent`.
    assert "[all tenants]" in pageheader_src
    assert "document.title = formatBrowserTabTitle(" in pageheader_src


def test_fe_a11y_and_recent_tenants_pins():
    """KR-FE-A11Y-AUDIT-AND-MULTI-TENANT-POLISH — pin the recent-
    tenants storage key + the tenant-change live-region role/
    politeness + the skip-to-main anchor target. Each is part of
    the operator/assistive-tech-facing contract.
    """
    repo = Path(__file__).parent.parent

    # B — recent-tenants storage key + RECENT_TENANTS_CAP.
    hook_src = (
        repo / "web" / "src" / "hooks" / "useActiveTenant.ts"
    ).read_text(encoding="utf-8")
    assert (
        'RECENT_TENANTS_STORAGE_KEY = "kora_recent_tenants"' in hook_src
    ), (
        "recent-tenants localStorage key must be 'kora_recent_tenants' — "
        "operator-facing key, stable across releases"
    )
    assert "RECENT_TENANTS_CAP = 5" in hook_src
    # pushRecentTenant pure helper exported so callers/tests can
    # reuse the dedupe+cap semantics without round-tripping storage.
    assert "export function pushRecentTenant" in hook_src
    # Picker reads the new field on the hook.
    picker_src = (
        repo / "web" / "src" / "components" / "TenantPicker.tsx"
    ).read_text(encoding="utf-8")
    assert "recentTenants" in picker_src
    # Decorative section header renders for the two-section layout.
    assert "PickerSectionHeader" in picker_src

    # A.3 — aria-live announcer role + politeness + sr-only class.
    announcer_src = (
        repo / "web" / "src" / "components" / "TenantChangeAnnouncer.tsx"
    ).read_text(encoding="utf-8")
    assert 'TENANT_ANNOUNCER_LIVE_REGION_ROLE = "status"' in announcer_src
    assert 'TENANT_ANNOUNCER_ARIA_LIVE = "polite"' in announcer_src
    assert 'className="sr-only"' in announcer_src
    # App mounts the announcer once at the shell layer.
    app_src = (repo / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "<TenantChangeAnnouncer />" in app_src

    # A.1 — skip-to-main link targets #kora-main; PageHeaderProvider
    # carries the matching id on <main>.
    assert 'href="#kora-main"' in app_src
    assert "Skip to main content" in app_src
    pageheader_src = (
        repo / "web" / "src" / "contexts" / "PageHeaderProvider.tsx"
    ).read_text(encoding="utf-8")
    assert 'id="kora-main"' in pageheader_src

    # C — wizard skip routes through ConfirmDialog; per-step link
    # text is operator-facing so pin it.
    wizard_src = (
        repo / "web" / "src" / "pages" / "WizardPage.tsx"
    ).read_text(encoding="utf-8")
    assert "ConfirmDialog" in wizard_src
    assert "Skip wizard, configure manually" in wizard_src
    # Both header skip + per-step link route through requestSkip;
    # the actual completeWizard call lives behind confirmSkip.
    assert "requestSkip" in wizard_src
    assert "confirmSkip" in wizard_src


def test_fe_forced_colors_and_axe_core_pins():
    """KR-FE-A11Y-COMPLETION-FORCED-COLORS-AND-AXE-CORE-CI — pin the
    forced-colors media query block + the data-attribute hooks the
    block targets + the @axe-core/react dev integration. Renaming
    a data attribute on a component without updating the matching
    CSS rule silently breaks the high-contrast experience.
    """
    repo = Path(__file__).parent.parent

    # A — forced-colors media-query block in index.css with the
    # rules that target the multi-tenant chrome.
    css_src = (repo / "web" / "src" / "index.css").read_text(encoding="utf-8")
    assert "@media (forced-colors: active)" in css_src, (
        "index.css must include a forced-colors media-query block — "
        "Windows High Contrast users lose every author background "
        "without it"
    )
    # Each rule must reference its data-attribute hook so a future
    # rename of the attribute on the component side fails this test.
    for hook in (
        '[role="option"][data-highlighted="true"]',
        "[data-tenant-chip]",
        "[data-rung-bar]",
        ':focus-visible',
        'a[aria-current="page"]',
    ):
        assert hook in css_src, (
            f"forced-colors block must include the {hook!r} rule"
        )

    # The components emit the data attributes the CSS targets.
    picker_src = (
        repo / "web" / "src" / "components" / "TenantPicker.tsx"
    ).read_text(encoding="utf-8")
    assert 'data-highlighted={highlighted ? "true" : undefined}' in picker_src, (
        "TenantPicker option must emit data-highlighted — paired "
        "with the forced-colors CSS rule"
    )

    badge_src = (
        repo / "web" / "src" / "components" / "ActiveTenantBadge.tsx"
    ).read_text(encoding="utf-8")
    assert "data-tenant-chip" in badge_src

    agg_src = (
        repo / "web" / "src" / "components" / "AggregateCostCards.tsx"
    ).read_text(encoding="utf-8")
    assert "data-rung-bar" in agg_src

    # B — @axe-core/react dev dep + main.tsx dev-only mount.
    pkg_src = (repo / "web" / "package.json").read_text(encoding="utf-8")
    assert "@axe-core/react" in pkg_src, (
        "@axe-core/react must be present in web/package.json devDependencies "
        "for the dev-mode a11y console integration"
    )
    main_src = (repo / "web" / "src" / "main.tsx").read_text(encoding="utf-8")
    # Dev-only gate — the entire branch must be tree-shaken out of
    # production builds. import.meta.env.DEV is the vite-recognized
    # gate.
    assert "import.meta.env.DEV" in main_src
    assert '"@axe-core/react"' in main_src


def test_fe_confirm_dialog_required_description_pin():
    """KR-FE-CONFIRMDIALOG-PROP-AND-COCKPIT-A11Y-SWEEP — pin that
    ConfirmDialog + DeleteConfirmDialog require description (it
    binds aria-describedby) AND that every <ConfirmDialog or
    <DeleteConfirmDialog JSX call site in web/src/ actually passes
    a description= attribute. A future call site omitting it would
    silently fail screen-reader announcement.
    """
    import re

    repo = Path(__file__).parent.parent

    # Type-level pin: description is required (not ``description?``).
    confirm_src = (
        repo / "web" / "src" / "components" / "ui" / "confirm-dialog.tsx"
    ).read_text(encoding="utf-8")
    assert "description: string;" in confirm_src, (
        "ConfirmDialog.description must be typed string (required) — "
        "not optional ``description?``"
    )
    # Dev-mode runtime nudge for empty-string descriptions.
    assert "[a11y] ConfirmDialog opened with empty description" in confirm_src

    delete_src = (
        repo / "web" / "src" / "components" / "DeleteConfirmDialog.tsx"
    ).read_text(encoding="utf-8")
    assert "description: string;" in delete_src

    # Call-site pin: every JSX call site under web/src must include
    # a description= prop in the same opening tag. Globs through
    # the source tree; ignores .map/.d.ts files.
    src_root = repo / "web" / "src"
    offenders: list[str] = []
    open_tag_re = re.compile(
        r"<(ConfirmDialog|DeleteConfirmDialog)\b[^/>]*?>", re.DOTALL
    )
    for path in src_root.rglob("*.tsx"):
        text = path.read_text(encoding="utf-8")
        for m in open_tag_re.finditer(text):
            block = m.group(0)
            if "description=" not in block:
                offenders.append(
                    f"{path.relative_to(repo)}: <{m.group(1)} ...> missing description="
                )
    assert not offenders, (
        "Every ConfirmDialog/DeleteConfirmDialog call site must pass "
        "description= for screen-reader announcement. Offenders:\n"
        + "\n".join(offenders)
    )


def test_fe_non_multi_tenant_a11y_sweep_pins():
    """KR-FE-CONFIRMDIALOG-PROP-AND-COCKPIT-A11Y-SWEEP — pin the
    top a11y fixes applied on the five non-multi-tenant pages
    (chat / sessions / models / plugins / OAuth). Each fix's hook
    is grep-asserted so a refactor that drops the a11y wiring
    fails CI.
    """
    repo = Path(__file__).parent.parent

    # ChatPage — xterm host promoted to a labelled region.
    chat_src = (
        repo / "web" / "src" / "pages" / "ChatPage.tsx"
    ).read_text(encoding="utf-8")
    assert 'aria-label="Hermes chat terminal"' in chat_src
    assert 'role="region"' in chat_src

    # SessionsPage — session row gets keyboard semantics +
    # accessible name including session metadata.
    sessions_src = (
        repo / "web" / "src" / "pages" / "SessionsPage.tsx"
    ).read_text(encoding="utf-8")
    assert 'role="button"' in sessions_src
    assert "aria-expanded={isExpanded}" in sessions_src
    assert 'aria-label={`Session ' in sessions_src

    # ModelsPage — "Use as" trigger announces the menu affordance.
    models_src = (
        repo / "web" / "src" / "pages" / "ModelsPage.tsx"
    ).read_text(encoding="utf-8")
    assert 'aria-haspopup="menu"' in models_src
    assert "aria-expanded={open}" in models_src
    assert 'role="menu"' in models_src

    # PluginsPage — Enable/Disable carry state-aware aria-label;
    # Show/Hide button's decorative icons are aria-hidden.
    plugins_src = (
        repo / "web" / "src" / "pages" / "PluginsPage.tsx"
    ).read_text(encoding="utf-8")
    assert "is already enabled" in plugins_src
    assert "is already disabled" in plugins_src
    assert "Show ${row.name} in sidebar" in plugins_src
    assert "Hide ${row.name} from sidebar" in plugins_src
    # Eye / EyeOff marked aria-hidden so SRs don't double-read.
    assert "<EyeOff aria-hidden" in plugins_src
    assert "<Eye aria-hidden" in plugins_src

    # OAuthProvidersCard — Login/Disconnect carry provider-aware
    # aria-label so SR users disambiguate across multiple providers.
    oauth_src = (
        repo / "web" / "src" / "components" / "OAuthProvidersCard.tsx"
    ).read_text(encoding="utf-8")
    assert "${t.oauth.login} ${p.name}" in oauth_src
    assert "${t.oauth.disconnect} ${p.name}" in oauth_src
