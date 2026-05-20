"""Config schema for the IsoKron memory provider.

Loaded by the plugin entry point from `~/.kora/config.yaml` under
the ``plugins.entries.isokron`` block (matches the Hermes-inherited
plugin config layout). Validation goes through pydantic v2.

Example::

    plugins:
      enabled:
        - isokron
      entries:
        isokron:
          isokron_dsn: postgres://kora_runtime:${KORA_DB_PASSWORD}@db.isokron.local:5432/isokron
          mcp_endpoint: stdio://node ../isokron/packages/sea-mcp-server/dist/cli.js
          default_workspace_id: 00000000-0000-0000-0000-000000000001
          cache_ttl_seconds: 60

KR-2 ST1 ships this schema with field-level validation and clear error
messages; ST2-ST4 add fields as new substrate surfaces wire in (none
expected today).
"""

from __future__ import annotations

import os
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


# Stdio URL prefix Kora's runtime uses to mean "spawn local subprocess";
# pulled out as a constant so the MCP client + tests + this validator
# all reference the same literal.
STDIO_SCHEME = "stdio://"


def parse_mcp_transport(endpoint: str) -> Literal["stdio", "http"]:
    """Discriminate the MCP transport from the ``mcp_endpoint`` value.

    ``stdio://<command>`` → ``"stdio"``; ``http(s)://...`` → ``"http"``.
    Caller already passed pydantic validation, so we don't re-validate
    the scheme here.
    """
    if endpoint.startswith(STDIO_SCHEME):
        return "stdio"
    return "http"


class IsoKronProviderConfig(BaseModel):
    """Pydantic-validated config block for the IsoKron memory provider."""

    model_config = ConfigDict(
        extra="forbid",  # unknown keys are user typos, surface them
        str_strip_whitespace=True,
        validate_assignment=True,
    )

    isokron_dsn: str = Field(
        ...,
        description=(
            "Postgres DSN for the IsoKron substrate. Required for read paths "
            "(Role Charter, capability matrix, policy registry, scratchpad, "
            "event_log). Environment variables in the form ${VAR} are "
            "expanded by the plugin entry point before instantiation."
        ),
    )

    mcp_endpoint: str = Field(
        ...,
        description=(
            "MCP server endpoint for write paths. Two transports accepted: "
            "``stdio://<command>`` (spawns the Sea MCP server as a subprocess) "
            "or ``http(s)://host:port/...`` (talks to a long-running HTTP MCP "
            "server). KR-2 ST1 wires only the schema; ST3 picks one transport "
            "and STOP-gates if neither works."
        ),
    )

    default_workspace_id: Optional[str] = Field(
        default=None,
        description=(
            "Kora's primary IsoKron workspace UUID. When unset, the provider "
            "falls back to per-session workspace resolution via the gateway "
            "or CLI session context. Required for cron / scheduled jobs that "
            "have no per-session context."
        ),
    )

    cache_ttl_seconds: int = Field(
        default=60,
        ge=0,
        le=3600,
        description=(
            "TTL (seconds) for the in-process caches of Role Charter, "
            "capability matrix, and policy registry reads. The TS-side "
            "@hivex/sb1-substrate-shapes reader uses 60s per-workspace; "
            "match unless KR-2 ST2 finds a reason to diverge."
        ),
    )

    actor_kind: str = Field(
        default="kora",
        description=(
            "actor_kind enum value Kora uses when writing to the substrate. "
            "Always 'kora' in normal operation; overridable for test fixtures "
            "and synthetic-actor scenarios."
        ),
    )

    enable_legacy_fallback: bool = Field(
        default=False,
        description=(
            "When True, the provider falls back to Hermes' flat MEMORY.md / "
            "USER.md files for reads if the substrate is unreachable. KR-2 "
            "default is False — IsoKron is the source of truth. Set True "
            "explicitly to opt into the BC bridge during the KR-2 cutover."
        ),
    )

    mcp_service_token: Optional[SecretStr] = Field(
        default=None,
        description=(
            "Service token for authenticating to the Sea MCP server. KR-7a "
            "wires the auth-injection plumbing; substrate-team provisions "
            "the actual token (see coordination/from_kora_pm/"
            "24_kora_runtime_service_token_provisioning_request.md). When "
            "unset, the provider checks the ``KORA_SERVICE_TOKEN`` env var "
            "via the plugin loader's ``${VAR}`` expansion. HTTP transport "
            "injects as ``Authorization: Bearer <token>``; stdio transport "
            "injects as ``KORA_SERVICE_TOKEN`` in the subprocess env."
        ),
    )

    @field_validator("isokron_dsn")
    @classmethod
    def _dsn_must_be_postgres(cls, v: str) -> str:
        if not (v.startswith("postgres://") or v.startswith("postgresql://")):
            raise ValueError(
                "isokron_dsn must be a postgres:// or postgresql:// URI; "
                f"got {v!r}"
            )
        return v

    @field_validator("mcp_endpoint")
    @classmethod
    def _mcp_endpoint_known_transport(cls, v: str) -> str:
        if not (
            v.startswith("stdio://")
            or v.startswith("http://")
            or v.startswith("https://")
        ):
            raise ValueError(
                "mcp_endpoint must use stdio://, http://, or https:// "
                f"transport; got {v!r}"
            )
        return v

    @property
    def mcp_transport(self) -> Literal["stdio", "http"]:
        """Discriminate transport mode from the validated ``mcp_endpoint``.

        Kept as a property (rather than a Field) so operator config
        files stay backward-compatible — the existing ``mcp_endpoint``
        URL prefix is the source of truth.
        """
        return parse_mcp_transport(self.mcp_endpoint)

    def resolve_service_token(self) -> Optional[str]:
        """Return the service token's plain-text value, env-var fallback.

        Order: explicit ``mcp_service_token`` field → ``KORA_SERVICE_TOKEN``
        env var → ``None``. Returned plain so the MCP client can inject
        it as the Authorization header (HTTP) or subprocess env var
        (stdio); callers MUST NOT log this value.
        """
        if self.mcp_service_token is not None:
            return self.mcp_service_token.get_secret_value()
        env_token = os.environ.get("KORA_SERVICE_TOKEN")
        return env_token or None


# Schema metadata for `kora memory setup` walkthrough. Mirrors the
# shape consumed by MemoryProvider.get_config_schema (List[Dict]).
ISOKRON_CONFIG_SCHEMA = [
    {
        "key": "isokron_dsn",
        "description": "Postgres DSN for the IsoKron substrate (reads).",
        "secret": True,
        "required": True,
        "env_var": "KORA_ISOKRON_DSN",
    },
    {
        "key": "mcp_endpoint",
        "description": (
            "MCP endpoint for the Sea MCP server (writes). "
            "stdio://<command> or http(s)://host:port/..."
        ),
        "required": True,
    },
    {
        "key": "default_workspace_id",
        "description": (
            "Kora's primary workspace UUID. Optional — "
            "falls back to per-session resolution if unset."
        ),
        "required": False,
    },
    {
        "key": "cache_ttl_seconds",
        "description": "Cache TTL for charter / matrix / policy reads.",
        "required": False,
        "default": 60,
    },
    {
        "key": "actor_kind",
        "description": "actor_kind value used for substrate writes.",
        "required": False,
        "default": "kora",
    },
    {
        "key": "enable_legacy_fallback",
        "description": (
            "Fall back to ~/.kora/memories/MEMORY.md when substrate is down."
        ),
        "required": False,
        "default": False,
    },
    {
        "key": "mcp_service_token",
        "description": (
            "Sea MCP service token (KR-7a auth). Substrate-team provisions; "
            "consumer can read from KORA_SERVICE_TOKEN env var instead."
        ),
        "secret": True,
        "required": False,
        "env_var": "KORA_SERVICE_TOKEN",
    },
]
