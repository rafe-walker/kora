"""Tests for KR-P2-DR-FLIP (/api/dr-state — live read + stub fallback).

Original bucket §4 contract guards (carried over from KR-P2-DR-PANEL #58):
  - top-level shape (current + epoch_history + recent_dr_events +
    runbook_pending + stub)
  - match_status enum validation
  - runbook_pending=True ⇒ match_status != "clean"
  - kora_paused_substrate=True ⇒ match_status ∈ {mismatch_detected, pending_runbook}
  - DR event payloads carry the documented fields
  - Cron-regression sanity

KR-P2-DR-FLIP additions:
  - Uninit path: no provider registered → stub:true + error field
  - Live path: mocked summary projects into the documented shape
  - Mismatch path: substrate > kora_known → runbook_pending + mismatch_detected
  - Error path: summary fetcher raises → stub:true + error includes the message
"""

import pytest


_VALID_MATCH = {"clean", "mismatch_detected", "pending_runbook", "unknown"}


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
    """Force the provider getter to return None — happens in CI / dev
    where the IsoKron plugin isn't registered."""
    import plugins.memory.isokron as isokron_pkg

    monkeypatch.setattr(isokron_pkg, "_last_active_provider", None)
    return None


class _FakeProvider:
    """Minimal stand-in for IsoKronMemoryProvider that the endpoint
    only touches via ``_resolve_workspace_id()`` + the summary helper
    (mocked separately)."""

    def __init__(self, workspace_id="00000000-0000-0000-0000-000000000001"):
        self._workspace_id = workspace_id

    def _resolve_workspace_id(self, **_kwargs):
        return self._workspace_id


def _install_fake_provider(monkeypatch, provider):
    import plugins.memory.isokron as isokron_pkg

    monkeypatch.setattr(isokron_pkg, "_last_active_provider", provider)


def _install_fake_summary(monkeypatch, summary):
    """Override get_dr_state_summary at the endpoint's import site."""
    import plugins.memory.isokron.dr_epoch as dr_epoch_mod

    async def _fake(provider, workspace_id, **_kwargs):
        return summary

    monkeypatch.setattr(dr_epoch_mod, "get_dr_state_summary", _fake)


def _install_summary_raise(monkeypatch, exc):
    import plugins.memory.isokron.dr_epoch as dr_epoch_mod

    async def _fake(provider, workspace_id, **_kwargs):
        raise exc

    monkeypatch.setattr(dr_epoch_mod, "get_dr_state_summary", _fake)


# ---- 1. Top-level shape (both branches share the schema) ---------------


@pytest.mark.asyncio
async def test_endpoint_returns_documented_top_level_shape(_isolate_config, _no_active_provider):
    from kora_cli import web_server

    result = await web_server.get_dr_state()
    assert isinstance(result, dict)
    # Both branches return at least these 5 keys; the error branch
    # adds an "error" field, so we check superset rather than equality.
    assert {
        "current",
        "epoch_history",
        "recent_dr_events",
        "runbook_pending",
        "stub",
    } <= set(result.keys())
    assert isinstance(result["current"], dict)
    assert isinstance(result["epoch_history"], list)
    assert isinstance(result["recent_dr_events"], list)
    assert isinstance(result["runbook_pending"], bool)


@pytest.mark.asyncio
async def test_match_status_in_documented_enum(_isolate_config, _no_active_provider):
    from kora_cli import web_server

    result = await web_server.get_dr_state()
    assert result["current"]["match_status"] in _VALID_MATCH


# ---- 2. Uninit path (no provider) → stub-true + error -----------------


@pytest.mark.asyncio
async def test_no_provider_returns_stub_true_with_error_field(_isolate_config, _no_active_provider):
    from kora_cli import web_server

    result = await web_server.get_dr_state()
    assert result["stub"] is True
    assert "error" in result
    assert "not initialised" in result["error"].lower() or "not initialized" in result["error"].lower()
    # Fallback shape: epoch lists empty, runbook_pending false (operator
    # shouldn't see a DR alert just because the provider isn't wired).
    assert result["epoch_history"] == []
    assert result["recent_dr_events"] == []
    assert result["runbook_pending"] is False


@pytest.mark.asyncio
async def test_no_workspace_returns_stub_true_with_error_field(_isolate_config, monkeypatch):
    """Provider exists but its config has no default_workspace_id +
    no per-call override. _resolve_workspace_id returns None."""
    provider = _FakeProvider(workspace_id=None)
    _install_fake_provider(monkeypatch, provider)

    from kora_cli import web_server

    result = await web_server.get_dr_state()
    assert result["stub"] is True
    assert "error" in result
    assert "workspace" in result["error"].lower()


# ---- 3. Live path with mocked summary ----------------------------------


@pytest.mark.asyncio
async def test_live_path_with_clean_summary_drops_stub_flag(_isolate_config, monkeypatch):
    from plugins.memory.isokron.dr_epoch import DRStateSummary

    _install_fake_provider(monkeypatch, _FakeProvider())
    _install_fake_summary(
        monkeypatch,
        DRStateSummary(
            substrate_epoch=12,
            kora_known_epoch=12,
            match_status="clean",
            kora_paused_substrate=False,
            last_check_at="2026-05-22T01:00:00Z",
            recent_dr_events=[],
            epoch_history=[],
        ),
    )

    from kora_cli import web_server

    result = await web_server.get_dr_state()
    assert result["stub"] is False
    assert "error" not in result
    assert result["current"]["substrate_epoch"] == 12
    assert result["current"]["kora_known_epoch"] == 12
    assert result["current"]["match_status"] == "clean"
    assert result["current"]["kora_paused_substrate"] is False
    assert result["runbook_pending"] is False


@pytest.mark.asyncio
async def test_live_path_mismatch_surfaces_runbook_pending(_isolate_config, monkeypatch):
    """Substrate has advanced past kora_known — gate 3b would catch this
    on next boot; the panel surfaces it now as mismatch_detected +
    runbook_pending=True so operator can act."""
    from plugins.memory.isokron.dr_epoch import DRStateSummary

    _install_fake_provider(monkeypatch, _FakeProvider())
    _install_fake_summary(
        monkeypatch,
        DRStateSummary(
            substrate_epoch=13,
            kora_known_epoch=12,
            match_status="mismatch_detected",
            kora_paused_substrate=False,
            last_check_at="2026-05-22T01:00:00Z",
            recent_dr_events=[],
            epoch_history=[],
        ),
    )

    from kora_cli import web_server

    result = await web_server.get_dr_state()
    assert result["stub"] is False
    assert result["current"]["match_status"] == "mismatch_detected"
    assert result["runbook_pending"] is True


@pytest.mark.asyncio
async def test_live_path_paused_substrate_surfaces_runbook_pending(_isolate_config, monkeypatch):
    """Holder reports PAUSED{substrate} even though epochs match (clean
    write happened but operator hasn't issued the kora_control reset
    yet) — match_status flips to pending_runbook + runbook_pending=True."""
    from plugins.memory.isokron.dr_epoch import DRStateSummary

    _install_fake_provider(monkeypatch, _FakeProvider())
    _install_fake_summary(
        monkeypatch,
        DRStateSummary(
            substrate_epoch=13,
            kora_known_epoch=13,
            match_status="pending_runbook",
            kora_paused_substrate=True,
            last_check_at="2026-05-22T01:00:00Z",
            recent_dr_events=[],
            epoch_history=[],
        ),
    )

    from kora_cli import web_server

    result = await web_server.get_dr_state()
    assert result["stub"] is False
    assert result["current"]["kora_paused_substrate"] is True
    assert result["current"]["match_status"] == "pending_runbook"
    assert result["runbook_pending"] is True


@pytest.mark.asyncio
async def test_live_path_passes_dr_events_through(_isolate_config, monkeypatch):
    """Live recent_dr_events from the summary are passed through to the
    response array unchanged."""
    from plugins.memory.isokron.dr_epoch import DRStateSummary

    sample_event = {
        "event_type": "kora.dr.observed",
        "occurred_at": "2026-05-20T12:34:56Z",
        "from_epoch": 11,
        "to_epoch": 12,
        "discarded_operation_ids": 2,
        "discarded_ledger_rows": 5,
        "cleared_at": "2026-05-20T12:40:00Z",
        "cleared_by": "operator@stormhaven",
    }
    _install_fake_provider(monkeypatch, _FakeProvider())
    _install_fake_summary(
        monkeypatch,
        DRStateSummary(
            substrate_epoch=12,
            kora_known_epoch=12,
            match_status="clean",
            kora_paused_substrate=False,
            last_check_at="2026-05-22T01:00:00Z",
            recent_dr_events=[sample_event],
            epoch_history=[],
        ),
    )

    from kora_cli import web_server

    result = await web_server.get_dr_state()
    assert result["recent_dr_events"] == [sample_event]


# ---- 3b. epoch_history synthesis (KR-P2-DR-EPOCH-SYNTH) ---------------


def test_synthesize_epoch_history_returns_empty_for_none_value():
    """Pre-first-boot state — nothing meaningful to project, so the
    helper returns an empty list rather than synthesizing a row with
    a null epoch."""
    from plugins.memory.isokron.dr_epoch import _synthesize_epoch_history

    assert _synthesize_epoch_history(None) == []


def test_synthesize_epoch_history_returns_single_tagged_entry():
    """When kora_known_epoch is set + no real history table exists,
    project a single entry with source="synthesized" + synthesized=True
    so the FE can distinguish it from a real audit-trail row. Both
    timestamps are null because the substrate STABLE accessors only
    expose the epoch value, not when it was first published or last
    written."""
    from plugins.memory.isokron.dr_epoch import _synthesize_epoch_history

    result = _synthesize_epoch_history(42)
    assert result == [
        {
            "epoch": 42,
            "observed_at": None,
            "kora_known_at": None,
            "source": "synthesized",
            "synthesized": True,
        }
    ]


@pytest.mark.asyncio
async def test_live_path_passes_synthesized_history_through(_isolate_config, monkeypatch):
    """When the summary carries a synthesized entry, the endpoint passes
    it through unchanged — source, synthesized flag, null timestamps
    all preserved for the FE."""
    from plugins.memory.isokron.dr_epoch import DRStateSummary

    synthesized_entry = {
        "epoch": 12,
        "observed_at": None,
        "kora_known_at": None,
        "source": "synthesized",
        "synthesized": True,
    }
    _install_fake_provider(monkeypatch, _FakeProvider())
    _install_fake_summary(
        monkeypatch,
        DRStateSummary(
            substrate_epoch=12,
            kora_known_epoch=12,
            match_status="clean",
            kora_paused_substrate=False,
            last_check_at="2026-05-22T01:00:00Z",
            recent_dr_events=[],
            epoch_history=[synthesized_entry],
        ),
    )

    from kora_cli import web_server

    result = await web_server.get_dr_state()
    assert result["epoch_history"] == [synthesized_entry]


@pytest.mark.asyncio
async def test_synthesized_flag_pairs_with_synthesized_source(_isolate_config, monkeypatch):
    """Contract guard for FE rendering: synthesized=True must always pair
    with source="synthesized", and source="synthesized" must always pair
    with synthesized=True. A future helper drift that uses the source
    string without the flag (or vice versa) would silently mis-render
    the FE badge tooltip."""
    from plugins.memory.isokron.dr_epoch import _synthesize_epoch_history

    for kora_known in (1, 42, 9999):
        for entry in _synthesize_epoch_history(kora_known):
            if entry.get("source") == "synthesized":
                assert entry.get("synthesized") is True, (
                    f"source=synthesized but synthesized flag is "
                    f"{entry.get('synthesized')!r} — FE tooltip would not render"
                )
            if entry.get("synthesized") is True:
                assert entry["source"] == "synthesized", (
                    f"synthesized=True but source={entry['source']!r} — "
                    f"FE source badge would say the wrong thing"
                )


# ---- 4. Error path → stub + error -------------------------------------


@pytest.mark.asyncio
async def test_summary_failure_returns_stub_with_error_field(_isolate_config, monkeypatch):
    """Any exception from the summary fetcher falls through to the
    stub-fallback branch with stub:true + error carrying the type +
    message — so the operator can debug from the FE without a server
    log dig."""
    _install_fake_provider(monkeypatch, _FakeProvider())
    _install_summary_raise(
        monkeypatch, RuntimeError("simulated asyncpg connection refused")
    )

    from kora_cli import web_server

    result = await web_server.get_dr_state()
    assert result["stub"] is True
    assert "error" in result
    assert "RuntimeError" in result["error"]
    assert "simulated asyncpg connection refused" in result["error"]
    # Fallback shape preserved — no false runbook_pending alert.
    assert result["runbook_pending"] is False


# ---- 5. Original contract guards (carried over from #58) -------------


@pytest.mark.asyncio
async def test_runbook_pending_implies_non_clean_match_status(_isolate_config, _no_active_provider):
    """runbook_pending must never be True when match is clean — that'd
    surface a false DR alert in the FE."""
    from kora_cli import web_server

    result = await web_server.get_dr_state()
    if result["runbook_pending"]:
        assert result["current"]["match_status"] != "clean"


@pytest.mark.asyncio
async def test_paused_substrate_implies_mismatch_or_pending_runbook(_isolate_config, _no_active_provider):
    """PAUSED{substrate} is only set when gate 3b detected a mismatch
    OR operator-pending state — never while match is clean."""
    from kora_cli import web_server

    result = await web_server.get_dr_state()
    if result["current"]["kora_paused_substrate"]:
        assert result["current"]["match_status"] in {
            "mismatch_detected",
            "pending_runbook",
        }


# ---- 6. Cron-regression sanity -----------------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_dr_state_registered(_isolate_config):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
