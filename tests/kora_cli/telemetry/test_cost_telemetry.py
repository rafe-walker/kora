"""Tests for KR-CHEAP-COST-TELEMETRY — per-route counters.

Bucket §2 scenarios:

  Counter shape:
   1. record_call increments calls_count + per-route totals
   2. Token sums accumulate across multiple calls
   3. cost_estimate_usd_total sums; None values skipped
   4. escalation_count increments only when escalated_to_opus=True
   5. model_breakdown tracks per-model call counts

  Route taxonomy:
   6. Each canonical route accepted + bucketed correctly
   7. Unknown route string buckets to "unknown"
   8. Non-string route falls back to "unknown"

  Windows:
   9. record_call updates ALL three windows
  10. reset_window clears one window without affecting others
  11. reset_window with unknown window name is no-op + warns

  Concurrency:
  12. Concurrent record_call from multiple threads doesn't lose counts

  Singleton:
  13. get_telemetry returns same instance across calls
  14. _reset_singleton_for_tests gives a fresh instance

  Snapshot shape:
  15. snapshot returns dict with all 3 windows
  16. Each window has every known route pre-populated (stable shape)
  17. Snapshot is JSON-serializable

  Fail-soft:
  18. record_call with bad canonical_usage doesn't raise
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Optional

import pytest

from kora_cli.telemetry import (
    KNOWN_ROUTES,
    KNOWN_WINDOWS,
    ROUTE_ALERT_INVESTIGATION,
    ROUTE_EMAIL_INBOUND,
    ROUTE_MCP_TOOL,
    ROUTE_SLACK_DM,
    ROUTE_UNKNOWN,
    WINDOW_MONTHLY,
    WINDOW_PROCESS_LIFETIME,
    WINDOW_ROLLING_24H,
    CostRouteTelemetry,
    get_telemetry,
)
from kora_cli.telemetry.cost_telemetry import _reset_singleton_for_tests


@dataclass(frozen=True)
class _FakeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0


@pytest.fixture
def telemetry():
    """Fresh CostRouteTelemetry per test (bypasses the singleton so
    tests don't pollute each other)."""
    return CostRouteTelemetry()


# ===========================================================================
# Counter shape
# ===========================================================================


def test_record_call_increments_calls_count(telemetry):
    telemetry.record_call(
        route=ROUTE_SLACK_DM,
        model="claude-opus-4-7",
        canonical_usage=_FakeUsage(input_tokens=100, output_tokens=50),
        cost_estimate_usd=0.0042,
    )
    snap = telemetry.snapshot()
    assert snap[WINDOW_PROCESS_LIFETIME][ROUTE_SLACK_DM]["calls_count"] == 1
    assert snap[WINDOW_ROLLING_24H][ROUTE_SLACK_DM]["calls_count"] == 1
    assert snap[WINDOW_MONTHLY][ROUTE_SLACK_DM]["calls_count"] == 1


def test_token_sums_accumulate(telemetry):
    for _ in range(3):
        telemetry.record_call(
            route=ROUTE_SLACK_DM,
            model="claude-opus-4-7",
            canonical_usage=_FakeUsage(
                input_tokens=100,
                output_tokens=50,
                cache_read_tokens=200,
                cache_write_tokens=300,
            ),
            cost_estimate_usd=0.01,
        )
    row = telemetry.snapshot()[WINDOW_PROCESS_LIFETIME][ROUTE_SLACK_DM]
    assert row["calls_count"] == 3
    assert row["input_tokens_total"] == 300
    assert row["output_tokens_total"] == 150
    assert row["cache_read_tokens_total"] == 600
    assert row["cache_creation_tokens_total"] == 900
    assert row["cost_estimate_usd_total"] == pytest.approx(0.03)


def test_none_cost_skipped(telemetry):
    """Cost-estimate None values don't blow up the sum."""
    telemetry.record_call(
        route=ROUTE_SLACK_DM,
        model="claude-haiku-4-5",
        canonical_usage=_FakeUsage(input_tokens=10),
        cost_estimate_usd=None,
    )
    telemetry.record_call(
        route=ROUTE_SLACK_DM,
        model="claude-haiku-4-5",
        canonical_usage=_FakeUsage(input_tokens=10),
        cost_estimate_usd=0.0001,
    )
    row = telemetry.snapshot()[WINDOW_PROCESS_LIFETIME][ROUTE_SLACK_DM]
    assert row["calls_count"] == 2  # Both calls counted
    assert row["cost_estimate_usd_total"] == pytest.approx(0.0001)


def test_escalation_count_only_when_flag_true(telemetry):
    telemetry.record_call(
        route=ROUTE_SLACK_DM,
        model="claude-opus-4-7",
        canonical_usage=_FakeUsage(),
        cost_estimate_usd=0.01,
        escalated_to_opus=False,
    )
    telemetry.record_call(
        route=ROUTE_SLACK_DM,
        model="claude-opus-4-7",
        canonical_usage=_FakeUsage(),
        cost_estimate_usd=0.01,
        escalated_to_opus=True,
    )
    telemetry.record_call(
        route=ROUTE_SLACK_DM,
        model="claude-opus-4-7",
        canonical_usage=_FakeUsage(),
        cost_estimate_usd=0.01,
        escalated_to_opus=True,
    )
    row = telemetry.snapshot()[WINDOW_PROCESS_LIFETIME][ROUTE_SLACK_DM]
    assert row["calls_count"] == 3
    assert row["escalation_count"] == 2


def test_model_breakdown_tracks_per_model_calls(telemetry):
    for model in ["claude-opus-4-7", "claude-sonnet-4-6", "claude-opus-4-7"]:
        telemetry.record_call(
            route=ROUTE_SLACK_DM,
            model=model,
            canonical_usage=_FakeUsage(),
            cost_estimate_usd=0.001,
        )
    row = telemetry.snapshot()[WINDOW_PROCESS_LIFETIME][ROUTE_SLACK_DM]
    assert row["model_breakdown"] == {
        "claude-opus-4-7": 2,
        "claude-sonnet-4-6": 1,
    }


# ===========================================================================
# Route taxonomy
# ===========================================================================


def test_every_canonical_route_accepted(telemetry):
    for route in KNOWN_ROUTES:
        telemetry.record_call(
            route=route,
            model="claude-haiku-4-5",
            canonical_usage=_FakeUsage(input_tokens=1),
            cost_estimate_usd=0.0001,
        )
    snap = telemetry.snapshot()
    for route in KNOWN_ROUTES:
        assert snap[WINDOW_PROCESS_LIFETIME][route]["calls_count"] == 1


def test_unknown_route_string_buckets_to_unknown(telemetry):
    telemetry.record_call(
        route="this_is_not_a_real_route",
        model="claude-haiku-4-5",
        canonical_usage=_FakeUsage(),
        cost_estimate_usd=0.0001,
    )
    snap = telemetry.snapshot()
    assert snap[WINDOW_PROCESS_LIFETIME][ROUTE_UNKNOWN]["calls_count"] == 1
    # Other routes untouched.
    assert snap[WINDOW_PROCESS_LIFETIME][ROUTE_SLACK_DM]["calls_count"] == 0


def test_non_string_route_buckets_to_unknown(telemetry):
    telemetry.record_call(
        route=None,  # type: ignore[arg-type]
        model="claude-haiku-4-5",
        canonical_usage=_FakeUsage(),
        cost_estimate_usd=0.0001,
    )
    assert (
        telemetry.snapshot()[WINDOW_PROCESS_LIFETIME][ROUTE_UNKNOWN]["calls_count"]
        == 1
    )


# ===========================================================================
# Windows
# ===========================================================================


def test_record_call_updates_all_three_windows(telemetry):
    telemetry.record_call(
        route=ROUTE_SLACK_DM,
        model="claude-opus-4-7",
        canonical_usage=_FakeUsage(input_tokens=10),
        cost_estimate_usd=0.001,
    )
    snap = telemetry.snapshot()
    for window in KNOWN_WINDOWS:
        assert snap[window][ROUTE_SLACK_DM]["calls_count"] == 1
        assert snap[window][ROUTE_SLACK_DM]["input_tokens_total"] == 10


def test_reset_window_clears_one_window_only(telemetry):
    telemetry.record_call(
        route=ROUTE_SLACK_DM,
        model="claude-opus-4-7",
        canonical_usage=_FakeUsage(input_tokens=100),
        cost_estimate_usd=0.01,
    )
    telemetry.reset_window(WINDOW_ROLLING_24H)
    snap = telemetry.snapshot()
    # rolling_24h zeroed.
    assert snap[WINDOW_ROLLING_24H][ROUTE_SLACK_DM]["calls_count"] == 0
    assert snap[WINDOW_ROLLING_24H][ROUTE_SLACK_DM]["input_tokens_total"] == 0
    # process_lifetime + monthly untouched.
    assert snap[WINDOW_PROCESS_LIFETIME][ROUTE_SLACK_DM]["calls_count"] == 1
    assert snap[WINDOW_MONTHLY][ROUTE_SLACK_DM]["calls_count"] == 1


def test_reset_window_unknown_name_is_noop_and_warns(telemetry, caplog):
    telemetry.record_call(
        route=ROUTE_SLACK_DM,
        model="claude-opus-4-7",
        canonical_usage=_FakeUsage(input_tokens=10),
        cost_estimate_usd=0.001,
    )
    with caplog.at_level("WARNING"):
        telemetry.reset_window("not_a_real_window")
    snap = telemetry.snapshot()
    # All windows untouched.
    for window in KNOWN_WINDOWS:
        assert snap[window][ROUTE_SLACK_DM]["calls_count"] == 1
    assert any("unknown window" in r.message for r in caplog.records)


def test_reset_all_for_tests_clears_everything(telemetry):
    for route in [ROUTE_SLACK_DM, ROUTE_EMAIL_INBOUND, ROUTE_MCP_TOOL]:
        telemetry.record_call(
            route=route,
            model="claude-haiku-4-5",
            canonical_usage=_FakeUsage(input_tokens=10),
            cost_estimate_usd=0.001,
        )
    telemetry.reset_all_for_tests()
    snap = telemetry.snapshot()
    for window in KNOWN_WINDOWS:
        for route in KNOWN_ROUTES:
            assert snap[window][route]["calls_count"] == 0


# ===========================================================================
# Concurrency — basic safety check
# ===========================================================================


def test_concurrent_record_call_no_lost_counts(telemetry):
    """1000 calls split across 10 threads → final calls_count must
    be exactly 1000 (no lost increments under lock)."""
    calls_per_thread = 100
    threads = 10

    def worker():
        for _ in range(calls_per_thread):
            telemetry.record_call(
                route=ROUTE_SLACK_DM,
                model="claude-opus-4-7",
                canonical_usage=_FakeUsage(input_tokens=1, output_tokens=1),
                cost_estimate_usd=0.0001,
            )

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    row = telemetry.snapshot()[WINDOW_PROCESS_LIFETIME][ROUTE_SLACK_DM]
    assert row["calls_count"] == calls_per_thread * threads
    assert row["input_tokens_total"] == calls_per_thread * threads


# ===========================================================================
# Singleton
# ===========================================================================


def test_get_telemetry_returns_same_instance():
    _reset_singleton_for_tests()
    t1 = get_telemetry()
    t2 = get_telemetry()
    assert t1 is t2


def test_reset_singleton_gives_fresh_instance():
    _reset_singleton_for_tests()
    t1 = get_telemetry()
    t1.record_call(
        route=ROUTE_SLACK_DM,
        model="claude-opus-4-7",
        canonical_usage=_FakeUsage(input_tokens=1),
        cost_estimate_usd=0.001,
    )
    _reset_singleton_for_tests()
    t2 = get_telemetry()
    assert t2 is not t1
    snap = t2.snapshot()
    # Fresh — counters at zero.
    assert snap[WINDOW_PROCESS_LIFETIME][ROUTE_SLACK_DM]["calls_count"] == 0


# ===========================================================================
# Snapshot shape
# ===========================================================================


def test_snapshot_has_all_3_windows(telemetry):
    snap = telemetry.snapshot()
    assert set(snap.keys()) == set(KNOWN_WINDOWS)


def test_snapshot_pre_populates_every_route_per_window(telemetry):
    """Stable shape: every known route appears as a zero-counter
    entry in every window, even with no calls recorded."""
    snap = telemetry.snapshot()
    for window in KNOWN_WINDOWS:
        assert set(snap[window].keys()) == set(KNOWN_ROUTES)
        for route in KNOWN_ROUTES:
            row = snap[window][route]
            assert row["calls_count"] == 0
            assert row["model_breakdown"] == {}


def test_snapshot_is_json_serializable(telemetry):
    telemetry.record_call(
        route=ROUTE_SLACK_DM,
        model="claude-opus-4-7",
        canonical_usage=_FakeUsage(input_tokens=10),
        cost_estimate_usd=0.001,
        escalated_to_opus=True,
    )
    snap = telemetry.snapshot()
    # Roundtrip — if any value is non-serializable this raises.
    raw = json.dumps(snap)
    reparsed = json.loads(raw)
    assert reparsed[WINDOW_PROCESS_LIFETIME][ROUTE_SLACK_DM]["calls_count"] == 1


# ===========================================================================
# Fail-soft
# ===========================================================================


def test_record_call_bad_usage_no_raise(telemetry):
    """A canonical_usage that's not the expected shape shouldn't
    crash — defensive getattr fallback in _RouteCounters.add."""

    class BadUsage:
        # Missing all expected attrs; getattr fallbacks to 0.
        pass

    telemetry.record_call(
        route=ROUTE_SLACK_DM,
        model="claude-haiku-4-5",
        canonical_usage=BadUsage(),
        cost_estimate_usd=0.0,
    )
    row = telemetry.snapshot()[WINDOW_PROCESS_LIFETIME][ROUTE_SLACK_DM]
    assert row["calls_count"] == 1
    assert row["input_tokens_total"] == 0


def test_record_call_bad_cost_no_raise(telemetry):
    """Non-numeric cost_estimate_usd doesn't break the sum."""
    telemetry.record_call(
        route=ROUTE_SLACK_DM,
        model="claude-haiku-4-5",
        canonical_usage=_FakeUsage(input_tokens=5),
        cost_estimate_usd="not-a-number",  # type: ignore[arg-type]
    )
    row = telemetry.snapshot()[WINDOW_PROCESS_LIFETIME][ROUTE_SLACK_DM]
    assert row["calls_count"] == 1
    assert row["cost_estimate_usd_total"] == 0.0
