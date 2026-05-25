"""KR-PER-TENANT-AUDIT-JSONL — emit_audit + reader + BE endpoint
tenant scoping.

Validates the per-tenant audit JSONL contract from the
KR-TEST-STABILITY-FOLLOWUP-AND-PER-TENANT-AUDIT-JSONL megabucket:

  * Single-tenant default writes to the legacy path
    (backward-compat for existing readers).
  * Per-tenant writes route to ``<KORA_HOME>/audit/<tenant_id>/``.
  * Two distinct tenant_ids never cross-contaminate each other's
    JSONL.
  * Reader honors the tenant_id kwarg.
  * BE endpoint query-param name pinned 3-source (sink constant
    ↔ this test ↔ FE constant — FE pin in
    ``web/src/lib/audit.ts`` is grep-asserted below so a rename
    on either side breaks the test).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

import pytest

from kora_cli.audit import (
    DEFAULT_TENANT_ID,
    TENANT_ID_QUERY_PARAM_NAME,
    emit_audit,
)
from kora_cli.audit.jsonl_reader import read_audit_entries


@pytest.fixture(autouse=True)
def _disable_batching(monkeypatch):
    """Per-emit sync write so each test's writes are observable
    immediately. Batched flushing is exercised separately."""
    monkeypatch.setenv("KORA_AUDIT_BATCH_SIZE", "0")
    yield


@pytest.fixture
def kora_home(tmp_path, monkeypatch):
    """Point KORA_HOME at a tmp_path so writes don't pollute
    the developer's real ~/.kora."""
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.delenv("KORA_AUDIT_LOG_PATH", raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_emit_audit_with_no_tenant_writes_to_legacy_default_path(kora_home):
    """tenant_id=None → existing single-file path; no behavior
    change for current single-tenant deployments."""
    emit_audit(
        "mcp.tool_called",
        {"tool_name": "kora__noop", "caller_actor_kind": "operator"},
        source="mcp_http",
    )
    legacy = kora_home / "kora_audit_log.jsonl"
    assert legacy.is_file(), "default writes must land in <KORA_HOME>/kora_audit_log.jsonl"
    payload = json.loads(legacy.read_text().splitlines()[0])
    assert payload["seam"] == "mcp.tool_called"
    # The per-tenant subdir must NOT exist when nobody routed there.
    assert not (kora_home / "audit").exists()


def test_emit_audit_with_default_tenant_writes_to_legacy_default_path(kora_home):
    """tenant_id="default" is a no-op alias for the legacy path —
    callers can pass the sentinel explicitly without bifurcation."""
    emit_audit(
        "mcp.tool_called",
        {"tool_name": "kora__noop", "caller_actor_kind": "operator"},
        source="mcp_http",
        tenant_id=DEFAULT_TENANT_ID,
    )
    legacy = kora_home / "kora_audit_log.jsonl"
    assert legacy.is_file()
    assert not (kora_home / "audit").exists()


def test_emit_audit_with_explicit_tenant_writes_to_per_tenant_subdir(kora_home):
    """Per-tenant routing produces a fresh JSONL under
    ``<KORA_HOME>/audit/<tenant_id>/kora_audit_log.jsonl``."""
    emit_audit(
        "mcp.tool_called",
        {"tool_name": "marvin__inspect", "caller_actor_kind": "marvin"},
        source="mcp_http",
        tenant_id="marvin",
    )
    per_tenant = kora_home / "audit" / "marvin" / "kora_audit_log.jsonl"
    assert per_tenant.is_file()
    payload = json.loads(per_tenant.read_text().splitlines()[0])
    assert payload["details"]["caller_actor_kind"] == "marvin"
    # Legacy default-tenant file must NOT have been touched.
    legacy = kora_home / "kora_audit_log.jsonl"
    assert not legacy.exists()


def test_two_tenants_land_in_separate_files_no_cross_contamination(kora_home):
    """Two distinct tenant_ids never share storage — the audit
    panel for one tenant must not see another tenant's rows."""
    emit_audit(
        "mcp.tool_called",
        {"tool_name": "marvin__ping", "caller_actor_kind": "marvin"},
        source="mcp_http",
        tenant_id="marvin",
    )
    emit_audit(
        "mcp.tool_called",
        {"tool_name": "kora__ping", "caller_actor_kind": "kora"},
        source="mcp_http",
        tenant_id="other_tenant",
    )
    marvin = kora_home / "audit" / "marvin" / "kora_audit_log.jsonl"
    other = kora_home / "audit" / "other_tenant" / "kora_audit_log.jsonl"
    assert marvin.is_file()
    assert other.is_file()

    marvin_payloads = [json.loads(line) for line in marvin.read_text().splitlines()]
    other_payloads = [json.loads(line) for line in other.read_text().splitlines()]
    assert len(marvin_payloads) == 1
    assert len(other_payloads) == 1
    assert marvin_payloads[0]["details"]["tool_name"] == "marvin__ping"
    assert other_payloads[0]["details"]["tool_name"] == "kora__ping"


def test_path_traversal_in_tenant_id_falls_back_to_default(kora_home):
    """A caller passing ``"../"``-shaped tenant_id must not be able
    to write outside the audit/ subtree; resolver returns the
    legacy default path in that case."""
    emit_audit(
        "mcp.tool_called",
        {"tool_name": "kora__noop", "caller_actor_kind": "operator"},
        source="mcp_http",
        tenant_id="../escape",
    )
    legacy = kora_home / "kora_audit_log.jsonl"
    assert legacy.is_file()


# ---------------------------------------------------------------------------
# Reader-side queries
# ---------------------------------------------------------------------------


def test_reader_default_reads_default_tenant_only(kora_home):
    """Reader without tenant_id reads the legacy default path —
    per-tenant rows do not leak in."""
    emit_audit(
        "mcp.tool_called",
        {"tool_name": "default__ping", "caller_actor_kind": "operator"},
        source="mcp_http",
    )
    emit_audit(
        "mcp.tool_called",
        {"tool_name": "marvin__ping", "caller_actor_kind": "marvin"},
        source="mcp_http",
        tenant_id="marvin",
    )

    default_rows = read_audit_entries()
    assert len(default_rows) == 1
    assert default_rows[0].details["tool_name"] == "default__ping"


def test_reader_with_explicit_tenant_reads_only_that_tenant(kora_home):
    """Reader with tenant_id reads only that tenant's JSONL —
    default rows do not leak in."""
    emit_audit(
        "mcp.tool_called",
        {"tool_name": "default__ping", "caller_actor_kind": "operator"},
        source="mcp_http",
    )
    emit_audit(
        "mcp.tool_called",
        {"tool_name": "marvin__ping", "caller_actor_kind": "marvin"},
        source="mcp_http",
        tenant_id="marvin",
    )

    marvin_rows = read_audit_entries(tenant_id="marvin")
    assert len(marvin_rows) == 1
    assert marvin_rows[0].details["tool_name"] == "marvin__ping"


def test_reader_with_unknown_tenant_returns_empty_not_error(kora_home):
    """Reading a tenant with no JSONL file yet returns [] — fresh
    tenant before its first emit. Same fail-soft semantic as the
    default-tenant fresh-daemon path."""
    emit_audit(
        "mcp.tool_called",
        {"tool_name": "default__ping"},
        source="mcp_http",
    )
    rows = read_audit_entries(tenant_id="never_seen_this_tenant")
    assert rows == []


# ---------------------------------------------------------------------------
# Drift-guard pin: 3-source agreement on the query-param name
# ---------------------------------------------------------------------------


def test_tenant_id_query_param_name_constant_is_stable_wire_string():
    """BE allowlist + FE constant + test pin all import-pin on
    ``TENANT_ID_QUERY_PARAM_NAME``. If anyone renames the constant
    on the BE side, the FE pin below fails to grep — surfacing the
    drift before a deploy lands a 400-bad-request on the cockpit
    audit panel."""
    assert TENANT_ID_QUERY_PARAM_NAME == "tenant_id"


def test_fe_audit_helper_pins_tenant_id_query_param_to_the_be_constant():
    """3-source drift-guard: FE constant
    ``web/src/lib/audit.ts:TENANT_ID_QUERY_PARAM`` must equal the
    BE :data:`TENANT_ID_QUERY_PARAM_NAME` literal so the cockpit
    sends what the BE accepts. Stored as a TS const string so a
    string-grep is enough to verify."""
    fe_path = (
        Path(__file__).resolve().parents[3]
        / "web"
        / "src"
        / "lib"
        / "audit.ts"
    )
    if not fe_path.is_file():
        pytest.skip(
            "FE constant file not present in this checkout — pin "
            "deferred to CC#2's tenant-picker cockpit work which "
            "lands web/src/lib/audit.ts in a paired PR. When that "
            "lands, this skip flips to an active grep."
        )
    text = fe_path.read_text(encoding="utf-8")
    needle = f'"{TENANT_ID_QUERY_PARAM_NAME}"'
    assert needle in text, (
        f"web/src/lib/audit.ts must declare a constant whose value "
        f"is {needle!r} so FE → BE wire-name agreement is preserved. "
        f"Update the FE constant alongside the BE constant rename."
    )


# ---------------------------------------------------------------------------
# Backward-compat: existing call sites pass no tenant_id
# ---------------------------------------------------------------------------


def test_existing_callsites_with_no_tenant_id_kwarg_still_work(kora_home):
    """All 30+ pre-existing ``emit_audit(seam, details, ...)``
    callers omit ``tenant_id`` — pin that the kwarg-less call
    signature still produces the same legacy path."""
    emit_audit(
        "reasoning.tool_called",
        {
            "tool_name": "kora__noop",
            "caller_session_id": "sess-bc-pin",
            "tool_status": "ok",
        },
        source="reasoning",
        caller_session_id="sess-bc-pin",
    )
    legacy = kora_home / "kora_audit_log.jsonl"
    assert legacy.is_file()
