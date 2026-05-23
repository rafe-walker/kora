"""Tests for ``kora_cli.listeners.mcp_caller_auth`` — KR-MCP-RUNTIME-SURFACE ST2.

Covers:
  - YAML loader: valid, missing, unreadable, malformed, missing
    'callers' key, individual caller-entry validation
  - Cache invalidation on mtime change
  - resolve_caller Mode 1 (env), Mode 2 (yaml), fall-through to None
  - Mode 2 takes precedence over Mode 1 for the same token
  - Multi-token env support (comma-separated)
  - hash format: sha256:<hex>
  - Caller.can_invoke exact-match semantics
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from kora_cli.listeners import mcp_caller_auth
from kora_cli.listeners.mcp_caller_auth import (
    ANONYMOUS_CALLER,
    Caller,
    _reset_cache_for_tests,
    load_callers,
    resolve_caller,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def _write_yaml(path: Path, doc: dict) -> Path:
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _reset_cache():
    _reset_cache_for_tests()
    yield
    _reset_cache_for_tests()


# ---------------------------------------------------------------------------
# Caller dataclass
# ---------------------------------------------------------------------------


def test_caller_can_invoke_exact_match():
    c = Caller(
        actor_kind="claude_pm_isokron",
        allowed_caps=frozenset(
            {"kora__create_sea_ticket", "kora__request_state_transition"}
        ),
    )
    assert c.can_invoke("kora__create_sea_ticket") is True
    assert c.can_invoke("kora__request_state_transition") is True
    assert c.can_invoke("kora__send_webhook_test_event") is False
    # Substring matches MUST NOT pass — exact-match only.
    assert c.can_invoke("kora__create") is False
    assert c.can_invoke("") is False


def test_anonymous_caller_has_no_caps():
    assert ANONYMOUS_CALLER.actor_kind == "anonymous"
    assert ANONYMOUS_CALLER.allowed_caps == frozenset()
    assert ANONYMOUS_CALLER.can_invoke("kora__create_sea_ticket") is False


# ---------------------------------------------------------------------------
# YAML loader — happy path
# ---------------------------------------------------------------------------


def test_load_callers_valid_yaml(tmp_path):
    path = _write_yaml(
        tmp_path / "mcp_callers.yaml",
        {
            "callers": [
                {
                    "token_hash": _sha256("tok-a"),
                    "actor_kind": "claude_pm_isokron",
                    "allowed_caps": [
                        "kora__create_sea_ticket",
                        "kora__request_state_transition",
                    ],
                },
                {
                    "token_hash": _sha256("tok-b"),
                    "actor_kind": "kora_drone_7",
                    "allowed_caps": ["kora__get_recent_chain_events"],
                },
            ]
        },
    )
    callers = load_callers(path)
    assert len(callers) == 2
    a = callers[_sha256("tok-a")]
    assert a.actor_kind == "claude_pm_isokron"
    assert "kora__create_sea_ticket" in a.allowed_caps


def test_load_callers_missing_file_returns_empty(tmp_path):
    """Mode 2 disabled — mutating calls will DENY via the gate."""
    assert load_callers(tmp_path / "does-not-exist.yaml") == {}


def test_load_callers_unreadable_yaml_returns_empty(tmp_path, caplog):
    import logging

    caplog.set_level(logging.WARNING)
    path = tmp_path / "broken.yaml"
    path.write_text("callers: [{", encoding="utf-8")  # malformed
    assert load_callers(path) == {}
    # WARN logged so the operator can see the misconfig.
    assert any(
        "Mode 2 DISABLED" in r.getMessage() for r in caplog.records
    )


def test_load_callers_missing_callers_key_returns_empty(tmp_path, caplog):
    import logging

    caplog.set_level(logging.WARNING)
    path = _write_yaml(tmp_path / "no-callers.yaml", {"other_root": []})
    assert load_callers(path) == {}
    assert any(
        "missing top-level 'callers'" in r.getMessage() for r in caplog.records
    )


def test_load_callers_callers_not_list_returns_empty(tmp_path, caplog):
    import logging

    caplog.set_level(logging.WARNING)
    path = _write_yaml(
        tmp_path / "bad-shape.yaml", {"callers": "not-a-list"}
    )
    assert load_callers(path) == {}


def test_load_callers_skips_invalid_entries(tmp_path, caplog):
    """Mix of valid + invalid — valid entries load, invalid ones
    skipped with a WARN log; doesn't fail the whole file."""
    import logging

    caplog.set_level(logging.WARNING)
    path = _write_yaml(
        tmp_path / "mixed.yaml",
        {
            "callers": [
                {
                    "token_hash": _sha256("ok-tok"),
                    "actor_kind": "valid_caller",
                    "allowed_caps": ["kora__create_sea_ticket"],
                },
                {
                    "token_hash": "not-prefixed-properly",  # bad format
                    "actor_kind": "bad",
                    "allowed_caps": [],
                },
                {
                    "token_hash": _sha256("missing-actor"),
                    # no actor_kind
                    "allowed_caps": [],
                },
                "not-a-mapping",  # skipped
                {
                    "token_hash": _sha256("bad-caps"),
                    "actor_kind": "x",
                    "allowed_caps": "not-a-list",
                },
            ]
        },
    )
    callers = load_callers(path)
    # Only the first entry loaded.
    assert len(callers) == 1
    assert _sha256("ok-tok") in callers


# ---------------------------------------------------------------------------
# Cache invalidation on mtime
# ---------------------------------------------------------------------------


def test_load_callers_cached_within_mtime(tmp_path):
    """Repeated load_callers(same path, same mtime) returns the same
    object — no re-parse."""
    path = _write_yaml(
        tmp_path / "cached.yaml",
        {
            "callers": [
                {
                    "token_hash": _sha256("t"),
                    "actor_kind": "a",
                    "allowed_caps": [],
                }
            ]
        },
    )
    first = load_callers(path)
    second = load_callers(path)
    # Should be identical dict reference because the cache key
    # (path, mtime) is unchanged.
    assert first is second


def test_load_callers_reloads_on_mtime_change(tmp_path):
    """Atomic-replace bumps mtime → cache invalidates → new content
    loaded."""
    import time

    path = tmp_path / "rolling.yaml"
    _write_yaml(
        path,
        {
            "callers": [
                {
                    "token_hash": _sha256("v1"),
                    "actor_kind": "a",
                    "allowed_caps": [],
                }
            ]
        },
    )
    first = load_callers(path)
    assert _sha256("v1") in first

    # Force a different mtime (filesystem resolution varies).
    time.sleep(0.05)
    _write_yaml(
        path,
        {
            "callers": [
                {
                    "token_hash": _sha256("v2"),
                    "actor_kind": "b",
                    "allowed_caps": [],
                }
            ]
        },
    )
    second = load_callers(path)
    assert _sha256("v2") in second
    assert _sha256("v1") not in second


# ---------------------------------------------------------------------------
# resolve_caller
# ---------------------------------------------------------------------------


def test_resolve_caller_mode_2_yaml_match(tmp_path, monkeypatch):
    monkeypatch.delenv("KORA_MCP_BEARER_TOKEN", raising=False)
    path = _write_yaml(
        tmp_path / "callers.yaml",
        {
            "callers": [
                {
                    "token_hash": _sha256("yaml-tok"),
                    "actor_kind": "claude_pm",
                    "allowed_caps": ["kora__create_sea_ticket"],
                }
            ]
        },
    )
    caller = resolve_caller("yaml-tok", callers_path=path)
    assert caller is not None
    assert caller.actor_kind == "claude_pm"
    assert "kora__create_sea_ticket" in caller.allowed_caps


def test_resolve_caller_mode_1_env_match(monkeypatch):
    """No yaml → env token resolves to ANONYMOUS_CALLER."""
    monkeypatch.setenv("KORA_MCP_BEARER_TOKEN", "env-tok")
    caller = resolve_caller(
        "env-tok", callers_path=Path("/nonexistent")
    )
    assert caller is ANONYMOUS_CALLER


def test_resolve_caller_mode_1_multi_token_each_matches(monkeypatch):
    monkeypatch.setenv("KORA_MCP_BEARER_TOKEN", "tok-old, tok-new ")
    for t in ("tok-old", "tok-new"):
        caller = resolve_caller(t, callers_path=Path("/nonexistent"))
        assert caller is ANONYMOUS_CALLER


def test_resolve_caller_mode_2_takes_precedence_over_mode_1(
    tmp_path, monkeypatch
):
    """When the same token matches both yaml + env, the yaml caller
    wins (caller identity > anonymous)."""
    monkeypatch.setenv("KORA_MCP_BEARER_TOKEN", "shared-tok")
    path = _write_yaml(
        tmp_path / "callers.yaml",
        {
            "callers": [
                {
                    "token_hash": _sha256("shared-tok"),
                    "actor_kind": "identified",
                    "allowed_caps": ["kora__request_state_transition"],
                }
            ]
        },
    )
    caller = resolve_caller("shared-tok", callers_path=path)
    assert caller.actor_kind == "identified"


def test_resolve_caller_no_match_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("KORA_MCP_BEARER_TOKEN", "real-env")
    path = _write_yaml(
        tmp_path / "callers.yaml",
        {
            "callers": [
                {
                    "token_hash": _sha256("real-yaml"),
                    "actor_kind": "x",
                    "allowed_caps": [],
                }
            ]
        },
    )
    assert (
        resolve_caller("nonsense-token", callers_path=path) is None
    )


def test_resolve_caller_empty_token_returns_none(monkeypatch):
    monkeypatch.setenv("KORA_MCP_BEARER_TOKEN", "x")
    assert resolve_caller("", callers_path=Path("/nonexistent")) is None
    assert resolve_caller(None, callers_path=Path("/nonexistent")) is None


def test_resolve_caller_env_unset_no_yaml_returns_none(monkeypatch):
    """Fail-CLOSED — no auth source → no caller."""
    monkeypatch.delenv("KORA_MCP_BEARER_TOKEN", raising=False)
    assert (
        resolve_caller("any-tok", callers_path=Path("/nonexistent"))
        is None
    )


# ---------------------------------------------------------------------------
# KR-MCP-STOP-CONTROL ST2 — actor_id field
# ---------------------------------------------------------------------------


_VALID_ACTOR_UUID = "8d50b3aa-1111-4222-9333-cafebabe1234"


def test_caller_actor_id_defaults_to_none():
    """Existing callers (no actor_id field) still construct cleanly."""
    c = Caller(actor_kind="x", allowed_caps=frozenset())
    assert c.actor_id is None


def test_caller_actor_id_explicit_value():
    c = Caller(
        actor_kind="x", allowed_caps=frozenset(), actor_id=_VALID_ACTOR_UUID
    )
    assert c.actor_id == _VALID_ACTOR_UUID


def test_load_callers_without_actor_id_loads_with_none(tmp_path):
    """Backwards-compat — entries that omit actor_id load as None."""
    path = _write_yaml(
        tmp_path / "mcp_callers.yaml",
        {
            "callers": [
                {
                    "token_hash": _sha256("tok-no-id"),
                    "actor_kind": "claude_pm_legacy",
                    "allowed_caps": ["kora__create_sea_ticket"],
                }
            ]
        },
    )
    callers = load_callers(path)
    assert len(callers) == 1
    c = callers[_sha256("tok-no-id")]
    assert c.actor_id is None


def test_load_callers_with_valid_actor_id(tmp_path):
    path = _write_yaml(
        tmp_path / "mcp_callers.yaml",
        {
            "callers": [
                {
                    "token_hash": _sha256("tok-with-id"),
                    "actor_kind": "claude_pm_operator",
                    "actor_id": _VALID_ACTOR_UUID,
                    "allowed_caps": ["kora__request_stop"],
                }
            ]
        },
    )
    callers = load_callers(path)
    c = callers[_sha256("tok-with-id")]
    assert c.actor_id == _VALID_ACTOR_UUID


def test_load_callers_with_invalid_actor_id_skips_caller(tmp_path, caplog):
    """Malformed UUID → skip the caller fail-CLOSED."""
    import logging

    caplog.set_level(logging.WARNING)
    path = _write_yaml(
        tmp_path / "mcp_callers.yaml",
        {
            "callers": [
                {
                    "token_hash": _sha256("tok-bad-id"),
                    "actor_kind": "claude_pm_bad",
                    "actor_id": "not-a-uuid",
                    "allowed_caps": ["kora__request_stop"],
                }
            ]
        },
    )
    callers = load_callers(path)
    # Caller skipped — fail-CLOSED (mutating call would deny anyway,
    # but the WARN log is essential for operator diagnosis).
    assert callers == {}
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("actor_id" in w for w in warnings)


def test_load_callers_with_non_string_actor_id_skips_caller(tmp_path, caplog):
    """Non-string actor_id (e.g. accidentally a list / int) → skip."""
    import logging

    caplog.set_level(logging.WARNING)
    path = _write_yaml(
        tmp_path / "mcp_callers.yaml",
        {
            "callers": [
                {
                    "token_hash": _sha256("tok-int-id"),
                    "actor_kind": "claude_pm_x",
                    "actor_id": 12345,  # int instead of string
                    "allowed_caps": [],
                }
            ]
        },
    )
    callers = load_callers(path)
    assert callers == {}


def test_load_callers_accepts_unhyphenated_actor_id(tmp_path):
    """uuid.UUID accepts both hyphenated + unhyphenated; we normalize."""
    unhyphenated = _VALID_ACTOR_UUID.replace("-", "")
    path = _write_yaml(
        tmp_path / "mcp_callers.yaml",
        {
            "callers": [
                {
                    "token_hash": _sha256("tok-flat"),
                    "actor_kind": "x",
                    "actor_id": unhyphenated,
                    "allowed_caps": [],
                }
            ]
        },
    )
    callers = load_callers(path)
    c = callers[_sha256("tok-flat")]
    # Canonical hyphenated form on the way out.
    assert c.actor_id == _VALID_ACTOR_UUID


def test_load_callers_mixed_with_and_without_actor_id(tmp_path):
    """Two callers in one file — additive doesn't break existing entries."""
    path = _write_yaml(
        tmp_path / "mcp_callers.yaml",
        {
            "callers": [
                {
                    "token_hash": _sha256("tok-old"),
                    "actor_kind": "legacy",
                    "allowed_caps": ["kora__create_sea_ticket"],
                },
                {
                    "token_hash": _sha256("tok-new"),
                    "actor_kind": "operator",
                    "actor_id": _VALID_ACTOR_UUID,
                    "allowed_caps": ["kora__request_stop"],
                },
            ]
        },
    )
    callers = load_callers(path)
    assert len(callers) == 2
    assert callers[_sha256("tok-old")].actor_id is None
    assert callers[_sha256("tok-new")].actor_id == _VALID_ACTOR_UUID
