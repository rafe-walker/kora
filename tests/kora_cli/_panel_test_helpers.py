"""Shared test helpers for panel-endpoint test files — KR-FE-PANEL-HELPERS-DRY.

Centralizes patterns CC#2 duplicated across the panel test suite:

  * ``strip_ts_comments`` — removes JSX / block / line comments from a
    TSX source string before regex-pinning checks. The classic
    use-case is the ``dangerouslySetInnerHTML`` ban: the panel
    source carries a warning comment explaining the ban, so a naive
    ``"dangerouslySetInnerHTML" in src`` check would self-trigger.
    Stripping comments first makes the assertion test live code
    only. Originally duplicated identically across 5 test files;
    extracted here so the regex stays under one set of eyes.

  * ``assert_no_token_shapes`` — walk-payload sweep for the common
    credential shapes the 4-layer security contract bans across
    every panel (Anthropic sk-ant-, Slack xox*-, 32+ hex secrets,
    Bearer/Authorization headers). Each call site historically
    inlined its own regex constants + assertions; centralizing
    keeps the shape definitions in one place so a future
    "we also have to ban X" change updates everywhere.

  * ``isolated_kora_home`` — the 3-namespace ``get_kora_home``
    monkeypatch pattern from PR #137's fixture-isolation lesson.
    Endpoint code resolves ``get_kora_home`` from its own module
    namespace (``from kora_cli.config import get_kora_home``),
    not via a fresh import. Patching only ``kora_constants``
    silently misses; patching ``kora_cli.web_server`` is critical.
    This helper does all three + the standard env-var + config
    path overrides so per-file fixtures collapse to a one-liner.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping


# Comment-stripping regexes — applied in order: JSX block comments
# first ({/* ... */}), then C-style block comments, then line
# comments (with a guard so URLs like https:// don't get eaten).
_JSX_COMMENT_RE = re.compile(r"\{/\*.*?\*/\}", re.DOTALL)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"(^|[^:])//[^\n]*")


def strip_ts_comments(src: str) -> str:
    """Strip JSX block comments, C-style block comments, and line
    comments from a TSX source string. Used by the
    dangerouslySetInnerHTML source-pin tests so the warning comment
    explaining the ban doesn't self-trigger the assertion."""
    src = _JSX_COMMENT_RE.sub("", src)
    src = _BLOCK_COMMENT_RE.sub("", src)
    src = _LINE_COMMENT_RE.sub(r"\1", src)
    return src


# Credential-shape regexes — common across every panel's
# walk-payload SECURITY sweep. Adding a new banned shape here
# updates every assert_no_token_shapes() caller in one place.
_ANTHROPIC_KEY_SHAPE = re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b")
_SLACK_TOKEN_SHAPE = re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{8,}\b")
_HEX_SECRET_SHAPE = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_BEARER_TOKEN_SHAPE = re.compile(
    r"\b(?:Bearer|Authorization)\s*[: ]\s*[A-Za-z0-9+/_.-]{8,}",
    re.IGNORECASE,
)


def assert_no_token_shapes(
    payload: Mapping[str, Any] | list | str,
    *,
    anthropic: bool = True,
    slack: bool = True,
    hex_secret: bool = True,
    bearer: bool = True,
) -> None:
    """Walk-the-whole-payload sweep for credential / token shapes.

    Serializes the payload to JSON (so nested fields are covered)
    and asserts none of the enabled shape regexes match.

    Args:
      payload: The response (or substring) to sweep. Accepts dicts,
        lists, or pre-serialized JSON strings.
      anthropic / slack / hex_secret / bearer: toggle individual
        sweeps. Default all-on; individual panels can opt out of
        sweeps that would false-positive on documented content
        (e.g., a panel whose payload legitimately carries 32-char
        UUIDs would set hex_secret=False).
    """
    blob = (
        payload
        if isinstance(payload, str)
        else json.dumps(payload, default=str)
    )
    if anthropic:
        leaks = _ANTHROPIC_KEY_SHAPE.findall(blob)
        assert leaks == [], (
            f"payload contains Anthropic key shape(s): {leaks}"
        )
    if slack:
        leaks = _SLACK_TOKEN_SHAPE.findall(blob)
        assert leaks == [], (
            f"payload contains Slack token shape(s): {leaks}"
        )
    if hex_secret:
        leaks = _HEX_SECRET_SHAPE.findall(blob)
        assert leaks == [], (
            f"payload contains 32+ hex secret shape(s): {leaks}"
        )
    if bearer:
        leaks = _BEARER_TOKEN_SHAPE.findall(blob)
        assert leaks == [], (
            f"payload contains Bearer/Authorization shape(s): {leaks}"
        )


def isolated_kora_home(tmp_path: Path, monkeypatch) -> Path:
    """Configure an isolated KORA_HOME for the duration of a test.

    Per the KR-SLACK-DM-PANEL-FLIP (#137) fixture-isolation lesson:
    endpoint code resolves ``get_kora_home`` from its own module
    namespace (``from kora_cli.config import get_kora_home`` —
    a copy in the importer's namespace, not a transparent
    re-export). Monkeypatching only ``kora_constants.get_kora_home``
    silently misses for those call sites; the panel endpoint reads
    the value cached at module import time.

    This helper patches the resolver in ALL THREE module namespaces
    where it's been observed in use, plus the standard env-var +
    config-path overrides so per-file fixtures can collapse to:

        @pytest.fixture
        def env(tmp_path, monkeypatch):
            return isolated_kora_home(tmp_path, monkeypatch)

    Returns ``tmp_path`` so the caller can write fixture files
    into the isolated home.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setattr("kora_constants.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr("kora_cli.config.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr("kora_cli.web_server.get_kora_home", lambda: tmp_path)
    monkeypatch.setattr(
        "kora_cli.config.get_config_path",
        lambda: tmp_path / "config.yaml",
    )
    monkeypatch.setattr(
        "kora_cli.config.get_env_path",
        lambda: tmp_path / ".env",
    )
    return tmp_path
