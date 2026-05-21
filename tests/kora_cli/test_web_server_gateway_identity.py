"""Tests for the KR-P2-G gateway platform identity endpoints.

These cover the bucket §6 scenarios:
  1. List default platforms
  2. Get specific platform
  3. PUT sets display_name
  4. PUT empty string clears override
  5. PUT validation: length
  6. PUT validation: newlines
  7. PUT against orphan
  8. PUT against unconfigured-but-supported
  9. Cron-regression sanity
"""

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


def _seed_platforms(tmp_path: Path, platforms: dict) -> None:
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"platforms": platforms, "_config_version": 9}),
        encoding="utf-8",
    )


def _read_yaml_platforms(tmp_path: Path) -> dict:
    raw = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(raw).get("platforms", {})


# ---- 1. List default platforms --------------------------------------------


@pytest.mark.asyncio
async def test_list_default_platforms_includes_configured_and_supported(_isolate_config):
    _seed_platforms(_isolate_config, {
        "slack": {"enabled": True, "token": "xoxb-fake", "display_name": "DevKora"},
    })

    from kora_cli import web_server

    result = await web_server.list_gateway_platforms()
    by_id = {row["platform_id"]: row for row in result}

    # Slack is in config + has an adapter
    assert by_id["slack"]["display_name"] == "DevKora"
    assert by_id["slack"]["display_name_source"] == "config"
    assert by_id["slack"]["supported"] is True
    assert by_id["slack"]["enabled"] is True
    assert by_id["slack"]["token_status"] == "configured"

    # An adapter that exists but isn't in config — e.g. telegram — should
    # appear with display_name_source=default and enabled=false.
    assert "telegram" in by_id
    assert by_id["telegram"]["display_name_source"] == "default"
    assert by_id["telegram"]["display_name"] == "Kora"
    assert by_id["telegram"]["enabled"] is False
    assert by_id["telegram"]["supported"] is True
    assert by_id["telegram"]["token_status"] == "missing"


@pytest.mark.asyncio
async def test_list_resolves_display_name_from_extra_block(_isolate_config):
    _seed_platforms(_isolate_config, {
        "discord": {"enabled": True, "extra": {"display_name": "DiscordKora"}},
    })

    from kora_cli import web_server

    result = await web_server.list_gateway_platforms()
    discord = next(r for r in result if r["platform_id"] == "discord")

    assert discord["display_name"] == "DiscordKora"
    assert discord["display_name_source"] == "extra"
    assert discord["extra_keys"] == ["display_name"]


@pytest.mark.asyncio
async def test_list_token_status_distinguishes_env_referenced(_isolate_config):
    _seed_platforms(_isolate_config, {
        "telegram": {"enabled": True, "token": "${TELEGRAM_BOT_TOKEN}"},
    })

    from kora_cli import web_server

    result = await web_server.list_gateway_platforms()
    telegram = next(r for r in result if r["platform_id"] == "telegram")

    assert telegram["token_status"] == "env_referenced"


# ---- 2. Get specific platform ---------------------------------------------


@pytest.mark.asyncio
async def test_get_specific_platform_returns_200(_isolate_config):
    _seed_platforms(_isolate_config, {
        "slack": {"enabled": True, "display_name": "Architect"},
    })

    from kora_cli import web_server

    result = await web_server.get_gateway_platform("slack")
    assert result["platform_id"] == "slack"
    assert result["display_name"] == "Architect"


@pytest.mark.asyncio
async def test_get_specific_platform_404_for_unknown(_isolate_config):
    from fastapi import HTTPException
    from kora_cli import web_server

    with pytest.raises(HTTPException) as exc:
        await web_server.get_gateway_platform("never_existed_xyz")
    assert exc.value.status_code == 404


# ---- 3. PUT sets display_name ---------------------------------------------


@pytest.mark.asyncio
async def test_put_sets_display_name_and_roundtrips_in_yaml(_isolate_config):
    _seed_platforms(_isolate_config, {"slack": {"enabled": True}})

    from kora_cli import web_server
    from gateway.config import PlatformConfig

    body = web_server.GatewayPlatformIdentityUpdate(display_name="TestKora")
    result = await web_server.set_gateway_platform_identity("slack", body)

    assert result["display_name"] == "TestKora"
    assert result["display_name_source"] == "config"

    # YAML round-trip
    on_disk = _read_yaml_platforms(_isolate_config)
    assert on_disk["slack"]["display_name"] == "TestKora"

    # And the canonical resolver agrees
    resolved = PlatformConfig.from_dict(on_disk["slack"])
    assert resolved.display_name == "TestKora"


# ---- 4. PUT empty string clears override ----------------------------------


@pytest.mark.asyncio
async def test_put_empty_string_clears_override(_isolate_config):
    _seed_platforms(_isolate_config, {
        "slack": {"enabled": True, "display_name": "TestKora"},
    })

    from kora_cli import web_server
    from gateway.config import PlatformConfig

    body = web_server.GatewayPlatformIdentityUpdate(display_name="")
    result = await web_server.set_gateway_platform_identity("slack", body)

    assert result["display_name"] == "Kora"
    assert result["display_name_source"] == "default"

    on_disk = _read_yaml_platforms(_isolate_config)
    # Bucket §4: writes null so the from_dict fallback kicks in.
    assert "display_name" in on_disk["slack"]
    assert on_disk["slack"]["display_name"] is None

    # PlatformConfig resolves null → "Kora" via the not-a-string check.
    resolved = PlatformConfig.from_dict(on_disk["slack"])
    assert resolved.display_name == "Kora"


@pytest.mark.asyncio
async def test_put_whitespace_only_is_treated_as_clear(_isolate_config):
    _seed_platforms(_isolate_config, {
        "slack": {"enabled": True, "display_name": "TestKora"},
    })

    from kora_cli import web_server

    body = web_server.GatewayPlatformIdentityUpdate(display_name="   \t  ")
    result = await web_server.set_gateway_platform_identity("slack", body)

    assert result["display_name_source"] == "default"
    assert result["display_name"] == "Kora"


# ---- 5. PUT validation: length --------------------------------------------


@pytest.mark.asyncio
async def test_put_rejects_oversized_display_name(_isolate_config):
    _seed_platforms(_isolate_config, {"slack": {"enabled": True}})

    from fastapi import HTTPException
    from kora_cli import web_server

    body = web_server.GatewayPlatformIdentityUpdate(display_name="X" * 65)
    with pytest.raises(HTTPException) as exc:
        await web_server.set_gateway_platform_identity("slack", body)

    assert exc.value.status_code == 400
    assert exc.value.detail == {
        "field": "display_name",
        "error": "must be 64 bytes or fewer (UTF-8)",
    }

    # Reject means original config unchanged
    on_disk = _read_yaml_platforms(_isolate_config)
    assert "display_name" not in on_disk.get("slack", {})


# ---- 6. PUT validation: newlines ------------------------------------------


@pytest.mark.asyncio
async def test_put_rejects_newlines_in_display_name(_isolate_config):
    _seed_platforms(_isolate_config, {"slack": {"enabled": True}})

    from fastapi import HTTPException
    from kora_cli import web_server

    body = web_server.GatewayPlatformIdentityUpdate(display_name="foo\nbar")
    with pytest.raises(HTTPException) as exc:
        await web_server.set_gateway_platform_identity("slack", body)

    assert exc.value.status_code == 400
    assert exc.value.detail["field"] == "display_name"
    assert "newlines" in exc.value.detail["error"]


@pytest.mark.asyncio
async def test_put_rejects_non_string_display_name(_isolate_config):
    _seed_platforms(_isolate_config, {"slack": {"enabled": True}})

    from fastapi import HTTPException
    from kora_cli import web_server

    body = web_server.GatewayPlatformIdentityUpdate(display_name=42)
    with pytest.raises(HTTPException) as exc:
        await web_server.set_gateway_platform_identity("slack", body)

    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "must be a string"


# ---- 7. PUT against orphan ------------------------------------------------


@pytest.mark.asyncio
async def test_put_against_orphan_returns_409(_isolate_config):
    _seed_platforms(_isolate_config, {
        "legacy_xmpp": {"enabled": False, "display_name": "Stale"},
    })

    from fastapi import HTTPException
    from kora_cli import web_server

    body = web_server.GatewayPlatformIdentityUpdate(display_name="NewName")
    with pytest.raises(HTTPException) as exc:
        await web_server.set_gateway_platform_identity("legacy_xmpp", body)

    assert exc.value.status_code == 409
    assert exc.value.detail["platform_id"] == "legacy_xmpp"


@pytest.mark.asyncio
async def test_list_marks_orphan_as_supported_false(_isolate_config):
    _seed_platforms(_isolate_config, {
        "legacy_xmpp": {"enabled": False, "display_name": "Stale"},
    })

    from kora_cli import web_server

    result = await web_server.list_gateway_platforms()
    orphan = next(r for r in result if r["platform_id"] == "legacy_xmpp")
    assert orphan["supported"] is False
    assert orphan["display_name"] == "Stale"


# ---- 8. PUT against unconfigured-but-supported ----------------------------


@pytest.mark.asyncio
async def test_put_against_unconfigured_supported_creates_entry(_isolate_config):
    # Start with NO platforms block — telegram has an adapter but isn't in YAML.
    (_isolate_config / "config.yaml").write_text(
        yaml.safe_dump({"_config_version": 9}), encoding="utf-8"
    )

    from kora_cli import web_server

    body = web_server.GatewayPlatformIdentityUpdate(display_name="TelegramKora")
    result = await web_server.set_gateway_platform_identity("telegram", body)

    assert result["display_name"] == "TelegramKora"
    assert result["display_name_source"] == "config"
    assert result["supported"] is True

    # YAML now has a platforms block with telegram + just display_name
    on_disk = _read_yaml_platforms(_isolate_config)
    assert on_disk["telegram"]["display_name"] == "TelegramKora"


# ---- 9. Cron-regression sanity --------------------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_new_routes_registered(_isolate_config):
    """Catch import-time regressions: the new endpoints must not break
    sibling routes when web_server is imported."""
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
