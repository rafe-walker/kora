"""Tests for the KR-P2-CHAIN-EVENTS-PANEL endpoint + helper.

Bucket §5 scenarios:
  1. GET 200 + shape with realistic event rows (mocked read)
  2. Pagination: `before_ts` cursor narrows results
  3. `prefix` filter limits to matching event_types
  4. Defensive: malformed payload → {} fallback, WARN logged, no 500
  5. Uninit branch (no provider) → stub:true + error field
  6. Contract guard: limit clamped at 500 even if higher requested
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
    import plugins.memory.isokron as isokron_pkg

    monkeypatch.setattr(isokron_pkg, "_last_active_provider", None)
    return None


class _FakeConnection:
    """Stand-in for IsoKronConnection that just hands back a sentinel
    pool object — the test patches the read helper directly so the pool
    is never actually used."""

    def get_pg_pool(self):
        return object()


class _FakeProvider:
    def __init__(self, workspace_id="00000000-0000-0000-0000-000000000001"):
        self._workspace_id = workspace_id
        self._connection = _FakeConnection()

    def _resolve_workspace_id(self, **_kwargs):
        return self._workspace_id


def _install_fake_provider(monkeypatch, provider):
    import plugins.memory.isokron as isokron_pkg

    monkeypatch.setattr(isokron_pkg, "_last_active_provider", provider)


def _install_fake_read(monkeypatch, captured: dict, rows):
    """Replace read_recent_events with a fake that captures call kwargs
    and returns the given rows."""
    import plugins.memory.isokron.events as events_mod

    async def _fake(
        workspace_id, pool, *, event_type_prefix=None, actor_id_filter=None,
        limit=100, before_ts=None,
    ):
        captured["workspace_id"] = workspace_id
        captured["event_type_prefix"] = event_type_prefix
        captured["actor_id_filter"] = actor_id_filter
        captured["limit"] = limit
        captured["before_ts"] = before_ts
        return rows

    monkeypatch.setattr(events_mod, "read_recent_events", _fake)


def _row(event_type="kora.boot.ready", occurred_at="2026-05-22T01:30:00Z", **kwargs):
    """Build a ChainEventRow with sane defaults for tests."""
    from plugins.memory.isokron.events import ChainEventRow

    return ChainEventRow(
        event_id=kwargs.get("event_id", "evt_1"),
        event_type=event_type,
        actor_id=kwargs.get("actor_id", "actor_kora_uuid"),
        occurred_at=occurred_at,
        payload=kwargs.get("payload", {"sample": "data"}),
    )


# ---- 1. 200 + shape ---------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_returns_200_and_shape(_isolate_config, monkeypatch):
    _install_fake_provider(monkeypatch, _FakeProvider())
    captured: dict = {}
    _install_fake_read(monkeypatch, captured, [
        _row("kora.boot.ready", "2026-05-22T01:30:00Z"),
        _row(
            "kora.operational_state.transitioned",
            "2026-05-22T01:29:00Z",
            event_id="evt_2",
        ),
    ])

    from kora_cli import web_server

    result = await web_server.get_chain_events()
    assert isinstance(result, dict)
    assert set(result.keys()) >= {"events", "next_before_ts", "stub"}
    assert result["stub"] is False
    assert len(result["events"]) == 2
    first = result["events"][0]
    assert set(first.keys()) >= {
        "event_id",
        "event_type",
        "actor_id",
        "actor_kind",
        "workspace_id",
        "occurred_at",
        "payload",
        "envelope",
    }
    # actor_kind is intentionally None in v1 — JOIN to actor_registry
    # is future work.
    assert first["actor_kind"] is None


@pytest.mark.asyncio
async def test_constitution_event_carries_envelope(_isolate_config, monkeypatch):
    """kora.constitution.* events surface revision_id + rules_hash in
    the envelope so operators can spot a revision-flip at a glance."""
    _install_fake_provider(monkeypatch, _FakeProvider())
    captured: dict = {}
    _install_fake_read(monkeypatch, captured, [
        _row(
            "kora.constitution.revision_changed",
            "2026-05-22T01:30:00Z",
            payload={
                "revision_id": "rev_abc",
                "rules_hash": "sha256:deadbeef",
                "extra": "stuff",
            },
        ),
    ])

    from kora_cli import web_server

    result = await web_server.get_chain_events()
    event = result["events"][0]
    assert event["envelope"] == {
        "revision_id": "rev_abc",
        "rules_hash": "sha256:deadbeef",
    }


@pytest.mark.asyncio
async def test_non_constitution_event_has_null_envelope(_isolate_config, monkeypatch):
    """Other families don't have a documented envelope contract yet —
    surface as null so the FE skips the envelope block."""
    _install_fake_provider(monkeypatch, _FakeProvider())
    captured: dict = {}
    _install_fake_read(monkeypatch, captured, [_row("kora.boot.ready")])

    from kora_cli import web_server

    result = await web_server.get_chain_events()
    assert result["events"][0]["envelope"] is None


# ---- 2. Pagination cursor ---------------------------------------------


@pytest.mark.asyncio
async def test_before_ts_cursor_passes_through_to_read(_isolate_config, monkeypatch):
    """The endpoint forwards before_ts directly to read_recent_events so
    pagination works."""
    _install_fake_provider(monkeypatch, _FakeProvider())
    captured: dict = {}
    _install_fake_read(monkeypatch, captured, [])

    from kora_cli import web_server

    await web_server.get_chain_events(before_ts="2026-05-22T01:00:00Z")
    assert captured["before_ts"] == "2026-05-22T01:00:00Z"


@pytest.mark.asyncio
async def test_next_before_ts_is_oldest_event_occurred_at(_isolate_config, monkeypatch):
    """The pagination cursor for "Load older" is the LAST event's
    occurred_at (events come back ordered DESC; the last one is oldest)."""
    _install_fake_provider(monkeypatch, _FakeProvider())
    captured: dict = {}
    _install_fake_read(monkeypatch, captured, [
        _row(event_id="evt_newest", occurred_at="2026-05-22T01:30:00Z"),
        _row(event_id="evt_middle", occurred_at="2026-05-22T01:20:00Z"),
        _row(event_id="evt_oldest", occurred_at="2026-05-22T01:10:00Z"),
    ])

    from kora_cli import web_server

    result = await web_server.get_chain_events()
    assert result["next_before_ts"] == "2026-05-22T01:10:00Z"


@pytest.mark.asyncio
async def test_next_before_ts_null_when_no_events(_isolate_config, monkeypatch):
    _install_fake_provider(monkeypatch, _FakeProvider())
    captured: dict = {}
    _install_fake_read(monkeypatch, captured, [])

    from kora_cli import web_server

    result = await web_server.get_chain_events()
    assert result["next_before_ts"] is None


# ---- 3. prefix filter passes through ----------------------------------


@pytest.mark.asyncio
async def test_prefix_passes_through_to_read(_isolate_config, monkeypatch):
    _install_fake_provider(monkeypatch, _FakeProvider())
    captured: dict = {}
    _install_fake_read(monkeypatch, captured, [])

    from kora_cli import web_server

    await web_server.get_chain_events(prefix="kora.constitution.")
    assert captured["event_type_prefix"] == "kora.constitution."


@pytest.mark.asyncio
async def test_empty_prefix_becomes_none(_isolate_config, monkeypatch):
    """Empty-string prefix in the query string maps to None at the
    helper boundary — no LIKE filter applied."""
    _install_fake_provider(monkeypatch, _FakeProvider())
    captured: dict = {}
    _install_fake_read(monkeypatch, captured, [])

    from kora_cli import web_server

    await web_server.get_chain_events(prefix="")
    assert captured["event_type_prefix"] is None


# ---- 4. Defensive: malformed payload ----------------------------------


def test_read_recent_events_defensive_on_malformed_payload(caplog):
    """Unit test of the helper's defensive parsing. asyncpg can return
    jsonb as either a dict (codec installed) or a string. A malformed
    string payload should log WARN + use {} fallback rather than 500."""
    import asyncio
    import logging

    from plugins.memory.isokron.events import read_recent_events

    class _FakeConn:
        async def fetch(self, sql, *params):
            return [
                {
                    "event_id": "evt_bad",
                    "event_type": "kora.boot.ready",
                    "actor_id": "actor_uuid",
                    "occurred_at": "2026-05-22T01:30:00Z",
                    "payload": "this is not json {{{",
                }
            ]

    class _FakePool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(_inner):
                    return _FakeConn()

                async def __aexit__(_inner, *exc):
                    return False

            return _Ctx()

    with caplog.at_level(logging.WARNING, logger="isokron_client.events"):
        rows = asyncio.run(
            read_recent_events("ws", _FakePool(), event_type_prefix="kora.")
        )

    assert len(rows) == 1
    assert rows[0].payload == {}
    assert any("malformed payload" in rec.message for rec in caplog.records)


# ---- 5. Uninit branch (no provider) → stub + error -------------------


@pytest.mark.asyncio
async def test_no_provider_returns_stub_with_error(_isolate_config, _no_active_provider):
    from kora_cli import web_server

    result = await web_server.get_chain_events()
    assert result["stub"] is True
    assert "error" in result
    assert "IsoKronMemoryProvider" in result["error"]
    assert result["events"] == []
    assert result["next_before_ts"] is None


@pytest.mark.asyncio
async def test_no_workspace_returns_stub_with_error(_isolate_config, monkeypatch):
    _install_fake_provider(monkeypatch, _FakeProvider(workspace_id=None))

    from kora_cli import web_server

    result = await web_server.get_chain_events()
    assert result["stub"] is True
    assert "error" in result
    assert "workspace" in result["error"].lower()


@pytest.mark.asyncio
async def test_read_failure_returns_stub_with_error(_isolate_config, monkeypatch):
    _install_fake_provider(monkeypatch, _FakeProvider())

    import plugins.memory.isokron.events as events_mod

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated asyncpg failure")

    monkeypatch.setattr(events_mod, "read_recent_events", _boom)

    from kora_cli import web_server

    result = await web_server.get_chain_events()
    assert result["stub"] is True
    assert "RuntimeError" in result["error"]
    assert "simulated asyncpg failure" in result["error"]


# ---- 6. Limit clamped at 500 even if higher requested ---------------


@pytest.mark.asyncio
async def test_limit_clamped_at_max_in_helper(_isolate_config, monkeypatch):
    """Even if a query string asks for limit=10000, the helper clamps
    to MAX_RECENT_EVENTS_LIMIT=500 before issuing the SQL."""
    from plugins.memory.isokron.events import MAX_RECENT_EVENTS_LIMIT

    _install_fake_provider(monkeypatch, _FakeProvider())

    captured_params: list = []

    class _FakeConn:
        async def fetch(self, sql, *params):
            captured_params.extend(params)
            return []

    class _FakePool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(_inner):
                    return _FakeConn()

                async def __aexit__(_inner, *exc):
                    return False

            return _Ctx()

    from plugins.memory.isokron.events import read_recent_events

    await read_recent_events(
        "ws", _FakePool(), event_type_prefix="kora.", limit=10_000
    )
    # Last param is always the LIMIT — the SQL appends it last.
    assert captured_params[-1] == MAX_RECENT_EVENTS_LIMIT


@pytest.mark.asyncio
async def test_limit_clamped_at_1_when_zero_requested(_isolate_config):
    """Sanity guard: limit=0 would be a useless SQL and might be treated
    as "all rows" by some drivers — clamp to 1."""
    from plugins.memory.isokron.events import read_recent_events

    captured_params: list = []

    class _FakeConn:
        async def fetch(self, sql, *params):
            captured_params.extend(params)
            return []

    class _FakePool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(_inner):
                    return _FakeConn()

                async def __aexit__(_inner, *exc):
                    return False

            return _Ctx()

    await read_recent_events("ws", _FakePool(), limit=0)
    assert captured_params[-1] == 1


# ---- 7. Cron-regression sanity --------------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_chain_events_registered(_isolate_config):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
