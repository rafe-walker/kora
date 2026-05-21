"""Tests for the KR-P2-CHARTER-PANEL endpoint.

Bucket §5 scenarios:
  1. GET 200 + shape (active + capability_groups + substrate_tier_tools + stub:false)
  2. When no Constitution loaded: active is null
  3. When rules_available == false: rules is empty array (not null)
  4. Contract guard: revision_id always non-empty string OR null when active is non-null
  5. Capability groups match KR-P2-CAP-PANEL's shape (regression — both
     endpoints share the underlying TOOL_CAPABILITY_MAP)
  6. Cron-regression sanity
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("kora_cli.config.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr(
        "kora_cli.config.get_config_path", lambda: tmp_path / "config.yaml"
    )
    monkeypatch.setattr(
        "kora_cli.config.get_env_path", lambda: tmp_path / ".env"
    )
    return tmp_path


@pytest.fixture
def _no_active_provider(monkeypatch):
    """Force the IsoKron provider lookup to return None — test env never
    has the plugin registered, but be explicit so the test self-documents
    the no-provider case."""
    import plugins.memory.isokron as isokron_pkg

    monkeypatch.setattr(isokron_pkg, "_last_active_provider", None)
    return None


# ---- 1. 200 + shape -----------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_returns_200_and_top_level_shape(_isolate_config, _no_active_provider):
    from kora_cli import web_server

    result = await web_server.get_charter()
    assert isinstance(result, dict)
    assert set(result.keys()) == {
        "active",
        "capability_groups",
        "substrate_tier_tools",
        "stub",
    }
    assert result["stub"] is False
    assert isinstance(result["capability_groups"], list)
    assert isinstance(result["substrate_tier_tools"], list)


# ---- 2. No Constitution loaded → active is null ------------------------


@pytest.mark.asyncio
async def test_active_is_null_when_no_provider_registered(_isolate_config, _no_active_provider):
    """In CI / dev environments without IsoKron plugin registered,
    ``active`` must be null so the FE renders the "no active
    Constitution" empty-state card rather than crashing."""
    from kora_cli import web_server

    result = await web_server.get_charter()
    assert result["active"] is None


# ---- 3. rules_available=false ⇒ rules is empty array (not null) -------


@pytest.mark.asyncio
async def test_rules_is_empty_array_when_provider_present_but_rules_unavailable(
    _isolate_config, monkeypatch
):
    """v1 fallback mode: when an active provider exists, ``rules`` must
    be an empty list (never null) and ``rules_available`` must be False.
    The FE conditionally renders the rules section; null vs [] matters
    for the conditional."""
    import plugins.memory.isokron as isokron_pkg

    # Inject a fake provider that returns the documented fallback shape.
    class _FakeProvider:
        def get_active_constitution_summary(self, workspace_id=None):
            return {
                "revision_id": "rev_abc",
                "rules_hash": "sha256:deadbeef",
                "loaded_at": "2026-05-21T22:00:00Z",
                "workspace_id": "00000000-0000-0000-0000-000000000001",
                "rules": [],
                "rules_available": False,
            }

    monkeypatch.setattr(isokron_pkg, "_last_active_provider", _FakeProvider())

    from kora_cli import web_server

    result = await web_server.get_charter()
    assert result["active"] is not None
    assert isinstance(result["active"]["rules"], list)
    assert result["active"]["rules"] == []
    assert result["active"]["rules_available"] is False


# ---- 4. Contract: revision_id always non-empty string OR null --------


@pytest.mark.asyncio
async def test_revision_id_is_string_or_null_when_active_present(
    _isolate_config, monkeypatch
):
    """Bucket §5 #4: revision_id is non-empty when set. The provider's
    sentinel ``(None, None)`` for fresh-workspace surfaces revision_id
    as null — but never as empty string. Pin that."""
    import plugins.memory.isokron as isokron_pkg

    class _FakeProvider:
        def get_active_constitution_summary(self, workspace_id=None):
            return {
                "revision_id": "rev_abc",
                "rules_hash": "sha256:abc",
                "loaded_at": "2026-05-21T22:00:00Z",
                "workspace_id": "ws_test",
                "rules": [],
                "rules_available": False,
            }

    monkeypatch.setattr(isokron_pkg, "_last_active_provider", _FakeProvider())

    from kora_cli import web_server

    result = await web_server.get_charter()
    active = result["active"]
    assert active is not None
    # Either a non-empty string or null — never empty string.
    assert active["revision_id"] is None or (
        isinstance(active["revision_id"], str) and active["revision_id"]
    )


@pytest.mark.asyncio
async def test_fresh_workspace_sentinel_surfaces_as_null_revision(
    _isolate_config, monkeypatch
):
    """The provider returns (None, None) for fresh workspaces; the
    endpoint must pass that through as null fields rather than empty
    strings. FE renders "fresh workspace, no revisions" copy."""
    import plugins.memory.isokron as isokron_pkg

    class _FakeProvider:
        def get_active_constitution_summary(self, workspace_id=None):
            return {
                "revision_id": None,
                "rules_hash": None,
                "loaded_at": "2026-05-21T22:00:00Z",
                "workspace_id": "ws_fresh",
                "rules": [],
                "rules_available": False,
            }

    monkeypatch.setattr(isokron_pkg, "_last_active_provider", _FakeProvider())

    from kora_cli import web_server

    result = await web_server.get_charter()
    active = result["active"]
    assert active is not None
    assert active["revision_id"] is None
    assert active["rules_hash"] is None


# ---- 5. Capability groups regression — match CAP-PANEL shape --------


@pytest.mark.asyncio
async def test_capability_groups_match_cap_panel_shape(_isolate_config, _no_active_provider):
    """Both /api/capabilities and /api/charter project from the same
    ``TOOL_CAPABILITY_MAP``. The (cap_name, sorted-tools) pairs must
    agree across endpoints — drift here would silently desync the two
    operator-facing views of the same underlying policy map."""
    from kora_cli import web_server

    charter = await web_server.get_charter()
    capabilities = await web_server.get_capabilities()

    charter_pairs = {
        g["cap_name"]: tuple(g["tools"]) for g in charter["capability_groups"]
    }
    cap_panel_pairs = {
        g["cap_name"]: tuple(g["tools"]) for g in capabilities["groups"]
    }

    assert charter_pairs == cap_panel_pairs

    # Substrate-tier list also identical between the two.
    assert set(charter["substrate_tier_tools"]) == set(
        capabilities["substrate_tier"]
    )


# ---- 6. Cron-regression sanity --------------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_charter_registered(_isolate_config):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
