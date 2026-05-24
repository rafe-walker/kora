"""Reasoning engine daemon listener — KR-FEAT-AI-RESPONSE-LOOP ST2.

Wraps :class:`AnthropicReasoningEngine` in the
:class:`DaemonCoordinator` lifecycle:

  - Startup: construct the engine (loads system prompt from
    ``kora_docs/00_canonical_current_state/kora_system_prompt.md``;
    resolves credential cascade OAuth-first → API key → fail-CLOSED).
    Construction failure → daemon aborts boot (matches the bucket
    spec's "engine startup failure → daemon fails-CLOSED" since
    a daemon that can't reason is one that can't fulfill its
    primary purpose).
  - Hold: module-level ``_engine_singleton`` set via
    ``_set_singleton``; cleared on shutdown.
  - Shutdown: close the engine's underlying HTTP client. Best-
    effort; the coordinator's per-listener timeout (default 10s)
    caps the wait.

Mirrors ``kora_cli/listeners/mcp_consumption.py`` shape — singleton
pattern + ``current_reasoning_engine()`` accessor for cross-cutting
read from any code path (notably ``SlackDMHandler`` in ST2).

# Why startup failure should be FATAL

The bucket spec is explicit: "Engine startup failure during daemon
boot → daemon fails-CLOSED (no echoes since previous behavior is
replaced by reasoning that can't run)." After ST2 wires the
handler to the engine, the prior echo path is gone — if the engine
can't construct, the daemon has no useful response path. Better
to abort boot loudly than to ship a daemon that drops Joshua's
DMs into a canned-fallback loop.

The coordinator's :class:`DaemonCoordinator` handles this
naturally: any exception from ``startup()`` aborts the boot +
unwinds already-started listeners (KR-D-DAEMON ST1's lifecycle).

# KR-DAEMON-LISTENERS-VIA-GATEWAY Phase 3 — FATAL semantic contract
#
# When the future gateway-side consumer takes over lifecycle
# driving from Kora's ``DaemonCoordinator``, it MUST honor the
# FATAL contract documented above: any exception from
# ``startup(coordinator)`` aborts the consumer's boot. Today's
# Kora coordinator does this naturally; the
# :class:`BackgroundDaemonRegistry` surface is intentionally
# silent on the FATAL flag (registration-only; consumer drives
# lifecycle), so the contract lives in THIS docstring + the
# ``_hermes_entry`` registration comment below.
#
# A future gateway-consumer author reading this docstring will
# see the explicit FATAL contract for reasoning_engine and code
# their iteration loop to propagate startup exceptions (vs
# swallowing them silently — which would let Kora ship without a
# working engine, a behavior the bucket spec calls out as worse
# than a fail-fast abort).
#
# Implementation choice in this bucket: documentation-driven
# contract rather than a new Hermes-side ``startup_failure_is_fatal``
# field on ``BackgroundDaemonEntry``. Rationale: today's lifecycle
# is still driven by Kora's ``DaemonCoordinator`` (Path B thin-
# shim); the gateway consumer doesn't exist yet; adding a flag
# pre-emptively for a hypothetical consumer is YAGNI. If/when the
# gateway consumer lands and needs structured FATAL hinting, the
# additive ``startup_failure_is_fatal: bool = False`` field can
# land in that bucket. Documented this decision in the PM hand-
# off for the operator's awareness.
"""

from __future__ import annotations

import logging
from typing import Optional

from agent.background_daemon_registry import (
    BackgroundDaemonEntry,
    background_daemon_registry,
)
from kora_cli.daemon import DEFAULT_SHUTDOWN_TIMEOUT, register_daemon_listener
from kora_cli.reasoning.anthropic_engine import (
    AnthropicReasoningEngine,
    ReasoningEngineError,
)
from kora_cli.reasoning.engine import ReasoningEngine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level singleton + accessor (mirrors current_pool pattern)
# ---------------------------------------------------------------------------


_engine_singleton: Optional[ReasoningEngine] = None


def _set_singleton(engine: ReasoningEngine) -> None:
    global _engine_singleton
    _engine_singleton = engine


def _clear_singleton() -> None:
    global _engine_singleton
    _engine_singleton = None


def current_reasoning_engine() -> Optional[ReasoningEngine]:
    """Return the live :class:`ReasoningEngine`, or ``None``.

    ``None`` cases:
      - Daemon not running
      - Listener not yet started
      - Listener stopped (post-shutdown)
      - Listener startup failed AND the daemon proceeded anyway
        (shouldn't happen — fatal-CLOSED — but defensive)

    Mirrors :func:`kora_cli.listeners.mcp_consumption.current_pool`.
    """
    return _engine_singleton


# ---------------------------------------------------------------------------
# Listener lifecycle wrapper
# ---------------------------------------------------------------------------


class ReasoningEngineListener:
    """Owns the engine instance + sets the module-level singleton.

    Tests inject a pre-built engine via the constructor arg; the
    factory leaves it ``None`` so production startup creates a
    real ``AnthropicReasoningEngine``.
    """

    def __init__(
        self, engine: Optional[ReasoningEngine] = None
    ) -> None:
        self._engine: Optional[ReasoningEngine] = engine

    async def startup(self, coordinator=None) -> None:
        if self._engine is None:
            # Construction can raise ReasoningEngineNotConfigured /
            # ReasoningSystemPromptError. We do NOT catch — the
            # coordinator's startup-failure path unwinds the daemon,
            # which is the spec-mandated fail-CLOSED behavior.
            # FATAL contract: see module docstring "KR-DAEMON-
            # LISTENERS-VIA-GATEWAY Phase 3 — FATAL semantic contract"
            # for the explicit consumer-side requirement.
            try:
                self._engine = AnthropicReasoningEngine()
            except ReasoningEngineError as exc:
                logger.error(
                    "[kora.reasoning] engine construction failed: %r "
                    "— daemon will abort boot (fail-CLOSED). Operator "
                    "must configure credentials + system prompt before "
                    "the daemon can reply to DMs.",
                    exc,
                )
                raise
        _set_singleton(self._engine)
        logger.info("[kora.reasoning] engine listener active")

    async def shutdown(self) -> None:
        engine = self._engine
        _clear_singleton()
        if engine is None:
            return
        try:
            close_method = getattr(engine, "close", None)
            if close_method is not None:
                result = close_method()
                if hasattr(result, "__await__"):
                    await result
        except Exception as exc:
            logger.warning(
                "[kora.reasoning] engine shutdown raised %r — continuing",
                exc,
            )


# ---------------------------------------------------------------------------
# Factory + registration (import-time side effect)
# ---------------------------------------------------------------------------


# Process-wide singleton — KR-DAEMON-LISTENERS-VIA-GATEWAY Phase 3.
# Both registries point at the same instance so the cross-cutting
# current_reasoning_engine() accessor returns the same engine
# regardless of which consumer ran startup.
_listener_singleton = ReasoningEngineListener()


def _factory():
    return (
        _listener_singleton.startup,
        _listener_singleton.shutdown,
        DEFAULT_SHUTDOWN_TIMEOUT,
    )


register_daemon_listener("reasoning_engine", _factory)


# ---------------------------------------------------------------------------
# Hermes-side registration (Phase 3; Path B thin-shim same as snapshot #196)
# ---------------------------------------------------------------------------
# CRITICAL: this listener's startup is FATAL on failure. See the
# "KR-DAEMON-LISTENERS-VIA-GATEWAY Phase 3 — FATAL semantic contract"
# section in this module's docstring for the explicit requirement
# that gateway-side consumers must propagate any startup exception
# rather than swallowing it silently.
#
# Today's lifecycle is driven by Kora's DaemonCoordinator (Path B
# thin-shim) which already honors the FATAL contract; the Hermes
# entry is forward-compat for future consumers. No periodic_task —
# the engine is event-driven (other code paths call
# current_reasoning_engine() to invoke; no scheduled work owned
# by this listener).

_hermes_entry = BackgroundDaemonEntry(
    name="reasoning_engine",
    startup=_listener_singleton.startup,
    shutdown=_listener_singleton.shutdown,
    periodic_task=None,
    shutdown_timeout=DEFAULT_SHUTDOWN_TIMEOUT,
    plugin_name="kora",
)

try:
    background_daemon_registry().register(_hermes_entry)
except ValueError as _exc:
    logger.debug(
        "[kora.reasoning_engine_listener] hermes registry already had "
        "'reasoning_engine' entry: %s — skipping duplicate registration",
        _exc,
    )
