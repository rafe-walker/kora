"""Boot gate framework (R4.1 §9.2; KR-P2-H ST1).

The cold-boot gate sequence runs in order BEFORE the consumer loop
polls. This module ships the **framework**: an abstract :class:`Gate`
base, a :class:`BootGateRunner` that drives the sequence with the
spec's retry-with-backoff / fail-fast semantics, and the value-class
shape (:class:`GateResult`, :class:`GateClass`, :class:`GateOutcome`,
:class:`BootContext`).

ST2 ships the 7 concrete gate implementations. ST3 wires the runner's
result list into the chain emit + holder transition. ST4 ships the
``kora boot --check-only`` diagnostic CLI.

# Gate classification (R4.1 §9.2)

  - **TRANSIENT** — transient failures retry with exponential backoff
    (per-gate retry budget; default 5). On retry-budget exhaustion the
    runner stops; ST3 emits ``kora.boot.failed`` and transitions
    ``BOOTING → STOPPED``. While retrying, ST3 stages a
    :class:`DegradationReason` on the holder so the operator UI shows
    "booting + retrying gate X". On retry success the reason is
    removed.

  - **INVARIANT** — first failure stops the sequence. No retry. ST3
    emits ``kora.boot.failed`` + ``BOOTING → STOPPED``. The gate
    failures the doc calls invariant: gate 7 (canonical kora actor
    row missing), gate 10 (KR-7 attribution smoke).

# Diagnostic mode

``BootGateRunner(diagnostic_mode=True)`` runs every gate once (no
retry, no short-circuit on fail), returns the full result list, and
does NOT transition the holder or emit any chain event. Intended for
``kora boot --check-only`` and for CI smoke checks where an operator
wants to see every gate's outcome rather than the first failure.

# Why async

Gates do substrate I/O (asyncpg / MCP). The runner is async; CLI /
gateway entry points bridge via ``asyncio.run`` if they're sync.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar, Optional

if TYPE_CHECKING:
    from agent.operational_state_holder import OperationalStateHolder

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Value classes
# ---------------------------------------------------------------------------


class GateClass(Enum):
    """R4.1 §9.2 gate classification."""

    TRANSIENT = "transient"
    INVARIANT = "invariant"


class GateOutcome(Enum):
    """Per-attempt outcome of a single gate."""

    PASS = "pass"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class GateResult:
    """One gate's terminal result (after retries collapse).

    For a TRANSIENT gate that retried and eventually passed,
    ``outcome`` is PASS and ``attempts`` reflects the number of
    attempts taken. For one that exhausted its budget, ``outcome``
    is FAIL and ``attempts`` equals the retry-budget cap.

    String fields are operator-readable; ``detail`` carries the
    exception message or gate-defined diagnostic for FAIL outcomes,
    and a short OK-summary for PASS.
    """

    gate_id: str
    gate_class: GateClass
    outcome: GateOutcome
    detail: str
    elapsed_ms: int
    started_at: datetime
    completed_at: datetime
    attempts: int = 1


@dataclass
class BootContext:
    """Shared state passed through the gate sequence.

    The runner threads this through every gate's :meth:`Gate.run`.
    Gates may read provider / holder for the I/O surface, and may
    propagate cross-gate state via ``extras`` (e.g. Gate 7 stores the
    resolved kora_actor_uuid for Gate 10 to consume without re-querying).

    Typed fields cover the well-known cross-gate state. ``extras`` is
    the escape hatch for gate-defined data that doesn't justify a
    typed field; key collisions are the gate authors' problem.

    The dataclass is mutable on purpose — gates mutate it during the
    sequence. Frozen at the value-class layer would force every gate to
    return a new context, which is heavy for the few cross-gate writes
    we actually do.
    """

    memory_provider: Optional[Any] = None  # IsoKronMemoryProvider
    holder: Optional["OperationalStateHolder"] = None
    workspace_id: Optional[str] = None
    kora_actor_uuid: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Gate base
# ---------------------------------------------------------------------------


class Gate(ABC):
    """Abstract base class for boot gates.

    Subclasses set :attr:`gate_id`, :attr:`gate_class`, and
    :attr:`title` as class variables, and implement :meth:`run`. The
    runner consults :attr:`gate_class` to decide retry behavior.

    :meth:`run` must:
      - Time the work itself (record started_at / completed_at).
      - Return a :class:`GateResult` describing the outcome.
      - NOT raise on a gate-detected failure — return a FAIL result
        with the diagnostic in ``detail``. Unrecoverable exceptions
        (programmer errors, RuntimeError from missing infra) MAY
        propagate; the runner catches them as a FAIL result with
        ``detail = repr(exc)``.
    """

    gate_id: ClassVar[str]
    gate_class: ClassVar[GateClass]
    title: ClassVar[str]

    @abstractmethod
    async def run(self, context: BootContext) -> GateResult:
        """Execute this gate's check against ``context``."""


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


# Default retry budget for transient gates. R4.1 §9.2 doesn't pin a
# number; 5 attempts with exponential backoff (2s base, 30s cap) gives
# ~62s worst-case latency before declaring the gate STOPPED — fast
# enough for boot, slow enough to absorb a single network hiccup +
# substrate-side warm-up.
_DEFAULT_RETRY_BUDGET: int = 5
_DEFAULT_BACKOFF_BASE_SECONDS: float = 2.0
_DEFAULT_BACKOFF_CAP_SECONDS: float = 30.0


class BootGateRunner:
    """Runs the boot gate sequence in order.

    Production mode (``diagnostic_mode=False``):
      - INVARIANT gate fails → return immediately with results so far +
        the failing result. Caller (ST3) emits ``kora.boot.failed``
        and transitions ``BOOTING → STOPPED``.
      - TRANSIENT gate fails → backoff + retry up to ``retry_budget``
        attempts. If budget exhausted → same as INVARIANT-fail.
      - All gates pass → return the full result list. Caller emits
        ``kora.boot.ready`` and transitions ``BOOTING → READY``.

    Diagnostic mode (``diagnostic_mode=True``):
      - Each gate runs once (no retry).
      - Failures do NOT short-circuit; the runner continues through
        every gate so the operator sees the full picture.
      - The runner does NOT transition the holder or emit any chain
        event — that's the caller's responsibility, and the CLI
        diagnostic entry point intentionally skips both.

    Both modes return ``list[GateResult]``, length ≤ number of gates.
    """

    def __init__(
        self,
        gates: list[Gate],
        context: BootContext,
        *,
        diagnostic_mode: bool = False,
        retry_budget: int = _DEFAULT_RETRY_BUDGET,
        backoff_base_seconds: float = _DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_cap_seconds: float = _DEFAULT_BACKOFF_CAP_SECONDS,
    ) -> None:
        if retry_budget < 1:
            raise ValueError(
                f"retry_budget must be >= 1; got {retry_budget}"
            )
        if backoff_base_seconds <= 0:
            raise ValueError(
                f"backoff_base_seconds must be > 0; got {backoff_base_seconds}"
            )
        if backoff_cap_seconds < backoff_base_seconds:
            raise ValueError(
                f"backoff_cap_seconds ({backoff_cap_seconds}) must be >= "
                f"backoff_base_seconds ({backoff_base_seconds})"
            )
        self._gates = list(gates)
        self._context = context
        self._diagnostic_mode = diagnostic_mode
        self._retry_budget = retry_budget
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_cap_seconds = backoff_cap_seconds

    @property
    def diagnostic_mode(self) -> bool:
        return self._diagnostic_mode

    async def run_all(self) -> list[GateResult]:
        """Run gates in declared order. Return the result list.

        See class docstring for the production / diagnostic-mode flow.
        """
        results: list[GateResult] = []
        for gate in self._gates:
            result = await self._run_with_retries(gate)
            results.append(result)
            if result.outcome is GateOutcome.FAIL and not self._diagnostic_mode:
                # Production mode: short-circuit on terminal failure.
                # In diagnostic mode we keep going to collect all results.
                logger.warning(
                    "[kora.boot.gate] %s FAIL after %d attempt(s) — "
                    "stopping sequence (class=%s).",
                    gate.gate_id,
                    result.attempts,
                    result.gate_class.value,
                )
                break
        return results

    async def _run_with_retries(self, gate: Gate) -> GateResult:
        """Run ``gate`` once or with retries depending on gate class
        + diagnostic mode.

        Returns the terminal :class:`GateResult` for the gate (after
        the final attempt). The result's ``attempts`` field reflects
        how many runs happened.
        """
        max_attempts = self._max_attempts_for(gate)
        attempt = 0
        last_result: Optional[GateResult] = None
        while attempt < max_attempts:
            attempt += 1
            result = await self._run_once(gate)
            # Re-wrap with the cumulative attempt count.
            last_result = GateResult(
                gate_id=result.gate_id,
                gate_class=result.gate_class,
                outcome=result.outcome,
                detail=result.detail,
                elapsed_ms=result.elapsed_ms,
                started_at=result.started_at,
                completed_at=result.completed_at,
                attempts=attempt,
            )
            if last_result.outcome is GateOutcome.PASS:
                if attempt > 1:
                    logger.info(
                        "[kora.boot.gate] %s PASS on attempt %d/%d "
                        "(class=%s).",
                        gate.gate_id,
                        attempt,
                        max_attempts,
                        gate.gate_class.value,
                    )
                return last_result
            if attempt >= max_attempts:
                return last_result
            await self._backoff_sleep(attempt)
        # Defensive — loop always returns inside; reached only if
        # max_attempts was 0 (caught above).
        assert last_result is not None
        return last_result  # pragma: no cover

    def _max_attempts_for(self, gate: Gate) -> int:
        """How many attempts the runner will make for ``gate``."""
        if self._diagnostic_mode:
            return 1
        if gate.gate_class is GateClass.INVARIANT:
            return 1
        # TRANSIENT
        return self._retry_budget

    async def _run_once(self, gate: Gate) -> GateResult:
        """Execute the gate once. Catch unexpected exceptions and wrap
        them as FAIL results so the runner state stays consistent.
        """
        started_at = datetime.now(timezone.utc)
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        try:
            result = await gate.run(self._context)
        except Exception as exc:
            elapsed_ms = int((loop.time() - t0) * 1000)
            completed_at = datetime.now(timezone.utc)
            logger.warning(
                "[kora.boot.gate] %s raised unexpectedly: %r",
                gate.gate_id,
                exc,
            )
            return GateResult(
                gate_id=gate.gate_id,
                gate_class=gate.gate_class,
                outcome=GateOutcome.FAIL,
                detail=f"unexpected exception: {exc!r}",
                elapsed_ms=elapsed_ms,
                started_at=started_at,
                completed_at=completed_at,
            )
        return result

    async def _backoff_sleep(self, attempt: int) -> None:
        """Exponential backoff between transient-gate retry attempts.

        ``attempt`` is 1-indexed of the just-failed attempt. The sleep
        before the *next* attempt is ``base * 2^(attempt - 1)`` capped
        at ``cap``. So with base=2, cap=30:
          attempt 1 → sleep 2s before attempt 2
          attempt 2 → sleep 4s before attempt 3
          attempt 3 → sleep 8s before attempt 4
          attempt 4 → sleep 16s before attempt 5
          attempt 5 → sleep 30s (capped) before attempt 6 (if budget allowed)
        """
        seconds = min(
            self._backoff_base_seconds * (2 ** (attempt - 1)),
            self._backoff_cap_seconds,
        )
        await asyncio.sleep(seconds)
