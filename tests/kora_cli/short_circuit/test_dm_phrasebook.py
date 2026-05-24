"""Unit tests for KR-CHEAP-TRIVIAL-DM-SHORTCIRCUIT phrasebook module.

Covers:
  - load_phrasebook returns the bundled default when no override
  - load_phrasebook prefers an explicit path arg
  - operator override via ${KORA_HOME}/phrasebook/slack_dm.yml
  - Malformed override → WARN + fallback to default
  - Bundled default parses + contains expected categories
  - match_message: first-match-wins; case-insensitive
  - match_message: empty / whitespace-only / None text → None
  - render_reply with all fields present → rendered string
  - render_reply with missing snapshot → None
  - render_reply with literal "unknown" sentinel field → None
  - render_reply with None field → None
  - try_short_circuit: integration of match + render
  - try_short_circuit: no match → None
  - try_short_circuit: match but fall-through → None
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kora_cli.short_circuit.dm_phrasebook import (
    PhrasebookEntry,
    ShortCircuitMatch,
    load_phrasebook,
    match_message,
    render_reply,
    try_short_circuit,
)


def _fresh_snapshot() -> dict:
    """A canonical snapshot shape matching PR #157's schema."""
    return {
        "operational_state": {
            "primary": "ready",
            "paused": False,
            "pause_reason": None,
        },
        "alerts": {
            "active_count": 2,
            "by_severity": {"critical": 0, "warning": 1, "info": 1},
            "by_category": {"webhook_dead_letters": 1, "info_alert": 1},
        },
        "cost_ladder": {
            "current_tier": "normal",
            "monthly_budget_pct_used": 42.5,
            "model_default": "unknown",
        },
        "service_health": {
            "supabase": "healthy",
            "fly": "healthy",
            "vercel": "degraded",
            "sentry": "healthy",
            "doppler": "healthy",
        },
    }


# ---------------------------------------------------------------------------
# load_phrasebook
# ---------------------------------------------------------------------------


def test_load_bundled_default_returns_phrasebook(monkeypatch):
    """No override + KORA_HOME unset path → bundled default loads."""
    monkeypatch.setenv("KORA_HOME", "/nonexistent/kora/home")
    entries = load_phrasebook()
    assert len(entries) >= 5
    categories = {e.category for e in entries}
    # Spot-check core categories ship.
    for required in (
        "greeting",
        "burn_query",
        "status_query",
        "alert_query",
        "health_query",
    ):
        assert required in categories, (
            f"bundled default missing required category {required!r}"
        )


def test_load_phrasebook_accepts_explicit_path(tmp_path):
    """Explicit path arg overrides operator override + bundled."""
    custom = tmp_path / "custom.yml"
    custom.write_text(
        "entries:\n"
        "  - pattern: '^test$'\n"
        "    reply_template: 'only entry'\n"
        "    category: custom_test\n"
        "    description: one-off\n",
        encoding="utf-8",
    )
    entries = load_phrasebook(path=custom)
    assert len(entries) == 1
    assert entries[0].category == "custom_test"
    assert entries[0].reply_template == "only entry"


def test_load_phrasebook_prefers_operator_override(monkeypatch, tmp_path):
    """${KORA_HOME}/phrasebook/slack_dm.yml takes precedence over bundled."""
    override_dir = tmp_path / "phrasebook"
    override_dir.mkdir()
    (override_dir / "slack_dm.yml").write_text(
        "entries:\n"
        "  - pattern: '^operator-only$'\n"
        "    reply_template: 'override reply'\n"
        "    category: operator_override\n"
        "    description: edited by operator\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    entries = load_phrasebook()
    assert len(entries) == 1
    assert entries[0].category == "operator_override"


def test_load_phrasebook_malformed_override_falls_back(
    monkeypatch, tmp_path, caplog
):
    """Broken YAML in operator override → WARN log + bundled default."""
    import logging

    caplog.set_level(logging.WARNING)
    override_dir = tmp_path / "phrasebook"
    override_dir.mkdir()
    (override_dir / "slack_dm.yml").write_text(
        "entries:\n  - this is not a mapping",  # malformed entry
        encoding="utf-8",
    )
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    entries = load_phrasebook()
    # Got the bundled default, not the broken override.
    assert len(entries) >= 5
    warns = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("phrasebook" in r.getMessage() for r in warns)


def test_load_phrasebook_invalid_regex_in_override_falls_back(
    monkeypatch, tmp_path, caplog
):
    """Override with a syntactically broken regex → WARN + default."""
    import logging

    caplog.set_level(logging.WARNING)
    override_dir = tmp_path / "phrasebook"
    override_dir.mkdir()
    (override_dir / "slack_dm.yml").write_text(
        "entries:\n"
        "  - pattern: '[unclosed'\n"
        "    reply_template: 'never'\n"
        "    category: bad_re\n"
        "    description: invalid regex\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    entries = load_phrasebook()
    # Fell back to bundled default.
    assert len(entries) >= 5
    assert all(e.category != "bad_re" for e in entries)


# ---------------------------------------------------------------------------
# match_message
# ---------------------------------------------------------------------------


def test_match_message_case_insensitive():
    entries = load_phrasebook()
    assert match_message("Hey", entries).category == "greeting"
    assert match_message("HEY", entries).category == "greeting"
    assert match_message("hey", entries).category == "greeting"


def test_match_message_strips_whitespace():
    entries = load_phrasebook()
    assert match_message("  hello   ", entries).category == "greeting"


def test_match_message_first_match_wins(tmp_path):
    """Two patterns that both match → the first listed wins."""
    custom = tmp_path / "ordered.yml"
    custom.write_text(
        "entries:\n"
        "  - pattern: '^hello$'\n"
        "    reply_template: 'first'\n"
        "    category: first\n"
        "    description: first\n"
        "  - pattern: '^hello$'\n"
        "    reply_template: 'second'\n"
        "    category: second\n"
        "    description: second\n",
        encoding="utf-8",
    )
    entries = load_phrasebook(path=custom)
    assert match_message("hello", entries).category == "first"


def test_match_message_empty_and_whitespace_return_none():
    entries = load_phrasebook()
    assert match_message("", entries) is None
    assert match_message("   ", entries) is None
    assert match_message("\n\t  ", entries) is None


def test_match_message_non_string_returns_none():
    entries = load_phrasebook()
    assert match_message(None, entries) is None  # type: ignore[arg-type]
    assert match_message(42, entries) is None  # type: ignore[arg-type]


def test_match_message_no_match_returns_none():
    entries = load_phrasebook()
    # A full sentence Kora has no canned shape for.
    assert (
        match_message(
            "hey can you walk me through the migration plan?", entries
        )
        is None
    )


# ---------------------------------------------------------------------------
# render_reply
# ---------------------------------------------------------------------------


def test_render_reply_all_fields_present():
    entries = load_phrasebook()
    alerts_entry = next(e for e in entries if e.category == "alert_query")
    snapshot = _fresh_snapshot()
    out = render_reply(alerts_entry, snapshot)
    assert out is not None
    assert "2 active" in out
    assert "0 crit" in out
    assert "1 warn" in out
    assert "1 info" in out


def test_render_reply_returns_none_when_snapshot_none():
    entries = load_phrasebook()
    burn = next(e for e in entries if e.category == "burn_query")
    assert render_reply(burn, None) is None


def test_render_reply_returns_none_on_unknown_sentinel():
    """Snapshot field that equals literal 'unknown' (PR #157
    degraded sentinel) → fall through, don't ship half-filled."""
    entries = load_phrasebook()
    burn = next(e for e in entries if e.category == "burn_query")
    snap = _fresh_snapshot()
    snap["cost_ladder"]["current_tier"] = "unknown"
    assert render_reply(burn, snap) is None


def test_render_reply_returns_none_on_none_value():
    """Snapshot field is None → fall through."""
    entries = load_phrasebook()
    burn = next(e for e in entries if e.category == "burn_query")
    snap = _fresh_snapshot()
    snap["cost_ladder"]["monthly_budget_pct_used"] = None
    assert render_reply(burn, snap) is None


def test_render_reply_returns_none_on_missing_path():
    """Snapshot lacks an entire section → fall through."""
    entries = load_phrasebook()
    health = next(e for e in entries if e.category == "health_query")
    snap = _fresh_snapshot()
    del snap["service_health"]
    assert render_reply(health, snap) is None


def test_render_reply_renders_nested_dict_path():
    """alerts.by_severity.critical walks two levels deep."""
    entries = load_phrasebook()
    alerts = next(e for e in entries if e.category == "alert_query")
    out = render_reply(alerts, _fresh_snapshot())
    # The deep path resolved + interpolated.
    assert "0 crit / 1 warn / 1 info" in out


def test_render_reply_preserves_literal_punctuation():
    entries = load_phrasebook()
    greeting = next(e for e in entries if e.category == "greeting")
    out = render_reply(greeting, _fresh_snapshot())
    # Bare-greeting template has no placeholders, just text.
    assert out == "Hey. What's up?"


# ---------------------------------------------------------------------------
# try_short_circuit
# ---------------------------------------------------------------------------


def test_try_short_circuit_match_and_render():
    entries = load_phrasebook()
    result = try_short_circuit("status", entries, _fresh_snapshot())
    assert isinstance(result, ShortCircuitMatch)
    assert result.entry.category == "status_query"
    assert "ready" in result.reply_text


def test_try_short_circuit_no_match_returns_none():
    entries = load_phrasebook()
    assert (
        try_short_circuit(
            "tell me about quantum physics", entries, _fresh_snapshot()
        )
        is None
    )


def test_try_short_circuit_match_but_snapshot_none():
    """Match + missing snapshot → None (fall through to engine)."""
    entries = load_phrasebook()
    assert try_short_circuit("status", entries, None) is None


def test_try_short_circuit_match_but_degraded_field():
    """Match + degraded snapshot → None (fall through)."""
    entries = load_phrasebook()
    snap = _fresh_snapshot()
    snap["cost_ladder"]["monthly_budget_pct_used"] = "unknown"
    assert try_short_circuit("burn", entries, snap) is None


# ---------------------------------------------------------------------------
# Bundled default — sanity checks (operator review focus area)
# ---------------------------------------------------------------------------


def test_bundled_default_all_patterns_compile():
    """Every shipped pattern is valid regex (would've raised at
    load_phrasebook construction)."""
    entries = load_phrasebook()
    for e in entries:
        assert e.pattern is not None
        assert e.category != ""


def test_bundled_default_renders_for_fresh_snapshot():
    """Every entry that has placeholders renders successfully
    against the canonical fresh snapshot — no half-filled
    surprises in production."""
    entries = load_phrasebook()
    snap = _fresh_snapshot()
    for e in entries:
        if "{snapshot." not in e.reply_template:
            continue  # bare text — nothing to render
        rendered = render_reply(e, snap)
        assert rendered is not None, (
            f"entry {e.category!r} has placeholders that don't "
            f"render against the canonical snapshot — template: "
            f"{e.reply_template!r}"
        )


def test_bundled_default_falls_through_on_fully_degraded_snapshot():
    """Inverse: if everything is degraded ('unknown' / None),
    every entry with placeholders should fall through. Bare
    no-placeholder entries (greeting, thanks, ack) still render."""
    entries = load_phrasebook()
    degraded = {
        "operational_state": {
            "primary": "unknown",
            "paused": False,
            "pause_reason": None,
        },
        "alerts": {
            "active_count": "unknown",
            "by_severity": {
                "critical": "unknown",
                "warning": "unknown",
                "info": "unknown",
            },
        },
        "cost_ladder": {
            "current_tier": "unknown",
            "monthly_budget_pct_used": "unknown",
        },
        "service_health": {
            "supabase": "unknown",
            "fly": "unknown",
            "vercel": "unknown",
            "sentry": "unknown",
            "doppler": "unknown",
        },
    }
    for e in entries:
        rendered = render_reply(e, degraded)
        if "{snapshot." in e.reply_template:
            assert rendered is None, (
                f"entry {e.category!r} rendered against fully-degraded "
                f"snapshot — should have fallen through. Got: "
                f"{rendered!r}"
            )
        else:
            assert rendered is not None  # bare-text always renders
