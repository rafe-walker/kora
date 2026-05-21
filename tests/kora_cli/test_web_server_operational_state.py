"""Tests for ``GET /api/operational-state`` (KR-P2-CLEANUP ST5).

KR-P2-I-integration ST5 (PR #35) flipped this endpoint from a
hardcoded stub to a live ``OperationalStateHolder`` read but did not
update these tests. ST5 of this cleanup bucket realigns them.

# Mock-fixture pattern (lockstep with the endpoint)

Both branches of the endpoint must stay covered:

  * **Uninitialized branch** — ``get_holder()`` returns ``None`` (no
    agent session has run ``wire_operational_state`` yet). Endpoint
    returns the stub-shape with ``primary_state="booting"``,
    ``claim_permission="none"``, empty history, empty next-states,
    plus ``stub: True`` AND an ``error`` field naming the cause.
  * **Live branch** — holder initialized; endpoint returns the live
    state with ``transition_history`` from ``holder.history(limit=10)``
    + ``valid_next_states`` from ``transitions_from``, and NO
    ``stub``/``error`` fields.

The autouse ``_reset_holder`` fixture clears the module-level
singleton before AND after each test so the two branches stay
deterministic and don't leak across the test session. Future
endpoint changes must update both branches' coverage here in the
same PR; the "the stub used to be hardcoded" excuse from PR #35
is closed.
"""

import pytest

from agent.operational_state import (
    ClaimPermission,
    OperationalState,
    PrimaryState,
)
from agent.operational_state_holder import (
    _reset_holder_for_tests,
    init_holder,
)


_VALID_PRIMARY_STATES = {"booting", "ready", "active", "paused", "stopped"}
_VALID_CLAIM_PERMISSIONS = {"none", "critical_only", "normal"}
_VALID_DEGRADATION_REASONS = {
    "cost",
    "auth",
    "dispatch",
    "substrate",
    "migration",
    "operator",
    "token_expiring",
    "retry_ceiling",
}


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


@pytest.fixture(autouse=True)
def _reset_holder():
    """Wipe the module-level OperationalStateHolder singleton before +
    after every test so the uninit/live branch is deterministic."""
    _reset_holder_for_tests()
    yield
    _reset_holder_for_tests()


# ---------------------------------------------------------------------------
# 1. 200
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_operational_state_returns_200():
    from kora_cli import web_server

    result = await web_server.get_operational_state()
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# 2. Uninitialized branch — stub-shape + stub:True + error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uninitialized_branch_returns_stub_with_error():
    """No holder initialized (autouse fixture cleared it). Endpoint
    returns the stub-shape with stub:True + error field. Per
    KR-P2-I-integration ST5 design: the panel renders a distinct
    "no wire-in yet" banner when both flags are present, separate
    from cold-stub state."""
    from kora_cli import web_server

    result = await web_server.get_operational_state()

    assert result["stub"] is True
    assert "error" in result
    assert isinstance(result["error"], str)
    assert "not yet initialized" in result["error"]


@pytest.mark.asyncio
async def test_uninitialized_branch_top_level_shape():
    from kora_cli import web_server

    result = await web_server.get_operational_state()

    required = {
        "primary_state",
        "claim_permission",
        "degradation_reasons",
        "is_degraded",
        "transition_history",
        "valid_next_states",
        "stub",
        "error",
    }
    assert required.issubset(result.keys())
    # Cold-state defaults on the uninit branch.
    assert result["primary_state"] == "booting"
    assert result["claim_permission"] == "none"
    assert result["degradation_reasons"] == []
    assert result["is_degraded"] is False
    assert result["transition_history"] == []
    assert result["valid_next_states"] == []


@pytest.mark.asyncio
async def test_uninitialized_branch_uses_only_valid_enum_values():
    from kora_cli import web_server

    result = await web_server.get_operational_state()

    assert result["primary_state"] in _VALID_PRIMARY_STATES
    assert result["claim_permission"] in _VALID_CLAIM_PERMISSIONS
    for reason in result["degradation_reasons"]:
        assert reason in _VALID_DEGRADATION_REASONS


# ---------------------------------------------------------------------------
# 3. Live branch — holder initialized; real state surfaces
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_branch_returns_real_state_without_stub_or_error():
    """Initialize the holder; the endpoint must return live state
    WITHOUT ``stub`` or ``error`` fields. CC#2's panel renders the
    stub banner only when the field is truthy; dropping it auto-
    stops the banner."""
    from kora_cli import web_server

    init_holder(
        OperationalState(
            primary_state=PrimaryState.READY,
            claim_permission=ClaimPermission.NORMAL,
        )
    )

    result = await web_server.get_operational_state()

    assert "stub" not in result
    assert "error" not in result
    assert result["primary_state"] == "ready"
    assert result["claim_permission"] == "normal"


@pytest.mark.asyncio
async def test_live_branch_transition_history_populated_after_transition():
    """The endpoint's ``transition_history`` reads
    ``holder.history(limit=10)``. After one transition through the
    holder, the panel's history list must contain at least one entry
    with the documented shape."""
    from kora_cli import web_server

    holder = init_holder(
        OperationalState(
            primary_state=PrimaryState.BOOTING,
            claim_permission=ClaimPermission.NONE,
        )
    )
    await holder.transition_to(
        PrimaryState.READY,
        trigger="all §9.2 gates pass",
        new_claim_permission=ClaimPermission.NORMAL,
    )

    result = await web_server.get_operational_state()
    history = result["transition_history"]

    assert isinstance(history, list)
    assert len(history) == 1
    entry = history[0]
    assert set(entry.keys()) >= {
        "timestamp",
        "from_state",
        "to_state",
        "trigger",
    }
    assert entry["from_state"] == "booting"
    assert entry["to_state"] == "ready"
    assert entry["trigger"] == "all §9.2 gates pass"


@pytest.mark.asyncio
async def test_live_branch_valid_next_states_derived_from_transition_table():
    """The endpoint computes ``valid_next_states`` via
    ``transitions_from(state.primary_state)``. For READY, the table
    advertises ACTIVE, PAUSED, STOPPED arrows; codify so a future
    table change updates both the endpoint and this test."""
    from kora_cli import web_server

    init_holder(
        OperationalState(
            primary_state=PrimaryState.READY,
            claim_permission=ClaimPermission.NORMAL,
        )
    )

    result = await web_server.get_operational_state()
    next_states = result["valid_next_states"]

    assert isinstance(next_states, list)
    assert len(next_states) >= 1
    for entry in next_states:
        assert set(entry.keys()) >= {"to_state", "trigger"}
        assert entry["to_state"] in _VALID_PRIMARY_STATES
        assert isinstance(entry["trigger"], str) and entry["trigger"]

    targets = {n["to_state"] for n in next_states}
    assert targets == {"active", "paused", "stopped"}


@pytest.mark.asyncio
async def test_live_branch_degradation_reasons_render_sorted():
    """Multiple degradation reasons must render alphabetically sorted
    so cockpit-side diffs stay stable across panel reloads."""
    from agent.operational_state import DegradationReason
    from kora_cli import web_server

    holder = init_holder(
        OperationalState(
            primary_state=PrimaryState.READY,
            claim_permission=ClaimPermission.CRITICAL_ONLY,
        )
    )
    await holder.transition_to(
        PrimaryState.READY,
        trigger="degradation set",
        add_reasons={
            DegradationReason.DISPATCH,
            DegradationReason.AUTH,
            DegradationReason.COST,
        },
    )

    result = await web_server.get_operational_state()
    assert result["degradation_reasons"] == ["auth", "cost", "dispatch"]
    assert result["is_degraded"] is True


# ---------------------------------------------------------------------------
# Cron-regression sanity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_ops_state_registered():
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
