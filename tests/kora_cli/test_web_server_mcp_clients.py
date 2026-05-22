"""Tests for the /api/mcp/clients/list endpoint.

Originally landed by CC#2's KR-MCP-3 (PR #106) as a hardcoded stub.
CC#1's KR-MCP-CLIENTS-FLIP swaps the body for a live read from
``kora_mcp.catalog.load_effective_catalog`` (defaults github +
cloudflare + operator overrides from ``~/.kora/config.yaml``).

Scenarios:
  1. GET /api/mcp/clients/list returns 200
  2. Top-level shape (clients + generated_at + stub:false post-flip)
  3. Default catalog (github + cloudflare) surfaced when no
     operator overrides
  4. Each client entry has the required keys + valid status/
     transport enums (TS MCPClient interface contract)
  5. SECURITY (preserved): auth_token_env carries env-var NAME
     only, not value; auth_token_present is bool; no token-value-
     shaped field leaks
  6. Status mapping: unhealthy when auth env unset; configured_
     but_unconnected when set
  7. auth_token_present: false when env unset; true when set;
     false when empty string
  8. Operator config.yaml override respected (override-by-name +
     add-new)
  9. Cron-regression sanity
"""

import re

import pytest


_VALID_STATUS = {
    "connected",
    "configured_but_unconnected",
    "error",
    "unhealthy",
}
_VALID_TRANSPORT = {"stdio", "streamable_http"}
_EXPECTED_CLIENTS = {"github", "cloudflare"}


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


# ---- 1. 200 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_returns_200(_isolate_config):
    from kora_cli import web_server

    result = await web_server.list_mcp_clients()
    assert isinstance(result, dict)


# ---- 2. Top-level shape ----------------------------------------------


@pytest.mark.asyncio
async def test_response_shape_has_required_keys(_isolate_config):
    """Post-flip: stub flag is False (live read engaged). Top-level
    keys identical so the FE doesn't need a schema change."""
    from kora_cli import web_server

    result = await web_server.list_mcp_clients()
    assert set(result.keys()) == {"clients", "generated_at", "stub"}
    assert isinstance(result["clients"], list)
    assert isinstance(result["generated_at"], str)
    assert result["stub"] is False


# ---- 3. Expected clients ----------------------------------------------


@pytest.mark.asyncio
async def test_both_expected_clients_present(_isolate_config):
    """Pin the canonical 2-client stub list (github + cloudflare). CC#1's
    KR-MCP-1 ST2 will replace the body with real catalog data — but
    the stub list shape needs to stay stable so CC#1 can swap-and-go
    without breaking the FE that ships off this PR."""
    from kora_cli import web_server

    result = await web_server.list_mcp_clients()
    names = {c["name"] for c in result["clients"]}
    assert names == _EXPECTED_CLIENTS


# ---- 4. Per-entry shape + enums --------------------------------------


@pytest.mark.asyncio
async def test_each_client_entry_has_required_keys_and_valid_enums(_isolate_config):
    from kora_cli import web_server

    result = await web_server.list_mcp_clients()
    required = {
        "name",
        "transport",
        "endpoint",
        "status",
        "auth_token_env",
        "auth_token_present",
        "allowed_tools_regex",
        "tools_count",
        # KR-MCP-CONSUMPTION ST2 additive fields
        "last_check_at",
        "last_error",
    }
    for client in result["clients"]:
        assert set(client.keys()) == required
        assert client["transport"] in _VALID_TRANSPORT
        assert client["status"] in _VALID_STATUS
        assert isinstance(client["auth_token_env"], str) and client["auth_token_env"]
        assert isinstance(client["auth_token_present"], bool)
        # allowed_tools_regex: null or string
        assert client["allowed_tools_regex"] is None or isinstance(
            client["allowed_tools_regex"], str
        )
        # tools_count: null when not connected, int when connected
        if client["status"] == "connected":
            assert isinstance(client["tools_count"], int)
        else:
            assert client["tools_count"] is None
        # ST2 additive fields: null or string
        assert client["last_check_at"] is None or isinstance(
            client["last_check_at"], str
        )
        assert client["last_error"] is None or isinstance(
            client["last_error"], str
        )


# ---- 5. SECURITY: no token-value shapes ----------------------------


@pytest.mark.asyncio
async def test_auth_token_env_carries_env_var_name_not_value(_isolate_config):
    """Bucket hard-constraint: auth_token_env is the env-var NAME
    (e.g. KORA_MCP_GITHUB_TOKEN), never the token value. Pin the
    naming convention so a future drift can't silently substitute
    a value into this field."""
    from kora_cli import web_server

    result = await web_server.list_mcp_clients()
    for client in result["clients"]:
        env_name = client["auth_token_env"]
        # Env var names: UPPER_SNAKE_CASE, ascii, no whitespace.
        # Real tokens are typically much longer + contain mixed case
        # / dashes / dots / etc. — this regex passes for any plausible
        # env-var name and fails for actual token VALUES.
        assert re.match(r"^[A-Z][A-Z0-9_]*$", env_name), (
            f"{client['name']}: auth_token_env={env_name!r} doesn't look "
            f"like an env-var name — possible token-value leak"
        )
        # Conventionally Kora's MCP-client env vars start with KORA_MCP_*.
        # Loose check so a non-Kora-prefixed env var doesn't fail the
        # test, but flag when convention diverges for review.
        assert "TOKEN" in env_name or "SECRET" in env_name or "KEY" in env_name, (
            f"{client['name']}: auth_token_env={env_name!r} doesn't carry a "
            f"token-shaped suffix — verify it's really an env-var name"
        )


_TOKEN_VALUE_KEYS = re.compile(
    r"^(token|secret|access[_-]?token|api[_-]?key|password|bearer|"
    r"auth[_-]?token(?!_env)(?!_present))$",
    re.IGNORECASE,
)


def _walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_keys(item)


@pytest.mark.asyncio
async def test_no_token_value_shaped_keys_leak_in_response(_isolate_config):
    """Belt+braces: the only auth_token_* fields allowed in the response
    shape are auth_token_env (NAME) + auth_token_present (BOOL). Any
    other token-value-shaped key (token / secret / access_token / etc.
    bare, OR auth_token without _env/_present suffix) suggests a value
    leak. Catches aggregation accidents if a future MCP-client schema
    grows token-bearing fields."""
    from kora_cli import web_server

    result = await web_server.list_mcp_clients()
    offending: list[str] = []
    for key in _walk_keys(result):
        if _TOKEN_VALUE_KEYS.search(key):
            offending.append(key)
    assert offending == [], (
        f"response contains token-value-shaped key(s): {offending} — "
        f"tokens must NEVER appear in this surface; only env-var NAME "
        f"(auth_token_env) + bool presence (auth_token_present)"
    )


# ---- 6. Status mapping (post-flip) ------------------------------------


@pytest.mark.asyncio
async def test_unhealthy_status_when_auth_env_unset(_isolate_config, monkeypatch):
    """Post-flip: with no auth env vars set (default test env), every
    endpoint reports ``unhealthy`` — auth_token_env is configured on
    both defaults so its absence is a real gap."""
    monkeypatch.delenv("KORA_MCP_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("KORA_MCP_CLOUDFLARE_TOKEN", raising=False)
    from kora_cli import web_server

    result = await web_server.list_mcp_clients()
    for client in result["clients"]:
        assert client["status"] == "unhealthy", (
            f"{client['name']}: expected unhealthy when auth env unset, "
            f"got {client['status']!r}"
        )


@pytest.mark.asyncio
async def test_configured_but_unconnected_when_auth_env_set(
    _isolate_config, monkeypatch
):
    """Post-flip: with auth env set + no open connection yet (pool
    not wired into this endpoint surface; deferred to KR-MCP-
    CONSUMPTION), status is configured_but_unconnected."""
    monkeypatch.setenv("KORA_MCP_GITHUB_TOKEN", "ghp_test_value")
    monkeypatch.setenv("KORA_MCP_CLOUDFLARE_TOKEN", "cf_test_value")
    from kora_cli import web_server

    result = await web_server.list_mcp_clients()
    statuses = {c["name"]: c["status"] for c in result["clients"]}
    assert statuses["github"] == "configured_but_unconnected"
    assert statuses["cloudflare"] == "configured_but_unconnected"


# ---- 7. auth_token_present semantics ----------------------------------


@pytest.mark.asyncio
async def test_auth_token_present_false_when_env_unset(_isolate_config, monkeypatch):
    monkeypatch.delenv("KORA_MCP_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("KORA_MCP_CLOUDFLARE_TOKEN", raising=False)
    from kora_cli import web_server

    result = await web_server.list_mcp_clients()
    for client in result["clients"]:
        assert client["auth_token_present"] is False


@pytest.mark.asyncio
async def test_auth_token_present_true_when_env_set(_isolate_config, monkeypatch):
    monkeypatch.setenv("KORA_MCP_GITHUB_TOKEN", "ghp_real_value")
    from kora_cli import web_server

    result = await web_server.list_mcp_clients()
    presence = {c["name"]: c["auth_token_present"] for c in result["clients"]}
    assert presence["github"] is True


@pytest.mark.asyncio
async def test_auth_token_present_false_when_env_empty_string(
    _isolate_config, monkeypatch
):
    """Empty string treated as unset — Doppler sometimes injects empty
    values. Matches ST2's check_endpoint_health contract."""
    monkeypatch.setenv("KORA_MCP_GITHUB_TOKEN", "")
    monkeypatch.setenv("KORA_MCP_CLOUDFLARE_TOKEN", "   \n   ")
    from kora_cli import web_server

    result = await web_server.list_mcp_clients()
    for client in result["clients"]:
        assert client["auth_token_present"] is False


# ---- 8. Operator config.yaml override respected -----------------------


@pytest.mark.asyncio
async def test_operator_override_replaces_default_by_name(
    _isolate_config, monkeypatch
):
    """Operator config.yaml entry with name=github REPLACES the default
    github entry (per ST2's load_registry_from_config contract).
    Pin that the endpoint surfaces the operator's values."""
    import yaml

    config_yaml = {
        "mcp_clients": {
            "endpoints": [
                {
                    "name": "github",
                    "transport": "streamable_http",
                    "endpoint": "https://github-mcp.internal.acme.com",
                    "auth_token_env": "ACME_GITHUB_PAT",
                }
            ]
        }
    }
    (_isolate_config / "config.yaml").write_text(yaml.safe_dump(config_yaml))

    from kora_cli import web_server

    result = await web_server.list_mcp_clients()
    github = next(c for c in result["clients"] if c["name"] == "github")
    assert github["transport"] == "streamable_http"
    assert github["endpoint"] == "https://github-mcp.internal.acme.com"
    assert github["auth_token_env"] == "ACME_GITHUB_PAT"
    # Cloudflare default still present (operator didn't override it)
    assert any(c["name"] == "cloudflare" for c in result["clients"])


@pytest.mark.asyncio
async def test_operator_can_add_new_endpoint(_isolate_config):
    import yaml

    config_yaml = {
        "mcp_clients": {
            "endpoints": [
                {
                    "name": "slack",
                    "transport": "stdio",
                    "endpoint": "npx -y @modelcontextprotocol/server-slack",
                    "auth_token_env": "KORA_MCP_SLACK_TOKEN",
                }
            ]
        }
    }
    (_isolate_config / "config.yaml").write_text(yaml.safe_dump(config_yaml))

    from kora_cli import web_server

    result = await web_server.list_mcp_clients()
    names = {c["name"] for c in result["clients"]}
    assert names == {"github", "cloudflare", "slack"}


# ---- 7. Cron-regression sanity --------------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_mcp_clients_registered(_isolate_config):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)


# ---- 9. KR-MCP-CONSUMPTION ST2 snapshot wiring ----------------------


@pytest.fixture
def _clear_consumption_state():
    """Reset the listener's singletons between snapshot tests."""
    from kora_cli.listeners.mcp_consumption import (
        _clear_health_cache,
        _clear_singleton,
    )

    _clear_singleton()
    _clear_health_cache()
    yield
    _clear_singleton()
    _clear_health_cache()


def _seed_snapshot(
    prefix: str,
    *,
    connected: bool,
    tools_count,
    last_error,
    age_seconds: int = 0,
) -> None:
    from kora_cli.listeners.mcp_consumption import HealthSnapshot, _health_cache
    from datetime import datetime, timedelta, timezone

    _health_cache[prefix] = HealthSnapshot(
        connected=connected,
        tools_count=tools_count,
        last_check_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        last_error=last_error,
    )


@pytest.mark.asyncio
async def test_snapshot_connected_surfaces_status_and_tools_count(
    _isolate_config, _clear_consumption_state, monkeypatch
):
    """Fresh connected snapshot + auth env set → status=connected +
    tools_count=N + last_check_at populated + last_error=None."""
    monkeypatch.setenv("KORA_MCP_GITHUB_TOKEN", "ghp_real")
    _seed_snapshot("github", connected=True, tools_count=7, last_error=None)

    from kora_cli import web_server

    result = await web_server.list_mcp_clients()
    github = next(c for c in result["clients"] if c["name"] == "github")
    assert github["status"] == "connected"
    assert github["tools_count"] == 7
    assert github["last_check_at"] is not None
    assert github["last_error"] is None


@pytest.mark.asyncio
async def test_snapshot_failed_surfaces_configured_but_unconnected_and_last_error(
    _isolate_config, _clear_consumption_state, monkeypatch
):
    """Failed snapshot (connected=False) + auth env set →
    status=configured_but_unconnected + tools_count=null + last_error
    surfaced."""
    monkeypatch.setenv("KORA_MCP_GITHUB_TOKEN", "ghp_real")
    _seed_snapshot(
        "github",
        connected=False,
        tools_count=None,
        last_error="MCPCallFailed: transport timeout",
    )

    from kora_cli import web_server

    result = await web_server.list_mcp_clients()
    github = next(c for c in result["clients"] if c["name"] == "github")
    assert github["status"] == "configured_but_unconnected"
    assert github["tools_count"] is None
    assert github["last_error"] == "MCPCallFailed: transport timeout"
    assert github["last_check_at"] is not None


@pytest.mark.asyncio
async def test_stale_snapshot_degrades_to_configured_but_unconnected(
    _isolate_config, _clear_consumption_state, monkeypatch
):
    """Connected snapshot OLDER than the cadence → treated as stale
    → status=configured_but_unconnected even though
    snapshot.connected=True. The heartbeat scheduler missed a cycle;
    operator sees the gap rather than a false "connected" badge."""
    from kora_cli.listeners.mcp_consumption import (
        DEFAULT_HEALTH_CHECK_INTERVAL_SEC,
    )

    monkeypatch.setenv("KORA_MCP_GITHUB_TOKEN", "ghp_real")
    # Snapshot taken 2x the cadence ago — definitively stale
    _seed_snapshot(
        "github",
        connected=True,
        tools_count=7,
        last_error=None,
        age_seconds=int(DEFAULT_HEALTH_CHECK_INTERVAL_SEC * 2),
    )

    from kora_cli import web_server

    result = await web_server.list_mcp_clients()
    github = next(c for c in result["clients"] if c["name"] == "github")
    assert github["status"] == "configured_but_unconnected"
    # tools_count still surfaces (operator can see the last-known
    # count even when stale) — verify the policy: snapshot.connected=
    # True means tools_count IS the snapshot's count; staleness only
    # affects status string.
    assert github["tools_count"] == 7
    assert github["last_check_at"] is not None


@pytest.mark.asyncio
async def test_no_snapshot_surfaces_configured_but_unconnected_with_null_fields(
    _isolate_config, _clear_consumption_state, monkeypatch
):
    """Auth env set + no snapshot in cache (daemon just started; no
    heartbeat cycle yet) → status=configured_but_unconnected +
    tools_count/last_check_at/last_error all null."""
    monkeypatch.setenv("KORA_MCP_GITHUB_TOKEN", "ghp_real")
    monkeypatch.setenv("KORA_MCP_CLOUDFLARE_TOKEN", "cf_real")
    # Don't seed any snapshot

    from kora_cli import web_server

    result = await web_server.list_mcp_clients()
    for client in result["clients"]:
        assert client["status"] == "configured_but_unconnected"
        assert client["tools_count"] is None
        assert client["last_check_at"] is None
        assert client["last_error"] is None


@pytest.mark.asyncio
async def test_unhealthy_overrides_snapshot_when_auth_env_unset(
    _isolate_config, _clear_consumption_state, monkeypatch
):
    """Auth env unset → status=unhealthy + ALL snapshot fields null
    (snapshot ignored — the auth gap is the gating signal)."""
    monkeypatch.delenv("KORA_MCP_GITHUB_TOKEN", raising=False)
    # Seed a connected snapshot — it should NOT override unhealthy
    _seed_snapshot("github", connected=True, tools_count=99, last_error=None)

    from kora_cli import web_server

    result = await web_server.list_mcp_clients()
    github = next(c for c in result["clients"] if c["name"] == "github")
    assert github["status"] == "unhealthy"
    assert github["tools_count"] is None
    assert github["last_check_at"] is None
    assert github["last_error"] is None
