"""Tests for KR-PROBE-AUDIT-AND-CONVERT — fix-envelope declarations.

Bucket §2 Phase 4 scenarios:
   1. Each of the 5 probes has an envelope entry
   2. ``fly`` envelope has a concrete fix_name (the only non-none v1)
   3. supabase / vercel / sentry / doppler envelopes are explicit "(none)"
   4. is_envelope_enabled default False (env unset)
   5. is_envelope_enabled True only with truthy env AND non-none envelope
   6. Truthy values: true / 1 / yes / on (case-insensitive)
   7. is_envelope_enabled is False for "(none)" envelopes even if env truthy
   8. Unknown probe → is_envelope_enabled False
   9. requires_capability field on fly envelope reserves the cap literal
"""

from __future__ import annotations

import pytest

from kora_cli.probes.fix_envelopes import (
    ENABLE_ENV_DOPPLER,
    ENABLE_ENV_FLY,
    ENABLE_ENV_SENTRY,
    ENABLE_ENV_SUPABASE,
    ENABLE_ENV_VERCEL,
    ENVELOPES,
    is_envelope_enabled,
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """All envelope envs unset by default — fail-CLOSED."""
    for env in (
        ENABLE_ENV_SUPABASE,
        ENABLE_ENV_FLY,
        ENABLE_ENV_VERCEL,
        ENABLE_ENV_SENTRY,
        ENABLE_ENV_DOPPLER,
    ):
        monkeypatch.delenv(env, raising=False)


# ===========================================================================
# Declaration shape
# ===========================================================================


def test_every_probe_has_envelope_entry():
    assert set(ENVELOPES.keys()) == {
        "supabase",
        "fly",
        "vercel",
        "sentry",
        "doppler",
    }


def test_fly_envelope_is_only_non_none_v1():
    """v1 ships only the fly envelope. Spec §2 Phase 4 makes this
    explicit; the others are documented as operator-required."""
    fly = ENVELOPES["fly"]
    assert fly.fix_name == "restart_unhealthy_machine"
    assert "single_machine_not_started" in fly.in_envelope
    # OUT-of-envelope reflects the spec's "anything multi-machine"
    # exclusion — operator decides.
    assert "deploy_rollback" in fly.out_of_envelope
    assert "scale_up" in fly.out_of_envelope
    assert "multi_machine_restart" in fly.out_of_envelope


def test_other_probes_envelope_explicit_none():
    for probe in ("supabase", "vercel", "sentry", "doppler"):
        assert ENVELOPES[probe].fix_name == "(none)"
        # in_envelope set is empty for "(none)" envelopes.
        assert ENVELOPES[probe].in_envelope == frozenset()


def test_fly_envelope_reserves_capability_literal():
    """Capability-matrix gate literal is RESERVED in the envelope
    declaration so when the executor follow-on bucket lands, the
    cap-matrix audit can include this expected surface."""
    assert ENVELOPES["fly"].requires_capability == "probe_autofix_fly_restart"


# ===========================================================================
# is_envelope_enabled — fail-CLOSED defaults
# ===========================================================================


def test_default_all_disabled():
    """No envs set → every probe's envelope is disabled."""
    for probe in ENVELOPES:
        assert is_envelope_enabled(probe) is False


def test_fly_envelope_enables_with_true(monkeypatch):
    monkeypatch.setenv(ENABLE_ENV_FLY, "true")
    assert is_envelope_enabled("fly") is True


def test_fly_envelope_enables_with_alternate_truthy(monkeypatch):
    for val in ("1", "yes", "on", "TRUE", "Yes", "ON"):
        monkeypatch.setenv(ENABLE_ENV_FLY, val)
        assert is_envelope_enabled("fly") is True, (
            f"value {val!r} should enable but didn't"
        )


def test_fly_envelope_stays_off_for_falsy(monkeypatch):
    for val in ("false", "0", "no", "off", "", " ", "False", "garbage"):
        monkeypatch.setenv(ENABLE_ENV_FLY, val)
        assert is_envelope_enabled("fly") is False, (
            f"value {val!r} should NOT enable but did"
        )


def test_none_envelopes_stay_off_even_with_truthy_env(monkeypatch):
    """Setting KORA_PROBE_AUTOFIX_SUPABASE_ENABLED=true MUST NOT
    enable a non-existent envelope — fail-CLOSED."""
    monkeypatch.setenv(ENABLE_ENV_SUPABASE, "true")
    monkeypatch.setenv(ENABLE_ENV_VERCEL, "1")
    monkeypatch.setenv(ENABLE_ENV_SENTRY, "yes")
    monkeypatch.setenv(ENABLE_ENV_DOPPLER, "on")
    for probe in ("supabase", "vercel", "sentry", "doppler"):
        assert is_envelope_enabled(probe) is False


def test_unknown_probe_disabled():
    assert is_envelope_enabled("not_a_real_probe") is False
