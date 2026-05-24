"""Structural tests for the post_llm_call_can_reissue hook
firing block in ``agent/conversation_loop.py``.

The full ``run_conversation`` function is 4000+ lines and has
heavy setup requirements (transport, streaming, retry loop,
session DB, telemetry, etc.) — exercising it end-to-end for one
hook invocation is too much surface area. Instead we pin the
contract at the source level + verify the hook firing logic in
isolation via ``PluginManager``.

Coverage:

  1. Source contains the expected invoke_hook call with the
     contract kwargs (response / api_kwargs / agent / iteration
     / task_id / session_id / route).
  2. Source contains the anti-loop ``break`` after the re-issue
     — guarantees at most ONE re-issue per iteration.
  3. Source places the hook AFTER the retry-loop guard
     (``if response is None: ... break``) and BEFORE the
     ``normalize_response`` transport call.
  4. The telemetry feed for re-issued calls passes
     ``escalated_to_opus=True``.
"""

from __future__ import annotations

from pathlib import Path

import pytest


_LOOP_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "agent"
    / "conversation_loop.py"
).read_text()


def test_source_contains_post_llm_call_can_reissue_invocation():
    """The new hook fires with the named ``post_llm_call_can_reissue``
    hook ID — pinning the wire so future renames don't drift it
    silently."""
    assert (
        '"post_llm_call_can_reissue"' in _LOOP_SOURCE
        or "'post_llm_call_can_reissue'" in _LOOP_SOURCE
    )


def test_source_passes_contract_kwargs():
    """The hook contract documented in the bucket spec requires
    these kwargs to be present in the firing site."""
    for kw in (
        "response=response",
        "api_kwargs=api_kwargs",
        "agent=agent",
        "iteration=api_call_count",
        "task_id=effective_task_id",
        "session_id=agent.session_id",
        "route=",
    ):
        assert kw in _LOOP_SOURCE, (
            f"missing contract kwarg in hook firing: {kw!r}"
        )


def test_source_has_anti_loop_break():
    """After a successful re-issue, the loop MUST ``break`` so
    no second plugin can chain another re-issue (would cause
    infinite escalation chains)."""
    # The hook firing block ends with `break  # anti-loop:`.
    assert "anti-loop:" in _LOOP_SOURCE


def test_source_places_hook_after_retry_exhaustion_guard():
    """The hook MUST fire AFTER the retry-exhaustion guard
    (``if response is None: ... break``) — firing inside the
    retry loop would expose plugins to invalid responses + half-
    initialized state."""
    guard = "all_retries_exhausted_no_response"
    hook = "post_llm_call_can_reissue"
    g_idx = _LOOP_SOURCE.find(guard)
    h_idx = _LOOP_SOURCE.find(hook)
    assert g_idx != -1 and h_idx != -1
    assert g_idx < h_idx, (
        "post_llm_call_can_reissue hook must fire after the retry-"
        "exhaustion guard"
    )


def test_source_places_hook_before_post_api_request_observer():
    """The hook MUST fire BEFORE the per-iteration
    ``post_api_request`` observer so observers always see the
    final (possibly-re-issued) response. The spec's "post_llm_call
    observers see final response only" guarantee depends on this
    ordering."""
    # We want the LAST hook firing site (the new one we added) to
    # come before the post_api_request OBSERVER (not the literal
    # string "post_api_request" which appears in our own
    # docstring earlier). Use the unique observer signature.
    hook = "post_llm_call_can_reissue"
    observer_marker = '"post_api_request"'
    h_idx = _LOOP_SOURCE.find(hook)
    o_idx = _LOOP_SOURCE.find(observer_marker)
    assert h_idx != -1 and o_idx != -1
    assert h_idx < o_idx, (
        "post_llm_call_can_reissue hook must fire before the "
        "post_api_request observer fires"
    )


def test_source_telemetry_escalated_to_opus_for_reissue():
    """When a re-issue fires, the cost-ladder feed for the new
    response MUST pass ``escalated_to_opus=True`` so cockpit
    panels can compute escalation rate."""
    # Look for the specific re-issue telemetry call (not the
    # original-call site — that one defaults to False).
    assert "escalated_to_opus=True" in _LOOP_SOURCE


# ---------------------------------------------------------------------------
# Behavioral test: end-to-end through PluginManager
# ---------------------------------------------------------------------------


def test_reissue_plugin_dispatch_via_plugin_manager():
    """Drive the same invoke_hook semantics the conversation_loop
    uses. Verifies a real ``PluginManager`` returns the right
    shape for the loop to consume."""
    from kora_cli.plugins import PluginManager

    mgr = PluginManager()

    capture: dict = {}

    def reissue_plugin(**kw):
        # Capture the kwargs the loop will pass us — pins the
        # contract from the consumer side.
        capture.update(kw)
        return {"reissue_with": {"model": "claude-opus-4-7"}}

    mgr._hooks["post_llm_call_can_reissue"] = [reissue_plugin]
    results = mgr.invoke_hook(
        "post_llm_call_can_reissue",
        response=object(),
        api_kwargs={"model": "claude-haiku-4-5-20251001"},
        agent=object(),
        iteration=1,
        task_id="t1",
        session_id="s1",
        route="slack_dm",
    )
    assert len(results) == 1
    assert results[0]["reissue_with"]["model"] == "claude-opus-4-7"
    # Contract kwargs visible to the plugin
    assert set(capture.keys()) >= {
        "response",
        "api_kwargs",
        "agent",
        "iteration",
        "task_id",
        "session_id",
        "route",
    }


# ---------------------------------------------------------------------------
# KR-CC3-CLEANUP follow-up A — escalation_reason structured telemetry
# ---------------------------------------------------------------------------


def test_source_threads_escalation_reason_from_plugin_to_telemetry():
    """The reissue site MUST read ``escalation_reason`` from the
    plugin result and pass it through ``record_inference_from_response``
    so the cost-telemetry per-reason breakdown lights up."""
    # The literal that pins both the plugin-read and the telemetry-pass.
    assert '_reissue_result.get(\n                    "escalation_reason"\n                )' in _LOOP_SOURCE or '_reissue_result.get("escalation_reason")' in _LOOP_SOURCE, (
        "reissue site must read escalation_reason from plugin result"
    )
    assert "escalation_reason=_escalation_reason" in _LOOP_SOURCE, (
        "reissue telemetry call must forward escalation_reason"
    )


# ---------------------------------------------------------------------------
# KR-CC3-CLEANUP follow-up B — api_call_count accounting pin
# ---------------------------------------------------------------------------


def test_source_does_not_bump_api_call_count_on_reissue():
    """The post-#189 confirmation: re-issue is part of the SAME
    iteration; ``api_call_count`` is NOT incremented when a
    re-issue fires. Pin the absence of a bump between the hook
    start and the post_api_request observer.

    Strategy: slice the source from the hook firing line to the
    post_api_request observer fire site, then assert there is no
    ``api_call_count += `` (any variant) inside that slice.
    """
    hook = "post_llm_call_can_reissue"
    observer_marker = '"post_api_request"'
    h_idx = _LOOP_SOURCE.find(hook)
    o_idx = _LOOP_SOURCE.find(observer_marker)
    assert h_idx != -1 and o_idx != -1 and h_idx < o_idx
    slice_text = _LOOP_SOURCE[h_idx:o_idx]
    # Any of these forms would be a bump:
    for bump in (
        "api_call_count += 1",
        "api_call_count = api_call_count + 1",
        "api_call_count+=1",
    ):
        assert bump not in slice_text, (
            f"reissue path must NOT bump api_call_count "
            f"(found {bump!r}); per #189 follow-up B (transparent "
            f"upgrade semantic) the re-issue is part of the same "
            f"iteration."
        )


def test_source_does_not_consume_iteration_budget_on_reissue():
    """Mirror of the api_call_count pin for ``iteration_budget``.
    The budget tracks logical iterations; a transparent re-issue
    upgrade must not consume an extra slot."""
    hook = "post_llm_call_can_reissue"
    observer_marker = '"post_api_request"'
    h_idx = _LOOP_SOURCE.find(hook)
    o_idx = _LOOP_SOURCE.find(observer_marker)
    assert h_idx != -1 and o_idx != -1 and h_idx < o_idx
    slice_text = _LOOP_SOURCE[h_idx:o_idx]
    for consume in (
        "iteration_budget.consume(",
        "iteration_budget.use(",
    ):
        assert consume not in slice_text, (
            f"reissue path must NOT consume iteration_budget "
            f"(found {consume!r}); per #189 follow-up B"
        )
