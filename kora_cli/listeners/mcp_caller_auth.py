"""Caller identity + capability ACL — KR-MCP-RUNTIME-SURFACE ST2.

Two-mode bearer-token authentication:

  Mode 1 — legacy (single anonymous caller). ``KORA_MCP_BEARER_TOKEN``
  env var, comma-separated for multi-token rotation. Tokens match
  → anonymous caller; no ``actor_kind``; empty ``allowed_caps``. ONLY
  ungated read-only tools (``requires_cap_gate=False``) work for
  Mode 1 callers.

  Mode 2 — file ACL (per-caller identity + per-caller capability
  allowlist). ``~/.kora/mcp_callers.yaml``:

    callers:
      - token_hash: "sha256:abc123..."   # sha256 of bearer token, hex
        actor_kind: "claude_pm_isokron"   # for audit logging
        allowed_caps:
          - kora__create_sea_ticket
          - kora__request_state_transition
      - token_hash: "sha256:def456..."
        actor_kind: "kora_drone_7"
        allowed_caps:
          - kora__get_recent_chain_events   # read-only also gated optionally

  Resolution: hash the presented token, look up by ``sha256:<hex>``
  prefix. Match → ``Caller(actor_kind, allowed_caps)``.

# Fail-CLOSED posture

Per ``feedback_fail_closed_by_default_security_infra``:

  - mcp_callers.yaml missing → Mode 2 disabled; only Mode 1 anonymous
    tokens work; mutating tools DENY ALL.
  - mcp_callers.yaml unreadable / malformed → Mode 2 disabled with a
    loud WARN log; same DENY ALL on mutating tools.

The yaml MUST be present + readable + well-formed for mutating tool
calls to succeed. Loud failure surface so operator sees the
misconfiguration before agents start hitting mutating endpoints.

# Cache + reload

The yaml is loaded once per process + cached. A mtime check on each
read means an operator-edited yaml takes effect without a daemon
restart (file rewriter MUST atomically replace the file, not edit
in-place, to ensure mtime advances).

# NOT used: substrate ``actor_has_capability``

Per the spec's 2026-05-22 decision (post CC#3 ST1 K-DG catch),
substrate's ``actor_has_capability(capability: str)`` is implicitly
Kora-only — it checks Kora's row of the in-memory matrix mirror.
MCP CALLER caps are a different threat model: what we let other
agents ASK Kora to do. The ACL lives in the daemon, not substrate.

When the caller list exceeds ~10 OR substrate ships a caller-aware
capability function, migration to substrate-backed ACL is mechanical
(swap ``load_callers``'s data source; the dispatch logic stays).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)


DEFAULT_CALLERS_PATH = Path.home() / ".kora" / "mcp_callers.yaml"
BEARER_TOKEN_ENV = "KORA_MCP_BEARER_TOKEN"
HASH_PREFIX = "sha256:"


@dataclass(frozen=True, slots=True)
class Caller:
    """Resolved caller identity + capability allowlist.

    ``actor_kind`` is the audit-log identifier (e.g.
    ``claude_pm_isokron``, ``kora_drone_7``). ``allowed_caps`` is the
    exact-match set of tool names the caller may invoke; a tool name
    NOT in this set is denied at dispatch time.

    For Mode 1 (legacy env-token) callers, ``actor_kind`` is the
    sentinel ``"anonymous"`` and ``allowed_caps`` is empty. Mode 1
    callers cannot pass ``requires_cap_gate=True`` checks.
    """

    actor_kind: str
    allowed_caps: frozenset[str] = field(default_factory=frozenset)

    def can_invoke(self, tool_name: str) -> bool:
        """Allow iff the tool is in ``allowed_caps`` (exact match)."""
        return tool_name in self.allowed_caps


ANONYMOUS_CALLER = Caller(actor_kind="anonymous", allowed_caps=frozenset())


# ---------------------------------------------------------------------------
# YAML loader (Mode 2)
# ---------------------------------------------------------------------------


# Cache: (path, mtime) -> {token_hash: Caller}.
# Re-read on file mtime change. Process-lifetime cache.
_callers_cache: Dict[Tuple[str, float], Dict[str, Caller]] = {}


def _hash_token(token: str) -> str:
    """Return ``sha256:<hex>`` for a presented bearer token."""
    return HASH_PREFIX + hashlib.sha256(token.encode("utf-8")).hexdigest()


def load_callers(
    path: Optional[Path] = None,
) -> Dict[str, Caller]:
    """Load + cache the caller ACL keyed by ``sha256:<hex>`` token hash.

    Returns an empty dict if:
      - The file doesn't exist (Mode 2 disabled).
      - The file exists but is unreadable / malformed (Mode 2 disabled
        with a loud WARN; fail-CLOSED).

    Cached by (path, mtime). Operators editing the yaml must
    atomic-replace (so mtime advances + the cache invalidates) — an
    in-place sed/vi-edit-and-save may or may not bump mtime depending
    on the editor; atomic-replace is the safe path.
    """
    target = path or DEFAULT_CALLERS_PATH

    try:
        stat = os.stat(target)
    except FileNotFoundError:
        # Mode 2 not configured. Mutating calls will DENY at the
        # cap-gate; ungated read-only calls under Mode 1 still work.
        return {}
    except OSError as exc:
        logger.warning(
            "[mcp_caller_auth] %s stat failed: %r — Mode 2 DISABLED "
            "(mutating calls will DENY ALL)",
            target,
            exc,
        )
        return {}

    cache_key = (str(target), stat.st_mtime)
    cached = _callers_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "[mcp_caller_auth] %s read failed: %r — Mode 2 DISABLED "
            "(mutating calls will DENY ALL)",
            target,
            exc,
        )
        return {}

    try:
        doc = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        logger.warning(
            "[mcp_caller_auth] %s YAML parse failed: %r — Mode 2 "
            "DISABLED (mutating calls will DENY ALL)",
            target,
            exc,
        )
        return {}

    callers_list = doc.get("callers") if isinstance(doc, dict) else None
    if not isinstance(callers_list, list):
        logger.warning(
            "[mcp_caller_auth] %s missing top-level 'callers' list — "
            "Mode 2 DISABLED (mutating calls will DENY ALL)",
            target,
        )
        return {}

    callers: Dict[str, Caller] = {}
    for idx, entry in enumerate(callers_list):
        if not isinstance(entry, dict):
            logger.warning(
                "[mcp_caller_auth] %s caller[%d] is not a mapping; "
                "skipping",
                target,
                idx,
            )
            continue
        token_hash = entry.get("token_hash")
        actor_kind = entry.get("actor_kind")
        allowed_caps_raw = entry.get("allowed_caps") or []

        if not isinstance(token_hash, str) or not token_hash.startswith(
            HASH_PREFIX
        ):
            logger.warning(
                "[mcp_caller_auth] %s caller[%d] has invalid token_hash "
                "(must start with %r); skipping",
                target,
                idx,
                HASH_PREFIX,
            )
            continue
        if not isinstance(actor_kind, str) or not actor_kind:
            logger.warning(
                "[mcp_caller_auth] %s caller[%d] missing actor_kind; "
                "skipping",
                target,
                idx,
            )
            continue
        if not isinstance(allowed_caps_raw, list):
            logger.warning(
                "[mcp_caller_auth] %s caller[%d] allowed_caps must be a "
                "list; skipping",
                target,
                idx,
            )
            continue
        allowed_caps = frozenset(
            c for c in allowed_caps_raw if isinstance(c, str)
        )
        callers[token_hash] = Caller(
            actor_kind=actor_kind, allowed_caps=allowed_caps
        )

    _callers_cache[cache_key] = callers
    logger.info(
        "[mcp_caller_auth] loaded %d caller(s) from %s", len(callers), target
    )
    return callers


def _reset_cache_for_tests() -> None:
    """Test helper — clears the (path, mtime) cache."""
    _callers_cache.clear()


# ---------------------------------------------------------------------------
# Mode 1 env-token list (legacy)
# ---------------------------------------------------------------------------


def _env_tokens() -> List[str]:
    """Parse ``KORA_MCP_BEARER_TOKEN`` as a comma-separated list.

    Empty entries (e.g. trailing comma) are filtered. Returns ``[]``
    if the env var is unset or empty — Mode 1 disabled.
    """
    raw = os.environ.get(BEARER_TOKEN_ENV, "").strip()
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def resolve_caller(
    presented_token: Optional[str],
    *,
    callers_path: Optional[Path] = None,
) -> Optional[Caller]:
    """Resolve a presented bearer token to a Caller, or None.

    Resolution order:

      1. Mode 2 — hash the token + look up in mcp_callers.yaml. If
         match, return the file-configured Caller.
      2. Mode 1 — compare against env-listed tokens (constant-time
         iteration; comma-separated multi-token rotation). If match,
         return :data:`ANONYMOUS_CALLER` (no actor_kind, no caps).
      3. No match → ``None``. Caller of this function maps to 401.

    Constant-time compare on EVERY env token to avoid timing
    side-channel inference of which token matched.
    """
    if not presented_token:
        return None

    # Mode 2.
    token_hash = _hash_token(presented_token)
    callers = load_callers(callers_path)
    caller = callers.get(token_hash)
    if caller is not None:
        return caller

    # Mode 1 — iterate ALL env tokens to keep the timing constant
    # regardless of which (if any) matches.
    matched = False
    for env_tok in _env_tokens():
        if hmac.compare_digest(
            presented_token.encode("utf-8"), env_tok.encode("utf-8")
        ):
            matched = True
    if matched:
        return ANONYMOUS_CALLER

    return None
