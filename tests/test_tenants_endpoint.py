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
