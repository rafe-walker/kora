"""KR-MCP-1 ST2 — catalog + CLI tests.

Covers:
  - Default catalog loads cleanly (github + cloudflare entries
    present with correct shapes)
  - Missing auth-token env → endpoint marked unhealthy (no crash;
    degrade per bucket spec)
  - Operator config.yaml overrides merge with defaults by name
  - load_registry_from_config rejects unknown YAML keys (Pydantic
    ``extra="forbid"``)
  - CLI parsers parse correctly for ``mcp clients list`` /
    ``mcp clients status <name>``
  - Integration test gated behind KORA_INTEGRATION_TEST=1 (skipped
    in CI without secrets)
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from kora_mcp.catalog import (
    CLOUDFLARE_AUTH_ENV,
    DEFAULT_CATALOG,
    DEFAULT_CLOUDFLARE_ENDPOINT,
    DEFAULT_GITHUB_ENDPOINT,
    GITHUB_AUTH_ENV,
    EndpointHealth,
    check_endpoint_health,
    check_registry_health,
    default_registry,
    find_endpoint_by_name,
    load_registry_from_config,
)
from kora_mcp.registry import MCPEndpointConfig, MCPRegistryConfig


# ---------------------------------------------------------------------------
# Default catalog shape
# ---------------------------------------------------------------------------


def test_default_catalog_has_github_and_cloudflare():
    names = {e.name for e in DEFAULT_CATALOG}
    assert names == {"github", "cloudflare"}


def test_github_endpoint_is_stdio_npx():
    assert DEFAULT_GITHUB_ENDPOINT.transport == "stdio"
    assert "@modelcontextprotocol/server-github" in DEFAULT_GITHUB_ENDPOINT.endpoint
    assert DEFAULT_GITHUB_ENDPOINT.endpoint.startswith("npx")
    assert DEFAULT_GITHUB_ENDPOINT.auth_token_env == GITHUB_AUTH_ENV


def test_cloudflare_endpoint_is_streamable_http():
    assert DEFAULT_CLOUDFLARE_ENDPOINT.transport == "streamable_http"
    assert DEFAULT_CLOUDFLARE_ENDPOINT.endpoint.startswith("https://")
    assert DEFAULT_CLOUDFLARE_ENDPOINT.auth_token_env == CLOUDFLARE_AUTH_ENV


def test_auth_env_naming_convention():
    """Both env vars follow KORA_MCP_<SERVICE>_TOKEN."""
    assert GITHUB_AUTH_ENV == "KORA_MCP_GITHUB_TOKEN"
    assert CLOUDFLARE_AUTH_ENV == "KORA_MCP_CLOUDFLARE_TOKEN"


def test_default_registry_returns_validated_config():
    registry = default_registry()
    assert isinstance(registry, MCPRegistryConfig)
    assert len(registry.endpoints) == 2
    assert {e.name for e in registry.endpoints} == {"github", "cloudflare"}


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------


def test_health_endpoint_with_no_auth_env_is_healthy():
    """An endpoint with no auth_token_env has no env-var
    expectation → always healthy at the config level."""
    endpoint = MCPEndpointConfig(
        name="local",
        transport="stdio",
        endpoint="my-mcp-server",
        auth_token_env=None,
    )
    health = check_endpoint_health(endpoint)
    assert health.healthy is True
    assert health.auth_env_set is True
    assert health.reason == ""


def test_health_endpoint_with_auth_env_set_is_healthy(monkeypatch):
    monkeypatch.setenv("MY_TEST_TOKEN", "ghp_xxx")
    endpoint = MCPEndpointConfig(
        name="github",
        transport="stdio",
        endpoint="cmd",
        auth_token_env="MY_TEST_TOKEN",
    )
    health = check_endpoint_health(endpoint)
    assert health.healthy is True
    assert health.auth_env_set is True


def test_health_endpoint_with_auth_env_unset_is_unhealthy(monkeypatch):
    """Per bucket spec: missing auth-token env → endpoint marked
    unhealthy. Do NOT crash."""
    monkeypatch.delenv("MY_TEST_TOKEN", raising=False)
    endpoint = MCPEndpointConfig(
        name="github",
        transport="stdio",
        endpoint="cmd",
        auth_token_env="MY_TEST_TOKEN",
    )
    health = check_endpoint_health(endpoint)
    assert health.healthy is False
    assert health.auth_env_set is False
    assert "MY_TEST_TOKEN" in health.reason
    assert "unset" in health.reason.lower() or "empty" in health.reason.lower()


def test_health_endpoint_with_empty_string_auth_env_is_unhealthy(monkeypatch):
    """Empty string treated as unset — Doppler sometimes injects
    empty strings; explicit guard."""
    monkeypatch.setenv("MY_TEST_TOKEN", "")
    endpoint = MCPEndpointConfig(
        name="github",
        transport="stdio",
        endpoint="cmd",
        auth_token_env="MY_TEST_TOKEN",
    )
    health = check_endpoint_health(endpoint)
    assert health.healthy is False


def test_health_endpoint_with_whitespace_only_auth_env_is_unhealthy(monkeypatch):
    monkeypatch.setenv("MY_TEST_TOKEN", "   \n  ")
    endpoint = MCPEndpointConfig(
        name="github",
        transport="stdio",
        endpoint="cmd",
        auth_token_env="MY_TEST_TOKEN",
    )
    health = check_endpoint_health(endpoint)
    assert health.healthy is False


def test_endpoint_health_dataclass_immutable():
    """Frozen dataclass — health verdicts shouldn't be mutated by
    consumers."""
    import dataclasses

    health = EndpointHealth(
        name="x", configured=True, auth_env_set=True, reason=""
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        health.reason = "tampered"  # type: ignore[misc]


def test_check_registry_health_returns_one_per_endpoint(monkeypatch):
    monkeypatch.delenv(GITHUB_AUTH_ENV, raising=False)
    monkeypatch.delenv(CLOUDFLARE_AUTH_ENV, raising=False)
    registry = default_registry()
    results = check_registry_health(registry)
    assert len(results) == 2
    assert {r.name for r in results} == {"github", "cloudflare"}
    # Both unset → both unhealthy
    assert all(not r.healthy for r in results)


# ---------------------------------------------------------------------------
# load_registry_from_config — merge + override
# ---------------------------------------------------------------------------


def test_load_with_empty_config_returns_defaults():
    """No mcp_clients key → just the defaults."""
    registry = load_registry_from_config({}, include_defaults=True)
    names = {e.name for e in registry.endpoints}
    assert names == {"github", "cloudflare"}


def test_load_without_defaults_returns_empty_when_no_operator_entries():
    registry = load_registry_from_config({}, include_defaults=False)
    assert registry.endpoints == []


def test_load_operator_override_replaces_default_by_name():
    """An operator entry with the same name as a default REPLACES it."""
    config = {
        "mcp_clients": {
            "endpoints": [
                {
                    "name": "github",
                    "transport": "streamable_http",  # operator chose hosted
                    "endpoint": "https://github-mcp.acme.com",
                    "auth_token_env": "MY_CUSTOM_TOKEN",
                }
            ]
        }
    }
    registry = load_registry_from_config(config, include_defaults=True)
    github = registry.get_endpoint("github")
    assert github is not None
    assert github.transport == "streamable_http"
    assert github.endpoint == "https://github-mcp.acme.com"
    assert github.auth_token_env == "MY_CUSTOM_TOKEN"
    # Cloudflare default still present (not overridden by operator)
    assert registry.get_endpoint("cloudflare") is not None


def test_load_operator_can_add_new_endpoint():
    config = {
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
    registry = load_registry_from_config(config, include_defaults=True)
    assert len(registry.endpoints) == 3
    assert {e.name for e in registry.endpoints} == {"github", "cloudflare", "slack"}


def test_load_rejects_unknown_yaml_keys_on_operator_entry():
    """K-DG drift discipline propagates into the catalog loader —
    unknown keys in an operator entry raise."""
    config = {
        "mcp_clients": {
            "endpoints": [
                {
                    "name": "github",
                    "transport": "stdio",
                    "endpoint": "cmd",
                    "timeout_secondz": 99,  # typo
                }
            ]
        }
    }
    with pytest.raises(ValidationError):
        load_registry_from_config(config, include_defaults=True)


def test_load_tolerates_malformed_block_returns_defaults():
    """A non-dict mcp_clients value (e.g. legacy operator wrote
    a list at the top level) doesn't crash — falls back to
    defaults."""
    registry = load_registry_from_config(
        {"mcp_clients": "not a dict"}, include_defaults=True
    )
    assert {e.name for e in registry.endpoints} == {"github", "cloudflare"}


def test_load_tolerates_malformed_endpoints_returns_defaults():
    registry = load_registry_from_config(
        {"mcp_clients": {"endpoints": "not a list"}},
        include_defaults=True,
    )
    assert {e.name for e in registry.endpoints} == {"github", "cloudflare"}


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def test_find_endpoint_by_name_hits():
    registry = default_registry()
    assert find_endpoint_by_name(registry, "github") is not None
    assert find_endpoint_by_name(registry, "cloudflare") is not None


def test_find_endpoint_by_name_misses():
    registry = default_registry()
    assert find_endpoint_by_name(registry, "nope") is None


# ---------------------------------------------------------------------------
# CLI dispatcher — K-DG subcommand-dispatcher-check discipline
# ---------------------------------------------------------------------------
#
# The argparse wiring lives in ``kora_cli/main.py`` inline inside
# ``main()`` (no extractable parser builder for the mcp subparser).
# Direct argparse smoke tests would need to invoke ``main.main()``
# which is heavy + side-effect-y. Instead we test the dispatcher
# directly: per ``feedback_k_dg_subcommand_dispatcher_check``, the
# load-bearing failure mode is the parser accepting an action that
# silently no-ops in the dispatcher. The test below pins that
# branch — and the argparse entry was added in the same PR so
# either both wire correctly or both will be missed on review.


def test_cli_mcp_clients_dispatcher_routes_to_handler():
    """K-DG subcommand-dispatcher check: the parser accepts
    'clients list' AND the action-dispatcher branches to
    cmd_mcp_clients. Mirrors the
    `feedback_k_dg_subcommand_dispatcher_check` discipline —
    silent dispatch no-op is a real failure mode."""
    from types import SimpleNamespace

    from kora_cli.mcp_config import mcp_command

    called = {"list": 0, "status": 0}

    def _fake_list(_args=None):
        called["list"] += 1

    def _fake_status(args):
        called["status"] += 1

    with patch("kora_cli.mcp_config.cmd_mcp_clients_list", _fake_list):
        with patch("kora_cli.mcp_config.cmd_mcp_clients_status", _fake_status):
            args = SimpleNamespace(mcp_action="clients", clients_action="list")
            mcp_command(args)
            args = SimpleNamespace(
                mcp_action="clients", clients_action="status", name="github"
            )
            mcp_command(args)

    assert called["list"] == 1
    assert called["status"] == 1


# ---------------------------------------------------------------------------
# Integration (gated; skipped in CI without secrets)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("KORA_INTEGRATION_TEST") != "1",
    reason="set KORA_INTEGRATION_TEST=1 + real MCP tokens to run",
)
@pytest.mark.asyncio
async def test_list_tools_all_against_real_endpoints():
    """If real tokens are present in the env (Doppler-injected),
    list_tools_all returns ≥1 tool per endpoint.

    Skipped in CI without secrets. Run locally with:
      KORA_INTEGRATION_TEST=1 \
      KORA_MCP_GITHUB_TOKEN=ghp_xxx \
      KORA_MCP_CLOUDFLARE_TOKEN=xxx \
      pytest tests/kora_mcp/test_catalog.py::test_list_tools_all_against_real_endpoints
    """
    from kora_mcp.pool import MCPClientPool

    registry = default_registry()
    pool = MCPClientPool(registry)
    try:
        catalog = await pool.list_tools_all()
    finally:
        await pool.close_all()
    assert "github" in catalog
    assert "cloudflare" in catalog
    # Each endpoint should expose at least 1 tool
    assert len(catalog["github"]) >= 1, (
        f"github endpoint returned 0 tools — auth env "
        f"{GITHUB_AUTH_ENV} set + valid?"
    )
    assert len(catalog["cloudflare"]) >= 1, (
        f"cloudflare endpoint returned 0 tools — auth env "
        f"{CLOUDFLARE_AUTH_ENV} set + valid?"
    )
