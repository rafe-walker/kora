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

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
]
