"""Tests for dashboard MCP server endpoints (/api/mcp/*)."""

from pathlib import Path

import pytest
import yaml


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    """Redirect all kora_cli.config I/O to a temp directory."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("kora_cli.config.get_kora_home", lambda: tmp_path)
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    monkeypatch.setattr("kora_cli.config.get_config_path", lambda: config_path)
    monkeypatch.setattr("kora_cli.config.get_env_path", lambda: env_path)
    return tmp_path


def _seed(tmp_path: Path, mcp_servers: dict) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"mcp_servers": mcp_servers, "_config_version": 9}),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_list_mcp_servers_empty(_isolate_config):
    from kora_cli import web_server

    result = await web_server.list_mcp_servers()
    assert result == []


@pytest.mark.asyncio
async def test_list_mcp_servers_projects_http_and_stdio(_isolate_config):
    _seed(_isolate_config, {
        "ink": {
            "url": "https://mcp.example.com/mcp",
            "auth": "oauth",
            "tools": {"include": ["search", "list"]},
        },
        "local-fs": {
            "command": "uvx",
            "args": ["mcp-filesystem", "/tmp"],
            "enabled": False,
        },
    })

    from kora_cli import web_server

    result = await web_server.list_mcp_servers()
    by_name = {s["name"]: s for s in result}

    assert by_name["ink"]["transport_type"] == "http"
    assert by_name["ink"]["transport"] == "https://mcp.example.com/mcp"
    assert by_name["ink"]["enabled"] is True
    assert by_name["ink"]["auth_type"] == "oauth"
    assert by_name["ink"]["tools"]["summary"] == "2 selected"
    assert by_name["ink"]["tools"]["include"] == ["search", "list"]

    assert by_name["local-fs"]["transport_type"] == "stdio"
    assert by_name["local-fs"]["transport"].startswith("uvx ")
    assert by_name["local-fs"]["enabled"] is False
    assert by_name["local-fs"]["tools"]["summary"] == "all"


@pytest.mark.asyncio
async def test_get_mcp_server_404(_isolate_config):
    from fastapi import HTTPException
    from kora_cli import web_server

    with pytest.raises(HTTPException) as exc:
        await web_server.get_mcp_server("does-not-exist")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_set_mcp_server_tools_drops_block_when_all_enabled(_isolate_config):
    _seed(_isolate_config, {
        "ink": {
            "url": "https://mcp.example.com/mcp",
            "tools": {"include": ["search"]},
        },
    })

    from kora_cli import web_server
    from kora_cli.mcp_config import _get_mcp_servers

    body = web_server.MCPToolsUpdate(
        enabled_tools=["search", "list", "fetch"],
        all_tools=["search", "list", "fetch"],
    )
    updated = await web_server.set_mcp_server_tools("ink", body)

    assert updated["tools"]["summary"] == "all"
    assert updated["tools"]["include"] is None

    on_disk = _get_mcp_servers()["ink"]
    assert "tools" not in on_disk


@pytest.mark.asyncio
async def test_set_mcp_server_tools_writes_include_subset(_isolate_config):
    _seed(_isolate_config, {
        "ink": {"url": "https://mcp.example.com/mcp"},
    })

    from kora_cli import web_server
    from kora_cli.mcp_config import _get_mcp_servers

    body = web_server.MCPToolsUpdate(
        enabled_tools=["search"],
        all_tools=["search", "list", "fetch"],
    )
    updated = await web_server.set_mcp_server_tools("ink", body)

    assert updated["tools"]["include"] == ["search"]
    assert updated["tools"]["summary"] == "1 selected"

    on_disk = _get_mcp_servers()["ink"]
    assert on_disk["tools"]["include"] == ["search"]
    assert "exclude" not in on_disk["tools"]


@pytest.mark.asyncio
async def test_set_mcp_server_tools_clears_stale_exclude(_isolate_config):
    _seed(_isolate_config, {
        "ink": {
            "url": "https://mcp.example.com/mcp",
            "tools": {"exclude": ["fetch"]},
        },
    })

    from kora_cli import web_server
    from kora_cli.mcp_config import _get_mcp_servers

    body = web_server.MCPToolsUpdate(
        enabled_tools=["search"],
        all_tools=["search", "list"],
    )
    await web_server.set_mcp_server_tools("ink", body)

    on_disk = _get_mcp_servers()["ink"]
    assert on_disk["tools"] == {"include": ["search"]}


@pytest.mark.asyncio
async def test_set_mcp_server_tools_rejects_unknown_tool(_isolate_config):
    _seed(_isolate_config, {
        "ink": {"url": "https://mcp.example.com/mcp"},
    })

    from fastapi import HTTPException
    from kora_cli import web_server

    body = web_server.MCPToolsUpdate(
        enabled_tools=["search", "not-a-real-tool"],
        all_tools=["search", "list"],
    )
    with pytest.raises(HTTPException) as exc:
        await web_server.set_mcp_server_tools("ink", body)
    assert exc.value.status_code == 400
    assert "not-a-real-tool" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_enable_disable_mcp_server_round_trip(_isolate_config):
    _seed(_isolate_config, {
        "ink": {"url": "https://mcp.example.com/mcp", "enabled": True},
    })

    from kora_cli import web_server
    from kora_cli.mcp_config import _get_mcp_servers

    after_disable = await web_server.disable_mcp_server("ink")
    assert after_disable["enabled"] is False
    assert _get_mcp_servers()["ink"]["enabled"] is False

    after_enable = await web_server.enable_mcp_server("ink")
    assert after_enable["enabled"] is True
    assert _get_mcp_servers()["ink"]["enabled"] is True


@pytest.mark.asyncio
async def test_probe_mcp_server_wraps_failure_as_502(_isolate_config, monkeypatch):
    _seed(_isolate_config, {
        "ink": {"url": "https://mcp.example.com/mcp"},
    })

    from fastapi import HTTPException
    from kora_cli import web_server

    def _boom(name, cfg, **_kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(
        "kora_cli.mcp_config._probe_single_server", _boom
    )

    with pytest.raises(HTTPException) as exc:
        await web_server.probe_mcp_server("ink")
    assert exc.value.status_code == 502
    assert "connection refused" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_probe_mcp_server_returns_tool_list(_isolate_config, monkeypatch):
    _seed(_isolate_config, {
        "ink": {"url": "https://mcp.example.com/mcp"},
    })

    from kora_cli import web_server

    monkeypatch.setattr(
        "kora_cli.mcp_config._probe_single_server",
        lambda name, cfg, **kw: [
            ("search", "Run a search query"),
            ("list", "List resources"),
        ],
    )

    result = await web_server.probe_mcp_server("ink")
    assert result["name"] == "ink"
    assert [t["name"] for t in result["tools"]] == ["search", "list"]
    assert result["tools"][0]["description"] == "Run a search query"
    assert isinstance(result["elapsed_ms"], int)
