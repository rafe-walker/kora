"""KR-2 ST1 — IsoKron memory provider skeleton smokes.

Verifies the structural skeleton:

1. Plugin module loads + ``register(ctx)`` calls
   ``ctx.register_memory_provider`` on the fake collector.
2. ``IsoKronMemoryProvider`` instantiates with a minimally-valid config.
3. ``is_available()`` returns True with config + deps; False without.
4. ``initialize(session_id)`` starts the IO loop without crashing.
5. ``shutdown()`` cleans up.
6. Config validation: missing required keys raise ``ValidationError``;
   bad DSN scheme raises; bad MCP transport raises.
7. Every ST2-ST4 stub raises ``NotImplementedError`` with a Rule-6
   ``[kora.isokron.todo]`` message.
8. ``get_tool_schemas()`` returns empty list at ST1 (no tools yet).
9. ``get_config_schema()`` returns the seeded schema (6 fields).

Tests intentionally do NOT open Postgres or MCP connections; that's
ST2 / ST3 territory.
"""

from __future__ import annotations

import importlib
import pytest

# Pydantic v2 raises pydantic.ValidationError for field errors.
from pydantic import ValidationError


# ---------------------------------------------------------------------------
# Plugin discovery + register()
# ---------------------------------------------------------------------------


class _FakeProviderCollector:
    """Mirrors plugins/memory/__init__.py:_ProviderCollector."""

    def __init__(self):
        self.provider = None

    def register_memory_provider(self, provider):
        self.provider = provider


def test_plugin_module_imports():
    """The plugin's ``__init__.py`` imports without crashing."""
    mod = importlib.import_module("plugins.memory.isokron")
    assert hasattr(mod, "register")
    assert hasattr(mod, "IsoKronMemoryProvider")


def test_register_calls_collector(monkeypatch):
    """register(ctx) instantiates the provider and registers it.

    Config-less invocation (no config.yaml block) yields a provider whose
    is_available() returns False — but registration itself still succeeds.
    """
    # Pretend config.yaml has no isokron block.
    from plugins.memory import isokron as plugin_pkg

    monkeypatch.setattr(plugin_pkg, "_load_plugin_config", lambda: None)
    ctx = _FakeProviderCollector()
    plugin_pkg.register(ctx)
    assert ctx.provider is not None
    assert ctx.provider.name == "isokron"
    # Config-less → not available.
    assert ctx.provider.is_available() is False


# ---------------------------------------------------------------------------
# Construct + lifecycle
# ---------------------------------------------------------------------------


def _minimal_config() -> dict:
    return {
        "isokron_dsn": "postgres://kora:secret@localhost:5432/isokron",
        "mcp_endpoint": "stdio://node ./sea-mcp-server.js",
    }


def test_construct_with_valid_config():
    from plugins.memory.isokron.provider import IsoKronMemoryProvider

    provider = IsoKronMemoryProvider(config=_minimal_config())
    assert provider.name == "isokron"
    # Available IFF config parsed + asyncpg + mcp importable.
    assert provider.is_available() is True


def test_construct_without_config_is_unavailable():
    from plugins.memory.isokron.provider import IsoKronMemoryProvider

    provider = IsoKronMemoryProvider(config=None)
    assert provider.is_available() is False


def test_initialize_starts_io_loop():
    from plugins.memory.isokron.provider import IsoKronMemoryProvider

    provider = IsoKronMemoryProvider(config=_minimal_config())
    provider.initialize(session_id="test-session-001", platform="cli")
    assert provider._initialized is True
    assert provider._connection is not None
    assert provider._connection.is_started is True
    provider.shutdown()
    assert provider._initialized is False
    assert provider._connection.is_started is False


def test_shutdown_is_idempotent():
    from plugins.memory.isokron.provider import IsoKronMemoryProvider

    provider = IsoKronMemoryProvider(config=_minimal_config())
    provider.initialize(session_id="t1")
    provider.shutdown()
    # Second shutdown must not raise.
    provider.shutdown()


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_config_rejects_missing_required():
    from plugins.memory.isokron.config import IsoKronProviderConfig

    with pytest.raises(ValidationError):
        IsoKronProviderConfig(mcp_endpoint="stdio://x")  # missing dsn
    with pytest.raises(ValidationError):
        IsoKronProviderConfig(isokron_dsn="postgres://x/y")  # missing mcp


def test_config_rejects_non_postgres_dsn():
    from plugins.memory.isokron.config import IsoKronProviderConfig

    with pytest.raises(ValidationError) as excinfo:
        IsoKronProviderConfig(
            isokron_dsn="mysql://kora@localhost/isokron",
            mcp_endpoint="stdio://x",
        )
    assert "postgres" in str(excinfo.value).lower()


def test_config_rejects_unknown_mcp_transport():
    from plugins.memory.isokron.config import IsoKronProviderConfig

    with pytest.raises(ValidationError) as excinfo:
        IsoKronProviderConfig(
            isokron_dsn="postgres://kora@localhost/isokron",
            mcp_endpoint="ftp://wat",
        )
    assert "stdio" in str(excinfo.value).lower() or "transport" in str(excinfo.value).lower()


def test_config_cache_ttl_bounded():
    from plugins.memory.isokron.config import IsoKronProviderConfig

    with pytest.raises(ValidationError):
        IsoKronProviderConfig(
            isokron_dsn="postgres://x@y/z",
            mcp_endpoint="stdio://x",
            cache_ttl_seconds=10000,  # exceeds le=3600
        )


def test_config_rejects_unknown_keys():
    """Pydantic ``extra='forbid'`` catches operator typos at load time."""
    from plugins.memory.isokron.config import IsoKronProviderConfig

    # Use model_validate() with a raw dict so the typo isn't flagged
    # by static analysis — the runtime validator catching it IS what
    # this test asserts on.
    with pytest.raises(ValidationError):
        IsoKronProviderConfig.model_validate({
            "isokron_dsn": "postgres://x@y/z",
            "mcp_endpoint": "stdio://x",
            "isokrn_dns": "typo",  # intentional misspelling
        })


def test_env_var_expansion(monkeypatch):
    """``${VAR}`` in string config values gets expanded by the loader."""
    from plugins.memory.isokron import _expand_env_vars

    monkeypatch.setenv("KORA_TEST_DB_PASSWORD", "s3cret")
    expanded = _expand_env_vars(
        {"isokron_dsn": "postgres://kora:${KORA_TEST_DB_PASSWORD}@db:5432/isokron"}
    )
    assert expanded["isokron_dsn"] == "postgres://kora:s3cret@db:5432/isokron"


def test_env_var_expansion_missing_leaves_literal(monkeypatch):
    """Missing env vars leave the ``${VAR}`` token in place (surfaces validator)."""
    from plugins.memory.isokron import _expand_env_vars

    monkeypatch.delenv("KORA_TEST_UNSET_VAR", raising=False)
    expanded = _expand_env_vars({"x": "before/${KORA_TEST_UNSET_VAR}/after"})
    assert expanded["x"] == "before/${KORA_TEST_UNSET_VAR}/after"


# ---------------------------------------------------------------------------
# ST1 stubs all raise with Rule-6 honest label
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method, args, kwargs",
    [
        ("system_prompt_block", (), {}),
        ("prefetch", ("q",), {"session_id": "s"}),
        ("queue_prefetch", ("q",), {"session_id": "s"}),
        ("sync_turn", ("u", "a"), {"session_id": "s"}),
        ("handle_tool_call", ("t", {}), {}),
        ("on_turn_start", (1, "msg"), {}),
        ("on_session_end", ([],), {}),
        ("on_session_switch", ("new-id",), {"reset": True}),
        ("on_pre_compress", ([],), {}),
        ("on_delegation", ("task", "result"), {"child_session_id": "c"}),
        ("on_memory_write", ("add", "memory", "content"), {}),
        ("save_config", ({"key": "val"}, "/tmp/kora-home"), {}),
    ],
)
def test_stub_method_raises_with_rule6_message(method, args, kwargs):
    """Every ST1 stub raises NotImplementedError tagged ``[kora.isokron.todo]``."""
    from plugins.memory.isokron.provider import IsoKronMemoryProvider

    provider = IsoKronMemoryProvider(config=_minimal_config())
    fn = getattr(provider, method)
    with pytest.raises(NotImplementedError) as excinfo:
        fn(*args, **kwargs)
    assert "[kora.isokron.todo]" in str(excinfo.value), (
        f"{method} missing Rule-6 todo tag: {excinfo.value}"
    )
    assert "KR-2" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Static metadata
# ---------------------------------------------------------------------------


def test_tool_schemas_empty_at_st1():
    """No iso_node_* / iso_link_* tools surface until KR-3."""
    from plugins.memory.isokron.provider import IsoKronMemoryProvider

    provider = IsoKronMemoryProvider(config=_minimal_config())
    assert provider.get_tool_schemas() == []


def test_config_schema_carries_all_six_fields():
    from plugins.memory.isokron.provider import IsoKronMemoryProvider

    provider = IsoKronMemoryProvider(config=_minimal_config())
    schema = provider.get_config_schema()
    keys = {entry["key"] for entry in schema}
    assert keys == {
        "isokron_dsn",
        "mcp_endpoint",
        "default_workspace_id",
        "cache_ttl_seconds",
        "actor_kind",
        "enable_legacy_fallback",
    }
    # DSN must be flagged secret (carries credentials).
    dsn_entry = next(e for e in schema if e["key"] == "isokron_dsn")
    assert dsn_entry.get("secret") is True


# ---------------------------------------------------------------------------
# Connection plumbing — pool + MCP client are not opened in ST1
# ---------------------------------------------------------------------------


def test_pg_pool_accessor_raises_in_st1():
    from plugins.memory.isokron.provider import IsoKronMemoryProvider

    provider = IsoKronMemoryProvider(config=_minimal_config())
    provider.initialize(session_id="s1")
    try:
        connection = provider._connection
        assert connection is not None
        with pytest.raises(NotImplementedError) as excinfo:
            connection.pg_pool()
        assert "KR-2 ST2" in str(excinfo.value)
    finally:
        provider.shutdown()


def test_mcp_client_accessor_raises_in_st1():
    from plugins.memory.isokron.provider import IsoKronMemoryProvider

    provider = IsoKronMemoryProvider(config=_minimal_config())
    provider.initialize(session_id="s1")
    try:
        connection = provider._connection
        assert connection is not None
        with pytest.raises(NotImplementedError) as excinfo:
            connection.mcp_client()
        assert "KR-2 ST3" in str(excinfo.value)
    finally:
        provider.shutdown()
