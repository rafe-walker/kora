"""Tests for the KR-MCP-3 stub endpoint.

Bucket §4 scenarios:
  1. GET /api/mcp/clients/list returns 200
  2. Top-level shape (clients + generated_at + stub:true)
  3. Both expected clients present (github + cloudflare)
  4. Each client entry has the required keys + valid status/transport enums
  5. SECURITY: auth_token_env carries env-var NAME only, not value;
     auth_token_present is bool; no token-value-shaped field leaks
  6. Cron-regression sanity
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
    from kora_cli import web_server

    result = await web_server.list_mcp_clients()
    assert set(result.keys()) == {"clients", "generated_at", "stub"}
    assert isinstance(result["clients"], list)
    assert isinstance(result["generated_at"], str)
    assert result["stub"] is True


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


# ---- 6. Bucket §3 stub values pinned --------------------------------


@pytest.mark.asyncio
async def test_stub_returns_configured_but_unconnected_for_all_clients(_isolate_config):
    """The bucket §3 stub pins both clients as configured_but_unconnected
    (since stub can't actually open a connection). Dashboard "0 connected"
    aggregate count depends on this; pin it so a future stub edit
    can't silently flip the count."""
    from kora_cli import web_server

    result = await web_server.list_mcp_clients()
    for client in result["clients"]:
        assert client["status"] == "configured_but_unconnected", (
            f"{client['name']}: expected configured_but_unconnected in "
            f"stub, got {client['status']!r}"
        )


@pytest.mark.asyncio
async def test_stub_returns_auth_token_present_false_for_all_clients(_isolate_config):
    """Stub doesn't check real env vars; pins auth_token_present:false
    so the FE renders the red-x indicator for all clients. CC#1's
    KR-MCP-1 ST2 will resolve real env-var presence."""
    from kora_cli import web_server

    result = await web_server.list_mcp_clients()
    for client in result["clients"]:
        assert client["auth_token_present"] is False


# ---- 7. Cron-regression sanity --------------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_mcp_clients_registered(_isolate_config):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
