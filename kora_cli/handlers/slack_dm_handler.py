"""Slack-DM handler — KR-FEAT-SLACK-DM ST1.

Called from ``kora_cli/listeners/webhooks.py:_handle_slack`` after
HMAC verification + URL-verification handshake. Owns the Kora-side
DM-processing logic:

  - Identity check: sender must match ``KORA_SLACK_JOSHUA_USER_ID``.
    Non-Joshua messages are dropped silently (don't echo back to a
    third party).
  - Channel-type filter: only ``"im"`` events. Channel messages,
    app_mentions, etc. are filtered.
  - Bot-message filter: events with ``event.bot_id`` set are filtered
    (defense against echo-loops if Kora's own bot is ever in the
    conversation).
  - Subtype filter: only regular messages (no ``event.subtype``);
    drop message_changed / message_deleted / message_replied etc.
  - OperationalStateHolder gating: PAUSED or STOPPED → drop. Don't
    process Joshua's DM during a pause.
  - JSONL append-only persistence at ``<KORA_HOME>/slack_dm_log.jsonl``.
  - ``[kora.slack_dm.received]`` structured-log emit on Joshua DMs
    (chain-event vocab literal flagged for substrate follow-on; same
    pattern as KR-D-DAEMON ST3 webhook dead-letter + KR-MCP-RUNTIME-
    SURFACE ST2 audit log).

# Security posture

The signing secret is consumed by the HMAC verifier in the listener;
this handler NEVER sees it. JSONL entries are bounded to a fixed
allow-list of fields — body content is recorded (Joshua's own
message text is the operator-visible record by design), but no
header values, no signing secret, no bot token, no auth metadata.

A unit test asserts the JSONL does NOT contain the signing-secret
env value after a sequence of events.

# Exception posture

Any uncaught exception inside ``handle_event`` is caught at the
listener-layer wrap (see ``webhooks.py``) and logged to the
dead-letter logger; we return 200 to Slack so it doesn't retry
indefinitely. The handler's own internal failure modes (JSONL write
failure, holder unavailable, etc.) WARN-log + continue — never
crash the request, never block the 200 response.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# Env vars.
JOSHUA_USER_ID_ENV = "KORA_SLACK_JOSHUA_USER_ID"
LOG_PATH_ENV = "KORA_SLACK_DM_LOG_PATH"  # test override; defaults to KORA_HOME

# Handled-status enum for the JSONL ``handled_status`` field.
HANDLED_RECEIVED = "received"
HANDLED_FILTERED_NON_JOSHUA = "filtered_non_joshua"
HANDLED_FILTERED_NON_IM = "filtered_non_im"
HANDLED_FILTERED_BOT = "filtered_bot"
HANDLED_FILTERED_SUBTYPE = "filtered_subtype"
HANDLED_DROPPED_PAUSED = "dropped_paused"
HANDLED_DROPPED_STOPPED = "dropped_stopped"
HANDLED_HANDLER_ERROR = "handler_error"


def _resolve_log_path() -> Path:
    """Return the JSONL log path: env override → ``KORA_HOME/slack_dm_log.jsonl``."""
    override = os.environ.get(LOG_PATH_ENV, "").strip()
    if override:
        return Path(override)
    # Lazy import — keeps test paths that monkeypatch LOG_PATH_ENV from
    # needing the full kora_constants resolution chain.
    from kora_constants import get_kora_home

    return get_kora_home() / "slack_dm_log.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_extract(event: Dict[str, Any], *keys: str) -> Optional[Any]:
    """Walk ``event[k1][k2]...`` defensively; return None on any miss."""
    cur: Any = event
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
        if cur is None:
            return None
    return cur


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class SlackDMHandler:
    """Processes a single verified Slack Events payload.

    Stateless across requests — each ``handle_event`` call processes
    one event independently. Persistent state (the JSONL log) is
    file-backed; in-memory state is request-scoped.
    """

    def __init__(self, log_path: Optional[Path] = None) -> None:
        self._log_path = log_path or _resolve_log_path()

    async def handle_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Process a Slack Events payload.

        Returns the Slack-API-compliant response dict (always
        ``{"ok": True}`` — Slack uses 200 + ok as the acknowledgement;
        the daemon's response code is set in the listener layer).

        Filter order:

          1. PAUSED / STOPPED state → drop, no further processing.
          2. ``event.type`` must be ``message`` AND no subtype.
          3. ``event.channel_type`` must be ``im``.
          4. ``event.bot_id`` must be absent.
          5. ``event.user`` must match ``KORA_SLACK_JOSHUA_USER_ID``.

        Each filter writes a JSONL entry with the appropriate
        ``handled_status`` then returns ``{"ok": True}``.

        Exceptions during filter / log / emit are caught + WARN-logged
        + still return ``{"ok": True}`` — never let internal failures
        cause Slack to retry.
        """
        try:
            return await self._handle_event_inner(payload)
        except Exception as exc:
            # Last-resort guard. Log + return ok so Slack doesn't retry.
            logger.warning(
                "[kora.slack_dm] handler raised %r — returning ok to Slack",
                exc,
            )
            try:
                self._append_log_entry(
                    payload, HANDLED_HANDLER_ERROR, error=repr(exc)
                )
            except Exception as inner:
                logger.warning(
                    "[kora.slack_dm] failed to log handler error: %r", inner
                )
            return {"ok": True}

    async def _handle_event_inner(
        self, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        # Filter 1: OperationalStateHolder gating.
        gate_status = self._check_state_gate()
        if gate_status is not None:
            self._append_log_entry(payload, gate_status)
            logger.info(
                "[kora.slack_dm] %s — message dropped",
                gate_status,
            )
            return {"ok": True}

        event_type = _safe_extract(payload, "event", "type")
        subtype = _safe_extract(payload, "event", "subtype")
        channel_type = _safe_extract(payload, "event", "channel_type")
        bot_id = _safe_extract(payload, "event", "bot_id")
        user_id = _safe_extract(payload, "event", "user")

        # Filter 2: only regular messages (no subtype).
        if event_type != "message" or subtype:
            self._append_log_entry(
                payload,
                HANDLED_FILTERED_SUBTYPE,
                extra={"event_type": event_type, "subtype": subtype},
            )
            return {"ok": True}

        # Filter 3: only IM channel-type.
        if channel_type != "im":
            self._append_log_entry(
                payload,
                HANDLED_FILTERED_NON_IM,
                extra={"channel_type": channel_type},
            )
            return {"ok": True}

        # Filter 4: bot messages.
        if bot_id:
            self._append_log_entry(
                payload,
                HANDLED_FILTERED_BOT,
                extra={"bot_id": bot_id},
            )
            return {"ok": True}

        # Filter 5: identity — must be Joshua.
        expected_joshua = os.environ.get(JOSHUA_USER_ID_ENV, "").strip()
        if not expected_joshua:
            # Misconfigured — fail-CLOSED. Without the Joshua ID set,
            # we can't verify the sender, so drop everything.
            logger.warning(
                "[kora.slack_dm] %s unset — all messages dropped (fail-CLOSED)",
                JOSHUA_USER_ID_ENV,
            )
            self._append_log_entry(
                payload,
                HANDLED_FILTERED_NON_JOSHUA,
                extra={"reason": "joshua_id_env_unset"},
            )
            return {"ok": True}
        if user_id != expected_joshua:
            self._append_log_entry(
                payload,
                HANDLED_FILTERED_NON_JOSHUA,
                extra={"actual_user_id": user_id},
            )
            return {"ok": True}

        # All filters passed — Joshua DM received.
        self._append_log_entry(payload, HANDLED_RECEIVED)
        self._emit_received_event(payload)
        return {"ok": True}

    # ------------------------------------------------------------------
    # Filters / helpers
    # ------------------------------------------------------------------

    def _check_state_gate(self) -> Optional[str]:
        """Return a handled_status if the operational state should
        drop this message; otherwise None."""
        try:
            from agent.operational_state import PrimaryState
            from agent.operational_state_holder import get_holder
        except Exception:
            # If the operational-state module can't even be imported,
            # we're in an unusual test path. Don't gate; let the rest
            # of the filters apply.
            return None

        holder = get_holder()
        if holder is None:
            # No holder initialized → no gating. The handler is
            # running outside the daemon (or in a partial-init test);
            # let processing proceed.
            return None

        # holder.current is a @property — caught in KR-MCP-RUNTIME-SURFACE
        # ST1 K-DG corrections.
        state = holder.current
        ps = state.primary_state

        if ps is PrimaryState.PAUSED:
            return HANDLED_DROPPED_PAUSED
        if ps is PrimaryState.STOPPED:
            return HANDLED_DROPPED_STOPPED
        return None

    def _append_log_entry(
        self,
        payload: Dict[str, Any],
        handled_status: str,
        *,
        extra: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        """Append one JSONL entry. Best-effort: any write failure is
        WARN-logged + swallowed."""
        entry: Dict[str, Any] = {
            "received_at": _now_iso(),
            "channel_id": _safe_extract(payload, "event", "channel") or "",
            "thread_ts": _safe_extract(payload, "event", "thread_ts"),
            "user_id": _safe_extract(payload, "event", "user") or "",
            "text": _safe_extract(payload, "event", "text") or "",
            "event_ts": _safe_extract(payload, "event", "ts") or "",
            "handled_status": handled_status,
        }
        if extra:
            entry["extra"] = extra
        if error:
            entry["error"] = error

        try:
            # Ensure parent dir exists (KORA_HOME may need to be created
            # in test envs). Best-effort; failure path logged.
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError as exc:
            logger.warning(
                "[kora.slack_dm] log write failed (%s): %r",
                self._log_path,
                exc,
            )

    def _emit_received_event(self, payload: Dict[str, Any]) -> None:
        """Stable structured-log emit for an identified Joshua DM.

        ``kora.slack_dm.received`` is the intended chain-event vocab
        literal; if/when substrate ships the CHECK-constraint
        addition, this can extend to also call
        ``IsoKronMCPClient.invoke("kora__append_event", ...)``.
        For now structured log is the audit seam.
        """
        logger.info(
            "[kora.slack_dm.received] channel=%s user=%s ts=%s text_len=%d",
            _safe_extract(payload, "event", "channel") or "",
            _safe_extract(payload, "event", "user") or "",
            _safe_extract(payload, "event", "ts") or "",
            len(_safe_extract(payload, "event", "text") or ""),
        )
