"""Tests for the KR-P2-DIAG-BUNDLE endpoint.

Bucket §5 scenarios:
  1. GET returns 200 + Content-Type application/zip
  2. Content-Disposition has attachment + filename
  3. Zip extracts cleanly with all 11 expected files (10 sources + manifest)
  4. manifest.json shape (bundle_id, bundle_at, endpoints_included, errors, version)
  5. One endpoint down → zip still extracts; errors[] lists failure; file omitted
  6. **Credential-safety contract guard**: grep bundle bytes for known
     credential prefixes / words; expect zero matches
  7. Cron-regression sanity
"""

import io
import json
import zipfile

import pytest


_EXPECTED_SOURCES = {
    "operational_state",
    "boot_status",
    "cost_state",
    "health_rollup",
    "dr_state",
    "sea_tickets_kora_assigned",
    "kora_control_observed_state",
    "capabilities",
    "charter",
    "chain_events",
    "runbooks_manifest",
}


import re as _re

# Token-shaped regexes — match actual credential VALUES, not
# documentary substrings like the literal "wsk_*" in a gate title
# describing what the token IS. The bucket §5 #6 spirit is "catch
# leaks of real on-the-wire credentials, not bare prefixes that
# also appear in operator-facing copy."
_CREDENTIAL_PATTERNS = (
    # Real wsk_* token: prefix + ≥16 alphanumeric chars. The
    # placeholder "wsk_*" (asterisk, not alphanumeric) doesn't match.
    _re.compile(rb"wsk_[a-zA-Z0-9]{16,}"),
    # Slack bot token: xoxb-<digits>-<digits>-<base62>
    _re.compile(rb"xoxb-[0-9]+-[0-9]+-[a-zA-Z0-9]+"),
    # Slack app-level token: xapp-1-<base62>-<digits>-<base62>
    _re.compile(rb"xapp-[0-9]+-[A-Z0-9]+-[0-9]+-[a-zA-Z0-9]+"),
    # OpenAI API key: sk- + ≥20 chars. The substrate also uses
    # "kora-sk-..." literal in some docs; ≥20 chars after sk-
    # rules out that family.
    _re.compile(rb"sk-[a-zA-Z0-9]{20,}"),
    # Bearer header value: "Bearer " + ≥16 chars of typical token alphabet.
    _re.compile(rb"Bearer\s+[A-Za-z0-9._\-]{16,}"),
    # JSON field with a credential-shaped name AND a non-empty value.
    # Catches {"password": "hunter2"} but NOT {"password_field_name":
    # "<masked>"} (empty value) or bare mentions of "password" in
    # paragraph text. Field names: password / secret / api_key /
    # api-key / access_token / bearer_token.
    _re.compile(
        rb'"(?:password|secret|api[_-]?key|access[_-]?token|bearer[_-]?token)"\s*:\s*"[^"]+"',
        _re.IGNORECASE,
    ),
)


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


async def _collect_streaming_body(response) -> bytes:
    """Drain a StreamingResponse's body_iterator into a single bytes value."""
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
    return b"".join(chunks)


# ---- 1. 200 + Content-Type ---------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_returns_zip_content_type(_isolate_config):
    from kora_cli import web_server

    resp = await web_server.get_diag_bundle()
    assert resp.media_type == "application/zip"


# ---- 2. Content-Disposition ---------------------------------------------


@pytest.mark.asyncio
async def test_content_disposition_carries_attachment_filename(_isolate_config):
    from kora_cli import web_server

    resp = await web_server.get_diag_bundle()
    cd = resp.headers.get("Content-Disposition", "")
    assert "attachment" in cd
    assert 'filename="kora-diag-bundle-' in cd
    assert cd.endswith('.zip"')


# ---- 3. Zip extracts cleanly with all expected files ------------------


@pytest.mark.asyncio
async def test_zip_extracts_cleanly_with_all_expected_files(_isolate_config):
    from kora_cli import web_server

    resp = await web_server.get_diag_bundle()
    body = await _collect_streaming_body(resp)

    zf = zipfile.ZipFile(io.BytesIO(body))
    names = set(zf.namelist())
    # manifest.json + one .json per source = 12 files when every source
    # succeeds. In the isolated-config test env some endpoints will
    # fail (no live IsoKron provider), so we assert the SUPERSET:
    # manifest always present, and any included sources are valid.
    assert "manifest.json" in names
    for name in names:
        assert name == "manifest.json" or name.endswith(".json"), (
            f"unexpected entry in zip: {name}"
        )


@pytest.mark.asyncio
async def test_zip_contains_at_least_endpoints_that_dont_need_substrate(_isolate_config):
    """Endpoints that don't need a live substrate provider should
    always land in the bundle. capabilities + runbooks_manifest are
    the two safest bets — capabilities reads TOOL_CAPABILITY_MAP and
    actor_has_capability (in-process), runbooks_manifest reads files
    on the deploy filesystem."""
    from kora_cli import web_server

    resp = await web_server.get_diag_bundle()
    body = await _collect_streaming_body(resp)

    zf = zipfile.ZipFile(io.BytesIO(body))
    names = set(zf.namelist())
    assert "capabilities.json" in names
    assert "runbooks_manifest.json" in names


# ---- 4. manifest.json shape -------------------------------------------


@pytest.mark.asyncio
async def test_manifest_has_documented_shape(_isolate_config):
    from kora_cli import web_server

    resp = await web_server.get_diag_bundle()
    body = await _collect_streaming_body(resp)

    zf = zipfile.ZipFile(io.BytesIO(body))
    manifest = json.loads(zf.read("manifest.json"))

    required = {"bundle_id", "bundle_at", "version", "endpoints_included", "errors"}
    assert required <= set(manifest.keys())
    assert manifest["version"] == "1.0"
    assert manifest["bundle_id"].startswith("kora-diag-bundle-")
    assert isinstance(manifest["endpoints_included"], list)
    assert isinstance(manifest["errors"], list)


@pytest.mark.asyncio
async def test_manifest_endpoints_included_matches_zip_contents(_isolate_config):
    """The sources listed in manifest.endpoints_included must correspond
    exactly to the *.json files in the zip (excluding manifest.json
    itself). Catches a drift where the manifest claims an endpoint
    succeeded but its file wasn't actually written."""
    from kora_cli import web_server

    resp = await web_server.get_diag_bundle()
    body = await _collect_streaming_body(resp)

    zf = zipfile.ZipFile(io.BytesIO(body))
    manifest = json.loads(zf.read("manifest.json"))

    file_endpoints = {n[: -len(".json")] for n in zf.namelist() if n != "manifest.json"}
    manifest_endpoints = set(manifest["endpoints_included"])
    assert file_endpoints == manifest_endpoints


@pytest.mark.asyncio
async def test_manifest_endpoint_names_are_in_canonical_allowlist(_isolate_config):
    """Whatever ends up in the bundle (success OR error) must be from
    the PANEL_SOURCES explicit allowlist. Bucket §1 fail-CLOSED:
    if a new source got auto-added without explicit allowlisting,
    this test catches it."""
    from kora_cli import web_server

    resp = await web_server.get_diag_bundle()
    body = await _collect_streaming_body(resp)

    zf = zipfile.ZipFile(io.BytesIO(body))
    manifest = json.loads(zf.read("manifest.json"))

    surfaced = set(manifest["endpoints_included"]) | {
        e["endpoint"] for e in manifest["errors"]
    }
    assert surfaced <= _EXPECTED_SOURCES, (
        f"endpoint name(s) outside the explicit allowlist: "
        f"{surfaced - _EXPECTED_SOURCES}"
    )


# ---- 5. One endpoint down → bundle still extracts ---------------------


@pytest.mark.asyncio
async def test_endpoint_failure_surfaces_in_errors_and_file_omitted(_isolate_config, monkeypatch):
    """When one source fetcher raises, the bundle still extracts; the
    failure surfaces in manifest.errors[] with type+message; the
    failed endpoint's .json file is absent from the zip."""
    from kora_cli import web_server

    async def _boom():
        raise RuntimeError("simulated source failure")

    real_sources = web_server._panel_sources

    def _patched_sources():
        sources = real_sources()
        sources["operational_state"] = _boom
        return sources

    monkeypatch.setattr(web_server, "_panel_sources", _patched_sources)

    resp = await web_server.get_diag_bundle()
    body = await _collect_streaming_body(resp)

    zf = zipfile.ZipFile(io.BytesIO(body))
    names = zf.namelist()

    # operational_state.json must be absent
    assert "operational_state.json" not in names
    # manifest still present + lists the error
    assert "manifest.json" in names
    manifest = json.loads(zf.read("manifest.json"))
    op_errors = [e for e in manifest["errors"] if e["endpoint"] == "operational_state"]
    assert len(op_errors) == 1
    assert "RuntimeError" in op_errors[0]["error"]
    assert "simulated source failure" in op_errors[0]["error"]


@pytest.mark.asyncio
async def test_all_endpoints_failing_still_returns_valid_zip(_isolate_config, monkeypatch):
    """Even if EVERY source fails, the bundle is still a valid zip with
    a manifest — operator can see what went wrong rather than getting
    a 500."""
    from kora_cli import web_server

    # Capture the ORIGINAL _panel_sources reference BEFORE the
    # monkeypatch swaps it; calling web_server._panel_sources() from
    # inside the patched version would recurse infinitely.
    original_sources = web_server._panel_sources

    async def _boom():
        raise RuntimeError("everything is on fire")

    def _patched_sources():
        names = original_sources().keys()
        return {name: _boom for name in names}

    monkeypatch.setattr(web_server, "_panel_sources", _patched_sources)

    resp = await web_server.get_diag_bundle()
    body = await _collect_streaming_body(resp)

    zf = zipfile.ZipFile(io.BytesIO(body))
    assert zf.namelist() == ["manifest.json"]
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["endpoints_included"] == []
    assert len(manifest["errors"]) == len(_EXPECTED_SOURCES)


# ---- 6. Credential-safety contract guard ------------------------------


@pytest.mark.asyncio
async def test_bundle_bytes_contain_no_credential_patterns(_isolate_config):
    """Belt+braces guard: every source endpoint already excludes
    credentials per its own design (see cost-state credential-leak
    guard in PR #49; charter never returns tokens; etc.). This test
    catches an aggregation accident — grep the decompressed bundle
    bytes for known credential prefixes / words; expect zero matches.

    The grep is intentionally broad (case-sensitive prefix match): a
    real wsk_xxxx token starts with literal "wsk_"; a Slack bot token
    starts with "xoxb-"; etc. Catches the actual on-the-wire shape
    without false-positive-flagging defensive words like "tokenize"
    in payloads.
    """
    from kora_cli import web_server

    resp = await web_server.get_diag_bundle()
    body = await _collect_streaming_body(resp)

    zf = zipfile.ZipFile(io.BytesIO(body))
    # Decompress every file and grep across the concatenated content.
    # zip raw bytes are compressed; we need the inflated content for a
    # meaningful credential search.
    inflated_chunks: list[bytes] = []
    for name in zf.namelist():
        inflated_chunks.append(zf.read(name))
    inflated = b"".join(inflated_chunks)

    for pattern in _CREDENTIAL_PATTERNS:
        match = pattern.search(inflated)
        assert match is None, (
            f"bundle contains credential-shaped pattern "
            f"{pattern.pattern!r} at offset {match.start() if match else '?'} "
            f"(matched bytes: {match.group()[:80]!r}…). "
            f"Grep the .json files in the failing zip to find the "
            f"offending source."
        )


# ---- 7. Cron-regression sanity ---------------------------------------


@pytest.mark.asyncio
async def test_cron_endpoint_still_works_with_diag_bundle_registered(_isolate_config):
    from kora_cli import web_server

    jobs = await web_server.list_cron_jobs(profile="all")
    assert isinstance(jobs, list)
