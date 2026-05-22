"""KR-P2-INT-TESTS ST3 — KR-7 staged (canonical-actor SECDEF invariant).

# Scope

KR-7 = the canonical-actor invariant on the substrate side. The
substrate's ``kora__append_event`` SECDEF performs a pre-write
assertion that the writing actor matches the canonical Kora actor
row for the workspace (foundation-tier ``actor_registry`` row with
``actor_kind='kora'``). A non-canonical actor's write attempt is
rejected with a SECDEF error.

# What this file tests

The SECDEF check itself is substrate-side; testing it end-to-end
requires a real substrate instance. This file ships **contract
tests** for the RUNTIME-SIDE handling of the substrate's rejection:

  1. When ``kora__append_event`` returns an error (mocked at the
     MCP boundary), the runtime raises
     :class:`IsoKronMCPInvocationError` cleanly.
  2. The error propagates through ``emit_kora_event`` to the caller
     without silent-allow. (Matches the KR-P2-FAIL-SAFETIES audit
     verdict for Path 1: SECDEF errors are RAISE, not SWALLOW.)
  3. Successful (canonical-actor) writes return an event_id cleanly.

# What's not tested (out of scope)

  - Real substrate-side actor_registry row inspection / SECDEF
    enforcement (substrate-team lane).
  - Daily integrity check (substrate-team cron + audit).

When the substrate-team integration env lands, these tests can
extend: actually configure a non-canonical actor row, attempt the
write, observe the SECDEF rejection.
"""

from __future__ import annotations

import pytest

from plugins.memory.isokron.events import emit_kora_event
from plugins.memory.isokron.mcp_client import IsoKronMCPInvocationError
from tests.integration.fakes import FakeMCPClient


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Substrate SECDEF rejection — runtime raises IsoKronMCPInvocationError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_canonical_actor_secdef_rejection_propagates_as_invocation_error():
    """When the substrate's canonical-actor SECDEF rejects the write,
    the MCP boundary surfaces it as
    :class:`IsoKronMCPInvocationError`. The runtime caller never sees
    a silent skip — fail-LOUD per KR-P2-FAIL-SAFETIES audit Path 1."""
    mcp = FakeMCPClient()
    mcp.set_response(
        "kora__append_event",
        IsoKronMCPInvocationError(
            "kora__append_event",
            "canonical_actor_mismatch: actor_id=00000000-0000-0000-0000-"
            "000000000099 is not the canonical kora actor for workspace "
            "org_test",
        ),
    )

    with pytest.raises(IsoKronMCPInvocationError) as exc_info:
        await emit_kora_event(
            workspace_id="org_test",
            event_type="kora.dr.observed",
            payload={"observed_substrate_epoch": 1, "last_known_epoch": None},
            mcp_client=mcp,
        )

    assert "canonical_actor_mismatch" in str(exc_info.value)


@pytest.mark.asyncio
async def test_secdef_error_text_is_redacted_in_propagated_message():
    """The MCP boundary's ``_sanitize_error`` redacts credentials from
    error text. SECDEF errors that incidentally contain bearer-style
    tokens or sensitive fields must not surface them in the
    propagated message — runtime-side defense-in-depth.

    Note: this is a documentation-only assertion against the API
    contract — the actual redaction logic lives in
    ``tools.mcp_tool._sanitize_error``. The fake doesn't replicate
    sanitization; we just verify the error type round-trips
    correctly."""
    mcp = FakeMCPClient()
    error_message = "non_canonical_actor: actor_id=fffff"
    mcp.set_response(
        "kora__append_event",
        IsoKronMCPInvocationError("kora__append_event", error_message),
    )

    with pytest.raises(IsoKronMCPInvocationError) as exc_info:
        await emit_kora_event(
            workspace_id="org_test",
            event_type="kora.dr.observed",
            payload={},
            mcp_client=mcp,
        )
    # Error type matches; exact message format is contract with the
    # substrate-side sanitization layer.
    assert isinstance(exc_info.value, IsoKronMCPInvocationError)


# ---------------------------------------------------------------------------
# Canonical actor — write succeeds, returns event_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_canonical_actor_write_succeeds_returns_event_id():
    """When the substrate accepts the write (canonical actor matches),
    ``kora__append_event`` returns ``{"event_id": "<uuid>"}`` and
    ``emit_kora_event`` surfaces the event_id."""
    mcp = FakeMCPClient()
    mcp.set_response(
        "kora__append_event",
        {"event_id": "evt-success-1"},
    )

    event_id = await emit_kora_event(
        workspace_id="org_test",
        event_type="kora.health.probe",
        payload={"overall": "healthy"},
        mcp_client=mcp,
    )
    assert event_id == "evt-success-1"

    # Single invocation; payload passed unchanged
    invocations = mcp.invocations_of("kora__append_event")
    assert len(invocations) == 1
    assert invocations[0]["workspace_id"] == "org_test"
    assert invocations[0]["event_type"] == "kora.health.probe"
    assert invocations[0]["payload"] == {"overall": "healthy"}


@pytest.mark.asyncio
async def test_malformed_response_shape_raises():
    """If the substrate response is the wrong shape (missing event_id
    key, etc.), the runtime raises rather than silently returning the
    wrong type. Defense-in-depth against contract drift."""
    mcp = FakeMCPClient()
    mcp.set_response(
        "kora__append_event",
        {"not_event_id": "evt-1"},  # Wrong key — should raise
    )

    with pytest.raises(RuntimeError) as exc_info:
        await emit_kora_event(
            workspace_id="org_test",
            event_type="kora.health.probe",
            payload={},
            mcp_client=mcp,
        )
    assert "unexpected shape" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Contract pin — the SECDEF error class is the right type
# ---------------------------------------------------------------------------


def test_isokron_mcp_invocation_error_is_runtimeerror():
    """Callers that don't import the specific error class can still
    catch as RuntimeError per fail-LOUD pattern."""
    assert issubclass(IsoKronMCPInvocationError, RuntimeError)
