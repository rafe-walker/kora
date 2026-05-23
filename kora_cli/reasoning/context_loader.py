"""Conversation context loader — KR-FEAT-AI-RESPONSE-LOOP ST1.

Reads ``${HERMES_HOME}/slack_dm_log.jsonl`` (or
``KORA_SLACK_DM_LOG_PATH`` override), slices to a target Slack
channel + thread, and projects the most-recent N entries into a
:class:`ConversationContext` the reasoning engine consumes.

# JSONL schema reminder

Per KR-FEAT-SLACK-DM ST1 + ST2, two entry shapes coexist:

  - **Inbound**: ``{received_at, channel_id, thread_ts, user_id,
    text, event_ts, handled_status, ...}``
  - **Outbound**: ``{sent_at, channel_id, thread_ts, text,
    slack_message_ts, send_status, ...}``

Distinguished by ``received_at`` vs ``sent_at`` key presence.
Inbound entries with ``handled_status != "received"`` (filter
drops, state drops, errors) are skipped — only successfully-
processed Joshua DMs become conversation turns.

# Thread matching

Same Slack thread = same ``channel_id`` AND same ``thread_ts``.
A DM that's not in a thread (no ``thread_ts``) matches other
DMs without thread_ts in the same channel — Slack's IM channels
don't typically have threads, so this is the common case.

# Operational + cost state injection

The loader reads the holders' singletons at call time and
projects them into :class:`ConversationContext`'s
``current_operational_state`` + ``current_cost_ladder_rung``
fields. The reasoning engine consumes the canonical string
values (NOT the enum types) — keeps the engine module pure.

# Failure modes

  - JSONL missing → empty context (no turns; state strings
    "unknown"). The reasoning engine still runs; it just lacks
    history.
  - JSONL unreadable / malformed lines → WARN-log + skip the
    bad line; continue with valid lines.
  - Holder modules uninitialized → context strings are "unknown";
    the engine defaults to OPUS + skips the paused-state gate.
    Production daemon always has both holders initialized; this
    fall-through covers test paths.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from kora_cli.reasoning.engine import (
    ConversationContext,
    ConversationTurn,
)

logger = logging.getLogger(__name__)


# Mirror the env name + default-path resolution from
# kora_cli/handlers/slack_dm_handler.py so the loader points at
# the same file the handler writes to.
LOG_PATH_ENV = "KORA_SLACK_DM_LOG_PATH"

DEFAULT_MAX_TURNS = 10


def _resolve_log_path() -> Path:
    override = os.environ.get(LOG_PATH_ENV, "").strip()
    if override:
        return Path(override)
    from kora_constants import get_kora_home

    return get_kora_home() / "slack_dm_log.jsonl"


def _parse_iso(value: Any) -> Optional[datetime]:
    """Defensive ISO 8601 parser. Returns None on any failure."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _match_thread(
    entry: Dict[str, Any],
    *,
    channel_id: str,
    thread_ts: Optional[str],
) -> bool:
    """A JSONL entry belongs to the target thread iff:
    - same channel_id
    - same thread_ts (None matches None; non-None matches exact)
    """
    if entry.get("channel_id") != channel_id:
        return False
    entry_thread = entry.get("thread_ts")
    if thread_ts is None:
        return entry_thread is None
    return entry_thread == thread_ts


def _current_operational_state_str() -> str:
    """Best-effort resolution of the canonical PrimaryState string."""
    try:
        from agent.operational_state_holder import get_holder
    except Exception:
        return "unknown"
    holder = get_holder()
    if holder is None:
        return "unknown"
    try:
        # .current is a @property — caught in KR-MCP-RUNTIME-SURFACE ST1.
        return holder.current.primary_state.value
    except Exception:
        return "unknown"


def _current_cost_rung_str() -> str:
    """Best-effort resolution of the canonical CostRung string."""
    try:
        from agent.cost_state_holder import get_cost_holder
    except Exception:
        return "unknown"
    holder = get_cost_holder()
    if holder is None:
        return "unknown"
    try:
        # active_rung is a method (NOT @property) per K-DG check.
        return holder.active_rung().value
    except Exception:
        return "unknown"


def load_slack_dm_context(
    *,
    channel_id: str,
    thread_ts: Optional[str],
    max_turns: int = DEFAULT_MAX_TURNS,
    log_path: Optional[Path] = None,
) -> ConversationContext:
    """Load up to ``max_turns`` most-recent turns in the target thread.

    Args:
      channel_id: Slack channel ID (e.g. ``D01ABC...``). Required.
      thread_ts: Slack thread timestamp. ``None`` for non-threaded DMs.
      max_turns: Cap on returned turn count. Default 10 (5 in + 5 out
        in the typical alternating case; the loader doesn't enforce
        alternation — caller is responsible if SDK requires it).
      log_path: Override for tests. ``None`` → resolves via
        ``KORA_SLACK_DM_LOG_PATH`` env or ``get_kora_home()``.

    Returns:
      A ConversationContext with up to ``max_turns`` turns
      (oldest→newest) + current operational state + current cost
      rung. On missing/unreadable JSONL returns an empty context
      with state strings ``"unknown"``.
    """
    path = log_path or _resolve_log_path()
    turns: List[ConversationTurn] = []

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ConversationContext(
            recent_messages=[],
            current_operational_state=_current_operational_state_str(),
            current_cost_ladder_rung=_current_cost_rung_str(),
        )
    except OSError as exc:
        logger.warning(
            "[kora.reasoning] context loader: %s unreadable: %r — "
            "continuing with empty history",
            path,
            exc,
        )
        return ConversationContext(
            recent_messages=[],
            current_operational_state=_current_operational_state_str(),
            current_cost_ladder_rung=_current_cost_rung_str(),
        )

    for lineno, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning(
                "[kora.reasoning] context loader: %s line %d malformed: %r",
                path,
                lineno,
                exc,
            )
            continue
        if not isinstance(entry, dict):
            continue

        if not _match_thread(entry, channel_id=channel_id, thread_ts=thread_ts):
            continue

        # Inbound? (has received_at + handled_status)
        if "received_at" in entry and entry.get("handled_status") == "received":
            at = _parse_iso(entry.get("received_at")) or _epoch_dt()
            text = str(entry.get("text") or "")
            if text:
                turns.append(
                    ConversationTurn(direction="inbound", text=text, at=at)
                )
            continue

        # Outbound? (has sent_at + send_status == "ok")
        if "sent_at" in entry and entry.get("send_status") == "ok":
            at = _parse_iso(entry.get("sent_at")) or _epoch_dt()
            text = str(entry.get("text") or "")
            if text:
                turns.append(
                    ConversationTurn(direction="outbound", text=text, at=at)
                )
            continue

        # Filtered / failed / dropped entries are skipped (they
        # weren't part of Kora's reasoning history).

    # Sort by timestamp (oldest first), keep last N. We sort
    # defensively because JSONL append-order MAY occasionally race
    # against the wall-clock ordering (rare; outbound entry's
    # sent_at is set inside _send_echo_reply, after the inbound
    # received_at, but log writes are buffered).
    turns.sort(key=lambda t: t.at)
    if len(turns) > max_turns:
        turns = turns[-max_turns:]

    return ConversationContext(
        recent_messages=turns,
        current_operational_state=_current_operational_state_str(),
        current_cost_ladder_rung=_current_cost_rung_str(),
    )


def _epoch_dt() -> datetime:
    """Defensive fallback timestamp when a JSONL entry has a
    malformed ``received_at`` / ``sent_at``. Sorts to the start of
    history so the bad entry doesn't take precedence over good ones."""
    return datetime.fromtimestamp(0, tz=timezone.utc)


# ===========================================================================
# Email context — KR-FEAT-EMAIL-INBOUND-IMAP ST2
# ===========================================================================


EMAIL_INBOUND_LOG_PATH_ENV = "KORA_EMAIL_INBOUND_LOG_PATH"
EMAIL_OUTBOUND_LOG_PATH_ENV = "KORA_EMAIL_OUTBOUND_LOG_PATH"


def _resolve_email_log_paths() -> tuple[Path, Path]:
    """Return ``(inbound, outbound)`` JSONL paths, honoring env overrides
    then falling back to ``KORA_HOME/email_{inbound,outbound}_log.jsonl``."""
    inbound_override = os.environ.get(EMAIL_INBOUND_LOG_PATH_ENV, "").strip()
    outbound_override = os.environ.get(EMAIL_OUTBOUND_LOG_PATH_ENV, "").strip()
    from kora_constants import get_kora_home

    home = get_kora_home()
    inbound = (
        Path(inbound_override)
        if inbound_override
        else home / "email_inbound_log.jsonl"
    )
    outbound = (
        Path(outbound_override)
        if outbound_override
        else home / "email_outbound_log.jsonl"
    )
    return inbound, outbound


def _email_chain_anchors(
    message_id: str,
    in_reply_to: Optional[str],
    entries: List[Dict[str, Any]],
) -> set[str]:
    """Compute the transitive chain-anchor set for the focal email.

    Seeds with the focal ``message_id`` (+ the focal's
    ``in_reply_to`` when set), then iteratively expands by walking
    every entry's ``message_id`` ↔ ``in_reply_to`` link until the
    set stabilizes.

    RFC 5322 message-ids are globally unique so transitive walks
    are safe — an entry only enters the set if it threads back to
    the focal via at least one explicit link.
    """
    anchors: set[str] = set()
    if message_id:
        anchors.add(message_id)
    if in_reply_to:
        anchors.add(in_reply_to)

    while True:
        added = False
        for entry in entries:
            msg_id = entry.get("message_id")
            irt = entry.get("in_reply_to")
            in_chain = False
            if isinstance(msg_id, str) and msg_id in anchors:
                in_chain = True
            if isinstance(irt, str) and irt in anchors:
                in_chain = True
            if not in_chain:
                continue
            if isinstance(msg_id, str) and msg_id not in anchors:
                anchors.add(msg_id)
                added = True
            if isinstance(irt, str) and irt not in anchors:
                anchors.add(irt)
                added = True
        if not added:
            break

    return anchors


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Read + parse a JSONL file. Returns ``[]`` on missing / unreadable
    files; logs WARN + skips malformed lines."""
    entries: List[Dict[str, Any]] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return entries
    except OSError as exc:
        logger.warning(
            "[kora.reasoning] context loader: %s unreadable: %r — "
            "treating as empty",
            path,
            exc,
        )
        return entries
    for lineno, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning(
                "[kora.reasoning] context loader: %s line %d malformed: %r",
                path,
                lineno,
                exc,
            )
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def load_email_context(
    *,
    message_id: str,
    in_reply_to: Optional[str] = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    inbound_log_path: Optional[Path] = None,
    outbound_log_path: Optional[Path] = None,
) -> ConversationContext:
    """Load up to ``max_turns`` most-recent email turns in the chain.

    Args:
      message_id: The focal inbound email's ``Message-ID``. Anchors
        the chain — entries whose own ``message_id`` is this OR whose
        ``in_reply_to`` is this are pulled in.
      in_reply_to: The focal's ``In-Reply-To`` header (parent's
        message_id). When set, anchors the parent into the chain
        so a reply email pulls its parent + sibling turns.
      max_turns: Cap on returned turn count. Default 10.
      inbound_log_path / outbound_log_path: test overrides.

    Returns:
      A ConversationContext with up to ``max_turns`` turns
      (oldest→newest) drawn from BOTH JSONL files. Missing files
      → empty turns; state strings filled from the same holders
      the Slack loader uses.

    Chain closure is one-hop — handles the common Joshua↔Kora
    back-and-forth without the complexity of full transitive walks.
    Deeper chains slice cleanly if the focal email's parents
    populate ``in_reply_to`` correctly per RFC 5322.
    """
    if not message_id:
        # Defensive — message_id should always be present; the IMAP
        # client synthesizes one for incoming mail without a header.
        return ConversationContext(
            recent_messages=[],
            current_operational_state=_current_operational_state_str(),
            current_cost_ladder_rung=_current_cost_rung_str(),
        )

    inbound_default, outbound_default = _resolve_email_log_paths()
    in_path = inbound_log_path or inbound_default
    out_path = outbound_log_path or outbound_default

    inbound_entries = _read_jsonl(in_path)
    outbound_entries = _read_jsonl(out_path)
    anchors = _email_chain_anchors(
        message_id, in_reply_to, inbound_entries + outbound_entries
    )

    turns: List[ConversationTurn] = []

    for entry in inbound_entries:
        if entry.get("handled_status") != "received":
            continue
        if not _email_entry_in_chain(entry, anchors):
            continue
        at = _parse_iso(entry.get("received_at")) or _epoch_dt()
        text = str(entry.get("body_text_truncated_2k") or entry.get("text") or "")
        if text:
            turns.append(
                ConversationTurn(direction="inbound", text=text, at=at)
            )

    for entry in outbound_entries:
        if entry.get("send_status") != "ok":
            continue
        if not _email_entry_in_chain(entry, anchors):
            continue
        at = _parse_iso(entry.get("sent_at")) or _epoch_dt()
        # Outbound entries record ``text`` (the reply body) per the
        # KR-MCP-SEND-TOOLS shape. PurelymailClient's outbound JSONL
        # does NOT record body (subject + recipients only per the
        # outbound-bucket security contract); when we read those
        # entries we get an empty turn and skip via the truthy check.
        text = str(entry.get("text") or entry.get("body_text") or "")
        if text:
            turns.append(
                ConversationTurn(direction="outbound", text=text, at=at)
            )

    turns.sort(key=lambda t: t.at)
    if len(turns) > max_turns:
        turns = turns[-max_turns:]

    return ConversationContext(
        recent_messages=turns,
        current_operational_state=_current_operational_state_str(),
        current_cost_ladder_rung=_current_cost_rung_str(),
    )


def _email_entry_in_chain(
    entry: Dict[str, Any], anchors: set[str]
) -> bool:
    """An email JSONL entry is in-chain iff its own ``message_id`` or
    ``in_reply_to`` is one of the anchors."""
    msg_id = entry.get("message_id")
    irt = entry.get("in_reply_to")
    if isinstance(msg_id, str) and msg_id in anchors:
        return True
    if isinstance(irt, str) and irt in anchors:
        return True
    return False
