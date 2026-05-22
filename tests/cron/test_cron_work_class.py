"""KR-P2-D ST1 — CronWorkClass enum + classifier + registration validation.

Covers:
  - Enum membership (4 values, exact strings)
  - coerce_work_class: operator-facing fail-CLOSED, internal default,
    string → enum, unknown string, type rejection
  - dispatch_for_work_class + raise_substrate_blocked: LOCAL/OUTBOUND
    pass through; SUBSTRATE_* raise CronSubstratePathNotYetImplemented
    with the right blocker reason embedded
  - cron.jobs.create_job stores work_class on the job dict
  - cron.scheduler._run_job_impl raises on SUBSTRATE_* jobs
  - Operator-facing surface (tools/cronjob_tools.cronjob) rejects
    create-with-no-work_class
"""

from __future__ import annotations

import pytest

from agent.cron_work_class import (
    CronSubstratePathNotYetImplemented,
    CronWorkClass,
    CronWorkClassError,
    DispatchDecision,
    coerce_work_class,
    dispatch_for_work_class,
    raise_substrate_blocked,
)


# ---------------------------------------------------------------------------
# Enum membership
# ---------------------------------------------------------------------------


def test_enum_has_exactly_four_members():
    members = {m.value for m in CronWorkClass}
    assert members == {
        "local_only",
        "outbound_msg",
        "substrate_heartbeat",
        "substrate_mutation",
    }


# ---------------------------------------------------------------------------
# coerce_work_class
# ---------------------------------------------------------------------------


def test_coerce_passes_through_enum_member():
    assert (
        coerce_work_class(
            CronWorkClass.LOCAL_ONLY,
            operator_facing=True,
            surface="test",
        )
        is CronWorkClass.LOCAL_ONLY
    )


def test_coerce_maps_string_to_enum():
    assert (
        coerce_work_class(
            "outbound_msg", operator_facing=True, surface="test"
        )
        is CronWorkClass.OUTBOUND_MSG
    )


def test_coerce_none_internal_defaults_to_local_only():
    """Non-operator-facing callers (test fixtures, internal helpers)
    can pass None and get LOCAL_ONLY back. This is the back-compat
    layer that keeps the bulk of existing test fixtures green."""
    assert (
        coerce_work_class(None, operator_facing=False, surface="test")
        is CronWorkClass.LOCAL_ONLY
    )


def test_coerce_none_operator_facing_raises():
    """Fail-CLOSED at the operator-facing surface."""
    with pytest.raises(CronWorkClassError) as exc_info:
        coerce_work_class(None, operator_facing=True, surface="cronjob tool")
    assert "cronjob tool" in str(exc_info.value)
    assert "work_class is required" in str(exc_info.value)
    assert "local_only" in str(exc_info.value)  # lists valid values


def test_coerce_unknown_string_raises():
    with pytest.raises(CronWorkClassError) as exc_info:
        coerce_work_class(
            "bogus_class", operator_facing=True, surface="test"
        )
    assert "bogus_class" in str(exc_info.value)


def test_coerce_unsupported_type_raises():
    with pytest.raises(CronWorkClassError):
        coerce_work_class(
            42, operator_facing=False, surface="test"
        )


# ---------------------------------------------------------------------------
# dispatch_for_work_class + raise_substrate_blocked
# ---------------------------------------------------------------------------


def test_local_only_dispatches_to_run_local_flow():
    decision = dispatch_for_work_class(
        CronWorkClass.LOCAL_ONLY, job_id="j1"
    )
    assert decision is DispatchDecision.RUN_LOCAL_FLOW


def test_outbound_msg_dispatches_to_run_local_flow():
    decision = dispatch_for_work_class(
        CronWorkClass.OUTBOUND_MSG, job_id="j1"
    )
    assert decision is DispatchDecision.RUN_LOCAL_FLOW


def test_substrate_heartbeat_dispatches_to_blocked():
    decision = dispatch_for_work_class(
        CronWorkClass.SUBSTRATE_HEARTBEAT, job_id="j1"
    )
    assert decision is DispatchDecision.BLOCKED_PENDING_SUBSTRATE


def test_substrate_mutation_dispatches_to_blocked():
    decision = dispatch_for_work_class(
        CronWorkClass.SUBSTRATE_MUTATION, job_id="j1"
    )
    assert decision is DispatchDecision.BLOCKED_PENDING_SUBSTRATE


def test_raise_substrate_blocked_heartbeat_names_vocab_blocker():
    with pytest.raises(CronSubstratePathNotYetImplemented) as exc_info:
        raise_substrate_blocked(
            CronWorkClass.SUBSTRATE_HEARTBEAT, job_id="job_abc"
        )
    msg = str(exc_info.value)
    assert "job_abc" in msg
    assert "kora.cron.tick_fired" in msg
    assert "foundation/0159" in msg
    assert "ST3" in msg


def test_raise_substrate_blocked_mutation_names_mcp_blocker():
    with pytest.raises(CronSubstratePathNotYetImplemented) as exc_info:
        raise_substrate_blocked(
            CronWorkClass.SUBSTRATE_MUTATION, job_id="job_xyz"
        )
    msg = str(exc_info.value)
    assert "job_xyz" in msg
    assert "sea__create_ticket" in msg
    assert "Sea-S3" in msg
    assert "ST4" in msg


# ---------------------------------------------------------------------------
# cron.jobs.create_job — work_class persists on the job dict
# ---------------------------------------------------------------------------


@pytest.fixture
def _isolate_jobs_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("kora_cli.config.get_kora_home", lambda: tmp_path)
    return tmp_path


def test_create_job_stores_work_class(_isolate_jobs_state):
    from cron.jobs import create_job

    job = create_job(
        prompt="say hi",
        schedule="30m",
        work_class=CronWorkClass.OUTBOUND_MSG,
    )
    assert job["work_class"] == "outbound_msg"


def test_create_job_raises_when_work_class_omitted(_isolate_jobs_state):
    """KR-P2-D ST2: fail-CLOSED at the lowest layer. ST1's internal
    LOCAL_ONLY default has been removed — every caller (including
    test fixtures + internal helpers) must declare explicitly."""
    from cron.jobs import create_job

    with pytest.raises(CronWorkClassError) as exc_info:
        create_job(prompt="tick", schedule="30m")
    assert "cron.jobs.create_job" in str(exc_info.value)
    assert "work_class is required" in str(exc_info.value)


def test_create_job_accepts_string_value(_isolate_jobs_state):
    from cron.jobs import create_job

    job = create_job(
        prompt="tick", schedule="30m", work_class="substrate_heartbeat"
    )
    # Even though SUBSTRATE_* paths are runtime-blocked, the persistence
    # layer accepts the declaration. Runtime fires the dispatcher's
    # raise; registration itself succeeds. Audit pass (ST2) and
    # substrate work (ST3) unblock the runtime.
    assert job["work_class"] == "substrate_heartbeat"


# ---------------------------------------------------------------------------
# Scheduler _run_job_impl — SUBSTRATE_* raises
# ---------------------------------------------------------------------------


def test_scheduler_run_job_raises_on_substrate_heartbeat():
    """SUBSTRATE_HEARTBEAT job hits the dispatcher's blocked branch and
    raises before the scheduler tries to run the agent or the script."""
    from cron.scheduler import _run_job_impl

    job = {
        "id": "j1",
        "prompt": "doesn't matter",
        "schedule": {"kind": "periodic", "every_seconds": 3600},
        "work_class": "substrate_heartbeat",
    }
    with pytest.raises(CronSubstratePathNotYetImplemented) as exc_info:
        _run_job_impl(job)
    assert "kora.cron.tick_fired" in str(exc_info.value)


def test_scheduler_run_job_raises_on_substrate_mutation():
    from cron.scheduler import _run_job_impl

    job = {
        "id": "j2",
        "prompt": "doesn't matter",
        "schedule": {"kind": "periodic", "every_seconds": 3600},
        "work_class": "substrate_mutation",
    }
    with pytest.raises(CronSubstratePathNotYetImplemented) as exc_info:
        _run_job_impl(job)
    assert "sea__create_ticket" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Operator-facing surface — cronjob tool create action
# ---------------------------------------------------------------------------


def test_cronjob_tool_create_rejects_missing_work_class(
    _isolate_jobs_state, monkeypatch
):
    """The agent's cronjob tool must reject create with no work_class
    (fail-CLOSED at the operator-facing surface). Returns a tool_error
    JSON string (not a raise) because that's the tool-invocation
    convention."""
    import json

    from tools.cronjob_tools import cronjob

    result_json = cronjob(action="create", schedule="30m", prompt="hi")
    result = json.loads(result_json)
    assert result.get("success") is False
    assert "work_class is required" in result.get("error", "")


def test_cronjob_tool_create_accepts_explicit_work_class(
    _isolate_jobs_state, monkeypatch
):
    import json

    from tools.cronjob_tools import cronjob

    result_json = cronjob(
        action="create",
        schedule="30m",
        prompt="hi",
        work_class="local_only",
    )
    result = json.loads(result_json)
    # Successful creation surfaces the job_id; failure surfaces an
    # error. We accept either as long as the failure isn't because
    # of work_class (other env failures — e.g. missing skills dir —
    # are out of scope here).
    if result.get("success") is False:
        assert "work_class is required" not in result.get("error", "")
