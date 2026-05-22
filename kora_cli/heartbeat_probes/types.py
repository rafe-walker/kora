"""Pydantic types for KR-FEAT-HEARTBEAT.

Internal probe shape — richer than the
``HeartbeatService`` TS interface from KR-HB-PANEL (PR #103):

  - Status enum extends with ``"unknown"`` (covers the "can't probe"
    case: missing auth env, transport failure before the upstream
    service answers, probe-loop crash).
  - ``error: str | None`` carries the operator-readable failure
    string (sanitized — never contains the auth token).
  - ``latency_ms`` is nullable because an ``unknown`` probe never
    completed a roundtrip.

The KR-FEAT-HEARTBEAT ST2 endpoint projects this internal shape
onto the TS contract — ``unknown`` status + ``error`` field are
additive FE extensions (matches the KR-MCP-CONSUMPTION ST2
additive-FE pattern shipped in PR #113).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


ServiceStatus = Literal["healthy", "degraded", "unhealthy", "unknown"]

SERVICE_STATUSES: tuple[ServiceStatus, ...] = (
    "healthy",
    "degraded",
    "unhealthy",
    "unknown",
)


class ServiceHealthSnapshot(BaseModel):
    """One per-service health observation.

    Per K-DG drift discipline: ``extra="forbid"`` rejects unknown
    keys at construction so probe authors can't silently widen the
    shape without coordinating with the FE.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    status: ServiceStatus
    latency_ms: Optional[int] = None
    last_check_at: datetime
    details: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
