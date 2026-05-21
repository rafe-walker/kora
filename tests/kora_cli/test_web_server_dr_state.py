"""Tests for the KR-P2-DR-PANEL stub endpoint.

Bucket §4 scenarios:
  1. GET /api/dr-state returns 200 + shape
  2. match_status enum validation
  3. runbook_pending == True ⇒ match_status != "clean" (contract guard)
  4. kora_paused_substrate == True ⇒ match_status ∈ {mismatch_detected, pending_runbook}
  5. Epoch history monotonic in `epoch` field (desc-sorted in stub)
  6. DR event payloads carry the documented fields
  7. Cron-regression sanity
"""

import pytest


_VALID_MATCH = {"clean", "mismatch_detected", "pending_runbook", "unknown"}
_VALID_SOURCE = {"boot-success", "dr-recovery", "operator-bump"}


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


# ---- 1. 200 + shape ------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_returns_200_and_top_level_shape(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_dr_state()
    assert isinstance(result, dict)
    assert set(result.keys()) == {
        "current",
        "epoch_history",
        "recent_dr_events",
        "runbook_pending",
        "stub",
    }
    assert result["stub"] is True
    assert isinstance(result["current"], dict)
    assert isinstance(result["epoch_history"], list)
    assert isinstance(result["recent_dr_events"], list)
    assert isinstance(result["runbook_pending"], bool)


# ---- 2. match_status enum -----------------------------------------------


@pytest.mark.asyncio
async def test_current_has_required_fields_and_valid_match_status(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_dr_state()
    current = result["current"]
    assert set(current.keys()) >= {
        "substrate_epoch",
        "kora_known_epoch",
        "match_status",
        "last_check_at",
        "kora_paused_substrate",
    }
    assert current["match_status"] in _VALID_MATCH
    assert isinstance(current["substrate_epoch"], int)
    assert (
        current["kora_known_epoch"] is None
        or isinstance(current["kora_known_epoch"], int)
    )
    assert isinstance(current["kora_paused_substrate"], bool)


# ---- 3. runbook_pending ⇒ non-clean match_status ------------------------


@pytest.mark.asyncio
async def test_runbook_pending_implies_non_clean_match_status(_isolate_config):
    """Contract guard: runbook_pending should never be True when match
    is clean — they'd be contradictory and the FE would render the red
    DR alert on a healthy system."""
    from kora_cli import web_server

    result = await web_server.get_dr_state()
    if result["runbook_pending"]:
        assert result["current"]["match_status"] != "clean", (
            f"runbook_pending=True but match_status={result['current']['match_status']!r} "
            f"— these are contradictory; FE would surface a false DR alert"
        )


# ---- 4. kora_paused_substrate ⇒ mismatch_detected or pending_runbook ---


@pytest.mark.asyncio
async def test_paused_substrate_implies_mismatch_or_pending_runbook(_isolate_config):
    """Contract guard: PAUSED{substrate} is only set when gate 3b has
    detected an epoch mismatch (or operator has acknowledged it as
    pending-runbook). It must NOT be set while match is clean."""
    from kora_cli import web_server

    result = await web_server.get_dr_state()
    if result["current"]["kora_paused_substrate"]:
        assert result["current"]["match_status"] in {
            "mismatch_detected",
            "pending_runbook",
        }, (
            f"kora_paused_substrate=True but match_status="
            f"{result['current']['match_status']!r} — gate 3b shouldn't be "
            f"holding the pause without an active mismatch"
        )


# ---- 5. epoch_history shape + monotonic ---------------------------------


@pytest.mark.asyncio
async def test_epoch_history_entries_have_required_keys_and_valid_source(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_dr_state()
    for entry in result["epoch_history"]:
        assert set(entry.keys()) >= {
            "epoch",
            "observed_at",
            "kora_known_at",
            "source",
        }
        assert isinstance(entry["epoch"], int)
        assert entry["source"] in _VALID_SOURCE


@pytest.mark.asyncio
async def test_epoch_history_is_monotonic_when_sorted_by_observed_at(_isolate_config):
    """Epoch field should only go up over time. PITR / dr-recovery /
    operator-bump can jump by more than 1, but never decrease."""
    from kora_cli import web_server

    result = await web_server.get_dr_state()
    sorted_by_time = sorted(
        result["epoch_history"], key=lambda e: e["observed_at"]
    )
    for prev, curr in zip(sorted_by_time, sorted_by_time[1:]):
        assert curr["epoch"] >= prev["epoch"], (
            f"epoch went backwards: {prev['epoch']} → {curr['epoch']} "
            f"at {curr['observed_at']!r} — substrate epoch is monotonic"
        )


# ---- 6. DR event payloads -----------------------------------------------


@pytest.mark.asyncio
async def test_dr_events_carry_documented_fields(_isolate_config):
    from kora_cli import web_server

    result = await web_server.get_dr_state()
    for ev in result["recent_dr_events"]:
        assert set(ev.keys()) >= {
            "event_type",
            "occurred_at",
            "from_epoch",
            "to_epoch",
            "discarded_operation_ids",
            "discarded_ledger_rows",
            "cleared_at",
            "cleared_by",
        }
        assert ev["event_type"] == "kora.dr.observed"
        assert isinstance(ev["from_epoch"], int)
        assert isinstance(ev["to_epoch"], int)
        assert ev["to_epoch"] >= ev["from_epoch"], (
            f"DR event from_epoch ({ev['from_epoch']}) > to_epoch "
            f"({ev['to_epoch']}) — DR can't decrease the epoch"
        )
        assert isinstance(ev["discarded_operation_ids"], int)
        assert isinstance(ev["discarded_ledger_rows"], int)
        # cleared_at and cleared_by are paired — either both null or both set
        assert (ev["cleared_at"] is None) == (ev["cleared_by"] is None), (
            "cleared_at and cleared_by must be paired (both null or both set)"
        )


# ---- 7. Cron-regression sanity -----------------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_dr_state_registered(_isolate_config):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
